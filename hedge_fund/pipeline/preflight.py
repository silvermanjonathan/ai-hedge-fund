"""What this cycle will cost, worked out before it starts.

The guard is against SURPRISE, not against spend. Spend is already bounded:
PromptCache writes per call, so an aborted run keeps everything it paid for
and a rerun re-reasons only the tail. What is not bounded is expectation. A
week with no new filings costs nothing, and the same command the week after
a prompt edit costs full price for the entire universe — with nothing to
say why.

So this counts REAL cache misses rather than assuming every call is one. A
naive upper bound would refuse the free weekly run every week while it was
in fact free, and a gate that cries wolf gets switched off.

When there are misses it says where they came from, because "400 misses"
is not actionable and "400 misses because the system prompt changed" is:

    new name        this school has never been asked about this ticker
    new filing      asked before, but the snapshot has changed since
    prompt changed  same snapshot, different system prompt
    model changed   same snapshot, different model id
    unattributed    a miss the cache index cannot explain

Attribution reads the prompt cache, which holds the agent, ticker, model,
snapshot hash and system prompt behind every stored verdict. It is skipped
entirely when there are no misses to explain.
"""

from __future__ import annotations

import collections
import json
import logging
from dataclasses import dataclass, field

from hedge_fund.data.protocol import DataClient
from hedge_fund.llm.pricing import call_cost
from hedge_fund.signals.llm_agent import LLMAgent, Preview

logger = logging.getLogger(__name__)

NEW_NAME = "new name"
NEW_FILING = "new filing"
PROMPT_CHANGED = "prompt changed"
MODEL_CHANGED = "model changed"
UNATTRIBUTED = "unattributed"
REASON_ORDER = (NEW_NAME, NEW_FILING, PROMPT_CHANGED, MODEL_CHANGED, UNATTRIBUTED)


@dataclass
class Estimate:
    """What a cycle will cost, and why."""

    calls: int = 0  # (school, ticker) pairs that would reach a model
    hits: int = 0  # already cached; free
    misses: int = 0  # would be billed
    insufficient: int = 0  # would abstain on thin data; never billed
    quant_models: int = 0  # non-LLM models; not priced here
    cost: float | None = None  # None when a model's price is unknown
    exact: bool = True  # False => the naive upper bound (see naive())
    reasons: collections.Counter = field(default_factory=collections.Counter)
    unpriced_models: set[str] = field(default_factory=set)

    def render(self) -> str:
        basis = "measured against the prompt cache" if self.exact else "UPPER BOUND — cache not readable"
        cost = "unknown" if self.cost is None else f"${self.cost:,.2f}"
        head = f"cost estimate: {cost} — {self.misses} of {self.calls} calls would be billed ({basis})"
        lines = [head]
        if self.hits:
            lines.append(f"  {self.hits} cached (free)")
        if self.insufficient:
            lines.append(f"  {self.insufficient} would abstain on thin data (never billed)")
        for reason in REASON_ORDER:
            if self.reasons.get(reason):
                lines.append(f"  {self.reasons[reason]:>4} {reason}")
        if self.unpriced_models:
            lines.append(f"  no price listed for {', '.join(sorted(self.unpriced_models))}; cost is partial")
        return "\n".join(lines)


def naive(n_tickers: int, n_models: int, model: str) -> Estimate:
    """Upper bound: every call a miss. The fallback when the cache cannot be
    probed, labelled so it is never mistaken for the measured figure."""
    calls = n_tickers * n_models
    per = call_cost(model, system_tokens=500, user_tokens=700)
    return Estimate(
        calls=calls,
        misses=calls,
        cost=None if per is None else per * calls,
        exact=False,
        reasons=collections.Counter({UNATTRIBUTED: calls}),
        unpriced_models=set() if per is not None else {model},
    )


def estimate(fund, as_of: str, tickers: list[str], data_client: DataClient) -> Estimate:
    """Probe every (school, ticker) pair this cycle would ask about.

    Costs one snapshot build per pair against the warm data cache — sub-
    millisecond each, so a 400-pair run probes in well under a second.
    """
    est = Estimate()
    previews: list[Preview] = []
    for _, staff in fund.strategies:
        for model in staff:
            if not isinstance(model, LLMAgent):
                est.quant_models += 1
                continue
            for ticker in tickers:
                est.calls += 1
                try:
                    preview = model.preview(ticker, as_of, data_client)
                except Exception as exc:
                    # A probe must never be the reason a run does not start.
                    logger.warning("preflight: could not preview %s@%s: %s", ticker, as_of, exc)
                    est.misses += 1
                    est.reasons[UNATTRIBUTED] += 1
                    continue
                if preview.insufficient:
                    est.insufficient += 1
                elif preview.hit:
                    est.hits += 1
                else:
                    est.misses += 1
                    previews.append(preview)

    total, priced = 0.0, 0
    for preview in previews:
        per = call_cost(preview.model or "", preview.system_tokens, preview.user_tokens)
        if per is None:
            est.unpriced_models.add(preview.model or "?")
        else:
            total += per
            priced += 1
    # A number that covers none of the billed calls is worse than no number.
    est.cost = None if (previews and priced == 0) else total
    if previews:
        est.reasons.update(_attribute(previews))
    return est


def _attribute(misses: list[Preview]) -> collections.Counter:
    """Why each miss is a miss, from what the prompt cache already stores.

    Reads the cache each agent actually uses, taken from the Preview —
    never the default directory, which in a test or a non-standard install
    is somebody else's cache and would attribute against the wrong history.
    """
    directories = {m.cache_dir for m in misses if m.cache_dir is not None}
    index: dict[tuple[str, str], dict[str, tuple[str | None, str | None]]] = collections.defaultdict(dict)
    for directory in directories:
        if not directory.exists():
            continue
        for path in directory.glob("*.json"):
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            agent, ticker, snap = record.get("agent"), record.get("ticker"), record.get("snapshot_hash")
            if agent and ticker and snap:
                index[(agent, ticker)][snap] = (record.get("model"), record.get("system"))

    out: collections.Counter = collections.Counter()
    for miss in misses:
        seen = index.get((miss.school, miss.ticker))
        if not seen:
            out[NEW_NAME] += 1
        elif miss.snapshot_hash not in seen:
            out[NEW_FILING] += 1
        else:
            model, system = seen[miss.snapshot_hash]
            if system is not None and system != miss.system:
                out[PROMPT_CHANGED] += 1
            elif model is not None and model != miss.model:
                out[MODEL_CHANGED] += 1
            else:
                out[UNATTRIBUTED] += 1  # effort change, or a key we cannot explain
    return out
