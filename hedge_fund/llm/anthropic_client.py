"""Anthropic models through the official SDK.

Every other provider still reaches its model through a LangChain chat model
(ChatLLM in client.py). Anthropic gets its own transport because the SDK
offers what that path cannot on reasoning models:

- structured outputs: a JSON schema the API enforces without forced-tool
  mode (which reasoning models reject), so the answer is well-formed JSON;
- a prompt-cache breakpoint on the persona system prompt, so a backtest's
  many calls with one persona re-read a cached prefix instead of paying
  for it again;
- adaptive thinking steered by ``effort`` — never ``budget_tokens``;
- a typed refusal: ``stop_reason == "refusal"`` raises LLMRefusal and the
  agent abstains, per the failure contract in hedge_fund/signals/llm_agent.py.

The LLMClient contract is unchanged: complete(system, user) -> str returns
the raw text, and LLMAgent._parse remains the single validation point.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from hedge_fund.llm.client import DEFAULT_EFFORT, LLMRefusal, TokenListener
from hedge_fund.models import AnalystVerdict

logger = logging.getLogger(__name__)

# Thinking shares the output budget with the answer; large enough that
# adaptive thinking never truncates the JSON.
DEFAULT_MAX_TOKENS = 16000
DEFAULT_TIMEOUT = 300.0
# The SDK's own backoff handles the 429s that parallel analysts provoke.
# The SDK retries only retryable statuses (429, 5xx, 529, connection and
# timeout errors) with exponential backoff; 401, 403 and a 400 for an
# exhausted credit balance fail on the first call, which is what we want.
# Raised from 2 in Sept 2026: infrastructure failures now abort a cycle
# rather than abstaining, and at ~400 calls per seeding run even a 0.1%
# per-call escape rate would fail roughly a third of runs. A healthy run
# never touches these.
DEFAULT_MAX_RETRIES = 6

# The SDK refuses a non-streaming request it expects to run longer than ten
# minutes (anthropic._base_client._calculate_nonstreaming_timeout, sized as
# max_tokens / 128k of an hour). Mirror the rule: above it, stream.
_NONSTREAMING_MAX_TOKENS = 128_000 * 600 // 3600


def output_schema() -> dict[str, Any]:
    """AnalystVerdict as structured outputs accept it.

    transform_schema drops the numeric bounds the API does not support (they
    move into the field's description) and marks every object
    ``additionalProperties: false``. The bounds are still enforced
    client-side, by LLMAgent._parse.
    """
    return anthropic.transform_schema(AnalystVerdict.model_json_schema())


class AnthropicLLM:
    """An Anthropic model behind the LLMClient protocol, via the SDK.

    One anthropic.Anthropic() per instance; it is thread-safe, so the
    parallel analysts in run_cycle share it. *client* is an injection seam
    for tests — anything with ``.messages.create`` and ``.messages.stream``.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        on_token: TokenListener = None,
        effort: str | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = DEFAULT_MAX_RETRIES,
        client=None,
    ) -> None:
        self.model = model
        self._on_token = on_token
        self._effort = effort or DEFAULT_EFFORT
        self._schema = output_schema()
        self._max_tokens = max_tokens
        if client is None:
            client = anthropic.Anthropic(
                api_key=api_key,
                timeout=timeout,
                max_retries=max_retries,
            )
        self._client = client

    def complete(self, system: str, user: str) -> str:
        request = self._request(system, user)
        if self._on_token is not None or self._max_tokens > _NONSTREAMING_MAX_TOKENS:
            text, message = self._stream(request)
        else:
            message = self._client.messages.create(**request)
            text = "".join(block.text for block in message.content if block.type == "text")
        self._log(message)
        if message.stop_reason == "refusal":
            raise LLMRefusal(self._refusal_detail(message))
        return text

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _request(self, system: str, user: str) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self._max_tokens,
            # The breakpoint sits on the persona: the stable prefix every
            # call for that agent shares. Prompts under the model's minimum
            # cacheable length make it a silent no-op — acceptable, and
            # never a reason to pad a prompt.
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "effort": self._effort,
                "format": {"type": "json_schema", "schema": self._schema},
            },
            "thinking": {"type": "adaptive"},
        }

    def _stream(self, request: dict[str, Any]):
        """Stream the response: text deltas reach the listener as they land,
        thinking deltas are dropped. Returns (joined text, final message)."""
        parts: list[str] = []
        with self._client.messages.stream(**request) as stream:
            for event in stream:
                if event.type != "content_block_delta":
                    continue
                if event.delta.type != "text_delta":
                    continue
                parts.append(event.delta.text)
                if self._on_token is not None:
                    self._on_token(event.delta.text)
            message = stream.get_final_message()
        return "".join(parts), message

    def _log(self, message) -> None:
        """One INFO line per call — the way to see whether the prefix cache
        is hitting (cache_read_input_tokens > 0) and what a decision cost."""
        usage = message.usage
        logger.info(
            "anthropic model=%s effort=%s stop_reason=%s input_tokens=%s "
            "output_tokens=%s cache_creation_input_tokens=%s "
            "cache_read_input_tokens=%s",
            self.model,
            self._effort,
            message.stop_reason,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_creation_input_tokens,
            usage.cache_read_input_tokens,
        )

    def _refusal_detail(self, message) -> str:
        details = getattr(message, "stop_details", None)
        category = getattr(details, "category", None)
        explanation = getattr(details, "explanation", None)
        detail = explanation or category or "no detail given"
        return f"{self.model} refused the request: {detail}"
