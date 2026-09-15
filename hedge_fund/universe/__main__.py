"""aihf-universe — print a ticker universe from a Finviz Elite screen.

    aihf-universe quality --limit 25
    aihf-universe "geo_usa,cap_midover,fa_pe_u12"

Prints a comma-separated ticker list on stdout, so it composes directly:

    poetry run aihf mandate.yaml --tickers "$(poetry run aihf-universe quality --limit 25)"

By default each ticker is checked against EDGAR and dropped if it has no CIK
or files no 10-K/10-Q (foreign private issuers on 20-F/40-F, which the free
data source cannot analyse); --limit counts kept names, so a dropped one is
replaced by the next in export order. --no-edgar-check skips that. Notes
(preset, counts, drops with reasons) go to stderr, one line each. --json
prints a JSON array instead. See hedge_fund/universe/finviz.py for the
presets and for why this never feeds the analysts directly.
"""

from __future__ import annotations

import argparse
import json
import sys

from hedge_fund.data.edgar import EdgarClient, EdgarError
from hedge_fund.tui.keys import apply_credentials
from hedge_fund.universe.edgar_check import edgar_filter
from hedge_fund.universe.finviz import fetch, FinvizError, PRESETS, resolve


def main() -> None:
    apply_credentials()
    parser = argparse.ArgumentParser(prog="aihf-universe", description="Print a ticker universe from a Finviz Elite screen (needs FINVIZ_AUTH_TOKEN).")
    parser.add_argument("universe", help=f"a preset ({', '.join(PRESETS)}) or a raw Finviz filter string")
    parser.add_argument("--limit", type=int, default=None, help="keep only the first N tickers, in export order")
    parser.add_argument("--refresh", action="store_true", help="ignore the one-day disk cache and fetch again")
    parser.add_argument("--json", action="store_true", help="print a JSON array instead of a comma-separated list")
    parser.add_argument("--no-edgar-check", action="store_true", help="skip the EDGAR post-filter that drops tickers with no CIK or no 10-K/10-Q on file")
    args = parser.parse_args()

    preset, _ = resolve(args.universe)
    try:
        screened = fetch(args.universe, limit=args.limit if args.no_edgar_check else None, refresh=args.refresh)
    except FinvizError as exc:
        sys.exit(f"aihf-universe: {exc}")

    if args.no_edgar_check:
        tickers, dropped = screened, []
    else:
        try:
            with EdgarClient() as edgar:
                tickers, dropped = edgar_filter(screened, edgar=edgar, limit=args.limit)
        except EdgarError as exc:
            sys.exit(f"aihf-universe: {exc} (or pass --no-edgar-check)")

    print(f"aihf-universe: {preset or 'custom filters'} -> {len(screened)} screened, {len(tickers)} kept, {len(dropped)} dropped", file=sys.stderr)
    for ticker, reason in dropped:
        print(f"aihf-universe: dropped {ticker}: {reason}", file=sys.stderr)
    print(json.dumps(tickers) if args.json else ",".join(tickers))


if __name__ == "__main__":
    main()
