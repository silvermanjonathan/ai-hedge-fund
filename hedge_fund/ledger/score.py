"""The scorecard: per-school, per-horizon forward performance.

A bullish or bearish verdict is scored once its horizon (21, 63, or 126
trading days) has fully elapsed: excess return over SPY from the entry close
to the close of the horizon-th trading day after the event date, signed by
the call. Neutral verdicts are not scored for return; they count toward a
school's neutral share. Alongside the SPY comparison, each verdict is
measured against the equal-weight return of every ticker the same school saw
on the same desk on the same day — the fairer bar when the universe itself
was screened for quality.

The status column labels sample size honestly: "provisional" below
min_calls, then "earned" when the 63-day mean is positive with a hit rate
above one half, else "probation". It is information for the user; nothing
here changes a registry or a mandate.
"""

from __future__ import annotations

import json
import logging
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, timedelta

from hedge_fund.data.protocol import DataClient
from hedge_fund.ledger.store import BENCHMARK, Ledger

logger = logging.getLogger(__name__)

HORIZONS = (21, 63, 126)
STATUS_HORIZON = 63
_CALENDAR_STRETCH = 1.6  # calendar days per trading day, with slack


@dataclass
class ScoreRow:
    school: str
    horizon: int
    n: int
    hit_rate: float | None
    mean_signed: float | None
    median_signed: float | None
    conf_weighted_mean: float | None
    stderr: float | None
    mean_vs_universe: float | None
    neutral_share: float | None
    status: str


@dataclass
class Scorecard:
    today: str
    min_calls: int
    horizons: tuple[int, ...]
    rows: list[ScoreRow]

    def render(self) -> str:
        head = f"{'school':26}| {'h':>4} | {'n':>4} | {'hit':>6} | {'mean vs SPY':>18} | {'vs universe':>12} | {'neutral':>8} | status"
        lines = [
            f"Scorecard as of {self.today} (min_calls={self.min_calls}; excess over SPY, signed by the call)",
            head,
            "-" * len(head),
        ]
        for r in self.rows:
            mean = (
                "-"
                if r.mean_signed is None
                else f"{r.mean_signed:+.2%}" + ("" if r.stderr is None else f" ± {r.stderr:.2%}")
            )
            lines.append(
                f"{r.school:26}| {r.horizon:>4} | {r.n:>4} | {_pct(r.hit_rate):>6} | {mean:>18} | {_pct(r.mean_vs_universe, signed=True):>12} | {_pct(r.neutral_share):>8} | {r.status}"
            )
        return "\n".join(lines)

    def to_json(self) -> str:
        return json.dumps(
            {
                "today": self.today,
                "min_calls": self.min_calls,
                "horizons": list(self.horizons),
                "rows": [asdict(r) for r in self.rows],
            },
            indent=2,
        )


def scorecard(
    ledger: Ledger, data_client: DataClient, today: str, horizons: tuple[int, ...] = HORIZONS, min_calls: int = 20
) -> Scorecard:
    rows = ledger.rows()
    schools = sorted({r["school"] for r in rows})
    neutral_share = {s: _share([r for r in rows if r["school"] == s]) for s in schools}

    # Forward closes per (ticker, event_date), fetched once per pair.
    max_h = max(horizons)
    bars_cache: dict[tuple[str, str], list] = {}

    def forward_closes(ticker: str, event_date: str) -> list[float]:
        key = (ticker, event_date)
        if key not in bars_cache:
            start = date.fromisoformat(event_date)
            end = min(start + timedelta(days=math.ceil(max_h * _CALENDAR_STRETCH)), date.fromisoformat(today))
            try:
                bars = data_client.get_prices(ticker, start.isoformat(), end.isoformat())
            except Exception as exc:
                logger.warning("scorecard: no forward prices for %s from %s: %s", ticker, event_date, exc)
                bars = []
            after = sorted((b for b in bars if event_date < b.time[:10] <= today), key=lambda b: b.time)
            bars_cache[key] = [b.close for b in after]
        return bars_cache[key]

    # Raw forward returns for every priced row (neutral included: they define the universe bar).
    raw: dict[tuple[str, int], float] = {}  # (row key, horizon) -> close_h / entry - 1
    spy: dict[tuple[str, int], float] = {}  # (event_date, horizon) -> spy_h / spy_close - 1
    for r in rows:
        if r.get("entry_close") is None or r.get("spy_close") is None:
            continue
        closes = forward_closes(r["ticker"], r["event_date"])
        spy_closes = forward_closes(BENCHMARK, r["event_date"])
        for h in horizons:
            if len(closes) >= h and len(spy_closes) >= h:
                raw[(r["key"], h)] = closes[h - 1] / r["entry_close"] - 1
                spy[(r["event_date"], h)] = spy_closes[h - 1] / r["spy_close"] - 1

    # Universe bar: equal-weight mean raw return of every ticker the school saw on that desk that day.
    groups: dict[tuple[str, str, str, int], list[float]] = defaultdict(list)
    for r in rows:
        for h in horizons:
            if (r["key"], h) in raw:
                groups[(r["school"], r["desk"], r["event_date"], h)].append(raw[(r["key"], h)])
    universe_mean = {g: sum(v) / len(v) for g, v in groups.items()}

    stats: dict[tuple[str, int], list[tuple[float, float, float]]] = defaultdict(
        list
    )  # (signed_vs_spy, conf, signed_vs_universe)
    for r in rows:
        if r["signal"] not in ("bullish", "bearish"):
            continue
        direction = 1.0 if r["signal"] == "bullish" else -1.0
        for h in horizons:
            if (r["key"], h) not in raw:
                continue
            excess = raw[(r["key"], h)] - spy[(r["event_date"], h)]
            vs_universe = raw[(r["key"], h)] - universe_mean[(r["school"], r["desk"], r["event_date"], h)]
            stats[(r["school"], h)].append(
                (direction * excess, (r.get("confidence") or 0.0) / 100.0, direction * vs_universe)
            )

    def summary(school: str, h: int) -> dict:
        xs = stats.get((school, h), [])
        n = len(xs)
        if n == 0:
            return {
                "n": 0,
                "hit_rate": None,
                "mean_signed": None,
                "median_signed": None,
                "conf_weighted_mean": None,
                "stderr": None,
                "mean_vs_universe": None,
            }
        signed = [x[0] for x in xs]
        weights = [x[1] for x in xs]
        return {
            "n": n,
            "hit_rate": sum(1 for v in signed if v > 0) / n,
            "mean_signed": sum(signed) / n,
            "median_signed": statistics.median(signed),
            "conf_weighted_mean": (sum(v * w for v, w in zip(signed, weights)) / sum(weights))
            if sum(weights) > 0
            else None,
            "stderr": (statistics.stdev(signed) / math.sqrt(n)) if n >= 2 else None,
            "mean_vs_universe": sum(x[2] for x in xs) / n,
        }

    out: list[ScoreRow] = []
    for school in schools:
        anchor = summary(school, STATUS_HORIZON) if STATUS_HORIZON in horizons else None
        for h in horizons:
            s = summary(school, h)
            if s["n"] < min_calls or anchor is None or anchor["n"] < min_calls:
                status = "provisional"
            elif anchor["mean_signed"] > 0 and anchor["hit_rate"] > 0.5:
                status = "earned"
            else:
                status = "probation"
            out.append(ScoreRow(school=school, horizon=h, neutral_share=neutral_share[school], status=status, **s))
    return Scorecard(today=today, min_calls=min_calls, horizons=tuple(horizons), rows=out)


def _share(rows: list[dict]) -> float | None:
    return (sum(1 for r in rows if r["signal"] == "neutral") / len(rows)) if rows else None


def _pct(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+.2%}" if signed else f"{v:.0%}"
