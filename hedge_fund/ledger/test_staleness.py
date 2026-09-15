"""Facts-lag check over a fake EdgarClient — no network."""

from __future__ import annotations

from hedge_fund.ledger.rules import Candidate
from hedge_fund.ledger.staleness import (
    annotate_staleness,
    facts_lag_warning,
    latest_report_filing,
    WARNING,
)


class FakeEdgar:
    def __init__(self):
        self.ciks = {"CTSH": 1058290, "AAPL": 320193, "AMND": 7}
        self.forms = {
            1058290: [("10-Q", "2026-07-29"), ("10-Q", "2026-04-29"), ("8-K", "2026-08-01")],
            320193: [("10-Q", "2026-07-31"), ("10-K", "2025-11-01")],
            7: [("10-K/A", "2026-09-01"), ("10-K", "2026-02-20")],
        }

    def cik_for(self, ticker):
        return self.ciks.get(ticker)

    def submissions(self, cik):
        forms = self.forms[cik]
        return {"filings": {"recent": {"form": [f for f, _ in forms], "filingDate": [d for _, d in forms]}}}


def test_newer_filing_warns_with_the_exact_text():
    assert facts_lag_warning(FakeEdgar(), "CTSH", "2026-04-29") == "facts lag: 10-Q filed 2026-07-29 not yet in EDGAR companyfacts; verdicts reflect the prior quarter"
    assert WARNING.startswith("facts lag: ")


def test_same_or_newer_verdict_date_does_not_warn():
    assert facts_lag_warning(FakeEdgar(), "AAPL", "2026-07-31") is None
    assert facts_lag_warning(FakeEdgar(), "AAPL", "2026-08-15") is None


def test_unknown_ticker_and_missing_verdict_date_do_not_warn():
    assert facts_lag_warning(FakeEdgar(), "SPY", "2026-04-29") is None
    assert facts_lag_warning(FakeEdgar(), "CTSH", None) is None


def test_amendments_and_8ks_do_not_count_as_a_new_quarter():
    assert latest_report_filing(FakeEdgar(), "AMND") == ("10-K", "2026-02-20")
    assert facts_lag_warning(FakeEdgar(), "AMND", "2026-02-20") is None
    assert latest_report_filing(FakeEdgar(), "CTSH") == ("10-Q", "2026-07-29")  # the later 8-K is ignored


def test_annotate_appends_and_keeps_existing_warnings():
    stale_c = Candidate("CTSH", "long", ["consensus_long"], ["a"], [], [], 70.0, "2026-04-29", warnings=["chanos bearish at 62"])
    fresh_c = Candidate("AAPL", "long", ["consensus_long"], ["a"], [], [], 70.0, "2026-07-31")
    assert annotate_staleness([stale_c, fresh_c], FakeEdgar()) == ["CTSH"]
    assert stale_c.warnings == ["chanos bearish at 62", "facts lag: 10-Q filed 2026-07-29 not yet in EDGAR companyfacts; verdicts reflect the prior quarter"]
    assert fresh_c.warnings == []
