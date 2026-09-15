"""aihf-universe — print a ticker universe from a Finviz Elite screen.

    aihf-universe quality --limit 25
    aihf-universe "geo_usa,cap_midover,fa_pe_u12"

Prints a comma-separated ticker list on stdout, so it composes directly:

    poetry run aihf mandate.yaml --tickers "$(poetry run aihf-universe quality --limit 25)"

A one-line note (preset, row count) goes to stderr. --json prints a JSON
array instead. See hedge_fund/universe/finviz.py for the presets and for
why this never feeds the analysts directly.
"""

from __future__ import annotations

import argparse
import json
import sys

from hedge_fund.tui.keys import apply_credentials
from hedge_fund.universe.finviz import fetch, FinvizError, PRESETS, resolve


def main() -> None:
    apply_credentials()
    parser = argparse.ArgumentParser(prog="aihf-universe", description="Print a ticker universe from a Finviz Elite screen (needs FINVIZ_AUTH_TOKEN).")
    parser.add_argument("universe", help=f"a preset ({', '.join(PRESETS)}) or a raw Finviz filter string")
    parser.add_argument("--limit", type=int, default=None, help="keep only the first N tickers, in export order")
    parser.add_argument("--refresh", action="store_true", help="ignore the one-day disk cache and fetch again")
    parser.add_argument("--json", action="store_true", help="print a JSON array instead of a comma-separated list")
    args = parser.parse_args()

    preset, _ = resolve(args.universe)
    try:
        tickers = fetch(args.universe, limit=args.limit, refresh=args.refresh)
    except FinvizError as exc:
        sys.exit(f"aihf-universe: {exc}")

    print(f"aihf-universe: {preset or 'custom filters'} -> {len(tickers)} tickers", file=sys.stderr)
    print(json.dumps(tickers) if args.json else ",".join(tickers))


if __name__ == "__main__":
    main()
