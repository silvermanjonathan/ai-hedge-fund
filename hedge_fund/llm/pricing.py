"""What a call costs, for the pre-flight estimate. Estimates, not invoices.

List prices as of September 2026, per million tokens. They are a guide for
deciding whether to start a run, never an accounting record — a model whose
price is not listed yields no estimate rather than a wrong one, and an
unknown price must never be a reason to block a run.

These numbers were previously inlined in scripts/weekly.sh's cost summary.
One copy now.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Price:
    """Per million tokens, USD."""

    input: float
    output: float
    cache_write: float
    cache_read: float


PRICES: dict[str, Price] = {
    "claude-fable-5-1": Price(input=10.0, output=50.0, cache_write=12.5, cache_read=0.25),
}

# What an analyst answer costs on the way out. Measured over 240 real
# verdicts at --effort low (mean 206 tokens); thinking shares the output
# budget, so a higher effort spends more. Deliberately generous.
ASSUMED_OUTPUT_TOKENS = 400


def price_for(model: str) -> Price | None:
    return PRICES.get(model)


def call_cost(model: str, system_tokens: int, user_tokens: int, *, cached_system: bool = True) -> float | None:
    """One uncached call, or None when the model's price is unknown.

    The persona system prompt carries a cache breakpoint and is identical
    across every call that agent makes, so after the first it is billed at
    the cache-read rate. Input tokens are measured from the real prompt
    text; only the output side is assumed.
    """
    price = price_for(model)
    if price is None:
        return None
    system_rate = price.cache_read if cached_system else price.input
    return (
        system_tokens * system_rate / 1e6 + user_tokens * price.input / 1e6 + ASSUMED_OUTPUT_TOKENS * price.output / 1e6
    )
