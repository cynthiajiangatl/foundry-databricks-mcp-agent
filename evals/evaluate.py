"""Score the agent's recorded responses with the Azure AI Evaluation built-in evaluators.

This is the *scoring* half of evaluation. Feed it the JSONL that ``run_agent.py`` produced::

    python evals/run_agent.py
    python evals/evaluate.py

It runs every evaluator in one :func:`azure.ai.evaluation.evaluate` call, which handles
batching, column mapping and metric aggregation, and writes row-level scores plus aggregate
metrics to ``evals/output/results.json``.

The evaluators marked *prompt-based* use an LLM as judge, so they need a judge model. Point
them at an **Azure OpenAI endpoint** (``https://<resource>.openai.azure.com/``) — not the
Foundry project endpoint — via:

* ``AZURE_OPENAI_ENDPOINT``   (required)
* ``AZURE_OPENAI_DEPLOYMENT`` (required)
* ``AZURE_OPENAI_API_KEY``    (optional; without it, Entra ID via ``DefaultAzureCredential``
  is used, which needs the *Cognitive Services OpenAI User* role)

Use a **non-reasoning** deployment such as ``gpt-4o-mini`` or ``gpt-4o``, or a **reasoning**
deployment (``gpt-5*``, ``o1``, ``o3``, ``o4``) — reasoning models reject ``max_tokens``, so the
evaluators must be told to send ``max_completion_tokens`` instead. That is detected from the
deployment name and can be forced with ``AZURE_OPENAI_IS_REASONING_MODEL=1``/``0``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import Any

from azure.ai.evaluation import (
    AzureOpenAIModelConfiguration,
    CoherenceEvaluator,
    FluencyEvaluator,
    IntentResolutionEvaluator,
    RelevanceEvaluator,
    TaskAdherenceEvaluator,
    ToolCallAccuracyEvaluator,
    evaluate,
)
from dotenv import load_dotenv

from foundry_databricks_agent.config import ConfigError

logger = logging.getLogger("evals.evaluate")

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT = REPO_ROOT / "evals" / "output" / "responses.jsonl"
DEFAULT_OUTPUT = REPO_ROOT / "evals" / "output" / "results.json"

_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5")


def judge_is_reasoning_model(deployment: str) -> bool:
    """Whether the judge needs ``max_completion_tokens`` instead of ``max_tokens``.

    Reasoning deployments reject ``max_tokens`` outright, which fails every prompt-based
    evaluator. Detection is by deployment name, so custom names need the explicit override.
    """
    override = (os.getenv("AZURE_OPENAI_IS_REASONING_MODEL") or "").strip().lower()
    if override:
        return override in {"1", "true", "yes", "on"}
    return deployment.strip().lower().startswith(_REASONING_PREFIXES)


def judge_model_config() -> tuple[AzureOpenAIModelConfiguration, Any | None]:
    """Build the judge-model config, plus a credential when no API key is configured."""
    endpoint = (os.getenv("AZURE_OPENAI_ENDPOINT") or "").strip()
    deployment = (os.getenv("AZURE_OPENAI_DEPLOYMENT") or "").strip()
    api_key = (os.getenv("AZURE_OPENAI_API_KEY") or "").strip()

    missing = [
        name
        for name, value in (
            ("AZURE_OPENAI_ENDPOINT", endpoint),
            ("AZURE_OPENAI_DEPLOYMENT", deployment),
        )
        if not value
    ]
    if missing:
        raise ConfigError(
            "The prompt-based evaluators need a judge model. Set "
            + ", ".join(missing)
            + ". Use the Azure OpenAI endpoint (https://<resource>.openai.azure.com/), "
            "not the Foundry project endpoint."
        )

    if api_key:
        config = AzureOpenAIModelConfiguration(
            azure_endpoint=endpoint,
            azure_deployment=deployment,
            api_key=api_key,
        )
        return config, None

    from azure.identity import DefaultAzureCredential

    config = AzureOpenAIModelConfiguration(
        azure_endpoint=endpoint,
        azure_deployment=deployment,
    )
    return config, DefaultAzureCredential()


def build_evaluators(
    model_config: AzureOpenAIModelConfiguration, credential: Any | None
) -> dict[str, Any]:
    """Instantiate the built-in evaluators that fit a tool-calling data agent."""
    deployment = model_config.get("azure_deployment") or ""
    is_reasoning = judge_is_reasoning_model(deployment)
    logger.info(
        "Judge deployment %s (reasoning model: %s)", deployment, "yes" if is_reasoning else "no"
    )

    kwargs: dict[str, Any] = {
        "model_config": model_config,
        "is_reasoning_model": is_reasoning,
    }
    if credential is not None:
        kwargs["credential"] = credential

    return {
        # Did the agent understand and resolve what the user actually asked for?
        "intent_resolution": IntentResolutionEvaluator(**kwargs),
        # Did it follow its instructions (ground answers in tools, refuse when nothing fits)?
        "task_adherence": TaskAdherenceEvaluator(**kwargs),
        # Did it pick the right Databricks tool and pass the right arguments?
        "tool_call_accuracy": ToolCallAccuracyEvaluator(**kwargs),
        "relevance": RelevanceEvaluator(**kwargs),
        "coherence": CoherenceEvaluator(**kwargs),
        "fluency": FluencyEvaluator(**kwargs),
    }


# Maps the JSONL columns written by run_agent.py onto each evaluator's inputs.
EVALUATOR_CONFIG: dict[str, dict[str, Any]] = {
    "intent_resolution": {
        "column_mapping": {
            "query": "${data.query}",
            "response": "${data.response}",
            "tool_definitions": "${data.tool_definitions}",
        }
    },
    "task_adherence": {
        "column_mapping": {
            "query": "${data.query}",
            "response": "${data.response}",
            "tool_definitions": "${data.tool_definitions}",
        }
    },
    "tool_call_accuracy": {
        "column_mapping": {
            "query": "${data.query}",
            "response": "${data.response}",
            "tool_calls": "${data.tool_calls}",
            "tool_definitions": "${data.tool_definitions}",
        }
    },
    "relevance": {
        "column_mapping": {"query": "${data.query}", "response": "${data.response}"}
    },
    "coherence": {
        "column_mapping": {"query": "${data.query}", "response": "${data.response}"}
    },
    "fluency": {"column_mapping": {"response": "${data.response}"}},
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--name", default="foundry-databricks-agent", help="Evaluation run name."
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    load_dotenv(override=False)

    if not args.data.exists():
        raise SystemExit(f"{args.data} not found. Run `python evals/run_agent.py` first.")

    try:
        model_config, credential = judge_model_config()
    except ConfigError as exc:
        raise SystemExit(str(exc)) from exc

    args.output.parent.mkdir(parents=True, exist_ok=True)

    result = evaluate(
        evaluation_name=args.name,
        data=str(args.data),
        evaluators=build_evaluators(model_config, credential),
        evaluator_config=EVALUATOR_CONFIG,
        output_path=str(args.output),
    )

    print(json.dumps(result.get("metrics", {}), indent=2))
    print(f"\nRow-level results: {args.output}")


if __name__ == "__main__":
    main()
