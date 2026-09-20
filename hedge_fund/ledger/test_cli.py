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
    rec = _record(
        tmp_path / "r.json",
        "2026-09-15",
        [_signal(s, "AAPL", "bullish", c) for s, c in (("a", 60), ("b", 70), ("c", 65))]
        + [_signal("d", "AAPL", "neutral", 40)],
    )
    out = _run(monkeypatch, capsys, ["--ledger", str(ledger), "ingest", str(rec), str(rec)])
    assert "added=4 skipped=0 abstained=0" in out.out and "added=0 skipped=4" in out.out and "4 verdicts" in out.out

    # An explicit --mandate keeps this hermetic. Without one the CLI falls
    # back to ~/.hedge-fund/mandates/, so the outcome depended on whether the
    # machine running the tests happened to have desks configured: it passed
    # locally and failed on a clean CI runner, where nothing is staffed and
    # the ranked table is therefore empty.
    mandate = tmp_path / "desk.yaml"
    mandate.write_text(
        "name: desk\nstrategies:\n  - name: s\n    models:\n"
        + "".join(f"      - name: {s}\n" for s in ("a", "b", "c", "d"))
    )
    score_args = ["--ledger", str(ledger), "scorecard", "--today", "2026-09-16", "--mandate", str(mandate)]

    out = _run(monkeypatch, capsys, [*score_args, "--horizon", "63"])
    # Nothing is old enough to score, so no school carries a verdict and
    # every one is accounted for by a coverage state instead.
    assert "|    0 |" in out.out and "Coverage —" in out.out
    assert "provisional" in out.out  # staffed by the mandate above, accumulating

    out = _run(monkeypatch, capsys, [*score_args, "--json"])
    rows = json.loads(out.out)["rows"]
    assert all(r["status"] == "-" for r in rows)
    assert all(r["coverage"] for r in rows)
    assert {"a", "b", "c", "d"} <= {r["school"] for r in rows}

    out = _run(monkeypatch, capsys, ["--ledger", str(ledger), "candidates", "--today", "2026-09-16"])
    assert out.out.startswith("Candidates for review — not orders, not advice. Rules: min_schools=3")
    assert "AAPL   | long  | consensus_long" in out.out
    assert (
        "facts lag: 10-Q filed 2026-10-30 not yet in EDGAR companyfacts; verdicts reflect the prior quarter" in out.out
    )
    assert "stale facts: AAPL" in out.out
    csv_text = (tmp_path / "ledger" / "candidates-2026-09-16.csv").read_text()
    assert "facts lag: 10-Q filed 2026-10-30" in csv_text
    assert "wrote" in out.err
