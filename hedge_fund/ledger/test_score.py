"""Scorecard math over a synthetic ledger and a fake data client with known closes."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from hedge_fund.data.models import Price
from hedge_fund.ledger.score import scorecard
from hedge_fund.ledger.store import Ledger

# Trading-day series from 2026-01-02: UP rises 1%/day, DOWN falls 1%/day, FLAT and SPY are flat.
START = date(2026, 1, 2)
DRIFT = {"UP": 0.01, "DOWN": -0.01, "FLAT": 0.0, "SPY": 0.0}


def _days():
    d, i = START, 0
    while d <= date(2026, 12, 31):
        if d.weekday() < 5:
            yield i, d
            i += 1
        d += timedelta(days=1)


TRADING = list(_days())
INDEX = {d: i for i, d in TRADING}


def close(ticker, d):
    return 100.0 * (1 + DRIFT[ticker]) ** INDEX[d]


class FakeData:
    def get_prices(self, ticker, start_date, end_date, **kw):
        s, e = date.fromisoformat(start_date), date.fromisoformat(end_date)
        return [
            Price(
                open=close(ticker, d),
                close=close(ticker, d),
                high=1,
                low=1,
                volume=1,
                time=f"{d.isoformat()}T00:00:00Z",
            )
            for i, d in TRADING
            if s <= d <= e
        ]


def _row(school, ticker, signal, conf, event_idx, desk="d/p", key=None):
    d = TRADING[event_idx][1]
    return {
        "key": key or f"{school}|{ticker}|{event_idx}",
        "school": school,
        "ticker": ticker,
        "snapshot_hash": str(event_idx),
        "filing_date": None,
        "signal": signal,
        "confidence": conf,
        "value": 0.0,
        "desk": desk,
        "event_date": d.isoformat(),
        "logged_at": "t",
        "entry_close": close(ticker, d),
        "spy_close": close("SPY", d),
        "thesis": None,
    }


def _ledger(tmp_path, rows):
    p = tmp_path / "v.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in rows))
    return Ledger(p)


def _today(idx):
    return TRADING[idx][1].isoformat()


def _find(card, school, h):
    return next(r for r in card.rows if r.school == school and r.horizon == h)


def test_bullish_and_bearish_signed_returns(tmp_path):
    rows = [
        _row("a", "UP", "bullish", 80, 0),
        _row("a", "DOWN", "bearish", 60, 0),
        _row("b", "UP", "bearish", 70, 0),
        _row("b", "DOWN", "bullish", 70, 0),
    ]
    card = scorecard(_ledger(tmp_path, rows), FakeData(), _today(40), horizons=(21,), min_calls=1)
    a, b = _find(card, "a", 21), _find(card, "b", 21)
    assert (
        a.n == 2
        and a.hit_rate == 1.0
        and a.mean_signed == pytest.approx((1.01**21 - 1 + (1 - 0.99**21)) / 2, rel=1e-6)
    )
    assert b.n == 2 and b.hit_rate == 0.0 and b.mean_signed < 0
    assert a.conf_weighted_mean == pytest.approx((0.8 * (1.01**21 - 1) + 0.6 * (1 - 0.99**21)) / 1.4, rel=1e-6)
    assert a.stderr is not None and a.median_signed is not None


def test_neutral_excluded_from_returns_but_counted_in_share(tmp_path):
    rows = [_row("a", "UP", "bullish", 80, 0), _row("a", "FLAT", "neutral", 50, 0), _row("a", "DOWN", "neutral", 50, 0)]
    card = scorecard(_ledger(tmp_path, rows), FakeData(), _today(40), horizons=(21,), min_calls=1)
    a = _find(card, "a", 21)
    assert a.n == 1 and a.neutral_share == pytest.approx(2 / 3)


def test_rows_younger_than_the_horizon_are_excluded(tmp_path):
    rows = [_row("a", "UP", "bullish", 80, 0), _row("a", "UP", "bullish", 80, 30, key="young")]
    card = scorecard(_ledger(tmp_path, rows), FakeData(), _today(40), horizons=(21, 63), min_calls=1)
    assert _find(card, "a", 21).n == 1  # the row at index 30 has only 10 trading days of history
    assert _find(card, "a", 63).n == 0 and _find(card, "a", 63).mean_signed is None


def test_unpriced_rows_are_skipped(tmp_path):
    r = _row("a", "UP", "bullish", 80, 0)
    r["entry_close"] = None
    card = scorecard(_ledger(tmp_path, [r]), FakeData(), _today(40), horizons=(21,), min_calls=1)
    row = _find(card, "a", 21)
    # No priced rows -> nothing scored, so no verdict is offered. Coverage,
    # not status, carries the reason (see ledger/coverage.py).
    assert row.n == 0 and row.status == "-"


def test_status_earned_probation_and_coverage(tmp_path):
    """status is the verdict and only exists when the sample earns one;
    coverage says whether it does."""
    good = [_row("g", "UP", "bullish", 70, i, key=f"g{i}") for i in range(25)]
    bad = [_row("p", "UP", "bearish", 70, i, key=f"p{i}") for i in range(25)]
    few = [_row("f", "UP", "bullish", 70, i, key=f"f{i}") for i in range(5)]
    card = scorecard(
        _ledger(tmp_path, good + bad + few),
        FakeData(),
        _today(120),
        horizons=(21, 63),
        min_calls=20,
        staffed={"g", "p", "f"},
    )
    assert _find(card, "g", 63).status == "earned" and _find(card, "g", 21).status == "earned"
    assert _find(card, "g", 63).coverage == "scored"
    assert _find(card, "p", 63).status == "probation" and _find(card, "p", 63).coverage == "scored"

    # Five calls is a sample, not a result: staffed and accumulating, no verdict.
    f = _find(card, "f", 63)
    assert f.n == 5 and f.coverage == "provisional" and f.status == "-"


def test_a_thin_unstaffed_sample_is_ad_hoc_not_provisional(tmp_path):
    """The distinction the coverage column exists for: 'still accumulating'
    versus 'a handful of one-off calls that will never grow'."""
    few = [_row("f", "UP", "bullish", 70, i, key=f"f{i}") for i in range(5)]
    card = scorecard(_ledger(tmp_path, few), FakeData(), _today(120), horizons=(63,), min_calls=20, staffed=set())
    assert _find(card, "f", 63).coverage == "ad-hoc"


def test_universe_relative_mean(tmp_path):
    # Same school, desk, day: UP (bullish) and FLAT (neutral).
    # Universe mean = (r_UP + 0) / 2, so UP beats it by r_UP / 2.
    rows = [_row("a", "UP", "bullish", 80, 0), _row("a", "FLAT", "neutral", 50, 0)]
    card = scorecard(_ledger(tmp_path, rows), FakeData(), _today(40), horizons=(21,), min_calls=1)
    a = _find(card, "a", 21)
    r_up = 1.01**21 - 1
    assert a.mean_vs_universe == pytest.approx(r_up / 2, rel=1e-6)
    assert a.mean_signed == pytest.approx(r_up, rel=1e-6)  # SPY is flat


def test_render_and_json(tmp_path):
    card = scorecard(
        _ledger(tmp_path, [_row("a", "UP", "bullish", 80, 0)]),
        FakeData(),
        _today(40),
        horizons=(21,),
        min_calls=20,
        staffed={"a"},
    )
    text = card.render()
    assert "provisional" in text and "a " in text
    assert "Coverage —" in text  # the summary block naming every school
    row = next(r for r in json.loads(card.to_json())["rows"] if r["school"] == "a")
    assert row["status"] == "-" and row["coverage"] in ("ad-hoc", "provisional")


def test_price_fetch_window_clears_every_horizon_with_holiday_slack():
    """The only calendar approximation in the scorer.

    Horizons themselves are counted in real bars, so holidays need no
    modelling. But the fetch window is calendar-sized, and if it is too
    short the scorer simply finds fewer than h bars and drops the verdict —
    silently unscorable, not an error. A US year has 9-10 market holidays;
    a 21-day horizon over the year-end can meet three at once.
    """
    import math

    from hedge_fund.ledger.score import _CALENDAR_PAD, _CALENDAR_STRETCH, HORIZONS

    for h in HORIZONS:
        window = math.ceil(h * _CALENDAR_STRETCH) + _CALENDAR_PAD
        needed = h * 365.25 / 252  # calendar days a horizon really spans
        slack_trading_days = (window - needed) / (365.25 / 252)
        assert slack_trading_days >= 10, (
            f"h={h}: only {slack_trading_days:.1f} trading days of slack in a "
            f"{window}-day fetch window; a holiday cluster would make verdicts "
            "at this horizon silently unscorable"
        )
