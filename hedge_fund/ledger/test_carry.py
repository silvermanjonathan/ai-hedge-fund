"""Carrying held names past a screen exit."""

from __future__ import annotations

import pytest

from hedge_fund.ledger.carry import carried_tickers, holders, live_positions


def _row(school, ticker, signal, event_date="2026-10-01", logged=None):
    return {
        "key": f"{school}|{ticker}|{event_date}",
        "school": school,
        "ticker": ticker,
        "signal": signal,
        "confidence": 60.0,
        "event_date": event_date,
        "logged_at": logged or f"{event_date}T00:00:00+00:00",
        "desk": "d/s",
    }


SCHOOLS = ["akre", "fisher"]


# ---------------------------------------------------------------------------
# What qualifies
# ---------------------------------------------------------------------------


def test_a_directional_view_on_a_departed_name_is_carried():
    """The failure this fixes: a name that leaves the screen before its
    next filing is never re-asked, so the view can never flip — and the
    exits are disproportionately the names where a flip was likeliest."""
    rows = [_row("akre", "GONE", "bullish"), _row("akre", "STAYS", "bullish")]
    assert carried_tickers(rows, schools=SCHOOLS, screen=["STAYS"]) == ["GONE"]


@pytest.mark.parametrize("signal", ["neutral"])
def test_a_non_directional_view_is_not_carried(signal):
    """Nothing to flip from, and re-asking costs money to hear the school
    repeat that it has no opinion."""
    rows = [_row("akre", "GONE", signal)]
    assert carried_tickers(rows, schools=SCHOOLS, screen=[]) == []


def test_a_superseded_view_is_not_a_position():
    """Rule 3's definition of live, shared with the scorer."""
    rows = [
        _row("akre", "GONE", "bullish", "2026-10-01"),
        _row("akre", "GONE", "neutral", "2026-11-01"),  # changed its mind
    ]
    assert carried_tickers(rows, schools=SCHOOLS, screen=[]) == []


def test_a_pre_cutoff_view_does_not_qualify():
    """Views from before the cutoff were formed on snapshots since found
    wrong and prompts since replaced, and the scorecard discards them.
    Carrying on their strength would re-ask a name because of a view we
    decided not to count. DINO is the real case: three directional views
    from the Sept 15 pilot, and it does NOT qualify."""
    rows = [_row("akre", "DINO", "bullish", "2026-09-15")]
    assert carried_tickers(rows, schools=SCHOOLS, screen=[], since="2026-09-20") == []
    assert carried_tickers(rows, schools=SCHOOLS, screen=[]) == ["DINO"]  # without a cutoff


def test_a_name_still_on_the_screen_is_not_carried():
    rows = [_row("akre", "HERE", "bullish")]
    assert carried_tickers(rows, schools=SCHOOLS, screen=["here"]) == []  # case-insensitive


def test_a_returning_name_stops_being_carried():
    """The flag is recomputed from CURRENT membership every week rather
    than sticking, so a name that re-enters rejoins the screen cohort
    instead of sitting outside the universe bar on a historical exit."""
    rows = [_row("akre", "BACK", "bullish")]
    assert carried_tickers(rows, schools=SCHOOLS, screen=[]) == ["BACK"]
    assert carried_tickers(rows, schools=SCHOOLS, screen=["BACK"]) == []


def test_only_this_desk_s_schools_count():
    """A value school's holding must not drag a name into the quality run."""
    rows = [_row("klarman", "VALUEONLY", "bullish")]
    assert carried_tickers(rows, schools=SCHOOLS, screen=[]) == []
    assert carried_tickers(rows, schools=["klarman"], screen=[]) == ["VALUEONLY"]


def test_one_name_held_by_several_schools_is_carried_once():
    rows = [_row("akre", "GONE", "bullish"), _row("fisher", "GONE", "bearish")]
    assert carried_tickers(rows, schools=SCHOOLS, screen=[]) == ["GONE"]
    assert holders(rows, "GONE", schools=SCHOOLS) == ["akre", "fisher"]


def test_the_order_is_stable():
    """A run's universe should not churn for reasons unrelated to the
    screen."""
    rows = [_row("akre", t, "bullish") for t in ("ZZZ", "AAA", "MMM")]
    assert carried_tickers(rows, schools=SCHOOLS, screen=[]) == ["AAA", "MMM", "ZZZ"]


def test_live_positions_uses_logged_at_to_break_a_same_day_tie():
    rows = [
        _row("akre", "AAA", "bullish", "2026-10-01", logged="2026-10-01T02:00:00+00:00"),
        _row("akre", "AAA", "neutral", "2026-10-01", logged="2026-10-01T01:00:00+00:00"),
    ]
    assert live_positions(rows)[("akre", "AAA")]["signal"] == "bullish"
