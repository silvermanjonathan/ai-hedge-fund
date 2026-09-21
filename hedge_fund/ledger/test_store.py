"""Ledger ingest over synthetic CycleRecords and a fake data client — no network."""

from __future__ import annotations

import json
import logging

from hedge_fund.data.models import Price
from hedge_fund.ledger.store import Ledger


class FakeData:
    """Daily closes for any ticker: 100 + day-of-month, except tickers in `missing`."""

    def __init__(self, missing=()):
        self.missing = set(missing)
        self.calls = 0

    def get_prices(self, ticker, start_date, end_date, **kw):
        self.calls += 1
        if ticker in self.missing:
            return []
        from datetime import date, timedelta

        d, out = date.fromisoformat(start_date), []
        while d <= date.fromisoformat(end_date):
            if d.weekday() < 5:
                px = 100.0 + d.day + (5 if ticker == "SPY" else 0)
                out.append(Price(open=px, close=px, high=px, low=px, volume=1, time=f"{d.isoformat()}T00:00:00Z"))
            d += timedelta(days=1)
        return out


def _signal(school, ticker, signal, conf, snapshot="h1", filing="2026-07-29", abstained=False, prompt=None):
    if abstained:
        return {
            "model_name": school,
            "ticker": ticker,
            "date": "x",
            "value": 0.0,
            "reasoning": "abstained: insufficient data",
            "components": {},
            "metadata": {"abstained": True, "abstain_reason": "insufficient data", "cached": False},
        }
    value = {"bullish": 1, "bearish": -1, "neutral": 0}[signal] * conf / 100
    return {
        "model_name": school,
        "ticker": ticker,
        "date": "x",
        "value": value,
        "reasoning": f"{school} on {ticker}",
        "components": {},
        "confidence": conf,
        "snapshot_hash": snapshot,
        "filing_date": filing,
        "metadata": {
            "signal": signal,
            "confidence": conf,
            "model": "m",
            # In production this hashes persona + model + effort + the
            # rendered snapshot, so it moves whenever the snapshot does.
            "prompt_key": prompt or f"pk-{snapshot}",
            "snapshot_hash": snapshot,
            "cached": False,
            "abstained": False,
        },
    }


def _record(path, as_of, signals, fund="test-desk", strategy="pod"):
    path.write_text(
        json.dumps(
            {
                "fund": fund,
                "as_of": as_of,
                "strategies": [{"name": strategy, "slice": 1.0, "signals": signals, "convictions": {}, "weights": {}}],
            }
        )
    )
    return path


def test_ingest_logs_each_verdict_once_with_prices(tmp_path):
    rec = _record(
        tmp_path / "r1.json",
        "2026-09-15",
        [_signal("fisher", "AAPL", "bullish", 62), _signal("akre", "AAPL", "neutral", 45)],
    )
    ledger = Ledger(tmp_path / "verdicts.jsonl")
    result = ledger.ingest(rec, FakeData())
    assert (result.added, result.skipped, result.abstained) == (2, 0, 0)
    rows = ledger.rows()
    assert (
        rows[0]["key"] == "fisher|AAPL|pk-h1"
        and rows[0]["desk"] == "test-desk/pod"
        and rows[0]["event_date"] == "2026-09-15"
    )
    assert rows[0]["entry_close"] == 115.0 and rows[0]["spy_close"] == 120.0  # 2026-09-15 is a Tuesday
    assert rows[0]["filing_date"] == "2026-07-29" and rows[0]["thesis"] == "fisher on AAPL"
    assert Ledger(tmp_path / "verdicts.jsonl").rows() == rows  # persisted, reloadable


def test_second_ingest_of_the_same_record_adds_nothing(tmp_path):
    rec = _record(tmp_path / "r1.json", "2026-09-15", [_signal("fisher", "AAPL", "bullish", 62)])
    ledger = Ledger(tmp_path / "verdicts.jsonl")
    ledger.ingest(rec, FakeData())
    again = ledger.ingest(rec, FakeData())
    assert (again.added, again.skipped) == (0, 1) and len(ledger) == 1


def test_cache_hit_in_a_later_cycle_adds_nothing_but_a_new_filing_does(tmp_path):
    ledger = Ledger(tmp_path / "verdicts.jsonl")
    ledger.ingest(_record(tmp_path / "w1.json", "2026-09-15", [_signal("fisher", "AAPL", "bullish", 62)]), FakeData())
    same = ledger.ingest(
        _record(tmp_path / "w2.json", "2026-09-22", [_signal("fisher", "AAPL", "bullish", 62)]), FakeData()
    )
    assert (same.added, same.skipped) == (0, 1)
    new = ledger.ingest(
        _record(
            tmp_path / "w3.json",
            "2026-11-03",
            [_signal("fisher", "AAPL", "bearish", 70, snapshot="h2", filing="2026-10-30")],
        ),
        FakeData(),
    )
    assert new.added == 1
    latest = ledger.latest_per_ticker_school()[("AAPL", "fisher")]
    assert (
        latest["filing_date"] == "2026-10-30" and latest["signal"] == "bearish" and latest["event_date"] == "2026-11-03"
    )
    assert [r["event_date"] for r in ledger.rows()] == ["2026-09-15", "2026-11-03"]  # the repeat left no row


def test_abstained_counted_not_stored_and_quant_signals_skipped(tmp_path):
    quant = {
        "model_name": "pead",
        "ticker": "AAPL",
        "date": "x",
        "value": 0.3,
        "reasoning": None,
        "components": {"sue": 1.2},
        "metadata": {},
    }
    rec = _record(
        tmp_path / "r.json",
        "2026-09-15",
        [_signal("fisher", "AU", "bullish", 60, abstained=True), quant, _signal("akre", "AAPL", "bullish", 58)],
    )
    result = Ledger(tmp_path / "v.jsonl").ingest(rec, FakeData())
    assert (result.added, result.skipped, result.abstained) == (1, 1, 1)


def test_missing_price_stores_null_and_warns(tmp_path, caplog):
    rec = _record(tmp_path / "r.json", "2026-09-15", [_signal("fisher", "NOPX", "bullish", 62)])
    with caplog.at_level(logging.WARNING):
        Ledger(tmp_path / "v.jsonl").ingest(rec, FakeData(missing={"NOPX"}))
    row = Ledger(tmp_path / "v.jsonl").rows()[0]
    assert row["entry_close"] is None and row["spy_close"] == 120.0
    assert "no close for NOPX" in caplog.text


def test_legacy_record_without_signal_fields_falls_back_to_metadata(tmp_path):
    sig = _signal("fisher", "AAPL", "bullish", 62)
    for k in ("confidence", "snapshot_hash", "filing_date"):
        sig.pop(k)
    result = Ledger(tmp_path / "v.jsonl").ingest(_record(tmp_path / "r.json", "2026-09-15", [sig]), FakeData())
    row = Ledger(tmp_path / "v.jsonl").rows()[0]
    assert result.added == 1 and row["snapshot_hash"] == "h1" and row["confidence"] == 62 and row["filing_date"] is None


def test_a_prompt_change_relogs_the_same_facts():
    """The reason identity moved from snapshot_hash to prompt_key.

    A verdict is a school's answer to a question, and the question is the
    whole prompt — persona, model, effort and the rendered snapshot. Two
    answers on identical facts under different instructions are different
    events. Keying on the facts meant improving a prompt produced fresh
    verdicts that the ledger silently discarded as repeats.
    """
    import tempfile
    from pathlib import Path as _P

    tmp = _P(tempfile.mkdtemp())
    before = _record(tmp / "a.json", "2026-09-20", [_signal("fisher", "AAPL", "bullish", 70, prompt="v1")])
    after = _record(tmp / "b.json", "2026-09-27", [_signal("fisher", "AAPL", "bullish", 70, prompt="v2")])
    ledger = Ledger(tmp / "v.jsonl")

    assert ledger.ingest(before, FakeData()).added == 1
    assert ledger.ingest(after, FakeData()).added == 1, "a new question must log a new verdict"
    assert len(ledger) == 2

    rows = ledger.rows()
    assert {r["event_date"] for r in rows} == {"2026-09-20", "2026-09-27"}
    assert len({r["key"] for r in rows}) == 2


def test_an_unchanged_prompt_still_deduplicates():
    """The protection that must survive the change: a school re-asked the
    identical question between filings is a cache hit, not a new verdict."""
    import tempfile
    from pathlib import Path as _P

    tmp = _P(tempfile.mkdtemp())
    rec = _record(tmp / "a.json", "2026-09-20", [_signal("fisher", "AAPL", "bullish", 70, prompt="v1")])
    again = _record(tmp / "b.json", "2026-09-27", [_signal("fisher", "AAPL", "bullish", 70, prompt="v1")])
    ledger = Ledger(tmp / "v.jsonl")

    assert ledger.ingest(rec, FakeData()).added == 1
    result = ledger.ingest(again, FakeData())
    assert (result.added, result.skipped) == (0, 1)


def test_a_row_without_a_prompt_key_falls_back_to_the_snapshot():
    """Rows written before prompt_key existed keep working."""
    import tempfile
    from pathlib import Path as _P

    tmp = _P(tempfile.mkdtemp())
    sig = _signal("fisher", "AAPL", "bullish", 70)
    del sig["metadata"]["prompt_key"]
    ledger = Ledger(tmp / "v.jsonl")
    assert ledger.ingest(_record(tmp / "a.json", "2026-09-20", [sig]), FakeData()).added == 1
    assert ledger.rows()[0]["key"] == "fisher|AAPL|h1"
