"""LLMAgent — base class for LLM investor agents (the second AlphaModel flavor).

An LLMAgent reasons over a point-in-time FundamentalsSnapshot in a persona's
voice and emits the same Signal every quant model does. The base class owns
all the machinery; a persona is just a name + a system prompt:

    class BuffettAgent(LLMAgent):
        @property
        def name(self) -> str:
            return "buffett"

        def get_system_prompt(self) -> str:
            return "You are Warren Buffett..."

Failure contract (locked decisions). Two classes, and the split is the
point: an abstention must always mean something was ASKED and no view came
back, never that nobody asked.

- ABSTAIN — the model declined, or could not be understood, or the data was
  too thin to ask about. Signal(value=0.0, metadata.abstained=True).
  Specifically: a refusal (stop_reason "refusal"), an unparseable response,
  and InsufficientData.
- PROPAGATE — everything else. Data-layer errors, and any transport,
  auth, quota, rate-limit or timeout failure from the LLM provider. A
  broken snapshot must never silently become a neutral view, and neither
  must an expired API key.

The default is deliberately PROPAGATE, not abstain. Until Sept 2026 this
caught bare Exception around the LLM call, so an expired key or an
exhausted budget produced a cohort of abstentions indistinguishable from
schools declining names — and `Ledger.ingest` drops abstained rows, so the
evidence never reached the ledger. An unattended run reported success with
two thirds of its universe silently missing. Only positively identified
model-side conditions abstain now; an unrecognised failure is assumed to be
infrastructure, which is the safe direction and the only one that works for
providers whose exception types we cannot enumerate.

Stopping is cheap, which is what makes failing loud affordable: PromptCache
writes per call, atomically, the moment a response parses. A run that dies
at call 390 keeps 389 verdicts on disk, and a rerun re-reasons only what
did not finish.

Every LLM decision persists its exact prompt + response (via PromptCache),
and an unchanged snapshot never pays for a second LLM call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from hedge_fund.data.protocol import DataClient
from hedge_fund.features.snapshot import (
    build_snapshot,
    FundamentalsSnapshot,
    InsufficientData,
)
from hedge_fund.llm import (
    extract_json,
    LLMClient,
    LLMRefusal,
    make_llm,
    prompt_key,
    PromptCache,
)
from hedge_fund.models import AnalystVerdict, Signal
from hedge_fund.signals.base import AlphaModel

logger = logging.getLogger(__name__)

# What the model must return; folded into Signal.value below.
_SIGNAL_TO_SIGN = {"bullish": 1.0, "neutral": 0.0, "bearish": -1.0}


@dataclass(frozen=True)
class Preview:
    """What one (school, ticker) call would cost, decided without calling."""

    school: str
    ticker: str
    key: str | None = None
    snapshot_hash: str | None = None
    system: str | None = None
    model: str | None = None
    hit: bool = False
    insufficient: bool = False  # would abstain on thin data; never billed
    system_tokens: int = 0
    user_tokens: int = 0
    cache_dir: Path | None = None


def _tokens(text: str) -> int:
    """Rough token count. ~4 characters per token for English prose with
    numbers; good to maybe 15%, which is well inside what a spend guard
    needs. Only the INPUT side uses this — it is measured from the real
    prompt text, not assumed."""
    return len(text) // 4


class LLMAgent(AlphaModel):
    """Base for persona agents. Subclasses define `name` and `get_system_prompt`."""

    def __init__(
        self,
        llm: LLMClient | None = None,
        cache: PromptCache | None = None,
        effort: str | None = None,
    ) -> None:
        # A mandate steers effort per model via `params: {effort: medium}`
        # (Fund builds staff with ALPHA_MODEL_REGISTRY[name](**params)). It
        # is part of the cache key only when set: the same prompt at a
        # different effort is a different decision, and None leaves every
        # existing cache file keyed as it was.
        self._effort = effort
        self._llm = llm if llm is not None else make_llm(effort=effort)
        self._cache = cache if cache is not None else PromptCache()

    # ------------------------------------------------------------------
    # AlphaModel interface
    # ------------------------------------------------------------------

    def predict(self, ticker: str, date: str, data_client: DataClient) -> Signal:
        try:
            snapshot = self.build_snapshot(ticker, date, data_client)
        except InsufficientData as exc:
            return self._abstain(ticker, date, f"insufficient data: {exc}")
        # Any other data-layer exception (e.g. FDClientError) propagates.

        system, user, key = self._prompts(snapshot)

        cached = self._cache.get(key)
        if cached is not None and "parsed" in cached:
            return self._to_signal(ticker, date, cached["parsed"], key, snapshot, cached=True)

        try:
            response = self._llm.complete(system, user)
        except LLMRefusal as exc:
            logger.warning("%s refused for %s@%s: %s", self.name, ticker, date, exc)
            return self._abstain(ticker, date, "refusal")
        # Anything else propagates. A transport, auth, quota, rate-limit or
        # timeout failure is infrastructure, not a view, and swallowing it
        # would put "no opinion" in the record where "never asked" is the
        # truth. The provider SDK has already retried what is retryable by
        # the time an exception reaches here.

        record = {
            "agent": self.name,
            "model": self._llm.model,
            "ticker": ticker,
            "as_of": date,
            "snapshot_hash": snapshot.content_hash,
            "system": system,
            "user": user,
            "response": response,
        }

        try:
            parsed = self._parse(response)
        except Exception as exc:
            # Persist the raw response even when unparseable — the debug trail.
            self._cache.put(key, {**record, "parse_error": str(exc)})
            logger.warning("%s parse failed for %s@%s: %s", self.name, ticker, date, exc)
            return self._abstain(ticker, date, f"parse failed: {exc}")

        self._cache.put(key, {**record, "parsed": parsed})
        return self._to_signal(ticker, date, parsed, key, snapshot, cached=False)

    def preview(self, ticker: str, date: str, data_client: DataClient) -> "Preview":
        """What predict() WOULD do, without calling the model.

        The pre-flight cost estimate uses this to count real cache misses
        instead of assuming every call is one — the difference between "this
        run costs at most $6.84" and "this run costs $0.00 because nothing
        has changed since last week".

        It deliberately goes through the same _prompts() that predict() uses.
        Recomputing the key alongside predict rather than with it would make
        the estimate a second implementation of the cache key, free to drift
        from the real one and wrong in exactly the situation the estimate
        exists to catch.
        """
        try:
            snapshot = self.build_snapshot(ticker, date, data_client)
        except InsufficientData:
            return Preview(school=self.name, ticker=ticker, insufficient=True, cache_dir=self._cache.directory)
        system, user, key = self._prompts(snapshot)
        return Preview(
            school=self.name,
            ticker=ticker,
            key=key,
            snapshot_hash=snapshot.content_hash,
            system=system,
            model=self._llm.model,
            hit=self._cache.get(key) is not None,
            system_tokens=_tokens(system),
            user_tokens=_tokens(user),
            cache_dir=self._cache.directory,
        )

    def _prompts(self, snapshot: FundamentalsSnapshot) -> tuple[str, str, str]:
        """(system, user, cache key) for a snapshot. The single definition."""
        system = self.get_system_prompt()
        user = self.build_user_prompt(snapshot)
        return system, user, prompt_key(self.name, self._llm.model, system, user, effort=self._effort)

    # ------------------------------------------------------------------
    # Subclass surface
    # ------------------------------------------------------------------

    def get_system_prompt(self) -> str:
        """The persona — every subclass must define its voice."""
        raise NotImplementedError(f"{type(self).__name__} must define get_system_prompt()")

    def build_snapshot(self, ticker: str, date: str, data_client: DataClient) -> FundamentalsSnapshot:
        """What this persona is allowed to know. Default: the shared
        point-in-time fundamentals snapshot — right for value/quality
        personas. Override for personas that reason over different data
        (macro, news); when a second snapshot TYPE exists, extract the
        implicit interface (ticker/as_of/content_hash/render) into a
        Protocol — not before."""
        return build_snapshot(ticker, date, data_client)

    def build_user_prompt(self, snapshot: FundamentalsSnapshot) -> str:
        """Default user prompt: the rendered snapshot. Override to enrich."""
        return snapshot.render()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _parse(self, response: str) -> dict:
        """Extract + validate {signal, confidence, reasoning}.

        The single validation point for every provider: AnalystVerdict is
        also the schema the Anthropic client sends, but the API's guarantee
        is not relied on here — a LangChain provider's answer gets the same
        check. Signal is lowercased first so a "Bullish" still parses.
        """
        data = extract_json(response)
        verdict = AnalystVerdict.model_validate({**data, "signal": str(data.get("signal", "")).lower()})
        return {
            "signal": verdict.signal,
            "confidence": verdict.confidence,
            "reasoning": verdict.reasoning,
        }

    def _to_signal(
        self,
        ticker: str,
        date: str,
        parsed: dict,
        key: str,
        snapshot: FundamentalsSnapshot,
        cached: bool,
    ) -> Signal:
        value = _SIGNAL_TO_SIGN[parsed["signal"]] * parsed["confidence"] / 100.0
        return Signal(
            model_name=self.name,
            ticker=ticker,
            date=date,
            value=value,
            reasoning=parsed["reasoning"],
            confidence=parsed["confidence"],
            snapshot_hash=snapshot.content_hash,
            filing_date=snapshot.periods[0].filing_date if snapshot.periods else None,
            metadata={
                "signal": parsed["signal"],
                "confidence": parsed["confidence"],
                "model": self._llm.model,
                "prompt_key": key,
                "snapshot_hash": snapshot.content_hash,
                "cached": cached,
                "abstained": False,
            },
        )

    def _abstain(self, ticker: str, date: str, reason: str) -> Signal:
        return Signal(
            model_name=self.name,
            ticker=ticker,
            date=date,
            value=0.0,
            reasoning=f"abstained: {reason}",
            metadata={"abstained": True, "abstain_reason": reason, "cached": False},
        )
