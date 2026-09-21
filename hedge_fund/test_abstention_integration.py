"""The abstain path, end to end. It has never run in production.

Every September verdict came back clean: 100 signals, 0 abstentions. That
is the right result for integrity and the wrong one for confidence — the
abstain route has therefore never executed on real data. The clause telling
personas to go neutral on thin data, the InsufficientData route,
abstain_reason, and the ledger's deliberate skip of abstained rows are all
unexercised outside unit tests.

The widened universe makes that path likely: probing the full 56-name
quality screen already found a ticker whose snapshot cannot be built. So it
will fire for the first time unattended, on a Monday, on money.

These are integration tests, not unit tests: one abstention is followed
through blend, record, ingest and the scorecard's coverage counts, with
only the LLM and the network faked.
"""

from __future__ import annotations

import json

import pytest

from hedge_fund.brokers import SimBroker
from hedge_fund.data.models import CompanyFacts, FinancialMetrics, Price
from hedge_fund.fund.spec import Fund, FundSpec
from hedge_fund.ledger.coverage import UNSTAFFED
from hedge_fund.ledger.score import scorecard
from hedge_fund.ledger.store import Ledger
from hedge_fund.llm import PromptCache
from hedge_fund.pipeline import run_cycle
from hedge_fund.signals.llm_agent import LLMAgent

THIN = "THIN"  # too few filed periods to build a snapshot
GOOD = ["AAA", "BBB"]


class Data:
    def get_prices(self, ticker, start_date, end_date, **kw):
        return [
            Price(time=f"2026-09-{d:02d}T00:00:00Z", open=100.0, high=101.0, low=99.0, close=100.0, volume=1)
            for d in range(10, 16)
        ]

    def get_financial_metrics(self, ticker, end_date, period="ttm", limit=10):
        n = 1 if ticker == THIN else 6  # below MIN_PERIODS -> InsufficientData
        return [
            FinancialMetrics(
                ticker=ticker,
                report_period=f"2025-0{i + 1}-28",
                period="ttm",
                filing_date=f"2025-0{i + 1}-28",
                return_on_equity=0.2,
                net_margin=0.25,
                gross_margin=0.4,
                book_value_per_share=10.0 + i,
                debt_to_equity=0.5,
                market_cap=1e9,
            )
            for i in range(n)
        ]

    def get_company_facts(self, ticker):
        return CompanyFacts(ticker=ticker, sector="Tech")


class BullishLLM:
    model = "stub"

    def complete(self, system, user):
        return json.dumps({"signal": "bullish", "confidence": 80, "reasoning": "fine"})


class Agent(LLMAgent):
    def __init__(self, cache, name="tester"):
        super().__init__(llm=BullishLLM(), cache=cache)
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    def get_system_prompt(self) -> str:
        return "SYSTEM"


@pytest.fixture
def record(tmp_path):
    """One real cycle over two good tickers and one too thin to judge."""
    spec = FundSpec(
        name="desk",
        strategies=[{"name": "s", "models": [{"name": "tester"}]}],
        risk={"max_position_pct": 0.5, "max_gross_exposure": 1.0},
    )
    agent = Agent(PromptCache(tmp_path / "llm"))
    fund = Fund(spec, models={"s": [agent]})
    return run_cycle(fund, "2026-09-15", SimBroker(cash=100_000.0), Data(), [*GOOD, THIN])


# ---------------------------------------------------------------------------
# 1. The cycle: the abstention exists and is labelled
# ---------------------------------------------------------------------------


def test_the_thin_ticker_abstains_and_says_why(record):
    signals = {s.ticker: s for s in record.strategies[0].signals}
    assert set(signals) == {*GOOD, THIN}, "every requested ticker must appear"

    thin = signals[THIN]
    assert thin.value == 0.0
    assert thin.metadata["abstained"] is True
    assert "insufficient data" in thin.metadata["abstain_reason"]
    assert all(signals[t].metadata["abstained"] is False for t in GOOD)


def test_an_abstention_is_not_a_skip(record):
    """A skipped ticker had no price; an abstention had no view. Different
    things, recorded differently."""
    assert record.skipped == []


# ---------------------------------------------------------------------------
# 2. The blend: excluded from both sides, not counted as neutral
# ---------------------------------------------------------------------------


def test_the_abstention_does_not_dilute_the_book(record):
    """The property that makes abstention safe: "no opinion" must not
    average in as "opinion: neutral". Two bullish views split the gross
    target between them; the abstainer takes no share of it."""
    weights = record.strategies[0].weights
    assert weights[THIN] == 0.0
    assert weights["AAA"] == pytest.approx(0.5)
    assert weights["BBB"] == pytest.approx(0.5)
    assert sum(abs(w) for w in weights.values()) == pytest.approx(1.0)

    convictions = record.strategies[0].convictions
    assert convictions[THIN] == 0.0
    assert convictions["AAA"] == pytest.approx(0.8)


def test_no_order_is_placed_for_the_abstained_name(record):
    assert THIN not in {o.ticker for o in record.orders}
    assert THIN not in record.positions


# ---------------------------------------------------------------------------
# 3. The ledger: the abstention is skipped, and counted as skipped
# ---------------------------------------------------------------------------


def test_ingest_skips_the_abstention_without_error(tmp_path, record):
    path = tmp_path / "rec.json"
    path.write_text(record.model_dump_json())
    ledger = Ledger(tmp_path / "verdicts.jsonl")

    result = ledger.ingest(path, Data())

    assert result.abstained == 1
    assert result.added == 2  # the two real verdicts
    assert {r["ticker"] for r in ledger.rows()} == set(GOOD)
    assert THIN not in {r["ticker"] for r in ledger.rows()}


def test_reingesting_stays_idempotent_with_an_abstention_present(tmp_path, record):
    path = tmp_path / "rec.json"
    path.write_text(record.model_dump_json())
    ledger = Ledger(tmp_path / "verdicts.jsonl")
    ledger.ingest(path, Data())

    again = ledger.ingest(path, Data())

    assert again.added == 0 and again.skipped == 2 and again.abstained == 1
    assert len(ledger) == 2


# ---------------------------------------------------------------------------
# 4. The scorecard: counts stay sane
# ---------------------------------------------------------------------------


class NoPrices:
    def get_prices(self, *a, **kw):
        return []


def test_the_scorecard_counts_only_the_real_verdicts(tmp_path, record):
    path = tmp_path / "rec.json"
    path.write_text(record.model_dump_json())
    ledger = Ledger(tmp_path / "verdicts.jsonl")
    ledger.ingest(path, Data())

    card = scorecard(ledger, NoPrices(), "2026-09-16", horizons=(63,), staffed={"tester"})
    row = next(r for r in card.rows if r.school == "tester")

    # Two bullish verdicts, neither old enough to score.
    assert row.n == 0
    assert row.neutral_share == 0.0, "an abstention must not inflate neutral share"
    assert row.coverage != UNSTAFFED
    assert "Coverage —" in card.render()


def test_a_whole_desk_abstaining_produces_a_flat_book_not_a_crash(tmp_path):
    """The degenerate case run_cycle documents: every analyst abstains, so
    the targets are all zero and the fund closes to flat."""
    spec = FundSpec(
        name="desk",
        strategies=[{"name": "s", "models": [{"name": "tester"}]}],
        risk={"max_position_pct": 0.5, "max_gross_exposure": 1.0},
    )
    agent = Agent(PromptCache(tmp_path / "llm"))
    fund = Fund(spec, models={"s": [agent]})

    rec = run_cycle(fund, "2026-09-15", SimBroker(cash=100_000.0), Data(), [THIN])

    assert all(s.metadata["abstained"] for s in rec.strategies[0].signals)
    assert rec.final_weights == {THIN: 0.0}
    assert rec.orders == []
    assert rec.nav == pytest.approx(100_000.0)

    path = tmp_path / "rec.json"
    path.write_text(rec.model_dump_json())
    result = Ledger(tmp_path / "v.jsonl").ingest(path, Data())
    assert (result.added, result.abstained) == (0, 1)
