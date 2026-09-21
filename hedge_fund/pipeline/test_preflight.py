"""The pre-flight estimate: measured misses, and why they are misses.

The guard protects against surprise, not spend — an aborted run keeps every
verdict it paid for. What is unbounded is expectation: a week with no new
filings is free, and the same command after a prompt edit costs full price
for the whole universe with nothing to say why.

A naive upper bound cannot do that job. It would refuse the free weekly run
every week while it was in fact free, and a gate that cries wolf is a gate
that gets switched off. So these tests pin the two properties that make it
keepable: an unchanged run estimates at zero, and a miss says where it came
from.
"""

from __future__ import annotations

import pytest

from hedge_fund.data.models import CompanyFacts, FinancialMetrics
from hedge_fund.fund.spec import Fund, FundSpec
from hedge_fund.llm import PromptCache
from hedge_fund.llm.pricing import call_cost, PRICES
from hedge_fund.pipeline.preflight import (
    estimate,
    MODEL_CHANGED,
    naive,
    NEW_FILING,
    NEW_NAME,
    PROMPT_CHANGED,
)
from hedge_fund.signals.llm_agent import LLMAgent

MODEL = next(iter(PRICES))


class StubLLM:
    def __init__(self, model=MODEL):
        self.model = model

    def complete(self, system, user):
        raise AssertionError("the estimate must never call a model")


class Agent(LLMAgent):
    @property
    def name(self) -> str:
        return "tester"

    def get_system_prompt(self) -> str:
        return self._prompt

    def __init__(self, cache, prompt="SYSTEM PROMPT v1", model=MODEL):
        super().__init__(llm=StubLLM(model), cache=cache)
        self._prompt = prompt


class Data:
    """Enough filed history for a snapshot; SPARSE tickers get too little."""

    SPARSE = {"THIN"}

    def get_financial_metrics(self, ticker, end_date, period="ttm", limit=10):
        n = 1 if ticker in self.SPARSE else 6
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


def _fund(agent):
    spec = FundSpec(
        name="f",
        strategies=[{"name": "s", "models": [{"name": "tester"}]}],
        risk={"max_position_pct": 0.25, "max_gross_exposure": 1.0},
    )
    return Fund(spec, models={"s": [agent]})


def _prime(agent, tickers, data):
    """Fill the prompt cache as a completed run would have."""
    for t in tickers:
        p = agent.preview(t, "2026-01-05", data)
        if p.key:
            agent._cache.put(
                p.key,
                {
                    "agent": p.school,
                    "ticker": t,
                    "model": p.model,
                    "snapshot_hash": p.snapshot_hash,
                    "system": p.system,
                    "user": "u",
                    "response": "r",
                    "parsed": {},
                },
            )


# ---------------------------------------------------------------------------
# The property that makes the gate keepable
# ---------------------------------------------------------------------------


def test_a_fully_cached_cycle_estimates_at_zero(tmp_path):
    """The weekly run with no new filings. A naive bound would refuse this
    every week while it was free, and get switched off within a month."""
    agent = Agent(PromptCache(tmp_path / "llm"))
    data = Data()
    _prime(agent, ["AAA", "BBB"], data)

    est = estimate(_fund(agent), "2026-01-05", ["AAA", "BBB"], data)

    assert est.cost == 0.0
    assert (est.hits, est.misses, est.calls) == (2, 0, 2)
    assert est.exact and "0 of 2 calls would be billed" in est.render()


def test_the_estimate_never_calls_a_model(tmp_path):
    """StubLLM.complete raises; reaching it fails the test."""
    agent = Agent(PromptCache(tmp_path / "llm"))
    estimate(_fund(agent), "2026-01-05", ["AAA", "BBB", "CCC"], Data())


# ---------------------------------------------------------------------------
# Attribution: "400 misses" is not actionable, "400 because the prompt
# changed" is
# ---------------------------------------------------------------------------


def test_a_new_ticker_is_attributed_to_a_new_name(tmp_path):
    agent = Agent(PromptCache(tmp_path / "llm"))
    data = Data()
    _prime(agent, ["AAA"], data)

    est = estimate(_fund(agent), "2026-01-05", ["AAA", "NEW"], data)

    assert est.hits == 1 and est.misses == 1
    assert est.reasons[NEW_NAME] == 1


def test_a_changed_system_prompt_is_attributed_to_the_prompt(tmp_path):
    """The case that motivated attribution: the whole universe goes from
    free to full price and the output must say why."""
    cache = PromptCache(tmp_path / "llm")
    data = Data()
    _prime(Agent(cache, prompt="SYSTEM PROMPT v1"), ["AAA", "BBB"], data)

    est = estimate(_fund(Agent(cache, prompt="SYSTEM PROMPT v2")), "2026-01-05", ["AAA", "BBB"], data)

    assert est.misses == 2 and est.hits == 0
    assert est.reasons[PROMPT_CHANGED] == 2
    assert "prompt changed" in est.render()


def test_a_changed_model_is_attributed_to_the_model(tmp_path):
    cache = PromptCache(tmp_path / "llm")
    data = Data()
    _prime(Agent(cache, model=MODEL), ["AAA"], data)

    other = Agent(cache, model="some-other-model")
    est = estimate(_fund(other), "2026-01-05", ["AAA"], data)

    assert est.misses == 1
    assert est.reasons[MODEL_CHANGED] == 1
    # An unlisted model yields no number rather than a wrong one.
    assert est.cost is None and "some-other-model" in est.unpriced_models


def test_a_changed_snapshot_is_attributed_to_a_new_filing(tmp_path, monkeypatch):
    cache = PromptCache(tmp_path / "llm")
    data = Data()
    agent = Agent(cache)
    _prime(agent, ["AAA"], data)

    class Moved(Data):
        def get_financial_metrics(self, ticker, end_date, period="ttm", limit=10):
            rows = super().get_financial_metrics(ticker, end_date, period, limit)
            rows[0].return_on_equity = 0.99  # a new filing changed the facts
            return rows

    est = estimate(_fund(Agent(cache)), "2026-01-05", ["AAA"], Moved())

    assert est.misses == 1
    assert est.reasons[NEW_FILING] == 1


# ---------------------------------------------------------------------------
# Edges
# ---------------------------------------------------------------------------


def test_a_thin_snapshot_is_counted_as_free_not_billed(tmp_path):
    """InsufficientData abstains without calling a model, so it must not
    appear in the bill."""
    agent = Agent(PromptCache(tmp_path / "llm"))
    est = estimate(_fund(agent), "2026-01-05", ["AAA", "THIN"], Data())

    assert est.insufficient == 1
    assert est.misses == 1  # AAA only
    assert "abstain on thin data" in est.render()


def test_a_broken_probe_never_blocks_a_run(tmp_path):
    """A cost estimate must not be the reason a cycle does not start."""

    class Broken(Data):
        def get_financial_metrics(self, *a, **k):
            raise RuntimeError("data layer down")

    agent = Agent(PromptCache(tmp_path / "llm"))
    est = estimate(_fund(agent), "2026-01-05", ["AAA"], Broken())
    assert est.misses == 1  # counted, conservatively, as billable


def test_the_naive_fallback_is_labelled_as_a_bound():
    est = naive(n_tickers=10, n_models=4, model=MODEL)
    assert est.calls == est.misses == 40
    assert not est.exact
    assert "UPPER BOUND" in est.render()


def test_an_unpriced_model_yields_no_number():
    assert call_cost("not-a-real-model", 100, 100) is None


@pytest.mark.parametrize("cached_system", [True, False])
def test_cost_scales_with_the_measured_prompt(cached_system):
    small = call_cost(MODEL, 100, 100, cached_system=cached_system)
    large = call_cost(MODEL, 100, 10_000, cached_system=cached_system)
    assert large > small


def test_pricing_has_no_second_copy_in_the_weekly_script():
    """scripts/weekly.sh used to inline the same rates. One copy."""
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "weekly.sh"
    if script.exists():
        body = script.read_text()
        assert "hedge_fund.llm.pricing" in body or "cost:" not in body, (
            "weekly.sh still computes cost from inlined rates; point it at " "hedge_fund.llm.pricing instead"
        )
