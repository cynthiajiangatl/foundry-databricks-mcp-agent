# Multi-user Databricks agent on Azure AI Foundry (managed MCP)

A runnable **Python** multi-user **web app**: a **Microsoft Agent Framework** agent whose
**model** runs in **Azure AI Foundry** and whose tools are **Azure Databricks managed MCP**
servers — **Unity Catalog functions** and a **Genie space** — reached through a client-side
(local) MCP client. The agent run loop executes **in your own process** (the FastAPI app),
not in the Foundry Agent Service.

It supports **two multi-user use cases**, chosen by a single environment variable
(`WEBAPP_DATABRICKS_IDENTITY`):

1. **Per-user On-Behalf-Of (`obo`, default)** — every signed-in user reaches Databricks as
   **their own** Entra identity, so Unity Catalog governs data access **per user**.
2. **Shared app identity (`app`)** — every signed-in user reaches Databricks as the **app's
   own managed identity**, so all users share the same Databricks data permissions.

In **both** modes the **model** call to Azure AI Foundry always runs as the **app's managed
identity**, and users always sign in with Microsoft Entra ID.

Conversations are **multi-turn**: each user's chat history is kept in a Microsoft Agent
Framework session and persisted to **Azure Cosmos DB**, so it survives restarts and is shared
across replicas.

---

## Architecture

```
     Browser (end user) ── sign in (Entra ID / EasyAuth) ──► user token
        │  ask a question (+ conversation id)
        ▼
  ┌────────────────────────────────────────────────┐
  │  Web app · webapp.py (FastAPI)                  │
  │  Microsoft Agent Framework Agent                │
  │  (build_local_mcp_agent, runs in this process)  │
  └──┬───────────────┬────────────────────┬────────┘
     │ history       │  model turn        │  UC functions + ask_genie
     │ (AgentSession)│  as the APP        │  as the OBO user (obo mode) OR
     │               │  (managed id)      │  as the APP       (app mode)
     ▼               ▼                    ▼
 ┌─────────────┐ ┌────────────────────┐ ┌────────────────────────────────┐
 │ Azure       │ │  Azure AI Foundry  │ │  Azure Databricks · managed MCP │
 │ Cosmos DB   │ │  • model           │ │  governed by Unity Catalog      │
 │ • sessions  │ └────────────────────┘ │   ├─ /api/2.0/mcp/functions/…   │
 └─────────────┘                        │   └─ /api/2.0/mcp/genie/…       │
                                        └────────────────────────────────┘
```

- The agent is a Microsoft Agent Framework `Agent` backed by `FoundryChatClient`, built by
  [`build_local_mcp_agent`](src/foundry_databricks_agent/agent.py). Its run loop executes in
  the web app process.
- **Unity Catalog functions** are reached as **local MCP-client** tools
  (`MCPStreamableHTTPTool`): the app process connects to the Databricks managed MCP servers
  directly and attaches a Microsoft Entra ID OAuth bearer token, refreshed per call.
- **Genie** is exposed to the model as a single `ask_genie` tool
  ([`genie.py`](src/foundry_databricks_agent/genie.py)). Genie answers asynchronously, and that
  ask/poll cycle runs *inside* the tool call rather than being driven by the model — see
  [Conversation history and efficiency](#conversation-history-and-efficiency).
- Chat history lives in an `AgentSession` per user and conversation, persisted to **Azure
  Cosmos DB** when `COSMOS_ENDPOINT` is set (in-process memory otherwise).
- The **model** always authenticates as the **app's managed identity**
  (`DefaultAzureCredential`). The **Databricks** tools authenticate with the credential the
  request handler passes — the signed-in user (OBO) or the app identity — per the mode.
- All Databricks access is **governed by Unity Catalog** under whichever identity is used.

### The two identity modes

| `WEBAPP_DATABRICKS_IDENTITY` | Model identity | Databricks identity | Data governance |
| --- | --- | --- | --- |
| `obo` (default) | app managed identity | **signed-in user** (On-Behalf-Of) | Unity Catalog **per user** |
| `app` | app managed identity | **app** managed identity | shared — the app's grants apply to all users |

Both modes require sign-in (multi-user). They differ only in **which identity reaches
Databricks**, and therefore in **who the Unity Catalog grants must be given to**:

- `obo` → grant each **end user** (or an Entra group) Unity Catalog access.
- `app` → grant the **app's managed identity** Unity Catalog access.

---

## Project layout

```
foundry-databricks-mcp-agent/
├── README.md
├── DEPLOYMENT.md                  # Azure Container Apps deployment (both modes)
├── Dockerfile                     # container image for the web app
├── requirements.txt
├── pyproject.toml
├── .env.example
├── infra/                         # Bicep: the whole stack, including Cosmos DB
│   ├── main.bicep
│   └── modules/
├── tests/                         # unit tests (no Azure or Databricks needed)
└── src/foundry_databricks_agent/
    ├── config.py                 # env-based settings + managed-MCP URL builders
    ├── auth.py                   # centralized Entra ID OAuth (tokens + OBO credential)
    ├── databricks_mcp.py         # local MCP-client tool factories (UC functions, Genie)
    ├── genie.py                  # single-call ask_genie tool (runs Genie's ask/poll cycle)
    ├── agent.py                  # builds the Agent Framework agent + tools + history
    ├── conversation_store.py     # in-memory session store (local dev fallback)
    ├── cosmos_store.py           # durable session store backed by Azure Cosmos DB
    └── webapp.py                 # multi-user FastAPI web app (obo | app identity modes)
```

---

## Prerequisites

- **Python 3.10+**.
- An **Azure AI Foundry** project with a deployed chat model (e.g. `gpt-4o-mini`), and the
  app's identity granted a Foundry data-plane role (e.g. **Azure AI User**) to call the model.
- An **Azure Databricks** workspace with:
  - Unity Catalog functions in some `catalog.schema`,
  - (optional) a **Genie space**.
- *(optional)* An **Azure Cosmos DB** account (NoSQL API) for durable conversation history.
  Without it the app still runs, but history is kept in memory and lost on restart.
- **Microsoft Entra ID** identities:
  - the **app's** managed identity (in Azure) or your `az login` session (local dev) for the
    model, and
  - depending on the mode, either each **end user** (`obo`) or the **app's** identity (`app`)
    granted Unity Catalog access (`USE CATALOG` / `USE SCHEMA` / `EXECUTE`, and `CAN RUN` on
    the Genie space).

No Databricks personal access token (PAT) and no Databricks CLI profile are used —
Databricks access is Microsoft Entra ID OAuth, governed by Unity Catalog.

---

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# macOS/Linux:
# source .venv/bin/activate

# 2. Install the web app (package + web extra)
pip install -e ".[web]"

# 3. Configure
cp .env.example .env     # then edit .env  (Windows: copy .env.example .env)

# 4. Authenticate to Azure (local dev). In Azure, managed identity is used automatically.
az login
```

### Core `.env` settings

| Variable | Purpose |
| --- | --- |
| `FOUNDRY_PROJECT_ENDPOINT` | `https://<resource>.services.ai.azure.com/api/projects/<project>` (**HTTPS required**) |
| `FOUNDRY_MODEL` | Model deployment name, e.g. `gpt-4o-mini` |
| `DATABRICKS_HOST` | Workspace URL, e.g. `https://adb-….azuredatabricks.net` (**HTTPS required**) |
| `DATABRICKS_AZURE_SCOPE` | *(optional)* Override the Entra ID OAuth token scope (sovereign clouds) |
| `DATABRICKS_UC_CATALOG` / `DATABRICKS_UC_SCHEMA` | Unity Catalog functions location |
| `DATABRICKS_GENIE_SPACE_ID` | *(optional)* Genie space id (enables the Genie tool) |
| `WEBAPP_DATABRICKS_IDENTITY` | `obo` (default) or `app` — the Databricks data-access identity |
| `COSMOS_ENDPOINT` | *(optional)* Cosmos DB account endpoint for durable conversation history. Unset = in-memory (lost on restart) |
| `COSMOS_DATABASE` / `COSMOS_CONTAINER` | Cosmos database / container names (default `agent` / `conversations`) |

`DATABRICKS_HOST` and `FOUNDRY_PROJECT_ENDPOINT` must be HTTPS (a bearer token is sent to
them), and URL path segments (catalog, schema, Genie space id) are validated to block
injection. Values in `.env` take precedence over OS environment variables (the app loads it
with `load_dotenv(override=True)`), so a local `.env` is the source of truth during
development. Genie is added only when `DATABRICKS_GENIE_SPACE_ID` is set.

### Web-app auth settings (`WEBAPP_*`)

| Variable | Purpose |
| --- | --- |
| `WEBAPP_DATABRICKS_IDENTITY` | `obo` (per-user OBO, default) or `app` (shared app identity) |
| `WEBAPP_TENANT_ID` / `WEBAPP_CLIENT_ID` | Entra app registration (sign-in and, in `obo` mode, the OBO exchange) |
| `WEBAPP_USE_MANAGED_IDENTITY` | `1` to authenticate the OBO client with a managed identity (secretless, recommended in prod) |
| `WEBAPP_MANAGED_IDENTITY_CLIENT_ID` | Client id of the user-assigned managed identity (UAMI) for secretless OBO |
| `WEBAPP_CLIENT_SECRET` | OBO client secret — **local testing only**; omit in production (use managed identity) |
| `WEBAPP_REDIRECT_URI` / `WEBAPP_API_SCOPE` | Interactive local sign-in redirect + API scope (`api://<client-id>/access_as_user`); leave unset in production |
| `WEBAPP_LOCAL_DEV` | `1` to bypass sign-in and run as the local `az login` identity (dev only) |

> In `app` mode you do not need the Entra app registration for OBO; the app reaches
> Databricks as its own managed identity. You still enable sign-in (EasyAuth) so it remains
> a multi-user app.

---

## Run locally

```bash
pip install -e ".[web]"
uvicorn foundry_databricks_agent.webapp:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000>. In interactive mode you sign in with Microsoft Entra ID and
the agent answers; `GET /api/whoami` shows which identity Databricks will see (the signed-in
user in `obo` mode, or the app in `app` mode). For the full Azure Container Apps deployment
(managed identity, EasyAuth, secretless OBO, Unity Catalog grants), see
[`DEPLOYMENT.md`](DEPLOYMENT.md).

> **Browse at the same host as `WEBAPP_REDIRECT_URI`.** The default is
> `http://localhost:8000/auth/callback`, so open **`localhost`** (not `127.0.0.1`). The
> sign-in cookie is scoped to the host you browse, and the OAuth callback returns to the
> redirect URI's host — if they differ, the first sign-in silently fails and you'll be asked
> to sign in twice. Sign-in always shows the account picker (`prompt=select_account`).

### Local sign-in modes (env-driven)

| Mode | When | Enable with |
| --- | --- | --- |
| **Production** | Hosted behind platform auth | Azure Container Apps / App Service **EasyAuth** injects `X-MS-TOKEN-AAD-ACCESS-TOKEN`; in `obo` mode the app does the OBO exchange (secretless via managed identity + federated credential). |
| **Interactive sign-in** | Local browser test of the real per-user flow | Set `WEBAPP_TENANT_ID`, `WEBAPP_CLIENT_ID`, `WEBAPP_CLIENT_SECRET`, `WEBAPP_REDIRECT_URI`, `WEBAPP_API_SCOPE`. Serves `/auth/login`, `/auth/callback`, `/auth/logout` (MSAL auth-code flow). |
| **Local dev bypass** | Quick UI check as yourself | `WEBAPP_LOCAL_DEV=1` — skips sign-in and runs as your `az login` identity. **Never enable in a deployed environment.** |

---

## Authentication

All Databricks authentication is centralized in
[`auth.py`](src/foundry_databricks_agent/auth.py). Identity is **Microsoft Entra ID OAuth
2.0**; there are no Databricks PATs or CLI profiles anywhere.

- **Model (always the app):** `FoundryChatClient` authenticates with `DefaultAzureCredential`
  — managed identity in Azure, or `az login` locally.
- **Databricks (mode-dependent):**
  - `obo` — the signed-in user's Entra token is exchanged **On-Behalf-Of** for an Azure
    Databricks token (`build_on_behalf_of_credential`). The confidential client authenticates
    **secretlessly** with a managed identity (federated identity credential) in production, or
    a client secret for local testing.
  - `app` — the app's own managed identity (`DefaultAzureCredential`) mints the Databricks
    token; no per-user exchange.
- **Token for Databricks** is an Entra access token for the Azure Databricks login app
  (`2ff814a6-3304-4ab8-85cb-cd0e6f879c1d/.default`), sent as `Authorization: Bearer …`, cached
  and refreshed automatically before expiry, request-scoped so per-user credentials are never
  retained process-wide.

The web app **refuses to fall back to the app identity in `obo` mode** when no user token is
present (it returns 401), so per-user governance can't be silently bypassed.

---

## Conversation history and efficiency

Each chat is a Microsoft Agent Framework `AgentSession` keyed by **(signed-in user,
conversation id)**. The browser supplies the conversation id, but the server always scopes it
to the authenticated owner, so an id alone never grants access to a conversation.

| Concern | How it is handled |
| --- | --- |
| Where history lives | `AgentSession` state, saved each turn to Cosmos DB when `COSMOS_ENDPOINT` is set; process memory otherwise |
| Isolation | The Cosmos partition key is the authenticated owner; turns in one conversation are serialized |
| Growth | `ToolResultCompactionStrategy` collapses older tool results so the prompt cannot grow without bound |
| Expiry | A 30-day Cosmos TTL removes idle conversations automatically |
| Reset | `DELETE /api/conversations/{id}`, or the **+** button in the UI |

Two design choices keep both token spend and Databricks compute down:

- **Genie runs as a single tool call.** Genie answers asynchronously (`query_space` →
  `poll_response`). Letting the *model* drive that loop costs a full model call per poll, and a
  failed poll tends to make the model re-ask the question — which re-runs the SQL on the
  warehouse. `ask_genie` performs the loop internally and returns only the answer plus a bounded
  result preview; the generated SQL and result manifests are logged rather than sent to the model.
- **History is kept local, not service-side.** The agent sets `store=False`, because Foundry's
  service-managed history bypasses local history providers and would leave compaction with
  nothing to trim.

> **Gotcha:** the Agent Framework reserves `conversation_id` on MCP tool calls and strips it by
> default, which breaks Genie's polling and causes repeat questions. The managed MCP tools opt
> it back in with `additional_tool_argument_names`; removing that will return `BAD_REQUEST` on
> every poll.

---

## Security

- **No secrets in the app.** Entra ID OAuth only — no PATs, no Databricks CLI profiles, no
  secrets in code, logs, or `Settings`. Prefer **managed identity** (secretless) in production.
- **Identity & least privilege.** Databricks access is governed by **Unity Catalog** under the
  caller's identity. In `obo` mode grant each user (or an Entra group) only the catalogs,
  schemas, functions, and Genie spaces they need; in `app` mode grant those to the app's
  managed identity.
- **TLS enforced.** `DATABRICKS_HOST` and `FOUNDRY_PROJECT_ENDPOINT` must be `https://`; config
  raises otherwise, so a bearer token is never sent in cleartext. Certificate verification is on
  by default and never disabled.
- **Injection-resistant URLs.** URL path segments (`catalog`, `schema`, Genie space id) are
  validated against `[A-Za-z0-9._-]` and reject `..`.
- **Prompt-injection defense.** The agent's system instructions treat tool outputs and
  retrieved data as untrusted content, never as instructions, and never reveal credentials,
  headers, or configuration.
- **Token hygiene.** Entra tokens are short-lived, cached in memory only, refreshed before
  expiry, and never logged. Session tokens are kept in a server-side store, out of cookies.
- **Conversation isolation.** Stored conversations are partitioned by the authenticated owner
  and can only be loaded under that identity, so a conversation id from one user can never
  resolve into another user's history. Cosmos DB is reached with Entra ID only (account keys
  are disabled), and conversations expire automatically via a 30-day TTL.
- **Local hygiene.** `.env` is git-ignored; `.env.example` contains no secrets.

---

## How it maps to the SDKs

| Capability | Code | SDK surface |
| --- | --- | --- |
| Agent (client-side run loop) | `agent.build_local_mcp_agent` | `agent_framework.Agent` + `agent_framework.foundry.FoundryChatClient` |
| UC functions tools | `databricks_mcp.make_uc_functions_local_mcp_tool` | `agent_framework.MCPStreamableHTTPTool` |
| Genie as one tool call | `genie.make_genie_tool` | `agent_framework.FunctionTool` wrapping `MCPStreamableHTTPTool.call_tool` |
| Conversation history | `conversation_store` / `cosmos_store` | `agent_framework.AgentSession` + `InMemoryHistoryProvider` |
| History size control | `agent.build_local_mcp_agent` | `agent_framework.CompactionProvider` + `ToolResultCompactionStrategy` |
| Durable session storage | `cosmos_store.CosmosConversationSessionStore` | `azure.cosmos.aio` + `azure.identity.aio` |
| Model auth (app) | `auth.default_credential` | Microsoft Entra ID OAuth via `azure-identity` (`DefaultAzureCredential`) |
| Databricks token / headers | `auth.databricks_auth_headers` / `auth.token_header_provider` | Entra ID OAuth bearer token (`TokenCredential.get_token`) |
| Per-user On-Behalf-Of | `auth.build_on_behalf_of_credential` | `azure.identity.OnBehalfOfCredential` (secretless via federated credential) |

---

## Notes & troubleshooting

- **Model auth (app):** ensure the app's managed identity (or your `az login` identity
  locally) has a Foundry data-plane role (e.g. **Azure AI User**) on the project; otherwise the
  model call returns **403**.
- **Databricks auth:** in `obo` mode a signed-in user needs Unity Catalog grants and the app
  registration needs delegated `user_impersonation` on Azure Databricks (admin-consented); in
  `app` mode the app's managed identity needs the Unity Catalog grants.
- **Tenant match:** Azure Databricks only accepts Entra tokens issued by its own tenant — the
  workspace and the signing identity must share a tenant (`IncorrectClaimException` otherwise).
- **Genie is asynchronous** (ask → poll). The `ask_genie` tool runs that cycle internally with
  bounded backoff, so the model makes one tool call and never polls.
- **Genie follow-ups** reuse the Genie `conversation_id` stored in the session, so a follow-up
  continues the existing Genie conversation instead of re-running the same analysis.
- **Verify identity:** `GET /api/whoami` returns the non-sensitive claims of the Databricks
  token the tools use, plus `identityMode`, so you can confirm which identity Databricks sees.
