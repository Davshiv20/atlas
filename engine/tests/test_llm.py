from __future__ import annotations

import dspy
import pytest

from atlas.llm import build_lm, configure
from atlas.settings import get_settings

# The loop itself is DSPy's now — bounded iteration, tool dispatch, and
# returning a tool's exception to the model as an observation are all `ReAct`
# behaviours and are tested there. What stays ours is how the model is named
# and bound, and both have silent failure modes: a missing provider prefix
# routes to a different vendor, and an unbound LM only fails once a run starts.


@pytest.fixture(autouse=True)
def _settings(monkeypatch, tmp_path):
    # Settings read `.env` relative to the working directory, so without the
    # chdir a developer with a populated engine/.env sees different results
    # than CI — and "no key configured" is untestable because there is one.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("ATLAS_MODEL", "qwen/qwen3.7-plus")
    monkeypatch.setenv("ATLAS_EFFORT", "medium")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_the_model_is_routed_through_openrouter() -> None:
    """LiteLLM takes the provider from a prefix on the model string. Without
    it, `qwen/qwen3.7-plus` resolves to a different vendor entirely and the
    failure is a billing surprise, not an error."""
    assert build_lm().model == "openrouter/qwen/qwen3.7-plus"


def test_effort_is_carried_in_the_body_the_provider_accepts() -> None:
    """Sent as a top-level `reasoning_effort`, LiteLLM rejects the request for
    OpenRouter outright — it validates named parameters per provider rather
    than passing unknown ones through."""
    lm = build_lm()
    assert lm.kwargs["extra_body"] == {"reasoning": {"effort": "medium"}}


def test_a_missing_key_fails_before_a_run_starts(monkeypatch) -> None:
    """Minutes into an analysis is the wrong place to discover there is no
    credential."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    get_settings.cache_clear()
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        build_lm()


def test_configure_binds_the_model_for_the_process() -> None:
    """DSPy resolves the LM from a global at call time, so a run that never
    configured one fails inside a worker thread rather than at the entry."""
    bound = configure()
    assert dspy.settings.lm is bound
