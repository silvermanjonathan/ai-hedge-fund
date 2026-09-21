"""Run the AI hedge fund.

Usage::

    aihf
        No arguments: the interactive app — a Textual TUI (the same app as
        `aihf` with no arguments). Build a fund — pick stocks, strategies, rebalance
        cadence — or backtest a saved fund and watch its equity curve draw
        against its benchmark.

    aihf ~/.hedge-fund/mandates/example.yaml --tickers AAPL,MSFT
        With a mandate: run one cycle non-interactively. The full CycleRecord
        prints to stdout as JSON (pipe it anywhere); a short human summary
        goes to stderr. Add --out record.json to also write it to a file.

    aihf ~/.hedge-fund/mandates/example.yaml --tickers AAPL,MSFT --backtest
        Backtest the mandate: run_cycle looped over history at the mandate's
        rebalance cadence; the full result JSON prints to stdout.

A mandate is the desk — strategies, staff, risk, capital, cadence — and never
names tickers; --tickers says what to point it at for this run.

Both paths run the same engine underneath. The interactive app is a thin
client: it only *composes a FundSpec* — the same machine-facing YAML this
CLI reads. Humans click, machines write, the engine reads one thing.
"""

from __future__ import annotations

import argparse
import logging
import os
from datetime import date as _date
from datetime import timedelta
from pathlib import Path

from rich.console import Console

from hedge_fund.backtesting import backtest_fund, DEFAULT_BACKTEST_WEEKS
from hedge_fund.brokers import SimBroker
from hedge_fund.config import apply_credentials
from hedge_fund.data import (
    data_source,
    missing_data_key,
    open_data_client,
    unsupported_model_names,
)
from hedge_fund.fund import Fund, load_spec, normalize_universe
from hedge_fund.llm import DEFAULT_MODEL
from hedge_fund.paths import ensure_mandates_dir, RECORDS_DIR
from hedge_fund.pipeline import run_cycle
from hedge_fund.pipeline.preflight import estimate, naive

logger = logging.getLogger(__name__)


def main() -> None:
    apply_credentials()
    ensure_mandates_dir()
    parser = argparse.ArgumentParser(
        prog="aihf",
        description="Run the AI hedge fund. No arguments: launch the "
        "interactive app. With a mandate YAML: run one cycle and print the "
        "record.",
    )
    parser.add_argument(
        "mandate",
        nargs="?",
        help="path to a fund spec YAML, e.g. "
        "~/.hedge-fund/mandates/example.yaml "
        "(omit to launch the interactive app)",
    )
    parser.add_argument(
        "--tickers",
        help="what to trade this run, comma or space separated, e.g. "
        "AAPL,MSFT,NVDA — required with a mandate (a fund carries no "
        "watchlist; the universe is a run-time input)",
    )
    parser.add_argument(
        "--date",
        default=_date.today().isoformat(),
        help="as-of date YYYY-MM-DD (default: today); models only see data " "filed by this date",
    )
    parser.add_argument(
        "--backtest",
        action="store_true",
        help="backtest the mandate instead of running one cycle: one run_cycle "
        "per rebalance date from --start to --date, full result JSON on stdout",
    )
    parser.add_argument(
        "--start",
        help=f"backtest start date YYYY-MM-DD (default: {DEFAULT_BACKTEST_WEEKS} weeks " "before --date)",
    )
    parser.add_argument(
        "--model",
        help="LLM the investor agents reason with, e.g. claude-fable-5-1 "
        "(default: HEDGE_FUND_LLM_MODEL env, else the built-in default); quant models "
        "ignore it",
    )
    parser.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=None,
        help="how hard Anthropic models think (default: HEDGE_FUND_LLM_EFFORT env, "
        "else high); other providers ignore it",
    )
    parser.add_argument(
        "--data",
        choices=["free", "fd"],
        default=None,
        help="market data source: free = SEC EDGAR fundamentals + Yahoo Finance "
        "prices, no data key (needs HEDGE_FUND_SEC_USER_AGENT, the SEC's required "
        "contact); fd = Financial Datasets (needs FINANCIAL_DATASETS_API_KEY) "
        "(default: HEDGE_FUND_DATA env, else free)",
    )
    parser.add_argument(
        "--refresh-data",
        action="store_true",
        help="ignore the on-disk data cache for this run and rewrite it — for when "
        "a cached answer has gone stale (raw EDGAR and Yahoo payloads within their "
        "TTL are still reused); also honoured as HEDGE_FUND_DATA_REFRESH=1",
    )
    parser.add_argument(
        "--out",
        help=f"write an EXTRA copy of the record here. Every live cycle is "
        f"already saved to {RECORDS_DIR}/ whether or not this is passed",
    )
    parser.add_argument(
        "--max-cost",
        type=float,
        default=None,
        help="refuse to start if the estimated cost of this cycle exceeds this "
        "many dollars (also HEDGE_FUND_MAX_COST). The estimate counts real "
        "prompt-cache misses, so a week with no new filings estimates at $0",
    )
    parser.add_argument(
        "--screen",
        metavar="TICKERS",
        help="the tickers the SCREEN produced for this run, comma separated. "
        "Anything in --tickers that is not here was carried: a name a school "
        "still holds a directional view on after it left the screen. The "
        "ledger marks those rows so they do not inflate the screen's universe "
        "bar. Omit it and nothing is marked carried.",
    )
    parser.add_argument(
        "--no-ledger",
        action="store_true",
        help="do not log this cycle's verdicts to the ledger. The record is "
        "still written, so `aihf-ledger ingest` can pick it up later",
    )
    args = parser.parse_args()

    if args.model:
        os.environ["HEDGE_FUND_LLM_MODEL"] = args.model
    if args.effort:
        os.environ["HEDGE_FUND_LLM_EFFORT"] = args.effort
    if args.data:
        os.environ["HEDGE_FUND_DATA"] = args.data
    if args.refresh_data:
        os.environ["HEDGE_FUND_DATA_REFRESH"] = "1"

    if args.mandate is None:
        # The interactive experience is the Textual app. Import it lazily so
        # the non-interactive path never pays to load Textual.
        from hedge_fund.tui.app import HedgeFundApp

        HedgeFundApp().run()
        return

    if not args.tickers:
        parser.error("--tickers is required with a mandate, e.g. --tickers AAPL,MSFT")
    universe = normalize_universe(args.tickers.replace(",", " ").split())

    console = Console(stderr=True)  # status + summary on stderr; stdout stays pure JSON
    spec = load_spec(args.mandate)

    # Fail before any network call: the free source cannot feed every model,
    # and a source without its credential would only die mid-cycle.
    try:
        source = data_source()
    except ValueError as exc:
        parser.error(str(exc))
    unsupported = unsupported_model_names(m.name for s in spec.strategies for m in s.models)
    if unsupported:
        parser.error(
            f"--data {source} cannot staff {', '.join(unsupported)}: the free source has "
            "no earnings history with consensus surprises. Use --data fd, or a mandate "
            "without it."
        )
    env_var = missing_data_key(source)
    if env_var:
        parser.error(f"{env_var} is not set and --data {source} needs it; export it or add it to ~/.hedge-fund/.env")

    fund = Fund(spec)

    if args.backtest:
        start = args.start or (_date.fromisoformat(args.date) - timedelta(weeks=DEFAULT_BACKTEST_WEEKS)).isoformat()
        with open_data_client() as fd:
            with console.status(
                f"[cyan]{spec.name}: backtesting {start} → {args.date} "
                f"({spec.rebalance} rebalance vs {spec.benchmark}) "
                f"over {', '.join(universe)}…",
                spinner="dots",
            ):
                result = backtest_fund(fund, start, args.date, fd, universe)
        print(result.model_dump_json(indent=2))
        if args.out:
            Path(args.out).write_text(result.model_dump_json(indent=2))
        m = result.metrics
        console.print(
            f"[bold]{spec.name}[/] {result.start} → {result.end}  ·  "
            f"{m.n_cycles} cycles  ·  return {m.total_return_pct:+.1%} "
            f"vs {spec.benchmark} {m.benchmark_return_pct:+.1%}  ·  "
            f"sharpe {m.sharpe_ratio:.2f}  ·  max drawdown {m.max_drawdown_pct:.1%}"
        )
        return

    broker = SimBroker(cash=spec.capital)

    with open_data_client() as fd:
        if not _affordable(fund, args, universe, fd, console):
            raise SystemExit(2)
        n_models = sum(len(staff) for _, staff in fund.strategies)
        with console.status(
            f"[cyan]{spec.name}: running one cycle as of {args.date} — "
            f"{len(universe)} tickers x {n_models} models "
            f"across {len(fund.strategies)} strategies…",
            spinner="dots",
        ):
            record = run_cycle(fund, args.date, broker, fd, universe)

    print(record.model_dump_json(indent=2))

    # The record is the only durable trace of a cycle, and it is the ledger's
    # input. It is written unconditionally: --out is an extra copy, not the
    # thing that decides whether the run is remembered.
    payload = record.model_dump_json(indent=2)
    RECORDS_DIR.mkdir(parents=True, exist_ok=True)
    # Same naming the TUI uses, so its history pane lists CLI runs too.
    receipt = RECORDS_DIR / f"{spec.name}-run-{record.as_of}.json"
    receipt.write_text(payload)
    if args.out:
        Path(args.out).write_text(payload)

    _log_verdicts(record, receipt, args, console)

    for sr in record.strategies:
        abstained = sum(1 for s in sr.signals if s.metadata.get("abstained") is True)
        console.print(
            f"[dim]  {sr.name} ({sr.slice:.0%} of capital): " f"{len(sr.signals)} signals ({abstained} abstained)[/]"
        )
    n_signals = sum(len(sr.signals) for sr in record.strategies)
    console.print(
        f"[bold]{spec.name}[/] @ {record.as_of}  ·  "
        f"{len(record.strategies)} strategies  ·  {n_signals} signals  ·  "
        f"{len(record.clamps)} risk clamps  ·  "
        f"{len(record.orders)} orders  ·  NAV ${record.nav:,.2f}"
    )
    if record.skipped:
        console.print(f"[dim]skipped: {', '.join(s.ticker for s in record.skipped)}[/]")


def _affordable(fund, args, universe: list[str], data_client, console: Console) -> bool:
    """Print what this cycle will cost, and stop if it is more than allowed.

    The guard is against surprise rather than spend. A cycle that dies
    part-way keeps every verdict it paid for, so the exposure was never the
    money — it was expecting $0 and getting full price because something
    invalidated a cache nobody mentioned. So the estimate counts real misses,
    and when there are any it says where they came from.
    """
    ceiling = args.max_cost
    if ceiling is None:
        raw = os.environ.get("HEDGE_FUND_MAX_COST", "").strip()
        ceiling = float(raw) if raw else None

    try:
        with console.status("[cyan]estimating cost…", spinner="dots"):
            est = estimate(fund, args.date, universe, data_client)
    except Exception as exc:
        # Never let the estimate be the reason a run does not start.
        logger.warning("preflight: falling back to the upper bound: %s", exc)
        n_models = sum(len(staff) for _, staff in fund.strategies)
        est = naive(len(universe), n_models, os.environ.get("HEDGE_FUND_LLM_MODEL", DEFAULT_MODEL))

    console.print(f"[dim]{est.render()}[/]")
    if ceiling is None or est.cost is None or est.cost <= ceiling:
        return True
    console.print(
        f"[red]refusing to start: estimated ${est.cost:,.2f} exceeds the "
        f"${ceiling:,.2f} limit. Raise it with --max-cost, or set "
        f"HEDGE_FUND_MAX_COST.[/]"
    )
    return False


def _log_verdicts(record, receipt: Path, args, console: Console) -> None:
    """Log this cycle's verdicts to the ledger, unless it would be lookahead.

    Only live cycles are logged. A cycle run with a PAST --date is a manual
    backtest of one: its forward returns are already settled, so logging it
    would put a verdict into the scorecard whose outcome was known when it
    was written. That is the same contamination the backtest path is kept
    out of the ledger to avoid, and the scorecard cannot detect it — it
    reads event_date and nothing else.

    Skipping still leaves the record on disk, so a deliberate backfill
    remains one `aihf-ledger ingest` away. Ingest is idempotent, keyed on
    (school, ticker, snapshot_hash), so re-running costs nothing.
    """
    if args.no_ledger:
        console.print(f"[dim]ledger: skipped (--no-ledger); record at {receipt}[/]")
        return
    if args.date != _date.today().isoformat():
        console.print(
            f"[dim]ledger: skipped — --date {args.date} is not today, and a verdict "
            f"whose forward return is already settled would corrupt the scorecard. "
            f"Record at {receipt}; ingest it deliberately if you meant to.[/]"
        )
        return
    try:
        from hedge_fund.ledger import Ledger

        with open_data_client() as fd:
            screen = args.screen.replace(",", " ").split() if args.screen else None
            result = Ledger().ingest(receipt, fd, screen=screen)
        console.print(f"[dim]ledger: {result}[/]")
    except Exception as exc:  # a reporting side-effect must never fail the run
        console.print(f"[yellow]ledger: could not log this cycle ({exc}); record at {receipt}[/]")


if __name__ == "__main__":
    main()
