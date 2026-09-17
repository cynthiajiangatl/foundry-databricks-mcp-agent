# Deploying the web app to Azure

This guide deploys the Foundry + Databricks agent as a **multi-user web app** on Azure. It
supports **two identity modes** for Databricks, chosen by `WEBAPP_DATABRICKS_IDENTITY`:

- **`obo` (default)** — each signed-in user reaches Databricks as **their own** Entra
  identity (per-user Unity Catalog governance).
- **`app`** — every signed-in user reaches Databricks as the **app's managed identity**
  (all users share the same Databricks data permissions).

In **both** modes the **model** call to Azure AI Foundry runs as the **app's managed
identity**, and users sign in with Microsoft Entra ID. It targets **Azure Container Apps**
(the recommended host) and also notes App Service / AKS.

| | Local dev | Azure |
| --- | --- | --- |
| User sign-in | interactive MSAL or `WEBAPP_LOCAL_DEV` | **Entra ID** via Container Apps built-in auth (EasyAuth) |
| Model identity | your `az login` | the app's **managed identity** |
| Databricks identity (`obo`) | your `az login` / OBO | the **signed-in user** (On-Behalf-Of) |
| Databricks identity (`app`) | your `az login` | the app's **managed identity** |
| Config | `.env` file | environment variables on the service |

The application code does **not** change between modes: it authenticates with
`DefaultAzureCredential` ([`auth.py`](src/foundry_databricks_agent/auth.py)) and selects the
Databricks identity from `WEBAPP_DATABRICKS_IDENTITY`.

---

## 0. Two ways to deploy

**Infrastructure as code (recommended).** [`infra/main.bicep`](infra/main.bicep) provisions the
whole stack — managed identity, ACR, Key Vault, Log Analytics + Application Insights, **Cosmos
DB with its data-plane role assignment**, the Container Apps environment, and the container app
with EasyAuth and sticky sessions — and wires every environment variable:

```powershell
az deployment sub create `
  --location $LOCATION `
  --template-file infra/main.bicep `
  --parameters infra/main.parameters.json
```

Deploy once to create the infrastructure, build and push the image (§6), then redeploy passing
`containerImage=<acr>.azurecr.io/<app>:latest` to roll it out.

**Step by step with the CLI.** The rest of this guide. Use it to understand each piece, or to
adapt the solution to an existing estate.

---

## 1. Prerequisites

- An **Azure subscription** with permission to create resources and assign roles.
- An **Azure AI Foundry** project + deployed chat model (note the project endpoint and model
  deployment name).
- An **Azure Databricks** workspace in the **same Microsoft Entra tenant** as the identities
  used (Azure Databricks only accepts Entra tokens issued by its own tenant).
- **Azure CLI** (`az`) and **Docker** (or rely on `az acr build`).
- The **Databricks CLI** (`databricks`) to grant Unity Catalog access.

Set some shell variables (PowerShell):

```powershell
$RG       = "rg-foundry-databricks-agent"
$LOCATION = "eastus2"
$ACR      = "acrfdagent$((Get-Random))"   # must be globally unique, lowercase
$APP      = "foundry-databricks-agent"
$IDENTITY = "id-foundry-databricks-agent"
$ENVNAME  = "cae-foundry-databricks-agent"
```

---

## 2. What runs where

The container hosts the web UI ([`webapp.py`](src/foundry_databricks_agent/webapp.py)) where
each user signs in with Microsoft Entra ID. Per request:

| Call | Identity | How |
| --- | --- | --- |
| **Model** (Azure AI Foundry) | the **app's managed identity** | `DefaultAzureCredential` |
| **Databricks** — `obo` mode | the **signed-in user** | On-Behalf-Of exchange of the user's token → Azure Databricks token |
| **Databricks** — `app` mode | the **app's managed identity** | `DefaultAzureCredential` → Azure Databricks token |
| **Conversation history** (Cosmos DB) | the **app's managed identity** | `DefaultAzureCredential` → Cosmos **data-plane** RBAC (no account keys) |

The agent is rebuilt per request, so tokens are always fresh. Chat history is loaded from and
saved back to Cosmos DB on each turn, scoped to the signed-in user.

---

## 3. Create the identity and grant access

### 3a. Create a user-assigned managed identity (UAMI)

```powershell
az group create -n $RG -l $LOCATION

az identity create -g $RG -n $IDENTITY
$UAMI_ID       = az identity show -g $RG -n $IDENTITY --query id -o tsv
$UAMI_CLIENTID = az identity show -g $RG -n $IDENTITY --query clientId -o tsv
$UAMI_PRINCIPAL= az identity show -g $RG -n $IDENTITY --query principalId -o tsv
```

### 3b. Grant the managed identity access to Azure AI Foundry (model — both modes)

The app's managed identity runs the **model**. Give it a data-plane role on the Foundry
project (confirm against your project's RBAC):

```powershell
# <FOUNDRY_RESOURCE_ID> = the Azure resource id of your Foundry account/project
az role assignment create `
  --assignee-object-id $UAMI_PRINCIPAL `
  --assignee-principal-type ServicePrincipal `
  --role "Azure AI User" `
  --scope "<FOUNDRY_RESOURCE_ID>"
```

### 3c. Register an Entra app for user sign-in (+ On-Behalf-Of in `obo` mode)

Both modes sign users in with Microsoft Entra ID, so register (or reuse) an Entra **app
registration** for EasyAuth. Record the **client id** and **tenant id** — these become
`WEBAPP_CLIENT_ID` / `WEBAPP_TENANT_ID`.

**Only `obo` mode** additionally uses this app registration for the On-Behalf-Of exchange:

1. **API permissions → Add a permission → APIs my organization uses →** search
   **AzureDatabricks** (`2ff814a6-3304-4ab8-85cb-cd0e6f879c1d`) → **Delegated** →
   **`user_impersonation`** → add, then **grant admin consent**.
2. **Expose an API** (Application ID URI `api://<client-id>` with a scope such as
   `access_as_user`) so the token EasyAuth issues is audienced to this app — required for OBO.

Then make the OBO exchange **secretless** by having the app registration trust the UAMI:

```powershell
$TENANT = az account show --query tenantId -o tsv
$APP_OBJECT_ID = az ad app show --id "<app-registration-client-id>" --query id -o tsv

@{
  name      = "fic-uami"
  issuer    = "https://login.microsoftonline.com/$TENANT/v2.0"
  subject   = $UAMI_PRINCIPAL          # the UAMI's Object (principal) ID from 3a
  audiences = @("api://AzureADTokenExchange")
} | ConvertTo-Json | Out-File credential.json -Encoding utf8

az ad app federated-credential create --id $APP_OBJECT_ID --parameters credential.json
```

- **Subject** must be the UAMI's **Object (principal) ID** exactly, or the exchange fails with
  `AADSTS70021`. Only **user-assigned** managed identities can be used, and the app
  registration + identity must be in the **same tenant**.
- Then set `WEBAPP_USE_MANAGED_IDENTITY=1` and `WEBAPP_MANAGED_IDENTITY_CLIENT_ID=<UAMI client
  id>` on the app (no secret). For local testing you may use a client secret
  (`WEBAPP_CLIENT_SECRET`) instead.

> **`app` mode:** you can skip the delegated `user_impersonation` permission and the
> federated credential — the app reaches Databricks as its own managed identity. You still
> register an Entra app (or reuse one) for **EasyAuth sign-in**.

### 3d. Grant Unity Catalog access (to the right identity for the mode)

**`obo` mode** — grant each **end user** (or an Entra **group**):

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<user-or-group>`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema> TO `<user-or-group>`;
GRANT EXECUTE     ON ALL FUNCTIONS IN SCHEMA <catalog>.<schema> TO `<user-or-group>`;
```

**`app` mode** — grant the **app's managed identity** (add it to the Databricks workspace as a
service principal, then grant it) the same privileges:

```sql
GRANT USE CATALOG ON CATALOG <catalog> TO `<app-managed-identity>`;
GRANT USE SCHEMA  ON SCHEMA  <catalog>.<schema> TO `<app-managed-identity>`;
GRANT EXECUTE     ON ALL FUNCTIONS IN SCHEMA <catalog>.<schema> TO `<app-managed-identity>`;
```

- **Genie space**: grant the same identity **CAN RUN** on the Genie space.
- All identities must be in the **same tenant** as the workspace.

### 3e. Create Cosmos DB for conversation history

Conversation history is stored in Cosmos DB so it survives restarts and is shared across
replicas. The account is created with **keys disabled** — access is Entra ID only.

```powershell
$COSMOS = "cosfdagent$((Get-Random))"   # globally unique, lowercase

az cosmosdb create -g $RG -n $COSMOS `
  --locations regionName=$LOCATION failoverPriority=0 isZoneRedundant=false `
  --capabilities EnableServerless `
  --disable-local-auth true `
  --min-tls-version Tls12

az cosmosdb sql database create -g $RG -a $COSMOS -n agent

# /ownerId is both the tenancy boundary and the partition key; the TTL expires idle chats.
az cosmosdb sql container create -g $RG -a $COSMOS -d agent -n conversations `
  --partition-key-path "/ownerId" --ttl 2592000

# Cosmos data-plane access uses its own RBAC system, not Azure RBAC.
$COSMOS_ID = az cosmosdb show -g $RG -n $COSMOS --query id -o tsv
az cosmosdb sql role assignment create -g $RG -a $COSMOS `
  --role-definition-id "00000000-0000-0000-0000-000000000002" `
  --principal-id $UAMI_PRINCIPAL `
  --scope $COSMOS_ID

$COSMOS_ENDPOINT = az cosmosdb show -g $RG -n $COSMOS --query documentEndpoint -o tsv
```

> Assigning the **Azure RBAC** "Cosmos DB Account Reader" or "Contributor" role does **not**
> grant data access. Only the data-plane role above lets the app read and write documents.
>
> For local development, assign the same role to your own `az login` identity. Otherwise leave
> `COSMOS_ENDPOINT` unset and history stays in process memory.

---

## 4. Network considerations

- The web app connects to the Databricks managed MCP endpoints **directly** (local MCP
  client). Ensure the Container Apps environment's egress can reach the workspace (add its
  outbound IPs to any Databricks **IP access list**).
- `DATABRICKS_HOST` and `FOUNDRY_PROJECT_ENDPOINT` must be HTTPS (enforced by the app).

---

## 5. Containerize

The repo ships a [`Dockerfile`](Dockerfile) that installs the package with the `web` extra and
runs the web UI with uvicorn — no changes needed:

```dockerfile
CMD ["uvicorn", "foundry_databricks_agent.webapp:app", "--host", "0.0.0.0", "--port", "8000"]
```

---

## 6. Build and push the image

```powershell
az acr create -g $RG -n $ACR --sku Basic --admin-enabled false
az acr build -r $ACR -t "$APP`:latest" .
$IMAGE = "$ACR.azurecr.io/$APP`:latest"
```

---

## 7. Deploy to Azure Container Apps

```powershell
az extension add --name containerapp --upgrade
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.OperationalInsights

az containerapp env create -g $RG -n $ENVNAME -l $LOCATION

az containerapp create `
  -g $RG -n $APP `
  --environment $ENVNAME `
  --image $IMAGE `
  --registry-server "$ACR.azurecr.io" `
  --user-assigned $UAMI_ID `
  --registry-identity $UAMI_ID `
  --ingress external --target-port 8000 `
  --min-replicas 1 --max-replicas 3 `
  --env-vars `
    "AZURE_CLIENT_ID=$UAMI_CLIENTID" `
    "FOUNDRY_PROJECT_ENDPOINT=https://<resource>.services.ai.azure.com/api/projects/<project>" `
    "FOUNDRY_MODEL=gpt-4o-mini" `
    "DATABRICKS_HOST=https://adb-xxxxxxxxxxxx.x.azuredatabricks.net" `
    "DATABRICKS_UC_CATALOG=<catalog>" `
    "DATABRICKS_UC_SCHEMA=<schema>" `
    "DATABRICKS_GENIE_SPACE_ID=<genie-space-id>" `
    "WEBAPP_DATABRICKS_IDENTITY=obo" `
    "WEBAPP_TENANT_ID=<tenant-id>" `
    "WEBAPP_CLIENT_ID=<app-registration-client-id>" `
    "WEBAPP_USE_MANAGED_IDENTITY=1" `
    "WEBAPP_MANAGED_IDENTITY_CLIENT_ID=$UAMI_CLIENTID" `
    "COSMOS_ENDPOINT=$COSMOS_ENDPOINT" `
    "COSMOS_DATABASE=agent" `
    "COSMOS_CONTAINER=conversations"

# Keep a user's turns on one replica, which avoids concurrent writes to the same conversation.
az containerapp ingress sticky-sessions set -g $RG -n $APP --affinity sticky
```

Key points:

- **`AZURE_CLIENT_ID`** must be the UAMI's client id so `DefaultAzureCredential` selects the
  right managed identity for the **model** (and, in `app` mode, for Databricks).
- **`WEBAPP_DATABRICKS_IDENTITY`** = `obo` (default) or `app`.
  - For **`app` mode**, set `WEBAPP_DATABRICKS_IDENTITY=app` and you can drop the
    `WEBAPP_USE_MANAGED_IDENTITY` / `WEBAPP_MANAGED_IDENTITY_CLIENT_ID` OBO vars (they are only
    used for the per-user exchange). `WEBAPP_TENANT_ID` / `WEBAPP_CLIENT_ID` remain for EasyAuth.
  - For **`obo` mode** with a client secret instead of the federated credential, drop the two
    managed-identity vars and add `--secrets "webapp-client-secret=<secret>"` plus
    `"WEBAPP_CLIENT_SECRET=secretref:webapp-client-secret"`.
- Grant the UAMI **AcrPull** on the registry so it can pull the image:

  ```powershell
  $ACR_ID = az acr show -n $ACR --query id -o tsv
  az role assignment create --assignee-object-id $UAMI_PRINCIPAL `
    --assignee-principal-type ServicePrincipal --role AcrPull --scope $ACR_ID
  ```

### 7b. Enable Microsoft Entra ID authentication (EasyAuth)

Both modes require sign-in. Turn on the container app's **built-in authentication** so users
sign in with Entra ID and their token is injected as `X-MS-TOKEN-AAD-ACCESS-TOKEN` (and
identity as `X-MS-CLIENT-PRINCIPAL-NAME`). Use the app registration from §3c:

```powershell
az containerapp auth microsoft update -g $RG -n $APP `
  --client-id "<app-registration-client-id>" `
  --client-secret "<app-registration-client-secret>" `
  --tenant-id "<tenant-id>" `
  --yes

# Require authentication for all requests (redirect anonymous users to sign in):
az containerapp auth update -g $RG -n $APP `
  --unauthenticated-client-action RedirectToLoginPage --token-store true
```

> **EasyAuth's login secret** (the `--client-secret` above) is the platform's own sign-in
> secret; store it in **Key Vault** and reference it (`--client-secret-setting-name`). In
> `obo` mode with the federated-credential setup (§3c), the **app's OBO path is secretless**,
> so this EasyAuth login secret is the only one.

> **OBO audience (the #1 `obo`-mode failure point).** For the On-Behalf-Of exchange the
> `X-MS-TOKEN-AAD-ACCESS-TOKEN` EasyAuth injects must be **audienced to this app** (not
> Microsoft Graph). Configure the login to request your app's API scope: add
> `--scopes "api://<client-id>/access_as_user"` to `az containerapp auth microsoft update`. An
> `AADSTS500011` / invalid-audience error means this is missing.

Add the **redirect URI** Azure shows (typically `https://<app-fqdn>/.auth/login/aad/callback`)
to the app registration.

---

## 8. Verify

```powershell
$FQDN = az containerapp show -g $RG -n $APP --query properties.configuration.ingress.fqdn -o tsv
"Open https://$FQDN in a browser and sign in with Entra ID, then ask a question."
Invoke-RestMethod "https://$FQDN/healthz"   # health is anonymous
```

Sign in from the **browser**, ask a question, and use `GET /api/whoami` to confirm the
Databricks identity (`identityMode` = `obo` shows the user; `app` shows the app identity).
Tail logs:

```powershell
az containerapp logs show -g $RG -n $APP --follow
```

Common issues: **tenant mismatch** (`IncorrectClaimException`), in `obo` mode a **missing
`user_impersonation` grant / admin consent** or **missing per-user Unity Catalog grants**, in
`app` mode **missing Unity Catalog grants on the app identity**, or a **missing `Azure AI
User` role** on Foundry for the model.

If chats forget previous turns, check the startup log: `Conversation history: Azure Cosmos DB`
means it is persisting, while `Conversation history is in-memory` means `COSMOS_ENDPOINT` is
unset. A `403` from Cosmos means the **data-plane** role assignment (§3e) is missing.

---

## 9. Other hosts

- **Azure App Service (Linux container)**: enable a managed identity, set the same env vars as
  app settings, set `WEBSITES_PORT=8000`. Same image works.
- **Azure Kubernetes Service (AKS)**: use **Workload Identity Federation** to bind a
  Kubernetes service account to a UAMI; `DefaultAzureCredential` picks it up. Set env vars via
  a ConfigMap.

---

## 10. Security checklist (production)

- **Managed identity everywhere possible** — the model always uses the app's managed identity;
  in `obo` mode the OBO exchange is secretless (federated credential), and in `app` mode
  Databricks also uses the managed identity. The only remaining secret is **EasyAuth's login
  secret** — store it in Key Vault and rotate it.
- **Least privilege** — `Azure AI User` on Foundry for the app; Unity Catalog grants to the
  right identity (per-user in `obo`, the app identity in `app`); in `obo` mode delegated
  `user_impersonation` (not application) on Azure Databricks for the app registration.
- **HTTPS enforced** — `DATABRICKS_HOST` and `FOUNDRY_PROJECT_ENDPOINT` must be `https://`.
- **Governance** — Unity Catalog enforces access as the caller; the app never elevates beyond
  what that identity is granted.
- **Conversation data** — Cosmos DB is created with local auth disabled (Entra ID only),
  partitioned by the authenticated owner so one user's conversation id cannot reach another's
  history, and set to expire idle conversations after 30 days. Treat stored conversations as
  user data subject to your retention and privacy policy.
- **Private networking** — Private Link / VNet integration and IP access lists; ensure egress
  can reach Databricks.
- **Image hygiene** — pin a base image digest, scan the image, rebuild to pick up patches.

---

## 11. Clean up

```powershell
az group delete -n $RG --yes --no-wait
```
