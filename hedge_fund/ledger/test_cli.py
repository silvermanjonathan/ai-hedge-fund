"""aihf-ledger subcommands over a temp ledger and a fake data client."""

from __future__ import annotations

import json
from contextlib import contextmanager

from hedge_fund.ledger import __main__ as cli
from hedge_fund.ledger.test_store import _record, _signal, FakeData


@contextmanager
def _fake_client():
    yield FakeData()


class _FakeEdgar:
    """AAPL's latest 10-Q is newer than the 2026-07-29 filing the fake verdicts carry."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def cik_for(self, ticker):
        return 1 if ticker == "AAPL" else None

    def submissions(self, cik):
        return {"filings": {"recent": {"form": ["10-Q"], "filingDate": ["2026-10-30"]}}}


def _run(monkeypatch, capsys, argv):
    monkeypatch.setattr(cli, "apply_credentials", lambda: None)
    monkeypatch.setattr(cli, "open_data_client", _fake_client)
    monkeypatch.setattr(cli, "EdgarClient", _FakeEdgar)
    cli.main(argv)
    return capsys.readouterr()


def test_ingest_scorecard_candidates(tmp_path, monkeypatch, capsys):
    ledger = tmp_path / "ledger" / "verdicts.jsonl"
    rec = _record(tmp_path / "r.json", "2026-09-15", [_signal(s, "AAPL", "bullish", c) for s, c in (("a", 60), ("b", 70), ("c", 65))] + [_signal("d", "AAPL", "neutral", 40)])
    out = _run(monkeypatch, capsys, ["--ledger", str(ledger), "ingest", str(rec), str(rec)])
    assert "added=4 skipped=0 abstained=0" in out.out and "added=0 skipped=4" in out.out and "4 verdicts" in out.out

    out = _run(monkeypatch, capsys, ["--ledger", str(ledger), "scorecard", "--horizon", "63", "--today", "2026-09-16"])
    assert out.out.count("provisional") == 4 and "|    0 |" in out.out  # nothing old enough to score
    out = _run(monkeypatch, capsys, ["--ledger", str(ledger), "scorecard", "--json", "--today", "2026-09-16"])
    assert all(r["status"] == "provisional" and r["n"] == 0 for r in json.loads(out.out)["rows"])

    out = _run(monkeypatch, capsys, ["--ledger", str(ledger), "candidates", "--today", "2026-09-16"])
    assert out.out.startswith("Candidates for review — not orders, not advice. Rules: min_schools=3")
    assert "AAPL   | long  | consensus_long" in out.out
    assert "facts lag: 10-Q filed 2026-10-30 not yet in EDGAR companyfacts; verdicts reflect the prior quarter" in out.out
    assert "stale facts: AAPL" in out.out
    csv_text = (tmp_path / "ledger" / "candidates-2026-09-16.csv").read_text()
    assert "facts lag: 10-Q filed 2026-10-30" in csv_text
    assert "wrote" in out.err
