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
from hedge_fund.ledger.carry import carried_tickers, holders
from hedge_fund.ledger.coverage import mandate_paths, staffed_schools
from hedge_fund.ledger.history import (
    flips,
    render_flips,
    render_ticker_history,
    ticker_history,
)
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
    p_ingest.add_argument(
        "--screen",
        metavar="TICKERS",
        help="the tickers the screen produced, comma separated; anything else "
        "in the record was carried (see hedge_fund/ledger/carry.py)",
    )

    p_carry = sub.add_parser(
        "carried",
        help="names a desk's schools still hold a directional view on, after they left the screen",
    )
    p_carry.add_argument("--screen", required=True, metavar="TICKERS", help="this week's screen, comma separated")
    p_carry.add_argument("--school", action="append", required=True, help="a school on the desk; repeatable")
    p_carry.add_argument("--since", metavar="YYYY-MM-DD", help="ignore positions formed before this date")
    p_carry.add_argument("--explain", action="store_true", help="say which school holds each name, on stderr")

    p_score = sub.add_parser("scorecard", help="per-school, per-horizon forward performance")
    p_score.add_argument("--horizon", default="all", choices=["21", "63", "126", "all"])
    p_score.add_argument("--min-calls", type=int, default=20)
    p_score.add_argument("--today", default=date.today().isoformat(), help="score as of this date (default today)")
    p_score.add_argument("--json", action="store_true")
    p_score.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="count only verdicts made on or after this date. Use it after a "
        "universe change: older verdicts grade a school on names it will "
        "never see again, against a bar built from a different screen. "
        "Nothing is deleted; the rows stay as history.",
    )
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
    p_cand.add_argument(
        "--since",
        metavar="YYYY-MM-DD",
        help="consider only verdicts made on or after this date. This is the "
        "output that gets acted on, so the cutoff matters more here than on "
        "the scorecard: without it a name that has left the universe keeps "
        "surfacing as a candidate on a verdict from a screen that is gone.",
    )
    p_flips = sub.add_parser(
        "flips",
        help="where a school changed its mind — the most precise research lead here",
    )
    p_flips.add_argument("--since", metavar="YYYY-MM-DD", help="only changes on or after this date")
    p_flips.add_argument(
        "--include-prompt-changes",
        action="store_true",
        help="also show signal changes on an UNCHANGED filing. Those come from "
        "editing a prompt, not from the company: useful for judging an edit, "
        "misleading as a research lead, and excluded by default.",
    )

    p_tick = sub.add_parser("ticker", help="everything every school has said about one name")
    p_tick.add_argument("symbol")
    p_tick.add_argument("--no-theses", action="store_true", help="signals only, omit the reasoning")

    args = parser.parse_args(argv)

    ledger = Ledger(args.ledger)

    if args.command == "ingest":
        with open_data_client() as fd:
            screen = args.screen.replace(",", " ").split() if args.screen else None
            for path in args.records:
                result = ledger.ingest(path, fd, screen=screen)
                print(f"{path}: {result}")
        print(f"ledger: {len(ledger)} verdicts in {ledger.path}")
        return

    if args.command == "scorecard":
        horizons = HORIZONS if args.horizon == "all" else (int(args.horizon),)
        paths = mandate_paths(args.mandate)
        staffed = staffed_schools(paths)
        with open_data_client() as fd:
            card = scorecard(
                ledger,
                fd,
                args.today,
                horizons=horizons,
                min_calls=args.min_calls,
                staffed=staffed,
                since=args.since,
            )
        print(card.to_json() if args.json else card.render())
        if not args.json:
            where = ", ".join(p.name for p in paths) if paths else "none found"
            print(f"\nStaffing read from: {where}", file=sys.stderr)
        return

    if args.command == "carried":
        screen = args.screen.replace(",", " ").split()
        rows = ledger.rows()
        names = carried_tickers(rows, schools=args.school, screen=screen, since=args.since)
        print(",".join(names))  # stdout stays a clean ticker list for $(...)
        if args.explain:
            for name in names:
                who = holders(rows, name, schools=args.school, since=args.since)
                print(f"  carried {name}: held by {', '.join(who)}", file=sys.stderr)
            if not names:
                print("  nothing carried: every held name is still on the screen", file=sys.stderr)
        return

    if args.command == "flips":
        rows = ledger.rows()
        found = flips(rows, since=args.since, require_new_filing=not args.include_prompt_changes)
        hidden = (
            0
            if args.include_prompt_changes
            else len(flips(rows, since=args.since, require_new_filing=False)) - len(found)
        )
        print(render_flips(found, since=args.since, prompt_induced=hidden))
        return

    if args.command == "ticker":
        print(render_ticker_history(ticker_history(ledger.rows(), args.symbol), args.symbol, theses=not args.no_theses))
        return

    if args.command == "candidates":
        cfg = PlaybookConfig(
            min_schools=args.min_schools, min_conf=args.min_conf, follow=args.follow, follow_conf=args.follow_conf
        )
        candidates = playbook(ledger.latest_per_ticker_school(args.since), cfg)
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
