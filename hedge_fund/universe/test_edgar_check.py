"""The EDGAR post-filter over a fake EdgarClient — no network."""

from __future__ import annotations

import sys

import pytest

from hedge_fund.universe import __main__ as cli
from hedge_fund.universe.edgar_check import edgar_filter, files_domestic_reports


class FakeEdgar:
    def __init__(self):
        self.ciks = {"AAPL": 320193, "AU": 1067428, "XOM": 2115436}
        self.forms = {320193: ["10-Q", "8-K", "10-K"], 1067428: ["6-K", "20-F", "6-K"], 2115436: ["8-K12B", "10-Q"]}
        self.lookups = 0

    def cik_for(self, ticker):
        return self.ciks.get(ticker)

    def submissions(self, cik):
        self.lookups += 1
        return {"filings": {"recent": {"form": self.forms[cik]}}}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


def test_domestic_filer_kept_foreign_and_unknown_dropped():
    edgar = FakeEdgar()
    assert files_domestic_reports(edgar, "AAPL") == (True, None)
    ok, reason = files_domestic_reports(edgar, "AU")
    assert not ok and "20-F" in reason
    ok, reason = files_domestic_reports(edgar, "SPY")
    assert not ok and "no CIK" in reason


def test_filter_keeps_order_and_replaces_dropped_names_up_to_limit():
    edgar = FakeEdgar()
    kept, dropped = edgar_filter(["AU", "AAPL", "SPY", "XOM"], edgar=edgar, limit=2)
    assert kept == ["AAPL", "XOM"]
    assert [t for t, _ in dropped] == ["AU", "SPY"]
    assert edgar.lookups == 3  # AU, AAPL, XOM; SPY has no CIK so no submissions call


def test_filter_stops_once_limit_is_filled():
    edgar = FakeEdgar()
    kept, dropped = edgar_filter(["AAPL", "XOM", "AU"], edgar=edgar, limit=2)
    assert kept == ["AAPL", "XOM"] and dropped == []
    assert edgar.lookups == 2


def _run_cli(monkeypatch, capsys, argv, edgar_factory):
    monkeypatch.setattr(cli, "apply_credentials", lambda: None)
    monkeypatch.setattr(cli, "fetch", lambda universe, limit=None, refresh=False: ["AU", "AAPL", "SPY", "XOM"][:limit])
    monkeypatch.setattr(cli, "EdgarClient", edgar_factory)
    monkeypatch.setattr(sys, "argv", ["aihf-universe", *argv])
    cli.main()
    return capsys.readouterr()


def test_cli_drops_and_reports_on_stderr(monkeypatch, capsys):
    out = _run_cli(monkeypatch, capsys, ["quality", "--limit", "2"], lambda: FakeEdgar())
    assert out.out.strip() == "AAPL,XOM"
    assert "4 screened, 2 kept, 2 dropped" in out.err
    assert "dropped AU: no 10-K/10-Q on file" in out.err and "dropped SPY: no CIK" in out.err


def test_cli_no_edgar_check_keeps_everything(monkeypatch, capsys):
    def no_client():
        raise AssertionError("EdgarClient must not be constructed with --no-edgar-check")

    out = _run_cli(monkeypatch, capsys, ["quality", "--limit", "3", "--no-edgar-check"], no_client)
    assert out.out.strip() == "AU,AAPL,SPY"
    assert "0 dropped" in out.err


def test_cli_names_the_user_agent_variable_when_missing(monkeypatch, capsys):
    from hedge_fund.data.edgar import EdgarError

    def failing():
        raise EdgarError("HEDGE_FUND_SEC_USER_AGENT is not set")

    with pytest.raises(SystemExit) as exc:
        _run_cli(monkeypatch, capsys, ["quality"], failing)
    assert "HEDGE_FUND_SEC_USER_AGENT" in str(exc.value) and "--no-edgar-check" in str(exc.value)
