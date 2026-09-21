"""Who is on the bench: school names and why any of them is held back.

Import-light on purpose, like paths.py. The names live here rather than in
hedge_fund.signals because the ledger needs them and must not pay for the
LLM stack to get them: importing any signals submodule runs that package's
__init__, which constructs the agent classes and pulls in `anthropic` —
about 800ms, and it makes a read-only scorecard depend on the LLM client
working. test_roster.py keeps this list and ALPHA_MODEL_REGISTRY in step,
so the split cannot drift.
"""

from __future__ import annotations

# The quant models. Not schools; they carry no persona and no prompt.
QUANT_MODELS = ("pead",)

# Every LLM investor school in ALPHA_MODEL_REGISTRY.
SCHOOLS = (
    "akre",
    "buffett",
    "chanos",
    "dalio_resilience",
    "damodaran",
    "dreman",
    "druckenmiller",
    "earnings_quality_skeptic",
    "fisher",
    "fundsmith",
    "graham",
    "greenblatt",
    "klarman",
    "lynch",
    "munger",
    "pabrai",
    "quality_compounder",
    "schloss",
)

# Schools deliberately left unstaffed because something they need is not
# there yet. A blocked school is NOT a school that failed: it has no
# scorecard row because it was never asked, and staffing it before the
# dependency lands would produce a row that looks like evidence and is not.
#
# "Blocked" here means deferred behind a decision we have not made, not
# waiting on something outside our control. Each entry says which.
#
# name -> (what it is waiting on, which strategies already staff it)
BLOCKED: dict[str, tuple[str, tuple[str, ...]]] = {
    "druckenmiller": (
        "a price block in the snapshot — DEFERRED pending a design, not "
        "blocked on anything external. The framework is rate-of-change: is "
        "the trend inflecting, and does the multiple already say so. The "
        "point-in-time FundamentalsSnapshot carries no price series, only "
        "per-period P/E and market cap, and its own prompt concedes the "
        'gap: "You have no macro or price-action data here." '
        "\n\n"
        "An earlier version of this note said the blocker was a price-action "
        "DATA seam and pointed at swapping yfinance for Finviz. That was "
        "wrong. The prices are already fetched — DataClient.get_prices "
        "returns daily bars today — and build_snapshot simply never calls "
        "them. The yfinance/Finviz swap is a dependency change on its own "
        "merits and unblocks nothing here. "
        "\n\n"
        "What is missing is a design: which window, how to summarise a "
        "series so it does not bloat every prompt, and whether changing the "
        "snapshot for all 18 schools to serve one is worth it. The cost is "
        "known and is why this is not folded into a passing change — under "
        "the rule-3 scorer a snapshot change costs a full re-seed AND "
        "resets every in-flight horizon. See ARCHITECTURE.md §11.11 for the "
        "two branches: design the price block, or retire the school.",
        ("inflections", "fundamental-ls"),
    ),
}


def blocked_reason(school: str) -> str | None:
    """Why *school* is held back, or None if it is not."""
    entry = BLOCKED.get(school)
    return entry[0] if entry else None


def blocked_strategies(school: str) -> tuple[str, ...]:
    """Library strategies already staffing *school*, waiting on the unblock."""
    entry = BLOCKED.get(school)
    return entry[1] if entry else ()
