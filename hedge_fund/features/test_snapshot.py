"""FundamentalsSnapshot tests — mocked data client, no network."""

from pathlib import Path

import pytest

from hedge_fund.data.models import CompanyFacts, FinancialMetrics
from hedge_fund.features.snapshot import build_snapshot, InsufficientData


class MockDataClient:
    """Returns canned metrics; records what it was asked for."""

    def __init__(self, metrics=None, facts=None):
        self._metrics = metrics or []
        self._facts = facts
        self.metrics_calls = []

    def get_financial_metrics(self, ticker, end_date, period="ttm", limit=10):
        self.metrics_calls.append({"ticker": ticker, "end_date": end_date, "period": period, "limit": limit})
        return self._metrics

    def get_company_facts(self, ticker):
        return self._facts


def _metric(report_period, **kwargs):
    defaults = {
        "ticker": "TEST",
        "period": "ttm",
        "filing_date": report_period,  # simplification for tests
        "return_on_equity": 0.20,
        "net_margin": 0.25,
        "gross_margin": 0.40,
        "book_value_per_share": 10.0,
        "debt_to_equity": 0.5,
        "market_cap": 1e9,
    }
    defaults.update(kwargs)
    return FinancialMetrics(report_period=report_period, **defaults)


def _history(n=8):
    """n periods, newest first, quarter-spaced."""
    quarters = [
        "2024-12-31",
        "2024-09-30",
        "2024-06-30",
        "2024-03-31",
        "2023-12-31",
        "2023-09-30",
        "2023-06-30",
        "2023-03-31",
    ]
    return [_metric(q) for q in quarters[:n]]


def test_as_of_passes_through_to_data_client():
    client = MockDataClient(metrics=_history())
    build_snapshot("TEST", "2025-01-15", client)
    call = client.metrics_calls[0]
    assert call["end_date"] == "2025-01-15"
    assert call["ticker"] == "TEST"


def test_insufficient_data_raises():
    client = MockDataClient(metrics=_history(3))  # below MIN_PERIODS
    with pytest.raises(InsufficientData):
        build_snapshot("TEST", "2025-01-15", client)


def test_aggregates():
    metrics = _history(4)
    # oldest gross margin 0.30, newest 0.40 -> trend +0.10
    metrics[-1] = _metric("2024-03-31", gross_margin=0.30)
    # BVPS oldest 8.0 -> newest 10.0 over 3 quarters (0.75y)
    metrics[-1].book_value_per_share = 8.0
    client = MockDataClient(metrics=metrics)

    snap = build_snapshot("TEST", "2025-01-15", client)

    assert snap.roe_avg == pytest.approx(0.20)
    assert snap.gross_margin_trend == pytest.approx(0.10)
    assert snap.debt_to_equity_latest == pytest.approx(0.5)
    assert snap.market_cap_latest == pytest.approx(1e9)
    assert snap.bvps_cagr == pytest.approx((10.0 / 8.0) ** (1 / 0.75) - 1, abs=1e-4)


def test_market_cap_comes_from_pit_metrics_not_facts():
    """company_facts market cap is latest-only (lookahead); the snapshot must
    use the most recent FILED metrics row instead."""
    facts = CompanyFacts(ticker="TEST", sector="Tech")
    client = MockDataClient(metrics=_history(), facts=facts)

    snap = build_snapshot("TEST", "2020-06-30", client)

    assert snap.market_cap_latest == pytest.approx(1e9)  # from metrics row
    assert snap.sector == "Tech"  # facts used only for slow-moving attributes


def test_content_hash_stable_and_sensitive():
    client_a = MockDataClient(metrics=_history())
    client_b = MockDataClient(metrics=_history())
    snap_a = build_snapshot("TEST", "2025-01-15", client_a)
    snap_b = build_snapshot("TEST", "2025-01-15", client_b)
    assert snap_a.content_hash == snap_b.content_hash  # same data -> same key

    changed = _history()
    changed[0] = _metric("2024-12-31", return_on_equity=0.35)
    snap_c = build_snapshot("TEST", "2025-01-15", MockDataClient(metrics=changed))
    assert snap_c.content_hash != snap_a.content_hash  # new filing -> new key


def test_same_data_different_as_of_same_render_and_hash():
    """Between filings the snapshot is unchanged — the hash and the rendered
    prompt must be identical on any as-of date, or the LLM cache never hits."""
    snap_jan = build_snapshot("TEST", "2025-01-15", MockDataClient(metrics=_history()))
    snap_feb = build_snapshot("TEST", "2025-02-15", MockDataClient(metrics=_history()))

    assert snap_jan.as_of != snap_feb.as_of  # the field itself still differs
    assert snap_jan.content_hash == snap_feb.content_hash
    assert snap_jan.render() == snap_feb.render()


def test_render_contains_the_facts():
    snap = build_snapshot("TEST", "2025-01-15", MockDataClient(metrics=_history()))
    text = snap.render()
    assert "2025-01-15" not in text  # as_of must never leak into the prompt
    assert "2024-12-31" in text
    assert "publicly filed" in text


# ---------------------------------------------------------------------------
# Golden prompt test — the cost model's tripwire
# ---------------------------------------------------------------------------
#
# The weekly desk run is free in a week with no new filings because an
# unchanged snapshot produces an identical prompt, which is a PromptCache hit.
# Three things have to hold for that: render() must be byte-stable, it must
# exclude as_of, and content_hash must key on the same content.
#
# The other tests in this file compare render() to itself, so a change to
# _fmt (say "%.2f" -> "%.3f") moves both sides together and they all still
# pass — while every cache entry on disk is silently orphaned and the next
# weekly run pays full price for verdicts it already owns, with no failure
# anywhere to notice. These two tests pin the output to values checked into
# the repo so that change cannot pass quietly.
#
# If you are here because one of these failed: that is the system working.
# Decide whether the prompt change is intended. If it is, regenerate the
# fixture and update the hash in the same commit, and expect the next run to
# re-reason every position from scratch.
#
#     poetry run python -m hedge_fund.features.regolden

GOLDEN_PATH = Path(__file__).resolve().parent / "snapshot_render_golden.txt"

# Pinned separately from the golden text: content_hash covers the whole
# serialized model, so adding or renaming a PeriodFundamentals field changes
# the cache key even when render() is untouched.
GOLDEN_CONTENT_HASH = "2d61a977d4bd72afcb4f5f21"


def golden_snapshot():
    """A fixed snapshot exercising every _fmt branch: billions, millions,
    plain floats, and None. Shared by the test and the regolden script, so
    the fixture can never be generated from different data than it checks."""
    rows = [
        _metric(
            "2024-12-31",
            market_cap=2.51e12,
            price_to_earnings_ratio=28.4,
            return_on_equity=0.1473,
            gross_margin=0.4412,
            operating_margin=0.3012,
            net_margin=0.2531,
            debt_to_equity=1.87,
            current_ratio=0.953,
            revenue_growth=0.0614,
            earnings_per_share=6.42,
            book_value_per_share=4.1234,
            free_cash_flow_per_share=7.0051,
        ),
        _metric("2024-09-30", market_cap=8.4e6, gross_margin=0.4390, book_value_per_share=4.05),
        _metric("2024-06-30", market_cap=1234.5, gross_margin=0.4355, book_value_per_share=3.98),
        # Nones must render as "-" and must not shift column count.
        _metric(
            "2024-03-31",
            market_cap=None,
            price_to_earnings_ratio=None,
            gross_margin=0.4301,
            book_value_per_share=3.90,
            free_cash_flow_per_share=None,
        ),
    ]
    facts = CompanyFacts(ticker="TEST", sector="Technology", industry="Consumer Electronics")
    return build_snapshot("TEST", "2025-01-15", MockDataClient(metrics=rows, facts=facts))


def test_render_matches_the_golden_prompt_byte_for_byte():
    """render() is the LLM prompt. A byte of drift orphans the whole cache."""
    actual = golden_snapshot().render()
    expected = GOLDEN_PATH.read_text()
    assert actual == expected, (
        "FundamentalsSnapshot.render() changed.\n\n"
        "Every prompt-cache entry under ~/.hedge-fund/cache/llm/ is now "
        "unreachable, and the next weekly run will pay full price to "
        "re-reason positions it already holds verdicts for.\n\n"
        "If the change is intended, regenerate the fixture and update "
        "GOLDEN_CONTENT_HASH in the same commit:\n"
        "    poetry run python -m hedge_fund.features.regolden\n"
    )


def test_content_hash_is_pinned():
    """The cache key itself, not just its self-consistency. Catches a model
    field change that render() would not show."""
    assert golden_snapshot().content_hash == GOLDEN_CONTENT_HASH, (
        "FundamentalsSnapshot.content_hash changed for fixed input. Every "
        "cached LLM verdict is now keyed to an address nothing will ask for. "
        "Regenerate deliberately if intended:\n"
        "    poetry run python -m hedge_fund.features.regolden\n"
    )


def test_golden_prompt_is_free_of_as_of():
    """The fixture itself must never contain a date that moves with the clock;
    if it did, the golden test would pass while the cache still broke daily."""
    assert "2025-01-15" not in GOLDEN_PATH.read_text()
