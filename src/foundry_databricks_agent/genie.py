"""Expose the Genie space to the model as one tool call instead of an ask/poll loop.

Genie answers asynchronously: ``query_space`` starts a message and ``poll_response`` is
called until it reaches a terminal state. Letting the *model* drive that loop is expensive
twice over: every poll is another model call that replays the whole conversation, and a
mis-formed poll usually makes the model re-ask the question, which runs the SQL on the
warehouse a second time. This module keeps the loop inside a single tool invocation and
returns only the answer, so the model sees one compact result.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import MutableMapping
from typing import Annotated, Any

from agent_framework import FunctionTool, MCPStreamableHTTPTool

logger = logging.getLogger(__name__)

# Genie message states that will not change unless a new question is asked.
_TERMINAL_STATES = frozenset({"COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"})

# Key under which the Genie conversation id is kept in the agent session state, so
# follow-up questions continue the same Genie conversation instead of starting a new one.
STATE_CONVERSATION_ID = "genie_conversation_id"

_MAX_RESULT_ROWS = 20
_MAX_WAIT_SECONDS = 150.0
_INITIAL_POLL_DELAY = 1.5
_MAX_POLL_DELAY = 8.0


def _result_text(result: str | list[Any]) -> str:
    """Return the first text block of an MCP result.

    The Genie managed MCP server repeats the same JSON payload in every content item;
    taking only the first avoids doubling it.
    """
    if isinstance(result, str):
        return result
    for content in result or []:
        text = getattr(content, "text", None)
        if isinstance(text, str) and text:
            return text
    return ""


def _payload(result: str | list[Any]) -> dict[str, Any] | None:
    """Decode the first JSON object in an MCP result."""
    text = _result_text(result)
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char == "{":
            try:
                decoded, _ = decoder.raw_decode(text[index:])
            except ValueError:
                continue
            if isinstance(decoded, dict):
                return decoded
    return None


def _cell(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("string_value", "str_value", "value"):
            if key in value:
                return str(value[key])
        return json.dumps(value, separators=(",", ":"))
    return "" if value is None else str(value)


def _row_values(row: Any) -> list[str]:
    if isinstance(row, dict):
        return [_cell(value) for value in row.get("values", [])]
    if isinstance(row, list):
        return [_cell(value) for value in row]
    return [_cell(row)]


def _table(attachment: dict[str, Any]) -> str:
    """Render a bounded preview of a Genie query result."""
    response = attachment.get("statement_response") or {}
    manifest = response.get("manifest") or {}
    schema = manifest.get("schema") or {}
    rows = (response.get("result") or {}).get("data_array") or []
    if not rows:
        return ""

    lines: list[str] = []
    columns = [str(column.get("name", "")) for column in schema.get("columns") or []]
    if columns:
        lines.append(" | ".join(columns))
    shown = rows[:_MAX_RESULT_ROWS]
    lines.extend(" | ".join(_row_values(row)) for row in shown)

    total = manifest.get("total_row_count")
    if isinstance(total, int) and total > len(shown):
        lines.append(f"... {len(shown)} of {total} rows shown")
    return "\n".join(lines)


def _compact_answer(payload: dict[str, Any]) -> str:
    """Keep Genie's narrative answer and result rows; drop SQL, manifests and chunk metadata."""
    content = payload.get("content") or {}
    parts = [
        text.strip()
        for text in content.get("textAttachments") or []
        if isinstance(text, str) and text.strip()
    ]
    for attachment in content.get("queryAttachments") or []:
        if not isinstance(attachment, dict):
            continue
        # Keep the generated SQL in logs for audit without spending model context on it.
        if attachment.get("query"):
            logger.debug("Genie SQL: %s", attachment["query"])
        table = _table(attachment)
        if table:
            parts.append(table)
    return "\n\n".join(parts) or "Genie returned no answer for this question."


def make_genie_tool(
    genie_mcp: MCPStreamableHTTPTool,
    space_id: str,
    state: MutableMapping[str, Any],
) -> FunctionTool:
    """Build the single-call Genie tool bound to one conversation's state."""
    query_tool = f"query_space_{space_id}"
    poll_tool = f"poll_response_{space_id}"

    async def ask_genie(
        question: Annotated[str, "A complete, self-contained analytics question."],
    ) -> str:
        arguments: dict[str, Any] = {"query": question}
        previous = state.get(STATE_CONVERSATION_ID)
        if isinstance(previous, str) and previous:
            arguments["conversation_id"] = previous

        payload = _payload(await genie_mcp.call_tool(query_tool, **arguments))
        if payload is None:
            return "Genie returned a response that could not be read."

        conversation_id = payload.get("conversationId")
        message_id = payload.get("messageId")
        if isinstance(conversation_id, str) and conversation_id:
            state[STATE_CONVERSATION_ID] = conversation_id

        deadline = time.monotonic() + _MAX_WAIT_SECONDS
        delay = _INITIAL_POLL_DELAY
        status = str(payload.get("status") or "").upper()

        while status not in _TERMINAL_STATES:
            if not (conversation_id and message_id) or time.monotonic() >= deadline:
                return (
                    "Genie is still working on this question. Tell the user it is taking "
                    "longer than usual and do not ask the question again."
                )
            await asyncio.sleep(delay)
            delay = min(delay * 2, _MAX_POLL_DELAY)
            polled = _payload(
                await genie_mcp.call_tool(
                    poll_tool, conversation_id=conversation_id, message_id=message_id
                )
            )
            if polled is not None:
                payload = polled
            status = str(payload.get("status") or "").upper()

        if status != "COMPLETED":
            return f"Genie could not answer this question (status: {status.lower()})."
        return _compact_answer(payload)

    return FunctionTool(
        name="ask_genie",
        description=(
            "Ask the governed Genie space a natural-language analytics question about the "
            "lakehouse data. Waits for the answer and returns it, and automatically "
            "continues the same Genie conversation across follow-up questions."
        ),
        func=ask_genie,
    )
