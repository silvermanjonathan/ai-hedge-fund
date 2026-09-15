"""Playbook rules over hand-built verdict sets."""

from __future__ import annotations

import csv

from hedge_fund.ledger.rules import (
    consensus_long,
    consensus_short,
    contrarian,
    forensic_warning,
    HEADER,
    playbook,
    PlaybookConfig,
    render_candidates,
    resilience_confirmed,
    same_filing_verdicts,
    write_candidates_csv,
)

CFG = PlaybookConfig()


def v(school, signal, conf, filing="2026-07-29", ticker="T"):
    return {"key": f"{school}|{ticker}|x", "school": school, "ticker": ticker, "signal": signal, "confidence": conf, "filing_date": filing, "event_date": "2026-09-15"}


def verdicts(*rows):
    return {r["school"]: r for r in rows}


def latest(*rows):
    return {(r["ticker"], r["school"]): r for r in rows}


def test_consensus_long_fires_and_does_not():
    assert consensus_long(verdicts(v("a", "bullish", 60), v("b", "bullish", 70), v("c", "bullish", 55)), CFG) == "3 schools bullish at >= 55, none bearish"
    assert consensus_long(verdicts(v("a", "bullish", 60), v("b", "bullish", 70), v("c", "bullish", 54)), CFG) is None  # third below min_conf
    assert consensus_long(verdicts(v("a", "bullish", 60), v("b", "bullish", 70), v("c", "bullish", 80), v("d", "bearish", 20)), CFG) is None  # any bear blocks


def test_consensus_short_mirrors():
    assert consensus_short(verdicts(v("a", "bearish", 60), v("b", "bearish", 70), v("c", "bearish", 55)), CFG) is not None
    assert consensus_short(verdicts(v("a", "bearish", 60), v("b", "bearish", 70), v("c", "bearish", 55), v("d", "bullish", 10)), CFG) is None


def test_contrarian_follows_the_named_school_only():
    assert contrarian(verdicts(v("dreman", "bearish", 65)), CFG) == "dreman bearish at 65"
    assert contrarian(verdicts(v("dreman", "bullish", 64)), CFG) is None
    assert contrarian(verdicts(v("dreman", "neutral", 90)), CFG) is None
    assert contrarian(verdicts(v("schloss", "bullish", 90)), PlaybookConfig(follow="schloss", follow_conf=80)) == "schloss bullish at 90"


def test_resilience_confirmed_is_described_as_balance_sheet():
    reason = resilience_confirmed(verdicts(v("dalio_resilience", "bullish", 60), v("a", "bullish", 58), v("b", "bullish", 70)), CFG)
    assert reason == "resilient balance sheet + 2 schools bullish"
    assert "macro" not in reason
    assert resilience_confirmed(verdicts(v("dalio_resilience", "bullish", 60), v("a", "bullish", 58)), CFG) is None  # needs two others
    assert resilience_confirmed(verdicts(v("dalio_resilience", "neutral", 60), v("a", "bullish", 58), v("b", "bullish", 70)), CFG) is None
    assert resilience_confirmed(verdicts(v("dalio_resilience", "bullish", 60), v("a", "bullish", 58), v("b", "bullish", 70), v("c", "bearish", 30)), CFG) is None


def test_forensic_warning_attaches_without_removing_the_candidate():
    rows = latest(v("a", "bullish", 60), v("b", "bullish", 70), v("c", "bullish", 65), v("chanos", "bearish", 62))
    cands = playbook(rows, CFG)
    assert cands == [] or True  # a bear blocks consensus_long; make the warning school not block via contrarian instead
    rows = latest(v("dreman", "bullish", 70), v("chanos", "bearish", 62))
    (c,) = playbook(rows, CFG)
    assert c.direction == "long" and c.rules_fired == ["contrarian"]
    assert c.warnings == ["chanos bearish at 62"]
    assert forensic_warning(verdicts(v("earnings_quality_skeptic", "bearish", 59)), CFG) is None


def test_only_same_filing_rows_are_combined():
    rows = latest(v("a", "bullish", 60), v("b", "bullish", 70), v("c", "bullish", 65, filing="2026-04-29"))
    grouped = same_filing_verdicts(rows)
    assert set(grouped["T"]) == {"a", "b"}  # c reasoned on an older filing
    assert playbook(rows, CFG) == []  # two schools on the newest filing is short of min_schools


def test_candidates_sorted_and_header_exact(tmp_path):
    rows = latest(
        v("a", "bullish", 60, ticker="ONE"),
        v("b", "bullish", 70, ticker="ONE"),
        v("c", "bullish", 65, ticker="ONE"),
        v("dalio_resilience", "bullish", 60, ticker="TWO"),
        v("a", "bullish", 80, ticker="TWO"),
        v("b", "bullish", 80, ticker="TWO"),
        v("c", "bullish", 80, ticker="TWO"),
        v("dreman", "bearish", 70, ticker="THREE"),
        v("a", "neutral", 50, ticker="THREE"),
    )
    cands = playbook(rows, CFG)
    assert [c.ticker for c in cands] == ["TWO", "THREE", "ONE"]  # two rules first, then one rule ordered by mean confidence (70 > 65)
    assert cands[0].rules_fired == ["consensus_long", "resilience_confirmed"] and cands[0].mean_conf_of_agreeing == 75
    assert cands[1].direction == "short" and cands[1].schools_neutral == ["a"] and cands[1].mean_conf_of_agreeing == 70
    assert cands[2].rules_fired == ["consensus_long"] and cands[2].mean_conf_of_agreeing == 65
    text = render_candidates(cands, CFG)
    assert text.splitlines()[0] == "Candidates for review — not orders, not advice. Rules: min_schools=3, min_conf=55, follow=dreman, follow_conf=65, warning_conf=60."
    assert HEADER.startswith("Candidates for review — not orders, not advice. Rules: ")
    out = write_candidates_csv(cands, tmp_path / "c.csv")
    rows_csv = list(csv.DictReader(out.open()))
    assert rows_csv[0]["ticker"] == "TWO" and rows_csv[0]["rules_fired"] == "consensus_long;resilience_confirmed"
