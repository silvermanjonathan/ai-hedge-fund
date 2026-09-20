"""aihf-ledger — ingest cycle records, score the schools, list candidates.

    aihf-ledger ingest ~/.hedge-fund/records/quality-desk-2026-09-15.json ...
    aihf-ledger scorecard [--horizon 21|63|126|all] [--json]
    aihf-ledger candidates [--min-schools N] [--min-conf N] [--follow SCHOOL] [--follow-conf N]

Prices come through open_data_client(), the same cached data source the
desks use. Outputs are read-only: candidates and scores, never orders.
"""

from __future__ import annotations

import argparse
import sys
from datetime import date

from hedge_fund.config import apply_credentials
from hedge_fund.data import open_data_client
from hedge_fund.data.edgar import EdgarClient, EdgarError
from hedge_fund.ledger.coverage import mandate_paths, staffed_schools
from hedge_fund.ledger.rules import (
    playbook,
    PlaybookConfig,
    render_candidates,
    write_candidates_csv,
)
from hedge_fund.ledger.score import HORIZONS, scorecard
from hedge_fund.ledger.staleness import annotate_staleness
from hedge_fund.ledger.store import DEFAULT_LEDGER_PATH, Ledger


def main(argv: list[str] | None = None) -> None:
    apply_credentials()
    parser = argparse.ArgumentParser(
        prog="aihf-ledger",
        description="Verdict ledger, scorecard, and playbook. Candidates and scores only — never orders.",
    )
    parser.add_argument(
        "--ledger", default=str(DEFAULT_LEDGER_PATH), help=f"ledger file (default {DEFAULT_LEDGER_PATH})"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_ingest = sub.add_parser("ingest", help="log the new verdicts in one or more CycleRecord files")
    p_ingest.add_argument("records", nargs="+", help="record JSON files written by aihf --out")

    p_score = sub.add_parser("scorecard", help="per-school, per-horizon forward performance")
    p_score.add_argument("--horizon", default="all", choices=["21", "63", "126", "all"])
    p_score.add_argument("--min-calls", type=int, default=20)
    p_score.add_argument("--today", default=date.today().isoformat(), help="score as of this date (default today)")
    p_score.add_argument("--json", action="store_true")
    p_score.add_argument(
        "--mandate",
        action="append",
        metavar="PATH",
        help="a mandate whose staffing counts as 'in the rotation'; repeatable. "
        "Decides provisional vs ad-hoc, which the ledger cannot: a school "
        "re-reasons only when a filing changes, so one squarely in the rotation "
        "can add no rows for a quarter. Default: every mandate in "
        "~/.hedge-fund/mandates/, which answers 'could run' rather than 'does "
        "run' — pass the desks you actually run (scripts/weekly.sh does).",
    )

    p_cand = sub.add_parser("candidates", help="combine the latest verdicts per ticker into candidates for review")
    p_cand.add_argument("--min-schools", type=int, default=PlaybookConfig.min_schools)
    p_cand.add_argument("--min-conf", type=float, default=PlaybookConfig.min_conf)
    p_cand.add_argument("--follow", default=PlaybookConfig.follow)
    p_cand.add_argument("--follow-conf", type=float, default=PlaybookConfig.follow_conf)
    p_cand.add_argument("--today", default=date.today().isoformat(), help="date stamp for the CSV (default today)")
    args = parser.parse_args(argv)

    ledger = Ledger(args.ledger)

    if args.command == "ingest":
        with open_data_client() as fd:
            for path in args.records:
                result = ledger.ingest(path, fd)
                print(f"{path}: {result}")
        print(f"ledger: {len(ledger)} verdicts in {ledger.path}")
        return

    if args.command == "scorecard":
        horizons = HORIZONS if args.horizon == "all" else (int(args.horizon),)
        paths = mandate_paths(args.mandate)
        staffed = staffed_schools(paths)
        with open_data_client() as fd:
            card = scorecard(ledger, fd, args.today, horizons=horizons, min_calls=args.min_calls, staffed=staffed)
        print(card.to_json() if args.json else card.render())
        if not args.json:
            where = ", ".join(p.name for p in paths) if paths else "none found"
            print(f"\nStaffing read from: {where}", file=sys.stderr)
        return

    if args.command == "candidates":
        cfg = PlaybookConfig(
            min_schools=args.min_schools, min_conf=args.min_conf, follow=args.follow, follow_conf=args.follow_conf
        )
        candidates = playbook(ledger.latest_per_ticker_school(), cfg)
        # Are the verdicts a quarter behind the filings? Ask EDGAR submissions.
        try:
            with EdgarClient() as edgar:
                stale = annotate_staleness(candidates, edgar)
        except EdgarError as exc:
            print(f"aihf-ledger: staleness check skipped: {exc}", file=sys.stderr)
            stale = []
        print(render_candidates(candidates, cfg))
        print(f"stale facts: {', '.join(stale) if stale else 'none'}")
        out = write_candidates_csv(candidates, ledger.path.parent / f"candidates-{args.today}.csv")
        print(f"wrote {out}", file=sys.stderr)
        return


if __name__ == "__main__":
    main()
