from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from foundry_databricks_agent import webapp
from foundry_databricks_agent.webapp import (
    _result_text,
    _step_detail,
    _tool_label,
    _turn_steps,
    _visible_steps,
)


def _call(call_id: str, name: str, arguments: object) -> SimpleNamespace:
    return SimpleNamespace(
        type="function_call", call_id=call_id, name=name, arguments=arguments
    )


def _result(call_id: str, value: object) -> SimpleNamespace:
    return SimpleNamespace(type="function_result", call_id=call_id, result=value)


class ToolLabelTests(unittest.TestCase):
    def test_names_the_system_for_known_tools(self) -> None:
        self.assertEqual("Lakebase — ran SQL query", _tool_label("query_lakebase"))
        self.assertEqual(
            "Lakebase — listed available tables", _tool_label("list_lakebase_tables")
        )
        self.assertEqual("Genie — asked a question", _tool_label("ask_genie"))

    def test_names_unity_catalog_functions(self) -> None:
        self.assertEqual(
            "Called UC function get_customer_by_email",
            _tool_label("adbwscj1__ai_agent__get_customer_by_email"),
        )

    def test_falls_back_for_unknown_tools(self) -> None:
        self.assertEqual("Called something", _tool_label("something"))
        self.assertEqual("Called a tool", _tool_label(""))


class StepDetailTests(unittest.TestCase):
    def test_marks_sql_so_the_ui_shows_it_as_code(self) -> None:
        self.assertEqual(("sql", "select 1"), _step_detail("query_lakebase", '{"sql": "select 1"}'))

    def test_marks_a_genie_question_as_prose(self) -> None:
        self.assertEqual(
            ("question", "how many?"), _step_detail("ask_genie", {"question": "how many?"})
        )

    def test_renders_qualified_table_names(self) -> None:
        self.assertEqual(
            ("code", "sales.tickets"),
            _step_detail(
                "describe_lakebase_table",
                {"table_schema": "sales", "table_name": "tickets"},
            ),
        )

    def test_renders_uc_functions_as_a_call_signature(self) -> None:
        self.assertEqual(
            ("code", 'get_customer_by_email(email="a@b.com")'),
            _step_detail(
                "adbwscj1__ai_agent__get_customer_by_email", {"email": "a@b.com"}
            ),
        )

    def test_handles_missing_arguments(self) -> None:
        self.assertEqual(("text", ""), _step_detail("list_lakebase_tables", None))
        self.assertEqual(("text", ""), _step_detail("list_lakebase_tables", {}))


class TurnStepsTests(unittest.TestCase):
    def test_pairs_calls_with_results_in_order(self) -> None:
        messages = [
            SimpleNamespace(contents=[_call("a", "ask_genie", {"question": "how many?"})]),
            SimpleNamespace(contents=[_result("a", "a lot")]),
            SimpleNamespace(contents=[_call("b", "query_lakebase", {"sql": "select 1"})]),
            SimpleNamespace(contents=[_result("b", "1")]),
            SimpleNamespace(contents=[SimpleNamespace(type="text", text="done")]),
        ]

        steps = _turn_steps(messages)

        self.assertEqual(
            ["Genie — asked a question", "Lakebase — ran SQL query"],
            [s["label"] for s in steps],
        )
        self.assertEqual("select 1", steps[1]["detail"])
        self.assertEqual("sql", steps[1]["kind"])
        self.assertEqual("1", steps[1]["result"])

    def test_marks_a_truncated_result(self) -> None:
        long_result = "x" * 2000
        messages = [
            SimpleNamespace(contents=[_call("a", "query_lakebase", {"sql": "select 1"})]),
            SimpleNamespace(contents=[_result("a", long_result)]),
        ]
        self.assertIn("result truncated", _turn_steps(messages)[0]["result"])

    def test_hides_schema_discovery_when_a_query_ran(self) -> None:
        messages = [
            SimpleNamespace(contents=[_call("a", "list_lakebase_tables", None)]),
            SimpleNamespace(contents=[_result("a", "schema | table")]),
            SimpleNamespace(
                contents=[_call("b", "describe_lakebase_table", {"table_name": "t"})]
            ),
            SimpleNamespace(contents=[_result("b", "col | type")]),
            SimpleNamespace(contents=[_call("c", "query_lakebase", {"sql": "select 1"})]),
            SimpleNamespace(contents=[_result("c", "1")]),
        ]

        steps = _turn_steps(messages)

        self.assertEqual(["Lakebase — ran SQL query"], [s["label"] for s in steps])
        self.assertEqual("select 1", steps[0]["detail"])

    def test_keeps_discovery_when_it_is_the_whole_answer(self) -> None:
        messages = [
            SimpleNamespace(contents=[_call("a", "list_lakebase_tables", None)]),
            SimpleNamespace(contents=[_result("a", "schema | table")]),
        ]
        self.assertEqual(
            ["Lakebase — listed available tables"],
            [s["label"] for s in _turn_steps(messages)],
        )

    def test_leaves_other_tools_untouched(self) -> None:
        messages = [
            SimpleNamespace(contents=[_call("a", "ask_genie", {"question": "q"})]),
            SimpleNamespace(contents=[_result("a", "answer")]),
        ]
        self.assertEqual(1, len(_turn_steps(messages)))

    def test_keeps_a_call_that_never_returned(self) -> None:
        messages = [SimpleNamespace(contents=[_call("a", "ask_genie", {"question": "q"})])]
        steps = _turn_steps(messages)
        self.assertEqual(1, len(steps))
        self.assertEqual("", steps[0]["result"])

    def test_no_tool_calls_yields_no_steps(self) -> None:
        self.assertEqual([], _turn_steps([SimpleNamespace(contents=None)]))


class ResultTextTests(unittest.TestCase):
    RAW = '{"is_truncated":false,"columns":["output"],"rows":[[396.0]]}'
    RESERIALISED = '{"is_truncated": false, "columns": ["output"], "rows": [[396.0]]}'

    def test_renders_a_columns_rows_payload_as_a_table(self) -> None:
        self.assertEqual("output\n396.0", _result_text(self.RAW))

    def test_collapses_a_reserialised_json_duplicate(self) -> None:
        self.assertEqual("output\n396.0", _result_text(f"{self.RAW}\n{self.RESERIALISED}"))

    def test_collapses_duplicates_across_list_items(self) -> None:
        self.assertEqual("output\n396.0", _result_text([self.RAW, self.RESERIALISED]))

    def test_renders_multiple_columns_and_rows(self) -> None:
        payload = '{"columns": ["a", "b"], "rows": [[1, null], [2, "x"]]}'
        self.assertEqual("a | b\n1 | \n2 | x", _result_text(payload))

    def test_flags_truncated_results(self) -> None:
        payload = '{"is_truncated": true, "columns": ["a"], "rows": [[1]]}'
        self.assertIn("result truncated", _result_text(payload))

    def test_pretty_prints_json_that_is_not_a_result_set(self) -> None:
        self.assertEqual('{\n  "a": 1\n}', _result_text('{"a":1}'))

    def test_never_collapses_identical_table_rows(self) -> None:
        rows = "status | n\nOpen | 5\nOpen | 5"
        self.assertEqual(rows, _result_text(rows))

    def test_passes_through_plain_values(self) -> None:
        self.assertEqual("plain", _result_text("plain"))
        self.assertEqual("", _result_text(None))


class VisibleStepsTests(unittest.TestCase):
    MESSAGES = [
        SimpleNamespace(contents=[_call("a", "query_lakebase", {"sql": "select 1"})]),
        SimpleNamespace(contents=[_result("a", "1")]),
    ]

    def test_per_user_mode_exposes_the_steps(self) -> None:
        with mock.patch.object(webapp, "_SHARED_IDENTITY", False):
            steps = _visible_steps(self.MESSAGES)
        self.assertEqual(["Lakebase — ran SQL query"], [s["label"] for s in steps])

    def test_shared_app_identity_withholds_the_steps(self) -> None:
        with mock.patch.object(webapp, "_SHARED_IDENTITY", True):
            self.assertEqual([], _visible_steps(self.MESSAGES))


if __name__ == "__main__":
    unittest.main()
