"""Client-side agent (backed by an Azure AI Foundry model) for Azure Databricks managed MCP.

This package wires a Microsoft Agent Framework agent whose **run loop executes in your own
process** (for example, a Container App) and whose model runs in Azure AI Foundry via
``FoundryChatClient``. It is **not** a Foundry Agent Service hosted agent. The agent reaches
Databricks **managed MCP** servers for:

* Unity Catalog functions
* Genie spaces
* a deployed Databricks (Mosaic AI) agent, invoked as a tool
"""

from .config import Settings, load_settings

__all__ = ["Settings", "load_settings"]
__version__ = "0.1.0"
