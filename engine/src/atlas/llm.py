"""OpenRouter client and the tool-calling loop.

OpenRouter speaks the OpenAI chat-completions dialect, so the loop is written by
hand rather than delegated to a provider SDK helper. That also keeps the tool
surface provider-neutral: a `Tool` is a name, a JSON schema, and a callable.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from openai import OpenAI

from atlas.settings import get_settings

logger = logging.getLogger(__name__)



@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    run: Callable[..., str]

    def call(self, arguments: dict[str, Any]) -> str:
        return self.run(**arguments)

    def to_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


def build_client(base_url: str | None = None, api_key: str | None = None) -> OpenAI:
    settings = get_settings()
    return OpenAI(
        base_url=base_url or settings.openrouter_base_url,
        api_key=api_key or settings.require_api_key(),
    )


def model_id() -> str:
    return get_settings().atlas_model


def effort() -> str:
    return get_settings().atlas_effort


def run_tool_loop(
    client: OpenAI,
    system: str,
    user: str,
    tools: Iterable[Tool],
    on_text: Callable[[str], None] | None = None,
) -> bool:
    """Drive the conversation until the model stops calling tools.

    Results are collected by the tools themselves (they write into a sink), so
    the return value is only whether the loop was cut short by the turn ceiling.
    That matters: a truncated run looks identical to a thorough one from the
    outside, and one observed run spent all forty turns on checks and recorded
    no claims at all. The final assistant prose is still incidental, and any
    claim that appears only there rather than through `record_claim` is
    deliberately discarded.
    """
    by_name = {tool.name: tool for tool in tools}
    schemas = [tool.to_schema() for tool in by_name.values()]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]

    max_turns = get_settings().atlas_max_turns
    for _ in range(max_turns):
        response = client.chat.completions.create(
            model=model_id(),
            messages=messages,
            tools=schemas,
            # OpenRouter's reasoning-effort knob. Unverified against Claude
            # models on this gateway: if unsupported it is ignored silently
            # rather than erroring, and the model runs at its own default.
            extra_body={"reasoning": {"effort": effort()}},
        )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if message.content and on_text:
            on_text(message.content)

        if not message.tool_calls:
            return False

        for tool_call in message.tool_calls:
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": _dispatch(by_name, tool_call),
                }
            )

    logger.warning("tool loop hit the %d-turn ceiling; stopping", max_turns)
    return True


def _dispatch(by_name: dict[str, Tool], tool_call) -> str:
    """Execute one tool call, returning every failure to the model as text.

    A raised exception here would abort a whole table's analysis over one bad
    argument; handing the error back lets the model correct itself.
    """
    tool = by_name.get(tool_call.function.name)
    if tool is None:
        return f"ERROR: no tool named {tool_call.function.name!r}."

    try:
        arguments = json.loads(tool_call.function.arguments or "{}")
    except json.JSONDecodeError as exc:
        return f"ERROR: arguments were not valid JSON ({exc}). Send them again."

    if not isinstance(arguments, dict):
        return "ERROR: arguments must be a JSON object."

    try:
        return tool.call(arguments)
    except TypeError as exc:
        return f"ERROR: wrong arguments for {tool.name} ({exc})."
    except Exception as exc:
        logger.exception("tool %s raised", tool.name)
        return f"ERROR: {tool.name} failed: {type(exc).__name__}: {exc}"
