"""OpenTelemetry tracing for the agent.

The Microsoft Agent Framework instruments itself: once OpenTelemetry providers are
configured, every agent run, model call and tool call (Unity Catalog functions, Genie,
Lakebase) emits GenAI-semantic-convention spans and metrics. This module only decides
*where* that telemetry goes, because OpenTelemetry allows exactly one provider per signal:

1. **Azure Application Insights** — when ``APPLICATIONINSIGHTS_CONNECTION_STRING`` is set
   (the Container App gets it from Bicep). Uses the Azure Monitor distro, which also
   instruments FastAPI and outbound HTTP so a trace spans the whole request.
2. **An OTLP collector** — when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set.
3. **Foundry Toolkit in VS Code** — when ``VS_CODE_EXTENSION_PORT`` is set (4317). Run the
   ``AI Toolkit: Open Tracing`` / ``ai-mlstudio.tracing.open`` command first to start the
   local collector.
4. **Console** — when ``ENABLE_CONSOLE_EXPORTERS=1``.

Prompts, completions and tool arguments are only recorded when ``ENABLE_SENSITIVE_DATA=1``,
since those payloads carry governed Databricks data. Keep it off in production unless the
telemetry store is cleared for that data.
"""

from __future__ import annotations

import logging
from typing import Any

from .config import Settings, load_settings

logger = logging.getLogger(__name__)

_configured: str | None = None


def configure_observability(settings: Settings | None = None) -> str | None:
    """Install OpenTelemetry providers once and return the destination that was used.

    Returns ``None`` when no telemetry destination is configured. Safe to call more than
    once: later calls are no-ops, because configuring providers twice duplicates spans.
    """
    global _configured

    if _configured is not None:
        return _configured

    settings = settings or load_settings()
    if not settings.tracing_enabled:
        logger.info(
            "Tracing is disabled. Set APPLICATIONINSIGHTS_CONNECTION_STRING, "
            "OTEL_EXPORTER_OTLP_ENDPOINT or VS_CODE_EXTENSION_PORT to enable it."
        )
        return None

    if settings.applicationinsights_connection_string:
        destination = _configure_azure_monitor(settings)
    else:
        destination = _configure_otlp(settings)

    if destination is not None:
        _configured = destination
        logger.info(
            "Tracing enabled -> %s (sensitive data: %s)",
            destination,
            "on" if settings.enable_sensitive_telemetry else "off",
        )
    return destination


def _configure_azure_monitor(settings: Settings) -> str | None:
    """Export Agent Framework telemetry to Application Insights."""
    try:
        from azure.monitor.opentelemetry import configure_azure_monitor
    except ImportError:
        logger.warning(
            "APPLICATIONINSIGHTS_CONNECTION_STRING is set but azure-monitor-opentelemetry "
            "is not installed. Install the 'tracing' extra to export traces."
        )
        return None

    from agent_framework.observability import enable_instrumentation

    configure_azure_monitor(
        connection_string=settings.applicationinsights_connection_string,
        resource=_resource(settings),
    )
    # Azure Monitor owns the providers; this only turns on Agent Framework's own spans.
    enable_instrumentation(enable_sensitive_data=settings.enable_sensitive_telemetry)
    return "Azure Application Insights"


def _configure_otlp(settings: Settings) -> str:
    """Export Agent Framework telemetry over OTLP (collector, Foundry Toolkit, or console)."""
    from agent_framework.observability import configure_otel_providers

    configure_otel_providers(
        enable_sensitive_data=settings.enable_sensitive_telemetry,
        enable_console_exporters=settings.enable_console_telemetry or None,
        vs_code_extension_port=settings.vs_code_extension_port,
    )
    if settings.otlp_endpoint:
        return f"OTLP endpoint {settings.otlp_endpoint}"
    if settings.vs_code_extension_port:
        return f"Foundry Toolkit on port {settings.vs_code_extension_port}"
    return "console"


def _resource(settings: Settings) -> Any:
    """Name the telemetry resource so traces are attributable to this service."""
    from opentelemetry.sdk.resources import SERVICE_NAME, Resource

    return Resource.create({SERVICE_NAME: settings.otel_service_name})
