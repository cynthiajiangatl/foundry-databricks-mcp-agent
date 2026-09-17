from __future__ import annotations

import json
import unittest

from foundry_databricks_agent import genie


def _message(status: str, *, answer: str | None = None, rows: bool = False) -> str:
    payload: dict = {
        "content": {"queryAttachments": [], "textAttachments": [], "suggestedQuestions": ["x"]},
        "conversationId": "conv-1",
        "messageId": "msg-1",
        "status": status,
    }
    if answer:
        payload["content"]["textAttachments"] = [answer]
    if rows:
        payload["content"]["queryAttachments"] = [
            {
                "query": "SELECT count(*) FROM t",
                "description": "a description",
                "statement_response": {
                    "statement_id": "stmt-1",
                    "manifest": {
                        "schema": {"columns": [{"name": "row_count"}]},
                        "chunks": [{"byte_count": 328}],
                        "total_row_count": 1,
                    },
                    "result": {"data_array": [{"values": [{"string_value": "24872"}]}]},
                },
            }
        ]
    text = json.dumps(payload)
    # The managed MCP server repeats the same payload in every content item.
    return f"{text}\n{text}"


class _FakeMcp:
    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict]] = []

    async def call_tool(self, tool_name: str, **kwargs: object) -> str:
        self.calls.append((tool_name, dict(kwargs)))
        return self._responses.pop(0)


class GenieToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._delay = genie._INITIAL_POLL_DELAY
        genie._INITIAL_POLL_DELAY = 0.0

    def tearDown(self) -> None:
        genie._INITIAL_POLL_DELAY = self._delay

    async def test_polls_until_complete_within_one_tool_call(self) -> None:
        mcp = _FakeMcp(
            _message("PENDING_WAREHOUSE"),
            _message("EXECUTING_QUERY"),
            _message("COMPLETED", answer="24,872 rows.", rows=True),
        )
        state: dict = {}
        tool = genie.make_genie_tool(mcp, "space", state)  # type: ignore[arg-type]

        answer = await tool.func(question="how many rows?")

        self.assertEqual(["query_space_space", "poll_response_space", "poll_response_space"],
                         [name for name, _ in mcp.calls])
        self.assertIn("24,872 rows.", answer)
        self.assertIn("row_count", answer)
        self.assertIn("24872", answer)

    async def test_drops_sql_and_manifest_noise_from_model_context(self) -> None:
        mcp = _FakeMcp(_message("COMPLETED", answer="Done.", rows=True))
        tool = genie.make_genie_tool(mcp, "space", {})  # type: ignore[arg-type]

        answer = await tool.func(question="q")

        for noise in ("SELECT count(*)", "stmt-1", "byte_count", "suggestedQuestions"):
            self.assertNotIn(noise, answer)

    async def test_continues_the_same_genie_conversation(self) -> None:
        state: dict = {}
        first = _FakeMcp(_message("COMPLETED", answer="one"))
        await genie.make_genie_tool(first, "space", state).func(question="q1")  # type: ignore[arg-type]
        self.assertEqual("conv-1", state[genie.STATE_CONVERSATION_ID])

        second = _FakeMcp(_message("COMPLETED", answer="two"))
        await genie.make_genie_tool(second, "space", state).func(question="q2")  # type: ignore[arg-type]

        _, kwargs = second.calls[0]
        self.assertEqual("conv-1", kwargs.get("conversation_id"))

    async def test_gives_up_without_re_asking_when_genie_stalls(self) -> None:
        genie_max = genie._MAX_WAIT_SECONDS
        genie._MAX_WAIT_SECONDS = -1.0
        try:
            mcp = _FakeMcp(_message("PENDING_WAREHOUSE"))
            answer = await genie.make_genie_tool(mcp, "space", {}).func(question="q")  # type: ignore[arg-type]
        finally:
            genie._MAX_WAIT_SECONDS = genie_max

        self.assertEqual(1, len(mcp.calls))
        self.assertIn("do not ask the question again", answer)


if __name__ == "__main__":
    unittest.main()
