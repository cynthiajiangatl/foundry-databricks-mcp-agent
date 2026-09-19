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

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

from azure.core.credentials import AccessToken, TokenCredential
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from opentelemetry import trace
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
from .observability import configure_observability


# Silence the cosmetic "Can't parse tool." warning: the Agent Framework's generic tool
# serializer logs it on the "agent_framework" logger for MCP tool objects. It's harmless —
# the tools are still registered and invoked correctly. Drop just that one message.
class _DropCantParseToolWarning(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return "Can't parse tool." not in record.getMessage()


logging.getLogger("agent_framework").addFilter(_DropCantParseToolWarning())

logger = logging.getLogger("foundry_databricks_agent.webapp")

# Resolves lazily, so the provider configured at startup is the one that gets used.
_tracer = trace.get_tracer("foundry_databricks_agent.webapp")

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
    # Before any agent runs, so the Agent Framework's instrumentation is active for them.
    configure_observability(settings)

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


def _conversation_span(conversation_id: str, owner_id: str):
    """Open the span the agent's own spans nest under, carrying conversation identity.

    This span is created here rather than relying on an HTTP server span: FastAPI is only
    auto-instrumented on the Application Insights path, so without it the correlation would
    silently vanish whenever traces go to an OTLP collector or the console.

    The owner key is hashed so telemetry carries a stable correlation id rather than the
    signed-in user's Entra object id.
    """
    return _tracer.start_as_current_span(
        "chat_turn",
        attributes={
            "gen_ai.conversation.id": conversation_id,
            "enduser.pseudo.id": hashlib.sha256(owner_id.encode("utf-8")).hexdigest()[:32],
        },
    )


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


# The provenance panel names the real systems on purpose: it exists to be audited, unlike
# the answer text, which stays in business language.
_TOOL_LABELS = {
    "list_lakebase_tables": "Lakebase — listed available tables",
    "describe_lakebase_table": "Lakebase — described table",
    "query_lakebase": "Lakebase — ran SQL query",
    "ask_genie": "Genie — asked a question",
}

_STEP_DETAIL_LIMIT = 800
_STEP_RESULT_LIMIT = 900

# Schema lookups the agent runs before writing SQL; hidden unless they are the whole answer.
_DISCOVERY_TOOLS = {"list_lakebase_tables", "describe_lakebase_table"}


def _uc_function_name(name: str) -> str:
    """Unity Catalog functions arrive from managed MCP as catalog__schema__function."""
    return name.rsplit("__", 1)[-1]


def _tool_label(name: str) -> str:
    if name in _TOOL_LABELS:
        return _TOOL_LABELS[name]
    if "__" in name:
        return f"Called UC function {_uc_function_name(name)}"
    return f"Called {name}" if name else "Called a tool"


def _as_mapping(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _step_detail(name: str, arguments: Any) -> tuple[str, str]:
    """What was sent, as ``(kind, text)``; kind tells the UI whether to show it as code."""
    arguments = _as_mapping(arguments)
    if isinstance(arguments, str):
        return "text", arguments[:_STEP_DETAIL_LIMIT]
    if not isinstance(arguments, dict) or not arguments:
        return "text", ""
    sql = arguments.get("sql")
    if isinstance(sql, str) and sql.strip():
        return "sql", sql.strip()[:_STEP_DETAIL_LIMIT]
    for key in ("question", "query"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return "question", value.strip()[:_STEP_DETAIL_LIMIT]
    schema, table = arguments.get("table_schema"), arguments.get("table_name")
    if isinstance(table, str) and table:
        name_text = f"{schema}.{table}" if isinstance(schema, str) and schema else table
        return "code", name_text
    if "__" in name:
        params = ", ".join(
            f"{key}={json.dumps(value, default=str)}" for key, value in arguments.items()
        )
        return "code", f"{_uc_function_name(name)}({params})"[:_STEP_DETAIL_LIMIT]
    return "code", json.dumps(arguments, default=str)[:_STEP_DETAIL_LIMIT]


def _dedupe_json_lines(text: str) -> str:
    """Drop repeated JSON payloads.

    MCP tool results arrive as the raw payload followed by a re-serialised copy of the same
    object, which differ only in spacing. Only lines that parse as JSON are compared, so
    genuinely identical table rows are never collapsed.
    """
    lines = text.splitlines()
    if len(lines) < 2:
        return text
    seen: set[str] = set()
    kept: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = None
        if stripped.startswith(("{", "[")):
            try:
                key = json.dumps(json.loads(stripped), sort_keys=True)
            except (json.JSONDecodeError, ValueError):
                key = None
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        kept.append(line)
    return "\n".join(kept)


def _as_table(payload: Any) -> str | None:
    """Render a Databricks ``{columns, rows}`` payload the way SQL results are already shown."""
    if not isinstance(payload, dict):
        return None
    columns, rows = payload.get("columns"), payload.get("rows")
    if not isinstance(columns, list) or not isinstance(rows, list):
        return None
    lines = [" | ".join(str(column) for column in columns)]
    for row in rows:
        cells = row if isinstance(row, list) else [row]
        lines.append(" | ".join("" if cell is None else str(cell) for cell in cells))
    if payload.get("is_truncated"):
        lines.append("... result truncated")
    return "\n".join(lines)


def _readable(line: str) -> str:
    """Turn a raw JSON payload into something a reviewer can scan."""
    stripped = line.strip()
    if not stripped.startswith(("{", "[")):
        return line
    try:
        payload = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return line
    table = _as_table(payload)
    if table is not None:
        return table
    return json.dumps(payload, indent=2, ensure_ascii=False)


def _flatten_result(result: Any) -> str:
    if isinstance(result, str):
        return result
    if isinstance(result, (list, tuple)):
        return "\n".join(_flatten_result(item) for item in result)
    text = getattr(result, "text", None)
    if isinstance(text, str):
        return text
    return "" if result is None else str(result)


def _result_text(result: Any) -> str:
    text = _dedupe_json_lines(_flatten_result(result))
    return "\n".join(_readable(line) for line in text.splitlines())


def _turn_steps(messages: list[Any]) -> list[dict[str, str]]:
    """Summarise the tool calls behind one answer, in the order they ran."""
    steps: dict[str, dict[str, str]] = {}
    tools: dict[str, str] = {}
    order: list[str] = []
    for message in messages:
        for content in getattr(message, "contents", None) or []:
            kind = getattr(content, "type", None)
            call_id = getattr(content, "call_id", None) or ""
            if kind == "function_call":
                key = call_id or f"step-{len(order)}"
                name = content.name or ""
                detail_kind, detail = _step_detail(name, content.arguments)
                steps[key] = {
                    "label": _tool_label(name),
                    "kind": detail_kind,
                    "detail": detail,
                    "result": "",
                }
                tools[key] = name
                order.append(key)
            elif kind == "function_result" and call_id in steps:
                text = _result_text(getattr(content, "result", None)).strip()
                if len(text) > _STEP_RESULT_LIMIT:
                    # Say so, otherwise a cut-off table reads as the complete result.
                    text = text[:_STEP_RESULT_LIMIT].rstrip() + "\n… result truncated for display"
                steps[call_id]["result"] = text

    answering = [key for key in order if tools[key] not in _DISCOVERY_TOOLS]
    # Discovery is scaffolding for writing the query; the query is what needs checking. Keep
    # it only when nothing else ran, so a pure schema question still shows its work.
    return [steps[key] for key in (answering or order)]


def _visible_steps(messages: list[Any]) -> list[dict[str, str]]:
    """Provenance for this turn, but only where the caller owns the data it exposes.

    In ``obo`` mode the steps only ever replay what Unity Catalog already let this user
    read. In ``app`` mode every caller shares the app's permissions, so returning queries
    and result rows would hand one user's data to everyone — the panel is withheld.
    """
    if _SHARED_IDENTITY:
        return []
    return _turn_steps(messages)


STARTER_PROMPT = (
    "Suggest four example questions a business user could ask you. First look at what data "
    "you can actually reach, so the questions refer to real subjects rather than invented "
    "ones. Vary them: at least one that looks up a single record and at least two that "
    "analyse or compare. Each must be under 70 characters, in plain business language, "
    "naming no system, table or column. Reply with only a JSON array of four strings."
)

# Per-owner because Unity Catalog governs visibility per user: two users can legitimately
# see different data and so deserve different suggestions.
_STARTERS: dict[str, list[str]] = {}
_STARTERS_LOCK = asyncio.Lock()
_STARTERS_MAX_OWNERS = 500


def _parse_starters(text: str) -> list[str]:
    """Pull the JSON array of questions out of the model's reply."""
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return []
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [item.strip() for item in parsed if isinstance(item, str) and item.strip()][:4]


async def _generate_starters(assertion: str | None) -> list[str]:
    """Ask the agent itself what it can answer, so suggestions match the live tools."""
    settings = load_settings()
    credential = _user_databricks_credential(assertion)
    async with build_local_mcp_agent(settings, databricks_credential=credential) as agent:
        result = await agent.run(STARTER_PROMPT)
    return _parse_starters(response_text(result))


@app.get("/api/starters")
async def starters(request: Request) -> dict[str, list[str]]:
    """Example questions derived from the tools and schema this caller can actually reach."""
    assertion = _user_assertion(request)
    if not assertion and not _LOCAL_DEV:
        return {"starters": []}
    # In shared mode every user reaches the same data, so one generation serves everyone.
    owner_id = "app" if _SHARED_IDENTITY else _conversation_owner(request, assertion)

    cached = _STARTERS.get(owner_id)
    if cached is not None:
        return {"starters": cached}

    async with _STARTERS_LOCK:
        cached = _STARTERS.get(owner_id)
        if cached is not None:
            return {"starters": cached}
        try:
            questions = await _generate_starters(assertion)
        except Exception:  # noqa: BLE001 - suggestions are optional; never break the page
            logger.exception("could not generate starter questions")
            questions = []
        if len(_STARTERS) >= _STARTERS_MAX_OWNERS:
            _STARTERS.clear()
        _STARTERS[owner_id] = questions

    return {"starters": questions}


@app.post("/api/chat")
async def chat(request: Request, body: ChatRequest) -> dict[str, Any]:
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

    with _conversation_span(conversation_id, owner_id):
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
        "steps": _visible_steps(list(getattr(result, "messages", None) or [])),
    }


# Raw: the embedded CSS and JS own their backslashes (regex escapes, \n, \u2019), so Python
# must not reinterpret them.
_INDEX_HTML = r"""<!doctype html>
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
    .row.agent .bubble-wrap { max-width: min(92%, 800px); }
    .bubble.md { white-space: normal; }
    .bubble.md > *:first-child { margin-top: 0; }
    .bubble.md > *:last-child { margin-bottom: 0; }
    .bubble.md p { margin: 0 0 9px; }
    .bubble.md h3, .bubble.md h4, .bubble.md h5, .bubble.md h6 {
      margin: 13px 0 6px; font-size: 14.5px; font-weight: 650; line-height: 1.3;
    }
    .bubble.md ul, .bubble.md ol { margin: 0 0 9px; padding-left: 21px; }
    .bubble.md li { margin: 3px 0; }
    .bubble.md code {
      background: var(--panel-2); border: 1px solid var(--border); border-radius: 5px;
      padding: 1px 4px; font-family: Consolas, "SF Mono", Menlo, monospace; font-size: 12.5px;
    }
    .bubble.md pre {
      background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px;
      padding: 9px 11px; margin: 0 0 9px; overflow-x: auto;
    }
    .bubble.md pre code { background: none; border: 0; padding: 0; }
    .tablewrap { overflow-x: auto; margin: 2px 0 9px; }
    .bubble.md table { border-collapse: collapse; font-size: 13px; }
    .bubble.md th, .bubble.md td {
      border: 1px solid var(--border); padding: 5px 10px; text-align: left; vertical-align: top;
      white-space: nowrap;
    }
    .bubble.md th { background: var(--panel-2); font-weight: 650; }
    .steps { margin: 6px 4px 0; font-size: 12px; }
    .steps > summary {
      cursor: pointer; color: var(--muted); user-select: none; list-style: none;
      padding: 3px 0;
    }
    .steps > summary::-webkit-details-marker { display: none; }
    .steps > summary::before { content: '▸  '; }
    .steps[open] > summary::before { content: '▾  '; }
    .steps > summary:hover { color: var(--text); }
    .step {
      border-left: 2px solid var(--border); margin: 9px 0 0; padding: 0 0 0 11px;
    }
    .step .step-label { color: var(--text); font-weight: 600; }
    .step .cap {
      color: var(--muted); font-size: 10px; letter-spacing: .06em; text-transform: uppercase;
      margin: 7px 0 3px;
    }
    .step pre {
      margin: 0; padding: 7px 9px; background: var(--panel-2);
      border: 1px solid var(--border); border-radius: 7px; overflow-x: auto;
      font-family: Consolas, "SF Mono", Menlo, monospace; font-size: 12px;
      white-space: pre; color: var(--text);
    }
    .step .prose { font-size: 12.5px; line-height: 1.5; color: var(--text); }
    .step .prose + .prose { margin-top: 4px; }
    .step table { border-collapse: collapse; font-size: 11.5px; }
    .step th, .step td {
      border: 1px solid var(--border); padding: 3px 8px; text-align: left;
      white-space: nowrap; color: var(--text);
    }
    .step th { background: var(--panel-2); font-weight: 650; }
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
        <div class="subtitle" id="subtitle">Answers limited to your permissions</div>
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
        <p>Ask about your data in plain language. You only ever see what your own
           permissions allow.</p>
        <div class="suggestions" id="suggestions"></div>
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

    // Minimal Markdown renderer. Answers restate untrusted tool data, so every value reaches
    // the DOM as a text node — nothing here ever assigns HTML.
    const INLINE = /(\*\*[^*]+\*\*|__[^_]+__|\*[^*\n]+\*|_[^_\n]+_|`[^`]+`)/g;

    function renderInline(text, parent) {
      let last = 0, m;
      INLINE.lastIndex = 0;
      while ((m = INLINE.exec(text)) !== null) {
        if (m.index > last) parent.appendChild(document.createTextNode(text.slice(last, m.index)));
        const token = m[0];
        let el, inner;
        if (token.startsWith('**') || token.startsWith('__')) {
          el = document.createElement('strong'); inner = token.slice(2, -2);
        } else if (token.startsWith('`')) {
          el = document.createElement('code'); inner = token.slice(1, -1);
        } else {
          el = document.createElement('em'); inner = token.slice(1, -1);
        }
        el.textContent = inner;
        parent.appendChild(el);
        last = m.index + token.length;
      }
      if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
    }

    const cellsOf = (line) =>
      line.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
    const isTableRule = (line) =>
      line != null && line.includes('-') && /^[\s:|-]+$/.test(line) && line.includes('|');
    const listItem = (line) => line.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);

    function renderMarkdown(text, root) {
      const lines = String(text).replace(/\r\n/g, '\n').split('\n');
      const para = [];
      let i = 0;

      function flush() {
        if (!para.length) return;
        const p = document.createElement('p');
        renderInline(para.join(' '), p);
        root.appendChild(p);
        para.length = 0;
      }

      while (i < lines.length) {
        const line = lines[i];

        if (/^\s*```/.test(line)) {
          flush();
          const code = [];
          i++;
          while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
          i++;
          const pre = document.createElement('pre');
          const c = document.createElement('code');
          c.textContent = code.join('\n');
          pre.appendChild(c); root.appendChild(pre);
          continue;
        }

        if (/^\s*\|/.test(line) && isTableRule(lines[i + 1])) {
          flush();
          const wrap = document.createElement('div');
          wrap.className = 'tablewrap';
          const table = document.createElement('table');
          const thead = document.createElement('thead');
          const headRow = document.createElement('tr');
          for (const cell of cellsOf(line)) {
            const th = document.createElement('th');
            renderInline(cell, th); headRow.appendChild(th);
          }
          thead.appendChild(headRow); table.appendChild(thead);
          i += 2;
          const tbody = document.createElement('tbody');
          while (i < lines.length && /^\s*\|/.test(lines[i])) {
            const tr = document.createElement('tr');
            for (const cell of cellsOf(lines[i])) {
              const td = document.createElement('td');
              renderInline(cell, td); tr.appendChild(td);
            }
            tbody.appendChild(tr); i++;
          }
          table.appendChild(tbody); wrap.appendChild(table); root.appendChild(wrap);
          continue;
        }

        const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
        if (heading) {
          flush();
          const el = document.createElement('h' + Math.min(heading[1].length + 2, 6));
          renderInline(heading[2], el); root.appendChild(el); i++;
          continue;
        }

        const item = listItem(line);
        if (item) {
          flush();
          const ordered = /\d/.test(item[1]);
          const list = document.createElement(ordered ? 'ol' : 'ul');
          while (i < lines.length) {
            const next = listItem(lines[i]);
            if (!next || /\d/.test(next[1]) !== ordered) break;
            const li = document.createElement('li');
            renderInline(next[2], li); list.appendChild(li); i++;
          }
          root.appendChild(list);
          continue;
        }

        if (!line.trim()) { flush(); i++; continue; }
        para.push(line.trim()); i++;
      }
      flush();
    }

    function addMessage(text, who, steps) {
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
      if (who === 'agent') { bubble.classList.add('md'); renderMarkdown(text, bubble); }
      else bubble.textContent = text;
      wrap.appendChild(meta); wrap.appendChild(bubble);
      if (Array.isArray(steps) && steps.length) wrap.appendChild(buildSteps(steps));
      row.appendChild(avatar); row.appendChild(wrap);
      thread.appendChild(row);
      scrollDown();
      return row;
    }

    // Every value here is tool output, so it is set with textContent and never as HTML.
    function buildSteps(steps) {
      const details = document.createElement('details');
      details.className = 'steps';
      const summary = document.createElement('summary');
      summary.textContent = steps.length === 1
        ? 'How I got this · 1 step'
        : 'How I got this · ' + steps.length + ' steps';
      details.appendChild(summary);
      steps.forEach((step, i) => {
        const box = document.createElement('div');
        box.className = 'step';
        const label = document.createElement('div');
        label.className = 'step-label';
        label.textContent = (i + 1) + '. ' + (step.label || 'Step');
        box.appendChild(label);
        if (step.detail) {
          box.appendChild(caption('Sent'));
          if (step.kind === 'question') {
            const p = document.createElement('div');
            p.className = 'prose';
            p.textContent = step.detail;
            box.appendChild(p);
          } else {
            const pre = document.createElement('pre');
            pre.textContent = step.detail;
            box.appendChild(pre);
          }
        }
        if (step.result) {
          box.appendChild(caption('Returned'));
          renderResult(step.result, box);
        }
        details.appendChild(box);
      });
      return details;
    }

    function caption(text) {
      const el = document.createElement('div');
      el.className = 'cap';
      el.textContent = text;
      return el;
    }

    // Tool results are pipe-delimited tables mixed with prose. Wrapping them in a <pre>
    // breaks column alignment on wide rows, so the tabular parts become real tables.
    const isPipeRow = (line) => line.split('|').length > 1 && line.trim() !== '';

    function renderResult(text, parent) {
      const lines = String(text).split('\n');
      let i = 0;
      while (i < lines.length) {
        const block = [];
        const tabular = isPipeRow(lines[i]);
        while (i < lines.length && isPipeRow(lines[i]) === tabular) block.push(lines[i++]);
        if (tabular && block.length >= 2) appendResultTable(block, parent);
        else appendProse(block, parent);
      }
    }

    function appendResultTable(rows, parent) {
      const wrap = document.createElement('div');
      wrap.className = 'tablewrap';
      const table = document.createElement('table');
      const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      for (const cell of rows[0].split('|')) {
        const th = document.createElement('th');
        th.textContent = cell.trim();
        headRow.appendChild(th);
      }
      thead.appendChild(headRow); table.appendChild(thead);
      const tbody = document.createElement('tbody');
      for (const row of rows.slice(1)) {
        const tr = document.createElement('tr');
        for (const cell of row.split('|')) {
          const td = document.createElement('td');
          td.textContent = cell.trim();
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
      table.appendChild(tbody); wrap.appendChild(table); parent.appendChild(wrap);
    }

    function appendProse(lines, parent) {
      for (const line of lines) {
        if (!line.trim()) continue;
        const el = document.createElement('div');
        el.className = 'prose';
        renderInline(line.trim(), el);
        parent.appendChild(el);
      }
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
        ? 'Answers limited to the app\u2019s shared permissions'
        : 'Answers limited to your own permissions';
      if (signedIn) {
        statusPill.classList.add('on');
        whoName.textContent = m.name || 'Signed in';
        if (m.interactive) {
          auth.textContent = 'Sign out'; auth.href = '/auth/logout'; auth.style.display = 'inline-block';
        }
        newChat.disabled = false;
        setEnabled(true); q.focus();
        loadStarters();
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

    // Suggestions come from the agent inspecting its own tools and schema, so they match
    // whatever this deployment can actually answer for this user.
    function loadStarters() {
      const box = document.getElementById('suggestions');
      if (!box) return;
      fetch('/api/starters').then(r => r.json()).then(d => {
        const items = Array.isArray(d.starters) ? d.starters : [];
        for (const question of items) {
          const chip = document.createElement('button');
          chip.className = 'chip';
          chip.type = 'button';
          chip.textContent = question;
          box.appendChild(chip);
        }
      }).catch(() => {});
    }

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
          addMessage(data.answer || '(no answer)', 'agent', data.steps);
        }
        else addMessage(data.detail || r.statusText || 'Request failed', 'err');
      } catch (err) {
        typing.remove();
        // fetch() rejects with a bare TypeError when the server cannot be reached at all.
        addMessage(err instanceof TypeError
          ? 'Cannot reach the server. Check that the app is still running, then try again.'
          : String(err), 'err');
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
