"""Multi-user web UI: chat with a Microsoft Agent Framework agent on Azure AI Foundry.

Users sign in with Microsoft Entra ID (Azure Container Apps / App Service built-in
authentication, EasyAuth, which injects ``X-MS-TOKEN-AAD-ACCESS-TOKEN``). The **model**
call to Azure AI Foundry always runs as the app's own **managed identity**. The
**Databricks** tools (Unity Catalog functions + Genie, via a local MCP client) authenticate
under one of two identities, chosen by ``WEBAPP_DATABRICKS_IDENTITY``:

* ``obo`` (default) — **per-user On-Behalf-Of**: each request reaches Databricks as the
  signed-in user, and Unity Catalog governs data access **per user**.
* ``app`` — **shared app identity**: every signed-in user reaches Databricks as the app's
  managed identity, so all users share the same Databricks data permissions.

Both modes require sign-in; they differ only in the Databricks data-access identity. See
``DEPLOYMENT.md`` for the Entra app registration + EasyAuth setup.

Run locally (needs the ``web`` extra: ``pip install -e .[web]``)::

    uvicorn foundry_databricks_agent.webapp:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID, uuid4

from azure.core.credentials import AccessToken, TokenCredential
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel

from .agent import build_local_mcp_agent, response_text
from .auth import (
    DEFAULT_AZURE_DATABRICKS_SCOPE,
    build_on_behalf_of_credential,
    default_credential,
)
from .config import ConfigError, load_settings
from .conversation_store import ConversationCapacityError, ConversationSessionStore
from .cosmos_store import ConversationConflictError, CosmosConversationSessionStore


# Silence the cosmetic "Can't parse tool." warning: the Agent Framework's generic tool
# serializer logs it on the "agent_framework" logger for MCP tool objects. It's harmless —
# the tools are still registered and invoked correctly. Drop just that one message.
class _DropCantParseToolWarning(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return "Can't parse tool." not in record.getMessage()


logging.getLogger("agent_framework").addFilter(_DropCantParseToolWarning())

logger = logging.getLogger("foundry_databricks_agent.webapp")

# Populate WEBAPP_* (and model settings) from a local .env for development runs. Real
# environment variables take precedence, so containers/production are unaffected.
load_dotenv(override=False)

# On-Behalf-Of app registration (confidential client). When these are set the app exchanges
# the signed-in user's token for a Databricks token via OBO. When they are absent the app
# runs in "direct token" mode (the incoming token is used as-is) — handy for local testing.
_OBO_TENANT_ID = (os.getenv("WEBAPP_TENANT_ID") or "").strip()
_OBO_CLIENT_ID = (os.getenv("WEBAPP_CLIENT_ID") or "").strip()
_OBO_CLIENT_SECRET = (os.getenv("WEBAPP_CLIENT_SECRET") or "").strip()

# Secretless OBO (recommended for production): authenticate the confidential client with a
# managed identity (federated identity credential) instead of a client secret. Set
# WEBAPP_MANAGED_IDENTITY_CLIENT_ID to a user-assigned identity's client id, or
# WEBAPP_USE_MANAGED_IDENTITY=1 to use the system-assigned identity. When enabled this takes
# precedence over WEBAPP_CLIENT_SECRET, so production needs no secret at all.
_MI_CLIENT_ID = (os.getenv("WEBAPP_MANAGED_IDENTITY_CLIENT_ID") or "").strip()
_USE_MI = (
    (os.getenv("WEBAPP_USE_MANAGED_IDENTITY") or "").strip().lower() in {"1", "true", "yes", "on"}
    or bool(_MI_CLIENT_ID)
)

# Databricks data-access identity mode:
#   "obo" (default) — reach Databricks as the signed-in user (per-user Unity Catalog governance).
#   "app"           — reach Databricks as the app's own managed identity (shared data permissions).
# Both modes still require the user to sign in; only the Databricks identity differs.
_DATABRICKS_IDENTITY = (os.getenv("WEBAPP_DATABRICKS_IDENTITY") or "obo").strip().lower()
if _DATABRICKS_IDENTITY not in {"obo", "app"}:
    _DATABRICKS_IDENTITY = "obo"
_SHARED_IDENTITY = _DATABRICKS_IDENTITY == "app"

# LOCAL DEVELOPMENT ONLY. When set (and no EasyAuth token is present on the request), the
# app skips the sign-in gate and runs the agent under the developer's own ``az login``
# identity (DefaultAzureCredential) for BOTH the model and the Databricks tools. This lets
# you exercise the browser UI without EasyAuth. NEVER enable this in a deployed environment
# — it removes the per-user sign-in requirement.
_LOCAL_DEV = (os.getenv("WEBAPP_LOCAL_DEV") or "").strip().lower() in {"1", "true", "yes", "on"}
if _LOCAL_DEV:
    logger.warning(
        "WEBAPP_LOCAL_DEV is ENABLED: requests run under the local az-login identity "
        "(DefaultAzureCredential), bypassing per-user sign-in. Do NOT use in production."
    )

# Interactive Entra ID sign-in (for local browser testing). When these are all set, the app
# serves /auth/login + /auth/callback so a user signs in with Microsoft in the browser; the
# resulting access token (audience = this app) becomes the OBO user assertion. In production
# you front the app with EasyAuth instead and leave WEBAPP_REDIRECT_URI/API_SCOPE unset.
_REDIRECT_URI = (os.getenv("WEBAPP_REDIRECT_URI") or "").strip()
_API_SCOPE = (os.getenv("WEBAPP_API_SCOPE") or "").strip()
_INTERACTIVE = bool(
    _OBO_TENANT_ID and _OBO_CLIENT_ID and _OBO_CLIENT_SECRET and _REDIRECT_URI and _API_SCOPE
)

# Opaque server-side session store (keeps bearer tokens out of cookies). Keyed by a random
# session id delivered via an http-only cookie. In-memory / single-process — fine for local
# testing; use a shared store for multi-instance production.
_SESSION_COOKIE = "fdba_sid"
_SESSIONS: dict[str, dict[str, object]] = {}

if _INTERACTIVE:
    logger.info(
        "Interactive Entra ID sign-in ENABLED (/auth/login) for client id %s", _OBO_CLIENT_ID
    )

logger.info("Databricks data-access identity mode: %s", _DATABRICKS_IDENTITY)

# Agent Framework conversation sessions, isolated per authenticated user. Swapped for the
# Cosmos DB-backed store during startup when COSMOS_ENDPOINT is configured.
_CONVERSATIONS: ConversationSessionStore | CosmosConversationSessionStore = (
    ConversationSessionStore()
)


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global _CONVERSATIONS

    settings = load_settings()
    if settings.cosmos_enabled:
        settings.validate_cosmos()
        _CONVERSATIONS = CosmosConversationSessionStore(
            endpoint=settings.cosmos_endpoint or "",
            database=settings.cosmos_database,
            container=settings.cosmos_container,
        )
        logger.info(
            "Conversation history: Azure Cosmos DB (%s/%s)",
            settings.cosmos_database,
            settings.cosmos_container,
        )
    else:
        _CONVERSATIONS = ConversationSessionStore()
        logger.warning(
            "Conversation history is in-memory and is lost on restart. Set COSMOS_ENDPOINT "
            "to persist it."
        )

    try:
        yield
    finally:
        aclose = getattr(_CONVERSATIONS, "aclose", None)
        if aclose is not None:
            await aclose()


app = FastAPI(title="foundry-databricks-agent", lifespan=_lifespan)


class ChatRequest(BaseModel):
    question: str
    conversation_id: UUID | None = None


class _StaticTokenCredential:
    """Wrap a pre-obtained access token as a :class:`TokenCredential` (direct-token mode)."""

    def __init__(self, token: str) -> None:
        # The token is used immediately for the current request; give it a short TTL.
        self._token = AccessToken(token, int(time.time()) + 3000)

    def get_token(self, *_scopes: str, **_kwargs: object) -> AccessToken:
        return self._token


def _session(request: Request) -> dict[str, object]:
    """Return the current browser session dict (empty if none / not signed in)."""
    sid = request.cookies.get(_SESSION_COOKIE)
    if sid and sid in _SESSIONS:
        return _SESSIONS[sid]
    return {}


def _msal_app():
    """Build the MSAL confidential-client application for interactive sign-in."""
    import msal

    return msal.ConfidentialClientApplication(
        _OBO_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{_OBO_TENANT_ID}",
        client_credential=_OBO_CLIENT_SECRET,
    )


def _user_assertion(request: Request) -> str | None:
    """Return the signed-in user's access token (EasyAuth header, Bearer, or session)."""
    token = request.headers.get("X-MS-TOKEN-AAD-ACCESS-TOKEN")
    if token:
        return token
    authz = request.headers.get("Authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip() or None
    assertion = _session(request).get("assertion")
    return assertion if isinstance(assertion, str) else None


def _user_name(request: Request) -> str:
    name = request.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
    if name:
        return name
    sname = _session(request).get("name")
    if isinstance(sname, str):
        return sname
    if _LOCAL_DEV:
        return "local dev (az login)"
    return "signed-in user"


def _conversation_owner(request: Request, assertion: str | None) -> str:
    """Return a stable isolation key so a conversation id only resolves for its owner."""
    principal = request.headers.get("X-MS-CLIENT-PRINCIPAL-ID")
    if request.headers.get("X-MS-TOKEN-AAD-ACCESS-TOKEN") and principal:
        return f"easyauth:{principal}"
    sid = request.cookies.get(_SESSION_COOKIE)
    if sid and sid in _SESSIONS:
        return f"interactive:{sid}"
    if assertion:
        return "bearer:" + hashlib.sha256(assertion.encode("utf-8")).hexdigest()
    if _LOCAL_DEV:
        return "local-dev"
    raise HTTPException(status_code=401, detail="Not signed in.")


def _user_databricks_credential(assertion: str | None) -> TokenCredential:
    """Build the credential used to reach Databricks for this request.

    In ``app`` mode (WEBAPP_DATABRICKS_IDENTITY=app) every request reaches Databricks as the
    app's own managed identity — all signed-in users share the same data permissions.

    In ``obo`` mode (default) it prefers **secretless** OBO (managed identity), then
    secret-based OBO, then direct-token mode (the incoming token is already a Databricks
    token). With no user token at all (local-dev only), it falls back to the app identity.
    """
    # Shared-identity mode: reach Databricks as the app itself. Sign-in is still enforced by
    # the request handlers, so this stays a multi-user app — only data permissions are shared.
    if _SHARED_IDENTITY:
        return default_credential()
    if assertion:
        if _OBO_TENANT_ID and _OBO_CLIENT_ID and (_USE_MI or _OBO_CLIENT_SECRET):
            return build_on_behalf_of_credential(
                tenant_id=_OBO_TENANT_ID,
                client_id=_OBO_CLIENT_ID,
                user_assertion=assertion,
                client_secret=(None if _USE_MI else _OBO_CLIENT_SECRET),
                managed_identity_client_id=(_MI_CLIENT_ID or None),
                use_managed_identity=_USE_MI,
            )
        # Direct-token mode: the incoming token is already an Azure Databricks token.
        return _StaticTokenCredential(assertion)  # type: ignore[return-value]
    # No user token on the request. NEVER fall back to the app identity when a user is
    # expected — querying Databricks as the app's service principal would bypass per-user
    # Unity Catalog governance (every user would see the app's data). Only the explicit
    # local-dev shortcut may use the developer's own identity.
    if _LOCAL_DEV:
        return default_credential()
    raise HTTPException(
        status_code=401,
        detail=(
            "No signed-in user identity available for Databricks. Refusing to run as the "
            "app identity so per-user Unity Catalog governance is preserved."
        ),
    )


# Identity claims we surface for verification (never the raw token or signature).
_IDENTITY_CLAIMS = (
    "preferred_username", "upn", "unique_name", "email",
    "oid", "tid", "aud", "appid", "azp", "scp", "idtyp",
)


def _decode_jwt_claims(token: str) -> dict[str, object]:
    """Best-effort decode of a JWT payload's identity claims (NO signature verification).

    Used only to *display* which identity a token represents so per-user passthrough can be
    verified — never for authorization. Returns {} for opaque / non-JWT tokens.
    """
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)  # restore base64url padding
        data = json.loads(base64.urlsafe_b64decode(payload))
    except Exception:  # noqa: BLE001 - display helper only
        return {}
    return {k: data[k] for k in _IDENTITY_CLAIMS if k in data}


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/me")
def me(request: Request) -> dict[str, object]:
    signed_in = _user_assertion(request) is not None or _LOCAL_DEV
    return {
        "name": _user_name(request),
        "signedIn": signed_in,
        "interactive": _INTERACTIVE,
        "mode": _DATABRICKS_IDENTITY,
    }


@app.get("/api/whoami")
def whoami(request: Request) -> dict[str, object]:
    """Verify which identity Databricks will see for this request.

    Acquires the *same* Databricks-scoped token the Unity Catalog / Genie tools use and
    returns its non-sensitive identity claims (never the raw token). In ``obo`` mode this
    shows the signed-in user's ``oid`` / ``upn``; in ``app`` mode it shows the app's managed
    identity (shared data access).
    """
    assertion = _user_assertion(request)
    if not assertion and not _LOCAL_DEV:
        raise HTTPException(status_code=401, detail="Not signed in.")
    try:
        credential = _user_databricks_credential(assertion)
        token = credential.get_token(DEFAULT_AZURE_DATABRICKS_SCOPE).token
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a clean error
        raise HTTPException(
            status_code=502, detail=f"Could not acquire the Databricks token: {exc}"
        ) from exc
    claims = _decode_jwt_claims(token)
    is_app_token = claims.get("idtyp") == "app" or ("appid" in claims and "oid" not in claims)
    return {
        "signedInAs": _user_name(request),
        "databricksTokenIdentity": {
            "user": claims.get("preferred_username")
            or claims.get("upn")
            or claims.get("unique_name"),
            "objectId": claims.get("oid"),
            "tenantId": claims.get("tid"),
            "audience": claims.get("aud"),
            "delegatedScopes": claims.get("scp"),
        },
        "isAppIdentity": is_app_token,
        "identityMode": _DATABRICKS_IDENTITY,
        "perUserGovernance": (not is_app_token) and bool(claims.get("oid")),
        "note": (
            "In 'obo' mode Unity Catalog enforces access as the signed-in user "
            "(isAppIdentity=false). In 'app' mode all users share the app's managed "
            "identity for Databricks (isAppIdentity=true by design)."
        ),
    }


@app.get("/auth/login")
def auth_login(request: Request):
    if not _INTERACTIVE:
        raise HTTPException(status_code=404, detail="Interactive sign-in is not configured.")
    # prompt="select_account" forces Microsoft to show the account picker instead of
    # silently reusing an existing SSO session, so the user is always asked to sign in.
    flow = _msal_app().initiate_auth_code_flow(
        scopes=[_API_SCOPE], redirect_uri=_REDIRECT_URI, prompt="select_account"
    )
    sid = request.cookies.get(_SESSION_COOKIE) or secrets.token_urlsafe(32)
    _SESSIONS.setdefault(sid, {})["flow"] = flow
    resp = RedirectResponse(flow["auth_uri"], status_code=302)
    resp.set_cookie(_SESSION_COOKIE, sid, httponly=True, samesite="lax", path="/")
    return resp


@app.get("/auth/callback")
def auth_callback(request: Request):
    if not _INTERACTIVE:
        raise HTTPException(status_code=404, detail="Interactive sign-in is not configured.")
    sid = request.cookies.get(_SESSION_COOKIE)
    sess = _SESSIONS.get(sid or "", {})
    flow = sess.get("flow")
    if not isinstance(flow, dict):
        return RedirectResponse("/", status_code=302)
    result = _msal_app().acquire_token_by_auth_code_flow(flow, dict(request.query_params))
    sess.pop("flow", None)
    if "access_token" not in result:
        detail = result.get("error_description") or result.get("error") or "sign-in failed"
        return HTMLResponse(f"<h3>Sign-in failed</h3><pre>{detail}</pre>", status_code=400)
    sess["assertion"] = result["access_token"]
    claims = result.get("id_token_claims") or {}
    sess["name"] = claims.get("preferred_username") or claims.get("name") or "user"
    return RedirectResponse("/", status_code=302)


@app.get("/auth/logout")
def auth_logout(request: Request):
    sid = request.cookies.get(_SESSION_COOKIE)
    if sid:
        _SESSIONS.pop(sid, None)
    resp = RedirectResponse("/", status_code=302)
    resp.delete_cookie(_SESSION_COOKIE, path="/")
    return resp


@app.delete("/api/conversations/{conversation_id}", status_code=204)
async def reset_conversation(request: Request, conversation_id: UUID) -> Response:
    """Drop one conversation's history so the next question starts from an empty context."""
    assertion = _user_assertion(request)
    if not assertion and not _LOCAL_DEV:
        raise HTTPException(status_code=401, detail="Not signed in.")
    await _CONVERSATIONS.reset(
        _conversation_owner(request, assertion), str(conversation_id)
    )
    return Response(status_code=204)


@app.post("/api/chat")
async def chat(request: Request, body: ChatRequest) -> dict[str, str]:
    assertion = _user_assertion(request)
    if not assertion and not _LOCAL_DEV:
        raise HTTPException(
            status_code=401,
            detail=(
                "Not signed in. Enable Microsoft Entra ID authentication on the container "
                "app so the user's token is available."
            ),
        )
    question = (body.question or "").strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question must not be empty.")
    conversation_id = str(body.conversation_id or uuid4())
    owner_id = _conversation_owner(request, assertion)

    try:
        settings = load_settings()
        credential = _user_databricks_credential(assertion)
        logger.info(
            "Running Databricks tools as %s (mode=%s)",
            "the app identity" if _SHARED_IDENTITY else _user_name(request),
            _DATABRICKS_IDENTITY,
        )
        async with _CONVERSATIONS.session(owner_id, conversation_id) as agent_session:
            async with build_local_mcp_agent(
                settings,
                databricks_credential=credential,
                conversation_state=agent_session.state,
            ) as agent:
                result = await agent.run(question, session=agent_session)
    except ConfigError as exc:
        raise HTTPException(status_code=500, detail=f"Configuration error: {exc}") from exc
    except ConversationCapacityError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ConversationConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface a clean error to the UI
        logger.exception("agent run failed for %s", _user_name(request))
        raise HTTPException(status_code=502, detail=f"Agent error: {exc}") from exc

    return {
        "user": _user_name(request),
        "answer": response_text(result),
        "conversation_id": conversation_id,
    }


_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Azure Databricks agent</title>
  <style>
    :root {
      color-scheme: light dark;
      --bg: #f7f8fa; --panel: #ffffff; --panel-2: #f0f2f5; --border: #e3e6ea;
      --text: #1b1f24; --muted: #6b7280; --accent: #2f6fed; --accent-contrast: #ffffff;
      --agent-bubble: #f0f2f5; --err-bg: #fdecec; --err-text: #b42318;
      --shadow: 0 1px 2px rgba(16,24,40,.06), 0 1px 3px rgba(16,24,40,.10);
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg: #0f1216; --panel: #161a20; --panel-2: #1e242c; --border: #2a313a;
        --text: #e7eaee; --muted: #9aa4b2; --accent: #4c8dff; --accent-contrast: #0b0e12;
        --agent-bubble: #1e242c; --err-bg: #3a1d1d; --err-text: #ff9a9a;
        --shadow: 0 1px 2px rgba(0,0,0,.4);
      }
    }
    * { box-sizing: border-box; }
    html, body { height: 100%; }
    body {
      margin: 0; background: var(--bg); color: var(--text);
      font-family: "Segoe UI", system-ui, -apple-system, Roboto, Arial, sans-serif;
      display: flex; flex-direction: column; height: 100dvh;
    }
    header {
      display: flex; align-items: center; justify-content: space-between; gap: 12px;
      padding: 12px 20px; background: var(--panel); border-bottom: 1px solid var(--border);
      box-shadow: var(--shadow); z-index: 5;
    }
    .brand { display: flex; align-items: center; gap: 12px; min-width: 0; }
    .brand .logo {
      width: 34px; height: 34px; border-radius: 9px; flex: none; color: #fff;
      background: linear-gradient(135deg, var(--accent), #7aa7ff);
      display: grid; place-items: center; font-weight: 700; font-size: 15px;
    }
    .brand .title { font-weight: 650; font-size: 15px; line-height: 1.15; }
    .brand .subtitle { color: var(--muted); font-size: 12px; }
    .who { display: flex; align-items: center; gap: 10px; font-size: 13px; }
    .icon-btn {
      width: 34px; height: 34px; padding: 0; display: grid; place-items: center;
      font-size: 20px; line-height: 1;
    }
    .pill {
      display: inline-flex; align-items: center; gap: 6px; padding: 5px 10px;
      border-radius: 999px; background: var(--panel-2); color: var(--muted);
      border: 1px solid var(--border); max-width: 240px;
    }
    .pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); flex: none; }
    .pill.on .dot { background: #22c55e; }
    .pill .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .who a { color: var(--accent); text-decoration: none; font-weight: 550; }
    .who a:hover { text-decoration: underline; }
    .btn {
      padding: 7px 14px; border-radius: 8px; border: 1px solid var(--border);
      background: var(--panel-2); color: var(--text); cursor: pointer; font-size: 13px;
      font-family: inherit; text-decoration: none;
    }
    .btn.primary { background: var(--accent); color: var(--accent-contrast); border-color: transparent; }
    .btn:hover { filter: brightness(1.03); }

    main { flex: 1; overflow-y: auto; }
    .thread {
      max-width: 860px; margin: 0 auto; padding: 24px 20px 8px;
      display: flex; flex-direction: column; gap: 18px;
    }
    .row { display: flex; gap: 12px; align-items: flex-start; }
    .row.you { flex-direction: row-reverse; }
    .avatar {
      width: 30px; height: 30px; border-radius: 50%; flex: none; display: grid;
      place-items: center; font-size: 12px; font-weight: 650; color: #fff;
    }
    .row.agent .avatar { background: linear-gradient(135deg, var(--accent), #7aa7ff); }
    .row.you .avatar { background: #6b7280; }
    .row.err .avatar { background: #ef4444; }
    .bubble-wrap { display: flex; flex-direction: column; max-width: min(80%, 680px); }
    .row.you .bubble-wrap { align-items: flex-end; }
    .meta { font-size: 11px; color: var(--muted); margin: 0 4px 3px; }
    .bubble {
      padding: 11px 14px; border-radius: 14px; line-height: 1.5; font-size: 14.5px;
      white-space: pre-wrap; overflow-wrap: anywhere; box-shadow: var(--shadow);
    }
    .row.agent .bubble { background: var(--agent-bubble); border-top-left-radius: 4px; }
    .row.you .bubble { background: var(--accent); color: var(--accent-contrast); border-top-right-radius: 4px; }
    .row.err .bubble { background: var(--err-bg); color: var(--err-text); }

    .typing { display: inline-flex; gap: 4px; align-items: center; padding: 3px 2px; }
    .typing span {
      width: 7px; height: 7px; border-radius: 50%; background: var(--muted);
      animation: blink 1.2s infinite ease-in-out;
    }
    .typing span:nth-child(2) { animation-delay: .2s; }
    .typing span:nth-child(3) { animation-delay: .4s; }
    @keyframes blink { 0%,80%,100% { opacity:.3; transform: translateY(0);} 40% { opacity:1; transform: translateY(-2px);} }

    .welcome { text-align: center; color: var(--muted); margin: 8vh auto 0; max-width: 620px; }
    .welcome h1 { color: var(--text); font-size: 22px; margin: 0 0 6px; }
    .welcome p { margin: 0 0 20px; font-size: 14px; }
    .suggestions { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
    .chip {
      padding: 10px 14px; border-radius: 12px; border: 1px solid var(--border);
      background: var(--panel); color: var(--text); cursor: pointer; font-size: 13px;
      text-align: left; box-shadow: var(--shadow); max-width: 280px; font-family: inherit;
    }
    .chip:hover { border-color: var(--accent); }

    .composer-wrap { border-top: 1px solid var(--border); background: var(--panel); }
    .composer {
      max-width: 860px; margin: 0 auto; padding: 12px 20px 6px;
      display: flex; gap: 10px; align-items: flex-end;
    }
    .composer textarea {
      flex: 1; resize: none; max-height: 180px; min-height: 24px; padding: 12px 14px;
      border-radius: 12px; border: 1px solid var(--border); background: var(--bg);
      color: var(--text); font-family: inherit; font-size: 14.5px; line-height: 1.4;
    }
    .composer textarea:focus { outline: none; border-color: var(--accent); }
    .composer textarea:disabled { opacity: .6; }
    .send-btn {
      flex: none; width: 44px; height: 44px; border-radius: 12px; border: 0;
      background: var(--accent); color: var(--accent-contrast); cursor: pointer;
      display: grid; place-items: center;
    }
    .send-btn:disabled { opacity: .45; cursor: default; }
    .hint {
      max-width: 860px; margin: 0 auto; padding: 0 20px 10px;
      font-size: 11px; color: var(--muted); text-align: center;
    }
  </style>
</head>
<body>
  <header>
    <div class="brand">
      <div class="logo">DB</div>
      <div>
        <div class="title">Azure Databricks agent</div>
        <div class="subtitle" id="subtitle">Governed by Unity Catalog</div>
      </div>
    </div>
    <div class="who">
      <button id="newChat" class="btn icon-btn" type="button" title="New conversation"
              aria-label="New conversation" disabled>+</button>
      <span id="statusPill" class="pill"><span class="dot"></span><span id="whoName" class="name">…</span></span>
      <a id="auth" class="btn" href="#" style="display:none"></a>
    </div>
  </header>

  <main id="main">
    <div class="thread" id="thread">
      <div class="welcome" id="welcome">
        <h1>How can I help?</h1>
        <p>Ask about your governed lakehouse data. I use Unity Catalog functions and Genie
           under your own identity.</p>
        <div class="suggestions" id="suggestions">
          <button class="chip" type="button">What Unity Catalog functions can you run?</button>
          <button class="chip" type="button">What can the Genie space answer?</button>
          <button class="chip" type="button">How many orders shipped last week?</button>
        </div>
      </div>
    </div>
  </main>

  <div class="composer-wrap">
    <form class="composer" id="f">
      <textarea id="q" rows="1" placeholder="Message the Databricks agent…" autocomplete="off"></textarea>
      <button class="send-btn" id="send" type="submit" title="Send" aria-label="Send">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
        </svg>
      </button>
    </form>
    <div class="hint" id="hint">Enter to send · Shift+Enter for a new line</div>
  </div>

  <script>
    const thread = document.getElementById('thread');
    const welcome = document.getElementById('welcome');
    const whoName = document.getElementById('whoName');
    const statusPill = document.getElementById('statusPill');
    const auth = document.getElementById('auth');
    const q = document.getElementById('q');
    const send = document.getElementById('send');
    const hint = document.getElementById('hint');
    const main = document.getElementById('main');
    const subtitle = document.getElementById('subtitle');
    const newChat = document.getElementById('newChat');
    let signedIn = false;
    let conversationId = localStorage.getItem('fdba_conversation_id') || crypto.randomUUID();
    localStorage.setItem('fdba_conversation_id', conversationId);

    function now() {
      return new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    function scrollDown() { main.scrollTop = main.scrollHeight; }
    function dismissWelcome() { if (welcome && welcome.parentNode) welcome.remove(); }

    function addMessage(text, who) {
      dismissWelcome();
      const row = document.createElement('div');
      row.className = 'row ' + who;
      const avatar = document.createElement('div');
      avatar.className = 'avatar';
      avatar.textContent = who === 'you' ? 'You' : who === 'err' ? '!' : 'AI';
      const wrap = document.createElement('div');
      wrap.className = 'bubble-wrap';
      const meta = document.createElement('div');
      meta.className = 'meta';
      meta.textContent = (who === 'you' ? 'You' : who === 'err' ? 'Error' : 'Agent') + ' · ' + now();
      const bubble = document.createElement('div');
      bubble.className = 'bubble';
      bubble.textContent = text;
      wrap.appendChild(meta); wrap.appendChild(bubble);
      row.appendChild(avatar); row.appendChild(wrap);
      thread.appendChild(row);
      scrollDown();
      return row;
    }

    function addTyping() {
      dismissWelcome();
      const row = document.createElement('div');
      row.className = 'row agent';
      row.innerHTML = '<div class="avatar">AI</div><div class="bubble-wrap">' +
        '<div class="meta">Agent · thinking…</div>' +
        '<div class="bubble"><div class="typing"><span></span><span></span><span></span></div></div></div>';
      thread.appendChild(row);
      scrollDown();
      return row;
    }

    function autoGrow() {
      q.style.height = 'auto';
      q.style.height = Math.min(q.scrollHeight, 180) + 'px';
    }
    function setEnabled(on) { q.disabled = !on; send.disabled = !on; }

    fetch('/api/me').then(r => r.json()).then(m => {
      signedIn = !!m.signedIn;
      if (subtitle) subtitle.textContent = m.mode === 'app'
        ? 'Governed by Unity Catalog · Databricks runs as the app (shared)'
        : 'Governed by Unity Catalog · Databricks runs as you (per-user)';
      if (signedIn) {
        statusPill.classList.add('on');
        whoName.textContent = m.name || 'Signed in';
        if (m.interactive) {
          auth.textContent = 'Sign out'; auth.href = '/auth/logout'; auth.style.display = 'inline-block';
        }
        newChat.disabled = false;
        setEnabled(true); q.focus();
      } else {
        whoName.textContent = 'Not signed in';
        if (m.interactive) {
          auth.textContent = 'Sign in with Microsoft'; auth.href = '/auth/login';
          auth.classList.add('primary'); auth.style.display = 'inline-block';
        }
        setEnabled(false);
        q.placeholder = 'Sign in to start chatting…';
        hint.textContent = m.interactive
          ? 'Please sign in with Microsoft to continue.'
          : 'Sign-in is required. Configure authentication to continue.';
      }
    }).catch(() => { whoName.textContent = ''; });

    document.getElementById('suggestions').addEventListener('click', (e) => {
      const chip = e.target.closest('.chip');
      if (!chip || !signedIn) return;
      q.value = chip.textContent.trim(); autoGrow(); q.focus();
    });

    newChat.addEventListener('click', async () => {
      const previous = conversationId;
      conversationId = crypto.randomUUID();
      localStorage.setItem('fdba_conversation_id', conversationId);
      try {
        await fetch('/api/conversations/' + encodeURIComponent(previous), { method: 'DELETE' });
      } finally {
        window.location.reload();
      }
    });

    async function submit() {
      const text = q.value.trim();
      if (!text || send.disabled) return;
      addMessage(text, 'you');
      q.value = ''; autoGrow();
      setEnabled(false);
      const typing = addTyping();
      try {
        const r = await fetch('/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ question: text, conversation_id: conversationId })
        });
        const data = await r.json().catch(() => ({}));
        typing.remove();
        if (r.ok) {
          if (data.conversation_id) {
            conversationId = data.conversation_id;
            localStorage.setItem('fdba_conversation_id', conversationId);
          }
          addMessage(data.answer || '(no answer)', 'agent');
        }
        else addMessage(data.detail || r.statusText || 'Request failed', 'err');
      } catch (err) {
        typing.remove();
        addMessage(String(err), 'err');
      } finally {
        setEnabled(true); q.focus();
      }
    }

    document.getElementById('f').addEventListener('submit', (e) => { e.preventDefault(); submit(); });
    q.addEventListener('input', autoGrow);
    q.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submit(); }
    });
  </script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return _INDEX_HTML
