"""The language model, configured for DSPy.

The hand-written chat-completions loop this replaces drove the conversation and
threw the reasoning away: every thought, tool call and observation was
discarded, so a run that recorded nothing left no trace of what it had spent
forty turns on. `dspy.ReAct` keeps the trajectory, which is the difference
between "the model produced no claims" and knowing why.

The model is still reached through OpenRouter. DSPy routes through LiteLLM, so
the provider is named as a prefix on the model string rather than by a base URL.

What this module does *not* do is decide anything. Checks are executed and
judged by `checks.py`, confidence is computed by `policy.py`, and a claim is
only real once `record_claim` has accepted its evidence. Changing the driver
changes how the model is asked, never what it is allowed to establish.
"""

from __future__ import annotations

import logging

import dspy

from atlas.settings import get_settings

logger = logging.getLogger(__name__)


def model_id() -> str:
    return get_settings().atlas_model


def effort() -> str:
    return get_settings().atlas_effort


def build_lm() -> dspy.LM:
    """The configured model.

    LiteLLM takes the provider as a prefix on the model name. Settings hold the
    bare id (`qwen/qwen3.7-plus`) because that is what OpenRouter's own
    documentation uses and what the console displays, so the prefix is added
    here rather than stored.
    """
    settings = get_settings()
    return dspy.LM(
        f"openrouter/{settings.atlas_model}",
        api_key=settings.require_api_key(),
        # OpenRouter's own knob, sent through the passthrough body rather than
        # as a top-level `reasoning_effort`: LiteLLM validates its named
        # parameters against the provider and rejects that one outright for
        # OpenRouter, so the request fails rather than degrading.
        extra_body={"reasoning": {"effort": settings.atlas_effort}},
    )


def configure(lm: dspy.LM | None = None) -> dspy.LM:
    """Bind the model for this process.

    DSPy resolves the LM from a global at call time, so this is set once when a
    run starts rather than per table — six tables analysed concurrently would
    otherwise reconfigure the same global six times.
    """
    resolved = lm or build_lm()
    dspy.configure(lm=resolved)
    return resolved
