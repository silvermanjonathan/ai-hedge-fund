"""AnthropicLLM against a fake Messages object — request shape, streaming,
refusal. Nothing here touches the network."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from hedge_fund.llm import AnthropicLLM, LLMRefusal, load_api_models
from hedge_fund.llm.anthropic_client import output_schema

VERDICT = '{"signal": "bullish", "confidence": 80, "reasoning": "Wonderful business."}'

_ANTHROPIC_IDS = [mid for _, mid, prov in load_api_models() if prov == "Anthropic"]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _usage():
    return SimpleNamespace(input_tokens=300, output_tokens=40, cache_creation_input_tokens=0, cache_read_input_tokens=0)


def _message(text, stop_reason="end_turn", stop_details=None):
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="weighing margins"),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop_reason,
        stop_details=stop_details,
        usage=_usage(),
    )


def _delta(kind, text):
    field = "text" if kind == "text_delta" else "thinking"
    return SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(type=kind, **{field: text}))


class FakeStream:
    """What messages.stream() returns: a context manager that iterates
    events and then hands over the accumulated message."""

    def __init__(self, events, final):
        self._events = events
        self._final = final

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        yield from self._events

    def get_final_message(self):
        return self._final


class FakeMessages:
    def __init__(self, text=VERDICT, stop_reason="end_turn", events=None):
        self._text = text
        self._stop_reason = stop_reason
        self._events = events or []
        self.requests: list[dict] = []
        self.streamed = False

    def create(self, **request):
        self.requests.append(request)
        return _message(self._text, self._stop_reason)

    def stream(self, **request):
        self.requests.append(request)
        self.streamed = True
        return FakeStream(self._events, _message(self._text, self._stop_reason))


def _llm(messages, **kwargs):
    return AnthropicLLM("claude-fable-5-1", client=SimpleNamespace(messages=messages), **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", _ANTHROPIC_IDS + ["claude-something-unreleased"])
def test_constructs_for_every_anthropic_id(model_id):
    """Every listed Anthropic id, and an unlisted claude-* one, builds a
    client that satisfies the LLMClient protocol."""
    llm = AnthropicLLM(model_id, client=SimpleNamespace(messages=FakeMessages()))
    assert llm.model == model_id
    assert llm.complete("s", "u") == VERDICT


def test_request_carries_cache_breakpoint_schema_effort_and_no_budget():
    messages = FakeMessages()
    _llm(messages, effort="medium").complete("persona", "snapshot")

    (request,) = messages.requests
    assert request["system"] == [{"type": "text", "text": "persona", "cache_control": {"type": "ephemeral"}}]
    assert request["messages"] == [{"role": "user", "content": "snapshot"}]
    assert request["output_config"]["effort"] == "medium"
    assert request["output_config"]["format"] == {"type": "json_schema", "schema": output_schema()}
    assert set(output_schema()["properties"]) == {"signal", "confidence", "reasoning"}
    assert request["thinking"] == {"type": "adaptive"}
    assert "budget_tokens" not in json.dumps(request)
    assert not messages.streamed  # no listener: a plain create


def test_stream_feeds_only_text_to_the_listener_and_returns_the_joined_json():
    events = [
        _delta("thinking_delta", "let me weigh the moat"),
        _delta("text_delta", '{"signal": "bullish", '),
        _delta("text_delta", '"confidence": 80, "reasoning": "Wonderful business."}'),
    ]
    messages = FakeMessages(events=events)
    seen: list[str] = []

    result = _llm(messages, on_token=seen.append).complete("s", "u")

    assert messages.streamed
    assert seen == ['{"signal": "bullish", ', '"confidence": 80, "reasoning": "Wonderful business."}']
    assert result == VERDICT
    assert json.loads(result)["signal"] == "bullish"


def test_refusal_raises_llm_refusal():
    messages = FakeMessages(text="", stop_reason="refusal")
    with pytest.raises(LLMRefusal):
        _llm(messages).complete("s", "u")
