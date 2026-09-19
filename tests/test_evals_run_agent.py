from __future__ import annotations

import importlib.util
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

_RUN_AGENT = Path(__file__).resolve().parent.parent / "evals" / "run_agent.py"
_spec = importlib.util.spec_from_file_location("evals_run_agent", _RUN_AGENT)
assert _spec and _spec.loader
run_agent = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = run_agent
_spec.loader.exec_module(run_agent)


class _Tool:
    def __init__(self, name: str) -> None:
        self._name = name

    def to_json_schema_spec(self) -> dict[str, object]:
        return {
            "type": "function",
            "function": {
                "name": self._name,
                "description": f"{self._name} description",
                "parameters": {"type": "object", "properties": {}},
            },
        }


def _call(name: str, arguments: object, call_id: str = "call-1") -> SimpleNamespace:
    return SimpleNamespace(type="function_call", name=name, arguments=arguments, call_id=call_id)


class ToolDefinitionTests(unittest.TestCase):
    def test_flattens_plain_and_mcp_tools(self) -> None:
        agent = SimpleNamespace(
            default_options={"tools": [_Tool("ask_genie")]},
            mcp_tools=[SimpleNamespace(functions=[_Tool("uc_lookup")])],
        )

        definitions = run_agent.tool_definitions(agent)

        self.assertEqual(["ask_genie", "uc_lookup"], [d["name"] for d in definitions])
        self.assertEqual({"name", "description", "parameters"}, set(definitions[0]))

    def test_handles_agent_without_tools(self) -> None:
        agent = SimpleNamespace(default_options={}, mcp_tools=[])
        self.assertEqual([], run_agent.tool_definitions(agent))


class ToolCallTests(unittest.TestCase):
    def test_extracts_calls_and_parses_json_arguments(self) -> None:
        messages = [
            SimpleNamespace(contents=[SimpleNamespace(type="text", text="thinking")]),
            SimpleNamespace(
                contents=[
                    _call("ask_genie", json.dumps({"question": "how many?"}), "call-a"),
                    _call("query_lakebase", {"sql": "select 1"}, "call-b"),
                ]
            ),
            SimpleNamespace(contents=[SimpleNamespace(type="function_result", result="42")]),
        ]

        calls = run_agent.tool_calls(messages)

        self.assertEqual(
            [
                {
                    "type": "tool_call",
                    "tool_call_id": "call-a",
                    "name": "ask_genie",
                    "arguments": {"question": "how many?"},
                },
                {
                    "type": "tool_call",
                    "tool_call_id": "call-b",
                    "name": "query_lakebase",
                    "arguments": {"sql": "select 1"},
                },
            ],
            calls,
        )

    def test_keeps_unparsable_arguments_verbatim(self) -> None:
        messages = [SimpleNamespace(contents=[_call("ask_genie", "not json")])]
        self.assertEqual("not json", run_agent.tool_calls(messages)[0]["arguments"])

    def test_ignores_messages_without_contents(self) -> None:
        self.assertEqual([], run_agent.tool_calls([SimpleNamespace(contents=None)]))


if __name__ == "__main__":
    unittest.main()
