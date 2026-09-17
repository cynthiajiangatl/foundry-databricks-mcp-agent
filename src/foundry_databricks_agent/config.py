"""Configuration loading and validation.

All runtime configuration is read from environment variables (optionally populated
from a local ``.env`` file). Nothing here performs network calls, so it is safe to
import in unit tests.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

try:
    # python-dotenv is optional at runtime; if present we hydrate os.environ.
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - dotenv is a declared dependency
    def load_dotenv(*_args, **_kwargs):  # type: ignore[misc]
        return False


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or inconsistent."""


# A URL path segment we are willing to interpolate into a Databricks URL. Restricting to
# this set prevents URL/path-traversal injection (for example a catalog name containing
# ``/`` or ``..`` that could redirect a bearer-authenticated request to another path).
_SAFE_URL_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


def _clean(value: str | None) -> str | None:
    """Return a stripped value, treating empty strings as ``None``."""
    if value is None:
        return None
    value = value.strip()
    return value or None


def _safe_segment(value: str, var_name: str) -> str:
    """Validate ``value`` as a safe single URL path segment, or raise :class:`ConfigError`.

    Rejects empty values, path separators, traversal sequences (``..``), whitespace, and
    any character outside ``[A-Za-z0-9._-]``.
    """
    if not value or ".." in value or not _SAFE_URL_SEGMENT.match(value):
        raise ConfigError(
            f"{var_name} value {value!r} is not a valid URL path segment. Use only "
            "letters, digits, '.', '_' and '-' (no '/', '..', or whitespace)."
        )
    return value


def _require_https_authority(raw: str, var_name: str) -> str:
    """Validate ``raw`` is an ``https://`` URL and return ``https://<authority>``.

    HTTPS is mandatory because a Microsoft Entra ID bearer token is sent to this host;
    plaintext HTTP would expose the token. Any path/query/fragment is dropped.
    """
    parsed = urlparse(raw)
    if parsed.scheme != "https":
        raise ConfigError(
            f"{var_name} must use HTTPS (got scheme {parsed.scheme or 'none'!r}). A bearer "
            "token is sent to this host, so TLS is required to keep it confidential."
        )
    if not parsed.netloc:
        raise ConfigError(
            f"{var_name} is not a valid URL; expected https://<host>."
        )
    return f"https://{parsed.netloc}"


@dataclass(slots=True)
class Settings:
    """Resolved configuration for the Foundry + Databricks sample.

    Attributes mirror the variables documented in ``.env.example``.
    """

    # Azure AI Foundry
    foundry_project_endpoint: str
    foundry_model: str

    # Azure Databricks workspace. Authentication uses Microsoft Entra ID OAuth
    # (see databricks_mcp.py); no personal access token or Databricks CLI profile
    # is used. Only the workspace host is required to build managed MCP URLs.
    databricks_host: str | None = None
    # Optional override of the Entra ID OAuth token scope (e.g. for sovereign clouds).
    databricks_azure_scope: str | None = None

    # Managed MCP / tool targets
    uc_catalog: str = "main"
    uc_schema: str = "default"
    genie_space_id: str | None = None

    # Durable conversation history. When cosmos_endpoint is set the web app keeps agent
    # sessions in Azure Cosmos DB instead of process memory, so history survives restarts
    # and is shared across replicas.
    cosmos_endpoint: str | None = None
    cosmos_database: str = "agent"
    cosmos_container: str = "conversations"

    # Internal: extra metadata for diagnostics.
    _source: str = field(default="environment", repr=False)

    # -- Derived Databricks managed MCP URLs ---------------------------------

    def _require_host(self) -> str:
        if not self.databricks_host:
            raise ConfigError(
                "DATABRICKS_HOST is required to build managed MCP server URLs. "
                "Set DATABRICKS_HOST to your workspace URL (https://<workspace-host>)."
            )
        return _require_https_authority(self.databricks_host, "DATABRICKS_HOST")

    @property
    def uc_functions_mcp_url(self) -> str:
        """Managed MCP server URL for Unity Catalog functions."""
        catalog = _safe_segment(self.uc_catalog, "DATABRICKS_UC_CATALOG")
        schema = _safe_segment(self.uc_schema, "DATABRICKS_UC_SCHEMA")
        return f"{self._require_host()}/api/2.0/mcp/functions/{catalog}/{schema}"

    @property
    def genie_mcp_url(self) -> str:
        """Managed MCP server URL for the configured Genie space."""
        if not self.genie_space_id:
            raise ConfigError(
                "DATABRICKS_GENIE_SPACE_ID is required to build the Genie managed MCP URL."
            )
        space_id = _safe_segment(self.genie_space_id, "DATABRICKS_GENIE_SPACE_ID")
        return f"{self._require_host()}/api/2.0/mcp/genie/{space_id}"

    # -- Validation ----------------------------------------------------------

    @property
    def cosmos_enabled(self) -> bool:
        """True when durable conversation history is configured."""
        return bool(self.cosmos_endpoint)

    def validate_cosmos(self) -> None:
        """Ensure the Cosmos DB settings needed for durable history are usable."""
        if not self.cosmos_endpoint:
            raise ConfigError(
                "COSMOS_ENDPOINT is required to store conversation history in Azure Cosmos DB."
            )
        if not self.cosmos_database or not self.cosmos_container:
            raise ConfigError("COSMOS_DATABASE and COSMOS_CONTAINER must not be empty.")
        # An Entra token is sent to this endpoint; require TLS.
        _require_https_authority(self.cosmos_endpoint, "COSMOS_ENDPOINT")

    def validate_foundry(self) -> None:
        """Ensure the Foundry settings needed to start an agent are present."""
        missing = [
            name
            for name, value in (
                ("FOUNDRY_PROJECT_ENDPOINT", self.foundry_project_endpoint),
                ("FOUNDRY_MODEL", self.foundry_model),
            )
            if not value
        ]
        if missing:
            raise ConfigError(
                "Missing required Foundry configuration: " + ", ".join(missing)
            )
        # The Foundry data-plane token is sent to this endpoint; require TLS.
        _require_https_authority(
            self.foundry_project_endpoint, "FOUNDRY_PROJECT_ENDPOINT"
        )

    def validate_databricks_auth(self) -> None:
        """Ensure Databricks can be reached and authenticated via Entra ID OAuth.

        Authentication uses a Microsoft Entra ID credential (the same identity used
        for Azure AI Foundry), so the only required setting here is the workspace host
        used to build the managed MCP URLs. The host is validated to be an HTTPS URL so
        the bearer token is never sent in cleartext.
        """
        if not self.databricks_host:
            raise ConfigError(
                "DATABRICKS_HOST is required. Authentication to Azure Databricks uses "
                "Microsoft Entra ID OAuth (run `az login`, or pass an azure-identity "
                "credential); no personal access token or Databricks CLI profile is needed."
            )
        # Validate scheme/authority now for a clear, early error.
        self._require_host()


def load_settings(env_file: str | None = None) -> Settings:
    """Load :class:`Settings` from the environment (and optional ``.env`` file).

    Parameters
    ----------
    env_file:
        Optional path to a ``.env`` file. When ``None`` python-dotenv searches the
        current working directory and parents.

    The ``.env`` file takes precedence over pre-existing OS environment variables
    (``override=True``). This makes a local ``.env`` the source of truth for development
    and avoids surprises when another tool injects variables into the shell (for example
    the Databricks VS Code extension setting ``DATABRICKS_HOST``). In production you
    typically have no ``.env`` file, so real environment variables apply as usual.
    """
    load_dotenv(dotenv_path=env_file, override=True)

    settings = Settings(
        foundry_project_endpoint=_clean(os.getenv("FOUNDRY_PROJECT_ENDPOINT")) or "",
        foundry_model=_clean(os.getenv("FOUNDRY_MODEL")) or "",
        databricks_host=_clean(os.getenv("DATABRICKS_HOST")),
        databricks_azure_scope=_clean(os.getenv("DATABRICKS_AZURE_SCOPE")),
        uc_catalog=_clean(os.getenv("DATABRICKS_UC_CATALOG")) or "main",
        uc_schema=_clean(os.getenv("DATABRICKS_UC_SCHEMA")) or "default",
        genie_space_id=_clean(os.getenv("DATABRICKS_GENIE_SPACE_ID")),
        cosmos_endpoint=_clean(os.getenv("COSMOS_ENDPOINT")),
        cosmos_database=_clean(os.getenv("COSMOS_DATABASE")) or "agent",
        cosmos_container=_clean(os.getenv("COSMOS_CONTAINER")) or "conversations",
    )
    return settings
