"""v2 Pydantic models — single source of truth for all data structures in the pipeline."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Quantitative Signal Models
# ---------------------------------------------------------------------------


class Signal(BaseModel):
    """A view from an alpha model — conviction on a ticker at a point in time.

    This is the output of any AlphaModel (quant or LLM agent). The
    generator is the AlphaModel; the thing it generates is a Signal.
    """

    model_name: str = Field(description="which alpha model produced it, e.g. 'pead', 'buffett'")
    ticker: str
    date: str = Field(description="as-of date the view was formed (YYYY-MM-DD)")
    value: float = Field(description="conviction from -1.0 (bearish) to +1.0 (bullish)")
    reasoning: str | None = None  # human-readable rationale — central for LLM agents
    components: dict[str, float] = Field(default_factory=dict)  # quant decomposition
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Carried by LLM agents so a verdict can be identified and dated without the
    # prompt cache: (model_name, ticker, snapshot_hash) is a verdict's identity.
    confidence: float | None = None
    snapshot_hash: str | None = None
    filing_date: str | None = None


class QuantSignals(BaseModel):
    """All signals for a single ticker on a single date."""

    ticker: str
    date: str
    signals: dict[str, Signal] = Field(default_factory=dict)
    composite_score: float | None = None


# ---------------------------------------------------------------------------
# LLM analyst output
# ---------------------------------------------------------------------------


class AnalystVerdict(BaseModel):
    """What an LLM investor agent must answer — the schema every persona
    prompt spells out, and the one the Anthropic client sends as structured
    output. Lives here rather than in hedge_fund.signals so hedge_fund.llm
    can import it without an import cycle. A response is validated against
    it exactly once, in LLMAgent._parse.

    `basis` separates two things that both arrive as "neutral" and mean
    opposite things: a school that read the facts and declined the name,
    and one that could not tell from what it was shown. Until Sept 2026 the
    only way to distinguish them was pattern-matching the thesis text after
    the fact, which measured 5% ignorance on a sample that will grow as the
    universe widens and snapshots thin out. It defaults to "judged" so a
    provider that omits the field still validates.
    """

    signal: Literal["bullish", "bearish", "neutral"]
    confidence: float = Field(ge=0, le=100, description="0-100")
    basis: Literal["judged", "insufficient"] = Field(
        default="judged",
        description="'judged' when a view was formed from the facts shown, "
        "including a neutral one; 'insufficient' when the facts could not "
        "support a call either way",
    )
    reasoning: str
