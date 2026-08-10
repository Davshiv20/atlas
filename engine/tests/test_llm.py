from __future__ import annotations

from types import SimpleNamespace

from atlas.llm import Tool, run_tool_loop
from atlas.settings import get_settings


class FakeMessage(SimpleNamespace):
    def model_dump(self, exclude_none: bool = False) -> dict:
        return {"role": "assistant", "content": self.content}


def message(content=None, tool_calls=None) -> FakeMessage:
    return FakeMessage(content=content, tool_calls=tool_calls or [])


def tool_call(name: str, arguments: str, call_id: str = "call_1") -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id, function=SimpleNamespace(name=name, arguments=arguments)
    )


class FakeClient:
    """Replays a scripted sequence of assistant messages."""

    def __init__(self, script: list[FakeMessage]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        nxt = self.script.pop(0) if self.script else message(content="done")
        return SimpleNamespace(choices=[SimpleNamespace(message=nxt)])


def echo_tool(recorder: list) -> Tool:
    return Tool(
        name="echo",
        description="Echo a value.",
        parameters={
            "type": "object",
            "properties": {"value": {"type": "string"}},
            "required": ["value"],
            "additionalProperties": False,
        },
        run=lambda value: recorder.append(value) or f"echoed {value}",
    )


def last_tool_result(client: FakeClient) -> str:
    messages = client.calls[-1]["messages"]
    return next(m["content"] for m in reversed(messages) if m.get("role") == "tool")


def test_tool_is_executed_and_result_fed_back() -> None:
    recorder: list = []
    client = FakeClient([message(tool_calls=[tool_call("echo", '{"value": "hi"}')])])

    run_tool_loop(client, "sys", "user", [echo_tool(recorder)])

    assert recorder == ["hi"]
    assert last_tool_result(client) == "echoed hi"


def test_malformed_arguments_are_returned_to_the_model() -> None:
    client = FakeClient([message(tool_calls=[tool_call("echo", "{not json")])])
    run_tool_loop(client, "sys", "user", [echo_tool([])])
    assert "not valid JSON" in last_tool_result(client)


def test_unknown_tool_is_reported_not_raised() -> None:
    client = FakeClient([message(tool_calls=[tool_call("nope", "{}")])])
    run_tool_loop(client, "sys", "user", [echo_tool([])])
    assert "no tool named" in last_tool_result(client)


def test_tool_exception_is_returned_to_the_model() -> None:
    def boom(value: str) -> str:
        raise RuntimeError("database on fire")

    exploding = Tool(name="echo", description="", parameters={}, run=boom)
    client = FakeClient([message(tool_calls=[tool_call("echo", '{"value": "x"}')])])

    run_tool_loop(client, "sys", "user", [exploding])
    assert "database on fire" in last_tool_result(client)


def test_wrong_arguments_are_returned_to_the_model() -> None:
    client = FakeClient([message(tool_calls=[tool_call("echo", '{"wrong": 1}')])])
    run_tool_loop(client, "sys", "user", [echo_tool([])])
    assert "wrong arguments" in last_tool_result(client)


def test_loop_stops_when_no_tool_calls() -> None:
    client = FakeClient([message(content="finished")])
    run_tool_loop(client, "sys", "user", [echo_tool([])])
    assert len(client.calls) == 1


def test_loop_is_bounded() -> None:
    """A model that calls tools forever must not run forever."""
    max_turns = get_settings().atlas_max_turns
    forever = [
        message(tool_calls=[tool_call("echo", '{"value": "x"}')]) for _ in range(max_turns + 5)
    ]
    client = FakeClient(forever)
    run_tool_loop(client, "sys", "user", [echo_tool([])])
    assert len(client.calls) == max_turns


def test_effort_is_sent_on_every_request() -> None:
    client = FakeClient([message(content="done")])
    run_tool_loop(client, "sys", "user", [echo_tool([])])
    expected = {"reasoning": {"effort": get_settings().atlas_effort}}
    assert client.calls[0]["extra_body"] == expected
