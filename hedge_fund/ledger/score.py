"""The scorecard: per-school, per-horizon forward performance.

A bullish or bearish verdict is scored once its horizon (21, 63, or 126
trading days) has fully elapsed: excess return over SPY from the entry close
to the close of the horizon-th trading day after the event date, signed by
the call. Neutral verdicts are not scored for return; they count toward a
school's neutral share. Alongside the SPY comparison, each verdict is
measured against the equal-weight return of every ticker the same school saw
on the same desk on the same day — the fairer bar when the universe itself
was screened for quality.

Every school in hedge_fund.roster appears in the output, whether or not it
has a single call, each carrying a coverage state (see ledger/coverage.py).
That is deliberate: this scorecard exists to decide which schools keep
their seat, and it cannot do that for a school it silently omits. A ranking
of the nine schools that happen to run weekly, printed with no mention of
the other nine, reads as complete when it is not.

Two columns, two questions. `coverage` says whether the numbers may be read
as a result at all. `status` is the judgment, and is only filled in when
coverage is "scored" — "earned" when the 63-day mean is positive with a hit
rate above one half, else "probation". Everything here is information for
the user; nothing changes a registry or a mandate.
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
from hedge_fund.ledger.coverage import (
    AD_HOC,
    classify,
    LEGEND,
    PROVISIONAL,
    SCORED,
    STATE_ORDER,
    UNSTAFFED,
)
from hedge_fund.ledger.store import BENCHMARK, Ledger
from hedge_fund.roster import blocked_reason, SCHOOLS

logger = logging.getLogger(__name__)

HORIZONS = (21, 63, 126)
STATUS_HORIZON = 63
# Horizons are counted in REAL trading days: forward_closes() keeps the bars
# the market actually printed and the scorer waits for len(closes) >= h, so
# holidays and half-days need no modelling and no market-calendar
# dependency. These two constants only size the price FETCH window.
#
# The pad is constant rather than proportional because holidays are roughly
# constant per window, not per trading day. At the old flat 1.6 multiplier
# the 21-day horizon had about two trading days of margin, and a window
# spanning Thanksgiving, Christmas and New Year can eat that — leaving
# fewer than 21 bars, which makes the verdict silently unscorable rather
# than raising. ~13 trading days of margin at every horizon now.
# Smallest cohort that makes the universe bar a mean rather than a
# comparison. A verdict is graded against the equal-weight return of the
# names sharing its (school, desk, event_date) group; at one name that is
# raw - raw = 0 by construction, and at two it is half the pairwise spread
# whatever the school said. Four leaves at least three others in the
# average.
#
# This matters more than it sounds: rows are dated by the cycle that made
# them and a cycle only writes rows for names whose snapshot changed, so
# over 269 weeks of quality-screen history the MEDIAN cohort is 1 and only
# 29% of weeks clear this floor. Below it the answer is None, not 0.0 —
# zeros would pull every school's mean toward zero and compress exactly
# the differences the metric exists to show. See ARCHITECTURE.md §11.6 for
# the standing-position design that would fix the cause.
MIN_UNIVERSE_COHORT = 4

_CALENDAR_STRETCH = 1.5  # calendar days per trading day (252/365 ~ 1.45)
_CALENDAR_PAD = 14  # absorbs any holiday cluster in the window


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
    n_vs_universe: int  # verdicts whose cohort cleared MIN_UNIVERSE_COHORT
    neutral_share: float | None
    insufficient_share: float | None
    coverage: str
    status: str


@dataclass
class Scorecard:
    today: str
    min_calls: int
    horizons: tuple[int, ...]
    rows: list[ScoreRow]
    since: str | None = None

    @property
    def coverage_by_state(self) -> dict[str, list[str]]:
        """school names grouped by coverage state, every roster school once."""
        seen: dict[str, str] = {}
        for r in self.rows:
            seen[r.school] = r.coverage
        grouped: dict[str, list[str]] = {state: [] for state in STATE_ORDER}
        for school, state in sorted(seen.items()):
            grouped.setdefault(state, []).append(school)
        return grouped

    def render(self) -> str:
        lines = [
            f"Scorecard as of {self.today} (min_calls={self.min_calls}; excess over SPY, signed by the call)"
            + (f"\n  counting verdicts made on or after {self.since}" if self.since else ""),
            "",
            self._render_coverage(),
            "",
        ]

        head = f"{'school':26}| {'h':>4} | {'n':>4} | {'hit':>6} | {'mean vs SPY':>18} | {'vs universe':>12} | {'neutral':>8} | {'cant tell':>9} | {'coverage':12} | status"  # noqa: E501
        lines += [head, "-" * len(head)]
        ranked = [r for r in self.rows if r.n or r.coverage in (SCORED, PROVISIONAL)]
        for r in ranked:
            mean = (
                "-"
                if r.mean_signed is None
                else f"{r.mean_signed:+.2%}" + ("" if r.stderr is None else f" ± {r.stderr:.2%}")
            )
            lines.append(
                f"{r.school:26}| {r.horizon:>4} | {r.n:>4} | {_pct(r.hit_rate):>6} | {mean:>18} | {_pct(r.mean_vs_universe, signed=True):>12} | {_pct(r.neutral_share):>8} | {_pct(r.insufficient_share):>9} | {r.coverage:12} | {r.status}"  # noqa: E501
            )

        # Schools with no rows at all are named here rather than printed as
        # empty table rows, so the table stays readable and they still cannot
        # be mistaken for absent.
        silent = [r.school for r in self.rows if not r.n and r.coverage not in (SCORED, PROVISIONAL)]
        for school in sorted(set(silent)):
            state = next(r.coverage for r in self.rows if r.school == school)
            note = blocked_reason(school)
            detail = f" — {note.split('.')[0]}." if note else ""
            lines.append(
                f"{school:26}|    - |    - |      - |                  - |            - |        - |         - | {state:12} | not ranked{detail}"  # noqa: E501
            )  # noqa: E501
        return "\n".join(lines)

    def _render_coverage(self) -> str:
        grouped = self.coverage_by_state
        total = sum(len(v) for v in grouped.values())
        out = [f"Coverage — {total} schools in the registry"]
        for state in STATE_ORDER:
            names = grouped.get(state) or []
            shown = ", ".join(names) if names else "—"
            out.append(f"  {state:12} ({len(names):>2})  {shown}")
        out.append("")
        for state in STATE_ORDER:
            out.append(f"  {state:12} {LEGEND[state]}")
        if grouped.get(AD_HOC):
            out.append("")
            out.append(
                "  Schools marked ad-hoc are ranked below on the calls they have, but no "
                "mandate\n  scanned here staffs them, so those numbers will not grow. Do not "
                "read them\n  against a scored school."
            )
        if grouped.get(UNSTAFFED) or grouped.get("blocked"):
            out.append("")
            out.append(
                "  A school with no calls has no row in the table below. Absence here is not "
                "a poor\n  result — it is no result. Note the ledger holds only what was "
                "ingested: a school run\n  ad-hoc whose record never went through "
                "`aihf-ledger ingest` reads as unstaffed here."
            )
        return "\n".join(out)

    def to_json(self) -> str:
        return json.dumps(
            {
                "today": self.today,
                "min_calls": self.min_calls,
                "horizons": list(self.horizons),
                "since": self.since,
                "rows": [asdict(r) for r in self.rows],
            },
            indent=2,
        )


def scorecard(
    ledger: Ledger,
    data_client: DataClient,
    today: str,
    horizons: tuple[int, ...] = HORIZONS,
    min_calls: int = 20,
    staffed: set[str] | None = None,
    since: str | None = None,
) -> Scorecard:
    """Score every school in the roster, not only those with calls.

    *staffed* is the set of school names some running mandate staffs, from
    ledger.coverage.staffed_schools(). It decides provisional vs ad-hoc; the
    ledger cannot, because a school in the rotation adds no rows between
    filings. None means "unknown", which reports every school with calls as
    ad-hoc rather than inventing a rotation.
    """
    rows = ledger.rows(since)
    staffed = staffed or set()
    with_calls = {r["school"] for r in rows}
    # The roster, plus anything in the ledger the roster has since dropped —
    # a renamed or retired school must not vanish from its own history.
    schools = sorted(set(SCHOOLS) | with_calls)
    neutral_share = {s: _share([r for r in rows if r["school"] == s]) for s in schools}
    # Of everything a school said, how much was "I cannot tell from this".
    # A high number is a data problem wearing a judgment's clothes.
    insufficient_share = {s: _insufficient([r for r in rows if r["school"] == s]) for s in schools}

    # Forward closes per (ticker, event_date), fetched once per pair.
    max_h = max(horizons)
    bars_cache: dict[tuple[str, str], list] = {}

    def forward_closes(ticker: str, event_date: str) -> list[float]:
        key = (ticker, event_date)
        if key not in bars_cache:
            start = date.fromisoformat(event_date)
            span = math.ceil(max_h * _CALENDAR_STRETCH) + _CALENDAR_PAD
            end = min(start + timedelta(days=span), date.fromisoformat(today))
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
    universe_mean = {g: (sum(v) / len(v) if len(v) >= MIN_UNIVERSE_COHORT else None) for g, v in groups.items()}

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
            bar = universe_mean[(r["school"], r["desk"], r["event_date"], h)]
            vs_universe = None if bar is None else direction * (raw[(r["key"], h)] - bar)
            stats[(r["school"], h)].append((direction * excess, (r.get("confidence") or 0.0) / 100.0, vs_universe))

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
                "n_vs_universe": 0,
            }
        signed = [x[0] for x in xs]
        weights = [x[1] for x in xs]
        # Only verdicts whose cohort cleared MIN_UNIVERSE_COHORT are graded
        # against the universe; the rest have no honest bar to grade against.
        graded = [x[2] for x in xs if x[2] is not None]
        return {
            "n": n,
            "hit_rate": sum(1 for v in signed if v > 0) / n,
            "mean_signed": sum(signed) / n,
            "median_signed": statistics.median(signed),
            "conf_weighted_mean": (sum(v * w for v, w in zip(signed, weights)) / sum(weights))
            if sum(weights) > 0
            else None,
            "stderr": (statistics.stdev(signed) / math.sqrt(n)) if n >= 2 else None,
            "mean_vs_universe": (sum(graded) / len(graded)) if graded else None,
            "n_vs_universe": len(graded),
        }

    out: list[ScoreRow] = []
    for school in schools:
        anchor = summary(school, STATUS_HORIZON) if STATUS_HORIZON in horizons else None
        coverage = classify(
            school,
            n_at_anchor=anchor["n"] if anchor else 0,
            has_calls=school in with_calls,
            staffed=staffed,
            min_calls=min_calls,
        )
        for h in horizons:
            s = summary(school, h)
            # A verdict is only offered when the sample earns one. Anything
            # short of "scored" gets "-", so a thin sample cannot be read as
            # a ranking just because a word appears in the column.
            if coverage != SCORED or s["n"] < min_calls or anchor is None:
                status = "-"
            elif anchor["mean_signed"] > 0 and anchor["hit_rate"] > 0.5:
                status = "earned"
            else:
                status = "probation"
            out.append(
                ScoreRow(
                    school=school,
                    horizon=h,
                    neutral_share=neutral_share[school],
                    insufficient_share=insufficient_share[school],
                    coverage=coverage,
                    status=status,
                    **s,
                )
            )
    return Scorecard(today=today, min_calls=min_calls, horizons=tuple(horizons), rows=out, since=since)


def _share(rows: list[dict]) -> float | None:
    return (sum(1 for r in rows if r["signal"] == "neutral") / len(rows)) if rows else None


def _insufficient(rows: list[dict]) -> float | None:
    """Share of a school's verdicts that reported basis "insufficient".

    None when no row carries a basis at all — rows written before the field
    existed say nothing, and 0.0 would claim they said "judged"."""
    known = [r for r in rows if r.get("basis")]
    if not known:
        return None
    return sum(1 for r in known if r["basis"] == "insufficient") / len(known)


def _pct(v: float | None, signed: bool = False) -> str:
    if v is None:
        return "-"
    return f"{v:+.2%}" if signed else f"{v:.0%}"
