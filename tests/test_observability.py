from __future__ import annotations

import unittest
from unittest import mock

from foundry_databricks_agent import observability
from foundry_databricks_agent.config import ConfigError, Settings, load_settings

_BASE_ENV = {
    "FOUNDRY_PROJECT_ENDPOINT": "https://example.services.ai.azure.com/api/projects/p",
    "FOUNDRY_MODEL": "gpt-4o-mini",
}


def _settings(**overrides: object) -> Settings:
    base = {
        "foundry_project_endpoint": _BASE_ENV["FOUNDRY_PROJECT_ENDPOINT"],
        "foundry_model": _BASE_ENV["FOUNDRY_MODEL"],
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


class TracingSettingsTests(unittest.TestCase):
    def test_tracing_disabled_without_a_destination(self) -> None:
        self.assertFalse(_settings().tracing_enabled)

    def test_any_destination_enables_tracing(self) -> None:
        self.assertTrue(
            _settings(applicationinsights_connection_string="InstrumentationKey=k").tracing_enabled
        )
        self.assertTrue(_settings(otlp_endpoint="http://localhost:4317").tracing_enabled)
        self.assertTrue(_settings(vs_code_extension_port=4317).tracing_enabled)
        self.assertTrue(_settings(enable_console_telemetry=True).tracing_enabled)

    def test_loads_telemetry_settings_from_environment(self) -> None:
        env = {
            **_BASE_ENV,
            "APPLICATIONINSIGHTS_CONNECTION_STRING": "InstrumentationKey=k",
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
            "VS_CODE_EXTENSION_PORT": "4317",
            "OTEL_SERVICE_NAME": "custom-service",
            "ENABLE_SENSITIVE_DATA": "true",
        }
        with mock.patch.dict("os.environ", env, clear=True), mock.patch(
            "foundry_databricks_agent.config.load_dotenv", return_value=False
        ):
            settings = load_settings()

        self.assertEqual("InstrumentationKey=k", settings.applicationinsights_connection_string)
        self.assertEqual("http://localhost:4317", settings.otlp_endpoint)
        self.assertEqual(4317, settings.vs_code_extension_port)
        self.assertEqual("custom-service", settings.otel_service_name)
        self.assertTrue(settings.enable_sensitive_telemetry)
        self.assertFalse(settings.enable_console_telemetry)

    def test_sensitive_telemetry_defaults_off(self) -> None:
        with mock.patch.dict("os.environ", _BASE_ENV, clear=True), mock.patch(
            "foundry_databricks_agent.config.load_dotenv", return_value=False
        ):
            settings = load_settings()
        self.assertFalse(settings.enable_sensitive_telemetry)
        self.assertFalse(settings.tracing_enabled)

    def test_non_integer_extension_port_is_rejected(self) -> None:
        env = {**_BASE_ENV, "VS_CODE_EXTENSION_PORT": "not-a-port"}
        with mock.patch.dict("os.environ", env, clear=True), mock.patch(
            "foundry_databricks_agent.config.load_dotenv", return_value=False
        ):
            with self.assertRaises(ConfigError):
                load_settings()


class ConfigureObservabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        observability._configured = None
        self.addCleanup(setattr, observability, "_configured", None)

    def test_no_destination_configures_nothing(self) -> None:
        with mock.patch.object(observability, "_configure_otlp") as otlp:
            self.assertIsNone(observability.configure_observability(_settings()))
        otlp.assert_not_called()

    def test_application_insights_wins_over_otlp(self) -> None:
        settings = _settings(
            applicationinsights_connection_string="InstrumentationKey=k",
            otlp_endpoint="http://localhost:4317",
        )
        with mock.patch.object(
            observability, "_configure_azure_monitor", return_value="Azure Application Insights"
        ) as monitor, mock.patch.object(observability, "_configure_otlp") as otlp:
            destination = observability.configure_observability(settings)

        self.assertEqual("Azure Application Insights", destination)
        monitor.assert_called_once()
        otlp.assert_not_called()

    def test_configures_only_once(self) -> None:
        settings = _settings(otlp_endpoint="http://localhost:4317")
        with mock.patch.object(
            observability, "_configure_otlp", return_value="OTLP endpoint"
        ) as otlp:
            observability.configure_observability(settings)
            observability.configure_observability(settings)

        otlp.assert_called_once()


if __name__ == "__main__":
    unittest.main()
