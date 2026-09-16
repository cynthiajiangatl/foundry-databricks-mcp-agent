"""Bridge Azure Databricks **managed MCP** servers to the Microsoft Agent Framework agent.

Databricks hosts ready-to-use, Unity-Catalog-governed MCP servers for Unity Catalog
functions and Genie spaces. They speak MCP over Streamable HTTP. This module exposes them
to the agent as **local MCP-client** tools: :class:`~agent_framework.MCPStreamableHTTPTool`
connects to the managed MCP server from the current process, and the ``header_provider``
attaches (and refreshes) a Microsoft Entra ID OAuth bearer token on every tool call.

The token is **request-scoped**, so the same tool factory serves either identity:

* a per-request On-Behalf-Of credential (the signed-in user), or
* the app's own managed identity (shared).

All authentication is centralized in :mod:`foundry_databricks_agent.auth`.
"""

from __future__ import annotations

from agent_framework import MCPStreamableHTTPTool
from azure.core.credentials import TokenCredential

from .config import Settings


def _make_local_mcp_tool(
    *,
    name: str,
    url: str,
    description: str,
    settings: Settings,
    credential: TokenCredential | None,
) -> MCPStreamableHTTPTool:
    """Connect to a Databricks managed MCP server from the local process.

    Auth is injected two ways for robustness: a custom ``http_client`` carrying the Entra
    OAuth bearer token as a default header (covers the initial handshake and tool
    listing), plus a ``header_provider`` that refreshes the token on each tool call.

    Uses a **request-scoped** token provider (:func:`token_header_provider`) so per-request
    credentials (for example an On-Behalf-Of credential for the signed-in user) are not
    retained in the process-wide cache. The Databricks calls run under whichever
    ``credential`` is passed in — the signed-in user (OBO) or the app's managed identity.
    """
    import httpx

    from .auth import token_header_provider

    headers = token_header_provider(settings, credential)
    # Explicit connect/read/write timeouts guard against hung connections.
    http_client = httpx.AsyncClient(
        headers=headers(),
        timeout=httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0),
    )

    return MCPStreamableHTTPTool(
        name=name,
        url=url,
        description=description,
        http_client=http_client,
        header_provider=lambda _runtime_kwargs: headers(),
    )


def make_uc_functions_local_mcp_tool(
    settings: Settings, *, credential: TokenCredential | None = None
) -> MCPStreamableHTTPTool:
    """Local MCP tool for Unity Catalog functions in the configured catalog.schema."""
    return _make_local_mcp_tool(
        name="databricks_uc_functions",
        url=settings.uc_functions_mcp_url,
        description=(
            f"Run Unity Catalog functions in {settings.uc_catalog}.{settings.uc_schema}."
        ),
        settings=settings,
        credential=credential,
    )


def make_genie_local_mcp_tool(
    settings: Settings, *, credential: TokenCredential | None = None
) -> MCPStreamableHTTPTool:
    """Local MCP tool for a Databricks Genie space."""
    return _make_local_mcp_tool(
        name="databricks_genie",
        url=settings.genie_mcp_url,
        description="Ask natural-language questions against the configured Genie space.",
        settings=settings,
        credential=credential,
    )
