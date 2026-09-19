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
     │ history       │  model turn        │  UC functions + ask_genie + Lakebase
     │ (AgentSession)│  as the APP        │  as the OBO user (obo mode) OR
     │               │  (managed id)      │  as the APP       (app mode)
     ▼               ▼                    ▼
 ┌─────────────┐ ┌────────────────────┐ ┌────────────────────────────────┐
 │ Azure       │ │  Azure AI Foundry  │ │  Azure Databricks               │
 │ Cosmos DB   │ │  • model           │ │  governed per caller identity   │
 │ • sessions  │ └────────────────────┘ │   ├─ /api/2.0/mcp/functions/…   │
 └─────────────┘                        │   ├─ /api/2.0/mcp/genie/…       │
                                        │   └─ Lakebase Postgres :5432    │
                                        └────────────────────────────────┘
```

- The agent is a Microsoft Agent Framework `Agent` backed by `FoundryChatClient`, built by
  [`build_local_mcp_agent`](src/foundry_databricks_agent/agent.py). Its run loop executes in
  the web app process.
- **Unity Catalog functions** are reached as **local MCP-client** tools
  (`MCPStreamableHTTPTool`): the app process connects to the Databricks managed MCP servers
  directly and attaches a Microsoft Entra ID OAuth bearer token, refreshed per call. The
  function list is **discovered per request** from
  `/api/2.0/mcp/functions/{catalog}/{schema}` — nothing is hardcoded. Each function's
  description and parameter docs are its Unity Catalog `COMMENT`s, so how well the agent
  routes to a function is controlled by how well that function is commented. The list is
  filtered by the caller's `USE CATALOG` / `USE SCHEMA` / `EXECUTE` grants, so adding a
  function needs no redeploy and two users can legitimately be offered different ones.
- **Genie** is exposed to the model as a single `ask_genie` tool
  ([`genie.py`](src/foundry_databricks_agent/genie.py)). Genie answers asynchronously, and that
  ask/poll cycle runs *inside* the tool call rather than being driven by the model — see
  [Conversation history and efficiency](#conversation-history-and-efficiency).
- Chat history lives in an `AgentSession` per user and conversation, persisted to **Azure
  Cosmos DB** when `COSMOS_ENDPOINT` is set (in-process memory otherwise).
- **Lakebase** (Databricks OLTP Postgres) is reached as **read-only SQL tools**
  ([`lakebase.py`](src/foundry_databricks_agent/lakebase.py)). Databricks publishes no managed
  MCP server for Lakebase, so the app mints a short-lived Postgres credential from the
  *caller's own* Databricks token — the connection authenticates as the signed-in user and
  Postgres role grants decide what they can read. Because the model has to author raw SQL
  here, it gets two discovery tools first: `list_lakebase_tables` and
  `describe_lakebase_table` (columns, types, nullability, defaults, PK/FK), so it works from
  the real schema instead of guessing column names.
- The **model** always authenticates as the **app's managed identity**
  (`DefaultAzureCredential`). The **Databricks** tools authenticate with the credential the
  request handler passes — the signed-in user (OBO) or the app identity — per the mode.
- All Databricks access is **governed by Unity Catalog** under whichever identity is used.

### Tool routing

Users ask in business terms and are never expected to know which system holds the data, so
they never name Genie, Lakebase or Unity Catalog — the agent decides from the tool
descriptions and the schemas it discovers:

| Question shape | Goes to | Why |
| --- | --- | --- |
| Matches a specific governed function (customer by email, billing, a calculation) | a **Unity Catalog function** | cheapest and most precise |
| Aggregates, breakdowns, trends over time, comparisons | **`ask_genie`** | the semantic model is built for analytics |
| A specific record, a current status, exact filtering or counting | **Lakebase SQL** | row-level and current-state, exact |

Two rules make this robust in practice:

- **Genie questions are phrased in business terms only.** The model must not put system
  names into a Genie question — Genie matches words like "Lakebase" against *column values*,
  so the query silently returns zero rows and the agent reports "no data" for data that
  exists.
- **A single subject can live on more than one surface.** If one tool returns nothing, the
  agent tries the other before telling the user the data is unavailable.

The answer itself never mentions the underlying system unless the user asks how the data was
obtained.

**Starter questions are derived, not hardcoded.** On an empty thread the UI shows example
questions from `GET /api/starters`, where the agent inspects its own tools and schema and
proposes four questions in business language. They therefore match whatever this deployment
can actually answer — and, in `obo` mode, whatever *this user* is permitted to see, so the
result is cached per owner (in `app` mode one generation serves everyone). Generation takes
roughly a minute on a cold cache; the chips appear when ready and never block typing.

### Showing the work

Answers are rendered as Markdown — tables, lists, headings, bold and inline code all display
properly — by a small built-in renderer that creates DOM nodes and assigns `textContent`.
There is no Markdown dependency, deliberately: answers restate untrusted tool output, so a
library driving `innerHTML` would turn a crafted value in a Databricks row into stored XSS.

In `obo` mode, any answer that used tools also carries a collapsible **"How I got this"**
panel showing what was sent and what came back:

```
▾ How I got this · 1 step
  1. Lakebase — ran SQL query
     SENT
       SELECT ticket_id, status, priority FROM … WHERE ticket_id = 1000
     RETURNED
       ticket_id | status | priority
       1000      | Resolved | Low
```

Genie steps show the question that was asked — useful, because the agent *rephrases* your
question and that rewrite is a common source of a wrong answer. Unity Catalog steps show the
call signature, e.g. `calculate_math_expression(expression="17 * 23 + 5")`. Unlike the answer
text, this panel **does** name the systems: it exists to be audited, so being concrete is the
point.

How it stays readable:

- **Schema discovery is omitted.** `list_lakebase_tables` and `describe_lakebase_table` still
  *run* — they are what stop the model guessing column names — but they are scaffolding, not
  the answer. They are hidden unless nothing else ran, so a pure "what tables are there?"
  question still shows its work. Traces always record them in full.
- **Pipe-delimited results become real tables** that scroll sideways. Left in a `<pre>`, a
  15-column row wraps and the values no longer line up under their headers, which defeats the
  point of the panel.
- **Databricks `{columns, rows}` payloads are rendered as tables too**, rather than raw JSON,
  and the duplicate copy the MCP layer returns is collapsed.

Two constraints on it:

- **`obo` mode only.** The panel replays queries and result rows, which is safe when the
  caller is the identity that fetched them. In `app` mode everyone shares the app's
  permissions, so `_visible_steps` returns nothing and the data never leaves the server —
  suppressing it in the browser would still have shipped it over the wire.
- **Rendered with `textContent`, never `innerHTML`.** Query text is capped at 800 characters
  and results at 900; a truncated result says so, because a cut-off table otherwise reads as
  the complete answer.

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
├── evals/                         # offline agent evaluation (Azure AI Evaluation SDK)
│   ├── dataset.jsonl              # test queries
│   ├── run_agent.py               # runs the real agent, records responses + tool calls
│   └── evaluate.py                # scores those responses with built-in evaluators
├── tests/                         # unit tests (no Azure or Databricks needed)
└── src/foundry_databricks_agent/
    ├── config.py                 # env-based settings + managed-MCP URL builders
    ├── auth.py                   # centralized Entra ID OAuth (tokens + OBO credential)
    ├── databricks_mcp.py         # local MCP-client tool factories (UC functions, Genie)
    ├── genie.py                  # single-call ask_genie tool (runs Genie's ask/poll cycle)
    ├── lakebase.py               # read-only SQL tools over Lakebase (OLTP Postgres)
    ├── agent.py                  # builds the Agent Framework agent + tools + history
    ├── observability.py          # OpenTelemetry tracing of agent, model and tool calls
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
  - (optional) a **Genie space**,
  - (optional) a **Lakebase** database instance, with a Postgres role for each identity that
    should query it.
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

#    Add the optional extras as needed:
#      tracing -> export agent/model/tool spans (Application Insights or OTLP)
#      evals   -> offline scoring with the Azure AI Evaluation SDK
# pip install -e ".[web,tracing,evals]"

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
| `DATABRICKS_LAKEBASE_ENDPOINT` | *(optional)* Lakebase endpoint resource name `projects/<p>/branches/<b>/endpoints/<e>` (enables the Lakebase tools) |
| `DATABRICKS_LAKEBASE_HOST` | *(optional)* Lakebase Postgres hostname (bare host, no scheme) |
| `DATABRICKS_LAKEBASE_DATABASE` / `DATABRICKS_LAKEBASE_PORT` | Defaults `databricks_postgres` / `5432` |
| `WEBAPP_DATABRICKS_IDENTITY` | `obo` (default) or `app` — the Databricks data-access identity |
| `COSMOS_ENDPOINT` | *(optional)* Cosmos DB account endpoint for durable conversation history. Unset = in-memory (lost on restart) |
| `COSMOS_DATABASE` / `COSMOS_CONTAINER` | Cosmos database / container names (default `agent` / `conversations`) |
| `APPLICATIONINSIGHTS_CONNECTION_STRING` | *(optional)* Send traces to Application Insights (set automatically in Azure by the Bicep deployment) |
| `OTEL_EXPORTER_OTLP_ENDPOINT` / `VS_CODE_EXTENSION_PORT` | *(optional)* Alternative trace destinations — an OTLP collector, or the Foundry Toolkit trace viewer |
| `ENABLE_SENSITIVE_DATA` | *(optional)* `1` records prompts, completions and tool arguments on spans. **Development only** |

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
pip install -e ".[web,tracing]"
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

### HTTP endpoints

| Endpoint | Auth | Purpose |
| --- | --- | --- |
| `GET /` | anonymous | The chat UI |
| `GET /healthz` | anonymous | Liveness/readiness probe |
| `POST /api/chat` | required | Run one turn. Returns `answer`, `conversation_id` and `steps` (the provenance trail; empty in `app` mode) |
| `GET /api/starters` | required | Example questions derived from the live tools and schema. Returns an empty list when signed out rather than failing |
| `GET /api/me` | anonymous | Sign-in state, display name and identity mode, for the UI |
| `GET /api/whoami` | required | Non-sensitive claims of the Databricks token the tools use |
| `DELETE /api/conversations/{id}` | required | Drop one conversation's history |
| `GET /auth/login` · `/auth/callback` · `/auth/logout` | anonymous | Interactive local sign-in only (unset in production) |

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

## Tracing

The Agent Framework instruments itself with OpenTelemetry, so once a destination is
configured every **agent run**, **model call** and **tool call** (Unity Catalog functions,
Genie, Lakebase) emits GenAI-semantic-convention spans and metrics — token usage, latency,
which tool was chosen, and where a turn failed. `observability.py` only picks the
destination, since OpenTelemetry allows one provider per signal:

| Priority | Set this | Traces go to |
| --- | --- | --- |
| 1 | `APPLICATIONINSIGHTS_CONNECTION_STRING` | Application Insights (also instruments FastAPI + outbound HTTP) |
| 2 | `OTEL_EXPORTER_OTLP_ENDPOINT` | Any OTLP collector (Aspire dashboard, Jaeger, …) |
| 3 | `VS_CODE_EXTENSION_PORT=4317` | The Foundry Toolkit trace viewer in VS Code |
| 4 | `ENABLE_CONSOLE_EXPORTERS=1` | stdout |

```bash
pip install -e ".[web,tracing]"
```

The deployed Container App already receives `APPLICATIONINSIGHTS_CONNECTION_STRING` from the
Bicep deployment, so tracing is on as soon as the tracing extra is installed (the Dockerfile
installs it). To trace a local run in VS Code, run the **AI Toolkit: Open Tracing** command
first to start the local collector, then set `VS_CODE_EXTENSION_PORT=4317`.

Each `/api/chat` request opens a `chat_turn` span carrying `gen_ai.conversation.id` and a
hashed `enduser.pseudo.id`, and the framework's spans nest under it, so a trace ties back to a
conversation without putting the user's Entra object id into telemetry:

```
chat_turn                                  gen_ai.conversation.id, enduser.pseudo.id
├─ initialize / tools/list                 MCP handshake with Databricks
└─ invoke_agent <agent name>
   ├─ chat <model>                         one per model turn, with token usage
   ├─ execute_tool list_lakebase_tables
   ├─ execute_tool describe_lakebase_table
   └─ chat <model>
```

The span is created by the app rather than relying on an HTTP server span, because FastAPI is
only auto-instrumented on the Application Insights path — without it the correlation would
vanish whenever traces go to an OTLP collector or the console.

> **Sensitive data is off by default.** Prompts, completions and tool arguments carry
> governed Databricks data, so they are only recorded when `ENABLE_SENSITIVE_DATA=1`. Turn it
> on for local debugging, not in production unless the telemetry store is cleared for that data.

---

## Evaluation

`evals/` scores the agent offline with the [Azure AI Evaluation SDK](https://learn.microsoft.com/azure/ai-foundry/concepts/observability),
in two steps: run the real agent to collect responses, then judge them.

```bash
pip install -e ".[web,tracing,evals]"

# 1. Run every query in evals/dataset.jsonl through the real agent
#    (real Foundry model, real Databricks tools) -> evals/output/responses.jsonl
python evals/run_agent.py

# 2. Score those responses -> evals/output/results.json
python evals/evaluate.py
```

`run_agent.py` builds a fresh agent per query — the same way the web app builds one per
request — and records `query`, `response`, `tool_calls` and `tool_definitions`, which is
exactly what the evaluators consume.

`evaluate.py` runs these built-in evaluators in a single `evaluate()` call, which handles
batching and aggregation:

| Evaluator | Answers |
| --- | --- |
| `IntentResolutionEvaluator` | Did the agent understand and resolve what was actually asked? |
| `TaskAdherenceEvaluator` | Did it follow its instructions — ground answers in tools, refuse when nothing fits? |
| `ToolCallAccuracyEvaluator` | Did it pick the right Databricks tool, with the right arguments? |
| `RelevanceEvaluator` | Does the answer address the question? |
| `CoherenceEvaluator` / `FluencyEvaluator` | Is the answer well-formed? |

The prompt-based evaluators use an LLM as judge, configured with `AZURE_OPENAI_ENDPOINT` +
`AZURE_OPENAI_DEPLOYMENT` (and optionally `AZURE_OPENAI_API_KEY`; without it,
`DefaultAzureCredential` is used and the identity needs **Cognitive Services OpenAI User**).
This must be an **Azure OpenAI** endpoint (`https://<resource>.openai.azure.com/`), not the
Foundry project endpoint.

> **Reasoning judges need a flag.** Reasoning deployments (`gpt-5*`, `o1`, `o3`, `o4`) reject
> `max_tokens`, which the evaluation SDK sends by default — every prompt-based evaluator then
> fails with `Unsupported parameter: 'max_tokens' is not supported with this model`.
> `evaluate.py` detects this from the deployment name and passes `is_reasoning_model=True` so
> the SDK sends `max_completion_tokens` instead. If your deployment has a custom name that
> doesn't start with `gpt-5`/`o1`/`o3`/`o4`, set `AZURE_OPENAI_IS_REASONING_MODEL=1`
> explicitly (or `0` to force it off).

Edit `evals/dataset.jsonl` to match your catalog, Genie space and Lakebase tables — the
shipped queries are placeholders shaped around the customer-support demo data. Rows may
include an optional `ground_truth` field, which is carried through for similarity-style
evaluators.

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
- **Lakebase is read-only.** Queries run in a Postgres read-only transaction with a statement
  timeout and a row cap, and the tool rejects anything that is not a single `SELECT`/`WITH`
  statement — so a model-generated write or multi-statement payload cannot modify data.
  `describe_lakebase_table` binds the schema and table as **query parameters**, so
  model-supplied names reach Postgres as values and are never concatenated into SQL.
- **Telemetry carries no prompts by default.** Traces record span names, latency, token counts
  and which tool ran; prompts, completions and tool arguments are only captured when
  `ENABLE_SENSITIVE_DATA=1`. Chat spans carry a hashed `enduser.pseudo.id` rather than the
  user's Entra object id, and evaluation output (`evals/output/`) is git-ignored because it
  contains real Databricks data.
- **Provenance is scoped to the identity that fetched the data.** The "How I got this" panel
  replays queries and result rows, so it is served only in `obo` mode, where those rows are
  already the caller's to see. In `app` mode the server omits it rather than hiding it in the
  browser, so the data never crosses the wire. Tool output is rendered with `textContent`,
  never as HTML, so a crafted value in a table cannot become stored XSS.
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
| Lakebase read-only SQL | `lakebase.make_lakebase_tools` | `agent_framework.FunctionTool` + `psycopg` over `POST /api/2.0/postgres/credentials` |
| Conversation history | `conversation_store` / `cosmos_store` | `agent_framework.AgentSession` + `InMemoryHistoryProvider` |
| History size control | `agent.build_local_mcp_agent` | `agent_framework.CompactionProvider` + `ToolResultCompactionStrategy` |
| Durable session storage | `cosmos_store.CosmosConversationSessionStore` | `azure.cosmos.aio` + `azure.identity.aio` |
| Tracing | `observability.configure_observability` | `agent_framework.observability` + `azure-monitor-opentelemetry` or OTLP exporters |
| Evaluation | `evals/run_agent.py` + `evals/evaluate.py` | `azure.ai.evaluation.evaluate` + built-in agent evaluators |
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
- **No traces showing up?** Check the startup log for `Tracing enabled -> …`. If it instead
  says tracing is disabled, no destination is configured; if the line is missing entirely, the
  `tracing` extra is not installed (`pip install -e ".[web,tracing]"`). Application Insights
  ingestion lags a minute or two.
- **Lakebase "column does not exist":** the model guessed a name. `describe_lakebase_table`
  exists to prevent this — confirm it is being called first (the trace shows the tool sequence),
  and that the signed-in user's Postgres role can read `information_schema`.
- **No "How I got this" panel?** It is suppressed in `app` mode by design (see
  [Showing the work](#showing-the-work)). In `obo` mode an answer with no panel simply used
  no tools — the model answered from the conversation so far.
- **Panel doesn't list the table/column lookups?** That is deliberate. The agent still runs
  `list_lakebase_tables` and `describe_lakebase_table`; they are just hidden from the panel
  once a real query answers the question. The traces show every call if you need to confirm.
- **A function you just created isn't being used?** Check it is in
  `DATABRICKS_UC_CATALOG.DATABRICKS_UC_SCHEMA`, that the calling identity has `EXECUTE` on
  it, and that it has a `COMMENT` — an undocumented function gives the model nothing to route
  on.
- **Starter chips slow to appear?** They are generated by a real agent turn that inspects the
  schema, so the first request per user takes about a minute; afterwards they are cached for
  the process lifetime. A restart clears the cache.
- **Genie returned nothing for data you know exists?** Open the panel and read the question
  the agent actually sent. Genie matches system names like "Lakebase" against column values,
  so a leaked tool name in the question yields zero rows.
