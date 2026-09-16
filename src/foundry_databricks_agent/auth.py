"""Centralized Microsoft Entra ID authentication for the web app.

This module is the **single source of truth** for authentication. Identity is **Microsoft
Entra ID OAuth 2.0**; there are no personal access tokens (PATs) and no Databricks CLI
profiles anywhere.

For **Azure Databricks**, a short-lived Entra access token is requested for the Azure
Databricks login application (well-known app id ``2ff814a6-3304-4ab8-85cb-cd0e6f879c1d``)
and sent as ``Authorization: Bearer <token>``. Azure Databricks accepts that token and
governs access through Unity Catalog under the caller's identity.

Two identities are used by the web app:

* the **app's own** identity — :func:`default_credential` (``DefaultAzureCredential``:
  managed identity in Azure, service principal, or ``az login`` locally). It runs the Azure
  AI Foundry **model**, and — in ``app`` mode — the Databricks tools.
* the **signed-in user** — :func:`build_on_behalf_of_credential` exchanges the user's Entra
  token On-Behalf-Of for a Databricks token (``obo`` mode). The confidential client
  authenticates **secretlessly** with a managed identity (federated identity credential),
  or a client secret for local testing.

Tokens are cached and refreshed automatically before expiry. :func:`token_header_provider`
yields a **request-scoped** provider so per-user credentials are never retained
process-wide.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from functools import lru_cache

from azure.core.credentials import AccessToken, TokenCredential
from azure.identity import DefaultAzureCredential

from .config import Settings

__all__ = [
    "AZURE_DATABRICKS_LOGIN_APP_ID",
    "DEFAULT_AZURE_DATABRICKS_SCOPE",
    "ENTRA_TOKEN_EXCHANGE_SCOPE",
    "default_credential",
    "databricks_auth_headers",
    "token_header_provider",
    "build_on_behalf_of_credential",
]

# Well-known Microsoft Entra ID application id for the Azure Databricks login service.
# Requesting a token for this resource (the ``.default`` scope) yields an Entra access
# token that Azure Databricks accepts as an OAuth bearer token.
AZURE_DATABRICKS_LOGIN_APP_ID = "2ff814a6-3304-4ab8-85cb-cd0e6f879c1d"
DEFAULT_AZURE_DATABRICKS_SCOPE = f"{AZURE_DATABRICKS_LOGIN_APP_ID}/.default"

# Audience for the Microsoft Entra token-exchange assertion used by **secretless** OBO:
# a managed identity mints a token for this resource and presents it as the confidential
# client's ``client_assertion`` (federated identity credential). No client secret needed.
ENTRA_TOKEN_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"

# Refresh a little before expiry so in-flight requests always carry a valid token.
_REFRESH_SKEW_SECONDS = 300


@lru_cache(maxsize=1)
def default_credential() -> TokenCredential:
    """Return a process-wide default Entra ID credential (``DefaultAzureCredential``).

    Resolves credentials from the standard enterprise chain: environment service
    principal / workload identity, managed identity, then the developer's Azure CLI
    session. This is the **app's own** identity — it runs the Azure AI Foundry model, and
    (in ``app`` mode) the Databricks tools. Pass an explicit
    :class:`azure.core.credentials.TokenCredential` to :func:`build_local_mcp_agent` or the
    tool factories to override.
    """
    return DefaultAzureCredential()


@lru_cache(maxsize=4)
def _managed_identity_credential(client_id: str | None) -> TokenCredential:
    """Return a (cached) managed-identity credential for the app's own identity.

    ``client_id`` selects a user-assigned managed identity; ``None`` uses the
    system-assigned identity. Safe to cache/share process-wide because it represents the
    *app* identity (not any end user). Used only to mint the token-exchange assertion for
    secretless OBO.
    """
    from azure.identity import ManagedIdentityCredential

    if client_id:
        return ManagedIdentityCredential(client_id=client_id)
    return ManagedIdentityCredential()


class _EntraTokenProvider:
    """Caches and refreshes a Microsoft Entra ID OAuth token for Azure Databricks.

    Thread-safe: a single lock guards refresh so concurrent tool calls share one token.
    """

    def __init__(self, credential: TokenCredential, scope: str) -> None:
        self._credential = credential
        self._scope = scope
        self._lock = threading.Lock()
        self._token: AccessToken | None = None

    def _is_fresh(self) -> bool:
        return (
            self._token is not None
            and (self._token.expires_on - time.time()) > _REFRESH_SKEW_SECONDS
        )

    def token(self) -> str:
        """Return a valid Entra access token, refreshing via OAuth when needed."""
        if not self._is_fresh():
            with self._lock:
                if not self._is_fresh():
                    self._token = self._credential.get_token(self._scope)
        assert self._token is not None  # populated above
        return self._token.token

    def headers(self) -> dict[str, str]:
        """Return ``{"Authorization": "Bearer <entra-token>"}``."""
        return {"Authorization": f"Bearer {self.token()}"}


# Memoize one provider per (credential, scope) so tokens are shared and refreshed once.
_PROVIDERS: dict[tuple[int, str], _EntraTokenProvider] = {}
_PROVIDERS_LOCK = threading.Lock()


def _scope_for(settings: Settings) -> str:
    return settings.databricks_azure_scope or DEFAULT_AZURE_DATABRICKS_SCOPE


def _provider_for(
    settings: Settings, credential: TokenCredential | None
) -> _EntraTokenProvider:
    cred = credential or default_credential()
    scope = _scope_for(settings)
    key = (id(cred), scope)
    provider = _PROVIDERS.get(key)
    if provider is None:
        with _PROVIDERS_LOCK:
            provider = _PROVIDERS.get(key)
            if provider is None:
                provider = _EntraTokenProvider(cred, scope)
                _PROVIDERS[key] = provider
    return provider


def databricks_auth_headers(
    settings: Settings, credential: TokenCredential | None = None
) -> dict[str, str]:
    """Return Entra ID OAuth bearer headers (``Authorization: Bearer ...``) for Databricks.

    These headers authenticate both managed MCP requests and serving-endpoint
    invocations. The underlying Entra token is cached and refreshed automatically, so
    calling this again yields a valid (and, when needed, freshly minted) token.
    """
    return _provider_for(settings, credential).headers()


def token_header_provider(
    settings: Settings, credential: TokenCredential | None = None
) -> "Callable[[], dict[str, str]]":
    """Return a **request-scoped** bearer-header provider (``() -> {"Authorization": ...}``).

    Unlike :func:`databricks_auth_headers`, this does **not** register the provider in the
    process-wide cache. Use it with **per-request / per-user credentials** (e.g. an
    On-Behalf-Of credential in a web app) so short-lived credentials don't accumulate in
    the module cache. The returned callable caches and refreshes the token for the lifetime
    of the credential it wraps.
    """
    cred = credential or default_credential()
    provider = _EntraTokenProvider(cred, _scope_for(settings))
    return provider.headers


def build_on_behalf_of_credential(
    *,
    tenant_id: str,
    client_id: str,
    user_assertion: str,
    client_secret: str | None = None,
    managed_identity_client_id: str | None = None,
    use_managed_identity: bool = False,
) -> TokenCredential:
    """Return an On-Behalf-Of (OBO) credential for the *signed-in end user*.

    Use this in a multi-user web app so Databricks calls run under **each user's** Microsoft
    Entra identity rather than the app's service identity. The confidential-client app
    (``client_id`` in ``tenant_id``) exchanges the user's access token (``user_assertion`` —
    typically the ``X-MS-TOKEN-AAD-ACCESS-TOKEN`` header injected by Azure Container Apps /
    App Service built-in authentication) for a downstream Azure Databricks token.

    The app authenticates itself one of two ways:

    * **Secretless (recommended for production)** — set ``use_managed_identity=True`` (and,
      for a user-assigned identity, ``managed_identity_client_id``). The app's managed
      identity mints a token-exchange assertion (:data:`ENTRA_TOKEN_EXCHANGE_SCOPE`) that
      authenticates the confidential client via a **federated identity credential**. No
      client secret is stored anywhere.
    * **Client secret (handy for local testing)** — pass ``client_secret``.

    Pass the result to :func:`foundry_databricks_agent.agent.build_local_mcp_agent` as
    ``databricks_credential`` so the **local MCP tools** (Unity Catalog functions and Genie)
    call Azure Databricks *as the user*, and Unity Catalog governs access per user. The
    model call stays on the app's own identity (``foundry_credential``).

    A fresh credential should be built per request (the ``user_assertion`` differs per
    user). The app registration must have delegated permission to **Azure Databricks**
    (``user_impersonation``, with admin consent) and, for the secretless path, a
    **federated identity credential** trusting the managed identity. See ``DEPLOYMENT.md``.
    """
    from azure.identity import OnBehalfOfCredential

    if not (tenant_id and client_id and user_assertion):
        raise ValueError(
            "On-Behalf-Of auth requires tenant_id, client_id, and a user_assertion "
            "(the signed-in user's access token)."
        )

    if use_managed_identity or managed_identity_client_id:
        mi = _managed_identity_credential(managed_identity_client_id or None)

        def _client_assertion() -> str:
            # The managed identity mints a token for the token-exchange audience; Entra
            # accepts it as the confidential client's assertion (federated credential).
            return mi.get_token(ENTRA_TOKEN_EXCHANGE_SCOPE).token

        return OnBehalfOfCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            user_assertion=user_assertion,
            client_assertion_func=_client_assertion,
        )

    if client_secret:
        return OnBehalfOfCredential(
            tenant_id=tenant_id,
            client_id=client_id,
            client_secret=client_secret,
            user_assertion=user_assertion,
        )

    raise ValueError(
        "On-Behalf-Of auth requires either a client_secret (secret-based) or a managed "
        "identity (use_managed_identity / managed_identity_client_id) for secretless auth."
    )
