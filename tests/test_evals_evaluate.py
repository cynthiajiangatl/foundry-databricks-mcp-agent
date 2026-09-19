from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

_EVALUATE = Path(__file__).resolve().parent.parent / "evals" / "evaluate.py"
# evals/evaluate.py imports azure-ai-evaluation, which only ships in the optional evals extra.
_HAS_EVALS = importlib.util.find_spec("azure.ai.evaluation") is not None

if _HAS_EVALS:
    _spec = importlib.util.spec_from_file_location("evals_evaluate", _EVALUATE)
    assert _spec and _spec.loader
    evaluate_mod = importlib.util.module_from_spec(_spec)
    sys.modules[_spec.name] = evaluate_mod
    _spec.loader.exec_module(evaluate_mod)


@unittest.skipUnless(_HAS_EVALS, "azure-ai-evaluation not installed (pip install -e '.[evals]')")
class JudgeReasoningDetectionTests(unittest.TestCase):
    def _detect(self, deployment: str, env: dict[str, str] | None = None) -> bool:
        with mock.patch.dict("os.environ", env or {}, clear=False):
            if env is None:
                # Make sure a real override in the developer's shell cannot skew the result.
                with mock.patch.dict("os.environ", {"AZURE_OPENAI_IS_REASONING_MODEL": ""}):
                    return evaluate_mod.judge_is_reasoning_model(deployment)
            return evaluate_mod.judge_is_reasoning_model(deployment)

    def test_detects_reasoning_deployments(self) -> None:
        for name in ("gpt-5.6-sol", "GPT-5", "o1-preview", "o3-mini", "o4"):
            self.assertTrue(self._detect(name), name)

    def test_treats_other_deployments_as_standard(self) -> None:
        for name in ("gpt-4o-mini", "gpt-4o", "gpt-4.1", "my-judge"):
            self.assertFalse(self._detect(name), name)

    def test_env_override_wins_both_ways(self) -> None:
        self.assertTrue(
            self._detect("my-judge", {"AZURE_OPENAI_IS_REASONING_MODEL": "1"})
        )
        self.assertFalse(
            self._detect("gpt-5.6-sol", {"AZURE_OPENAI_IS_REASONING_MODEL": "0"})
        )


if __name__ == "__main__":
    unittest.main()
