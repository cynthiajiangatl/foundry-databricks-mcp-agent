"""Run the agent over a query dataset and record what ``evaluate.py`` needs to score it.

This is the *collection* half of evaluation: it exercises the real agent (real Foundry
model, real Databricks managed MCP tools) so the scored responses reflect production
behaviour, then writes one JSONL row per query with the columns the Azure AI Evaluation
built-in evaluators expect — ``query``, ``response``, ``tool_calls`` and ``tool_definitions``.

Each query gets its own agent instance, matching the web app, where every request builds an
agent with fresh MCP connections and a fresh Genie conversation state.

Usage::

    python evals/run_agent.py                        # evals/dataset.jsonl -> evals/output/responses.jsonl
    python evals/run_agent.py --dataset my.jsonl --output out.jsonl
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from foundry_databricks_agent.agent import build_local_mcp_agent, response_text
from foundry_databricks_agent.config import load_settings
from foundry_databricks_agent.observability import configure_observability

logger = logging.getLogger("evals.run_agent")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = REPO_ROOT / "evals" / "dataset.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "evals" / "output" / "responses.jsonl"


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file, skipping blank lines."""
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    """Write rows as JSONL, creating the parent directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")


def tool_definitions(agent: Any) -> list[dict[str, Any]]:
    """Describe the agent's tools as ``{name, description, parameters}`` for the evaluators.

    Plain tools live in ``default_options['tools']``; MCP servers contribute their tools
    through ``mcp_tools``, whose ``functions`` are only populated once connected.
    """
    tools = list(agent.default_options.get("tools") or [])
    for mcp_tool in getattr(agent, "mcp_tools", []):
        tools.extend(getattr(mcp_tool, "functions", None) or [])

    definitions: list[dict[str, Any]] = []
    for tool in tools:
        spec = tool.to_json_schema_spec()
        function = spec.get("function", spec)
        definitions.append(
            {
                "name": function.get("name"),
                "description": function.get("description") or "",
                "parameters": function.get("parameters") or {},
            }
        )
    return definitions


def _arguments(raw: Any) -> Any:
    """Normalise tool-call arguments, which the model may emit as a JSON string."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw
    return raw


def tool_calls(messages: list[Any]) -> list[dict[str, Any]]:
    """Extract the tool calls the agent made, in the shape ToolCallAccuracyEvaluator wants."""
    calls: list[dict[str, Any]] = []
    for message in messages:
        for content in getattr(message, "contents", None) or []:
            if getattr(content, "type", None) != "function_call":
                continue
            calls.append(
                {
                    "type": "tool_call",
                    "tool_call_id": content.call_id,
                    "name": content.name,
                    "arguments": _arguments(content.arguments),
                }
            )
    return calls


async def run_query(row: dict[str, Any], settings: Any) -> dict[str, Any]:
    """Run one dataset row through a fresh agent and return the evaluation record."""
    query = row["query"]
    async with build_local_mcp_agent(settings) as agent:
        result = await agent.run(query)
        messages = list(getattr(result, "messages", None) or [])
        record: dict[str, Any] = {
            "id": row.get("id", query[:40]),
            "query": query,
            "response": response_text(result),
            "tool_calls": tool_calls(messages),
            "tool_definitions": tool_definitions(agent),
            # Optional for the evaluators, but the trace of how the answer was reached.
            "conversation": [message.to_dict() for message in messages],
        }
    if "ground_truth" in row:
        record["ground_truth"] = row["ground_truth"]
    return record


async def main_async(dataset: Path, output: Path) -> None:
    settings = load_settings()
    settings.validate_foundry()
    settings.validate_databricks_auth()
    configure_observability(settings)

    rows = read_jsonl(dataset)
    logger.info("Running %d queries from %s", len(rows), dataset)

    records: list[dict[str, Any]] = []
    for index, row in enumerate(rows, start=1):
        logger.info("[%d/%d] %s", index, len(rows), row["query"][:80])
        try:
            records.append(await run_query(row, settings))
        except Exception as exc:  # noqa: BLE001 - one bad query must not lose the whole run
            logger.exception("Query %s failed", row.get("id", index))
            records.append(
                {
                    "id": row.get("id", index),
                    "query": row["query"],
                    "response": f"ERROR: {exc}",
                    "tool_calls": [],
                    "tool_definitions": [],
                    "conversation": [],
                }
            )

    write_jsonl(output, records)
    logger.info("Wrote %d records to %s", len(records), output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    asyncio.run(main_async(args.dataset, args.output))


if __name__ == "__main__":
    main()
