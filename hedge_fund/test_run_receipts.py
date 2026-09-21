"""A live cycle must record itself; a dated one must not reach the ledger.

Before Sept 2026 `aihf <mandate> --tickers` printed its CycleRecord to
stdout and dropped it unless --out was passed. 101 paid LLM verdicts were
lost that way: they exist as prompt-cache entries and never reached the
ledger, which is the thing built to decide which schools keep their seat.
Nothing reported the difference between a run that was logged and one that
was not.

The other half is the guard. A cycle run with a PAST --date is a manual
backtest of one, and its forward return is already settled. Logging it
would put a verdict into the scorecard whose outcome was known when it was
written — the same lookahead the backtest path is kept out of the ledger to
avoid, and one the scorecard cannot detect, because it reads event_date and
nothing else.
"""

from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from hedge_fund import run as run_mod


class Recorder:
    def __init__(self):
        self.printed = []

    def print(self, msg):
        self.printed.append(str(msg))

    @property
    def text(self):
        return "\n".join(self.printed)


def _args(**kw):
    base = {"no_ledger": False, "screen": None, "date": date.today().isoformat()}
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def receipt(tmp_path):
    p = tmp_path / "fund-run-2026-09-20.json"
    p.write_text("{}")
    return p


def test_a_live_cycle_is_logged(monkeypatch, receipt):
    """The default path: today's run reaches the ledger with no flag."""
    logged = {}

    class FakeLedger:
        def ingest(self, path, client, *, screen=None):
            logged["path"] = path
            logged["screen"] = screen
            return "added=4 skipped=0 abstained=0"

    monkeypatch.setattr("hedge_fund.ledger.Ledger", FakeLedger)
    monkeypatch.setattr(run_mod, "open_data_client", _fake_client)
    console = Recorder()
    run_mod._log_verdicts(None, receipt, _args(), console)

    assert logged["path"] == receipt
    assert "added=4" in console.text


@pytest.mark.parametrize("days_back", [1, 30, 400])
def test_a_past_dated_cycle_is_never_logged(monkeypatch, receipt, days_back):
    """The lookahead guard. A settled forward return must not enter the
    scorecard as though it were a prediction."""
    called = []

    class FakeLedger:
        def ingest(self, path, client, *, screen=None):
            called.append(path)
            return "added=1"

    monkeypatch.setattr("hedge_fund.ledger.Ledger", FakeLedger)
    monkeypatch.setattr(run_mod, "open_data_client", _fake_client)
    console = Recorder()
    past = (date.today() - timedelta(days=days_back)).isoformat()
    run_mod._log_verdicts(None, receipt, _args(date=past), console)

    assert not called, f"a cycle dated {past} was logged to the ledger"
    assert "skipped" in console.text and "already settled" in console.text
    assert str(receipt) in console.text  # the record is still recoverable


def test_no_ledger_flag_skips_but_keeps_the_record(monkeypatch, receipt):
    called = []

    class FakeLedger:
        def ingest(self, path, client, *, screen=None):
            called.append(path)

    monkeypatch.setattr("hedge_fund.ledger.Ledger", FakeLedger)
    monkeypatch.setattr(run_mod, "open_data_client", _fake_client)
    console = Recorder()
    run_mod._log_verdicts(None, receipt, _args(no_ledger=True), console)

    assert not called
    assert str(receipt) in console.text


def test_a_ledger_failure_never_fails_the_run(monkeypatch, receipt):
    """Logging is a reporting side-effect. A cycle that traded successfully
    must not report failure because a downstream write broke."""

    class Exploding:
        def ingest(self, path, client, *, screen=None):
            raise RuntimeError("disk full")

    monkeypatch.setattr("hedge_fund.ledger.Ledger", Exploding)
    monkeypatch.setattr(run_mod, "open_data_client", _fake_client)
    console = Recorder()
    run_mod._log_verdicts(None, receipt, _args(), console)  # must not raise

    assert "disk full" in console.text and str(receipt) in console.text


def test_records_dir_is_outside_the_mandates_dir():
    """Receipts and mandates are different things. FUNDS_DIR used to alias
    MANDATES_DIR, so run receipts landed among the mandate YAMLs."""
    from hedge_fund.paths import MANDATES_DIR, RECORDS_DIR

    assert RECORDS_DIR != MANDATES_DIR
    assert RECORDS_DIR.name == "records"


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _fake_client(*a, **kw):
    return _FakeClient()


def test_the_screen_is_passed_through_to_the_ledger(monkeypatch, receipt):
    """Anything in the run that was not on the screen was carried, and the
    ledger needs to know which so carried names stay out of the screen's
    universe bar."""
    seen = {}

    class FakeLedger:
        def ingest(self, path, client, *, screen=None):
            seen["screen"] = screen
            return "added=1"

    monkeypatch.setattr("hedge_fund.ledger.Ledger", FakeLedger)
    monkeypatch.setattr(run_mod, "open_data_client", _fake_client)
    run_mod._log_verdicts(None, receipt, _args(screen="AAA,BBB"), Recorder())
    assert seen["screen"] == ["AAA", "BBB"]


def test_no_screen_means_nothing_is_marked_carried(monkeypatch, receipt):
    """The honest default for a caller that cannot say."""
    seen = {}

    class FakeLedger:
        def ingest(self, path, client, *, screen=None):
            seen["screen"] = screen
            return "added=1"

    monkeypatch.setattr("hedge_fund.ledger.Ledger", FakeLedger)
    monkeypatch.setattr(run_mod, "open_data_client", _fake_client)
    run_mod._log_verdicts(None, receipt, _args(), Recorder())
    assert seen["screen"] is None
