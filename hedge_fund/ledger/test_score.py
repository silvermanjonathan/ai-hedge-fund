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


def _drift(ticker):
    """Exact match first, then prefix: UP0..UP24 behave like UP, so a test
    can use distinct tickers (which rule 3 requires) without naming each
    one. Anything unrecognised is flat — filler for cohort-size tests."""
    if ticker in DRIFT:
        return DRIFT[ticker]
    for name, value in DRIFT.items():
        if ticker.startswith(name):
            return value
    return 0.0


def close(ticker, d):
    return 100.0 * (1 + _drift(ticker)) ** INDEX[d]


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


def _row(school, ticker, signal, conf, event_idx, desk="d/p", key=None, basis=None, logged_at="t"):
    """*logged_at* breaks the tie when two rows share an event_date — the
    shape a prompt edit leaves, which re-asks a whole cohort on one as_of.
    *basis* stays None by default so rows that never set it keep reading as
    unknown rather than as "judged"."""
    d = TRADING[event_idx][1]
    return {
        "key": key or f"{school}|{ticker}|{event_idx}",
        "school": school,
        "ticker": ticker,
        "snapshot_hash": str(event_idx),
        "filing_date": None,
        "signal": signal,
        "basis": basis,
        "confidence": conf,
        "value": 0.0,
        "desk": desk,
        "event_date": d.isoformat(),
        "logged_at": logged_at,
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
    # Distinct tickers, not repeats of one: under rule 3 a second verdict
    # on the same name supersedes the first rather than adding a call.
    good = [_row("g", f"UP{i}", "bullish", 70, 0, key=f"g{i}") for i in range(25)]
    bad = [_row("p", f"UP{i}", "bearish", 70, 0, key=f"p{i}") for i in range(25)]
    few = [_row("f", f"UP{i}", "bullish", 70, 0, key=f"f{i}") for i in range(5)]
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
    """One bullish riser against three flat names: the bar is r_UP / 4, so
    the call beats it by three quarters of its own return. Four names is
    the smallest cohort MIN_UNIVERSE_COHORT admits."""
    rows = [_row("a", "UP", "bullish", 80, 0)] + [
        _row("a", f"FLAT{i}", "neutral", 50, 0, key=f"f{i}") for i in range(3)
    ]
    card = scorecard(_ledger(tmp_path, rows), FakeData(), _today(40), horizons=(21,), min_calls=1)
    a = _find(card, "a", 21)
    r_up = 1.01**21 - 1
    assert a.mean_vs_universe == pytest.approx(r_up - r_up / 4, rel=1e-6)
    assert a.mean_signed == pytest.approx(r_up, rel=1e-6)  # SPY is flat
    assert a.n_vs_universe == 1


@pytest.mark.parametrize("cohort", [1, 2, 3])
def test_a_cohort_below_the_floor_reports_no_universe_bar(tmp_path, cohort):
    """The dilution this prevents.

    Rows are dated by the cycle that made them and a cycle only writes rows
    for names whose snapshot changed, so over 269 weeks of real filing
    history the median cohort is ONE. At one name the bar is raw - raw = 0
    exactly; at two it is half the pairwise spread whatever the school
    said. Scoring those as 0.0 would pull every school's mean toward zero
    and compress the differences the metric exists to show, so the honest
    answer is None.
    """
    rows = [_row("a", "UP", "bullish", 80, 0)] + [
        _row("a", f"N{i}", "neutral", 50, 0, key=f"n{i}") for i in range(cohort - 1)
    ]
    card = scorecard(_ledger(tmp_path, rows), FakeData(), _today(40), horizons=(21,), min_calls=1)
    a = _find(card, "a", 21)

    assert a.mean_vs_universe is None
    assert a.n_vs_universe == 0
    assert a.n == 1, "the verdict still scores against SPY; only the universe bar is withheld"
    assert a.mean_signed is not None


def test_a_school_mixes_graded_and_ungraded_cohorts(tmp_path):
    """Real ledgers have both. The mean must cover only the graded ones,
    and n_vs_universe must say how many that was — otherwise the dilution
    is invisible rather than absent."""
    big = [_row("a", "UP", "bullish", 80, 0)] + [_row("a", f"F{i}", "neutral", 50, 0, key=f"f{i}") for i in range(3)]
    lone = [_row("a", "SOLO", "bullish", 80, 1, key="solo")]  # its own event_date
    card = scorecard(_ledger(tmp_path, big + lone), FakeData(), _today(40), horizons=(21,), min_calls=1)
    a = _find(card, "a", 21)

    assert a.n == 2  # both score against SPY
    assert a.n_vs_universe == 1  # only the four-name cohort has a bar
    assert a.mean_vs_universe is not None


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


# ---------------------------------------------------------------------------
# Rule 3 — a verdict is scored only if it stood for its whole horizon
# ---------------------------------------------------------------------------


def test_a_superseded_verdict_is_not_scored(tmp_path):
    """Grading a revised opinion measures a school on something it no
    longer held. The first call is replaced on day 1 of a 21-day horizon,
    so only the replacement scores."""
    first = _row("a", "UP", "bullish", 80, 0, key="first")
    second = _row("a", "UP", "bearish", 80, 1, key="second")
    card = scorecard(_ledger(tmp_path, [first, second]), FakeData(), _today(40), horizons=(21,), min_calls=1)
    a = _find(card, "a", 21)

    assert a.n == 1, "only the standing verdict is scored"
    # UP rises, so a bearish call on it scores negatively.
    assert a.mean_signed < 0


def test_a_verdict_that_ran_its_full_horizon_still_scores(tmp_path):
    """Supersession only disqualifies a call revised BEFORE its horizon
    ended. One replaced afterwards had a fair test and keeps its score."""
    first = _row("a", "UP", "bullish", 80, 0, key="first")
    late = _row("a", "UP", "bearish", 80, 30, key="late")  # well past 21 days
    card = scorecard(_ledger(tmp_path, [first, late]), FakeData(), _today(60), horizons=(21,), min_calls=1)
    a = _find(card, "a", 21)

    assert a.n == 2, "both stood for a full 21 days before being replaced"


def test_supersession_is_per_school_and_ticker(tmp_path):
    """One school revising its view of AAPL says nothing about another
    school, or about the same school's view of MSFT."""
    rows = [
        _row("a", "UP", "bullish", 80, 0, key="a-up-1"),
        _row("a", "UP", "bearish", 80, 1, key="a-up-2"),  # supersedes the above
        _row("a", "DOWN", "bearish", 80, 0, key="a-down"),  # untouched
        _row("b", "UP", "bullish", 80, 0, key="b-up"),  # different school
    ]
    card = scorecard(_ledger(tmp_path, rows), FakeData(), _today(40), horizons=(21,), min_calls=1)

    assert _find(card, "a", 21).n == 2  # a-up-2 and a-down
    assert _find(card, "b", 21).n == 1


def test_a_prompt_edit_does_not_double_the_sample(tmp_path):
    """The reason rule 3 exists. Ledger identity keys on the question, so
    re-asking the whole cohort under a new prompt writes a second row per
    name. Counting both would double every school's n overnight with the
    same opinions measured from two entry prices — the 'numbers that mean
    less, sooner' outcome min_calls was held at 20 to avoid."""
    before = [_row("a", f"UP{i}", "bullish", 70, 0, key=f"v1-{i}") for i in range(5)]
    after = [_row("a", f"UP{i}", "bullish", 70, 1, key=f"v2-{i}") for i in range(5)]

    only_first = scorecard(_ledger(tmp_path, before), FakeData(), _today(40), horizons=(21,), min_calls=1)
    both = scorecard(_ledger(tmp_path, before + after), FakeData(), _today(40), horizons=(21,), min_calls=1)

    assert _find(only_first, "a", 21).n == 5
    assert _find(both, "a", 21).n == 5, "a re-ask replaces the call, it does not add one"


def test_a_same_day_re_ask_does_not_count_twice_in_the_share_columns(tmp_path):
    """The gap rule 3 left. `n` was already safe, but neutral_share and
    insufficient_share counted raw rows, so a prompt edit — which re-asks
    the cohort on one as_of and writes a second row under a new identity —
    moved them with no school having changed its mind. Only the standing
    row counts, and the two columns agree with the one `n` is built from."""
    rows = [
        # UP asked twice on one day: the first answer was replaced minutes later.
        _row("a", "UP", "neutral", 50, 0, key="up-early", basis="insufficient", logged_at="00:59"),
        _row("a", "UP", "bullish", 80, 0, key="up-late", basis="judged", logged_at="02:39"),
        _row("a", "DOWN", "neutral", 50, 0, key="down", basis="judged"),
    ]
    card = scorecard(_ledger(tmp_path, rows), FakeData(), _today(40), horizons=(21,), min_calls=1)
    a = _find(card, "a", 21)

    # Over all three rows these would read 2/3 and 1/3.
    assert a.neutral_share == pytest.approx(1 / 2), "the replaced neutral is not a second opinion"
    assert a.insufficient_share == pytest.approx(0.0), "nothing a school still says is 'cannot tell'"
    assert a.n == 1, "and the scored set is the same one: up-late, DOWN being neutral"
