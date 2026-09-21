"""Flips and per-ticker history: pure readers over the ledger."""

from __future__ import annotations

import pytest

from hedge_fund.ledger.history import (
    flips,
    render_flips,
    render_ticker_history,
    ticker_history,
)


def _row(school, ticker, signal, conf, event_date, filing, logged=None, thesis="t", carried=False):
    return {
        "key": f"{school}|{ticker}|{event_date}",
        "school": school,
        "ticker": ticker,
        "signal": signal,
        "confidence": conf,
        "basis": "judged",
        "filing_date": filing,
        "event_date": event_date,
        "logged_at": logged or f"{event_date}T00:00:00+00:00",
        "desk": "d/s",
        "thesis": thesis,
        "carried": carried,
    }


# ---------------------------------------------------------------------------
# A flip means the FACTS changed
# ---------------------------------------------------------------------------


def test_a_new_filing_that_changes_the_view_is_a_flip():
    rows = [
        _row("akre", "AAA", "bullish", 70, "2026-09-01", "2026-08-01"),
        _row("akre", "AAA", "bearish", 65, "2026-11-01", "2026-10-30"),
    ]
    found = flips(rows)
    assert len(found) == 1
    f = found[0]
    assert (f.before.signal, f.after.signal) == ("bullish", "bearish")
    assert f.reversal and f.filing_changed


def test_the_same_filing_with_a_changed_view_is_not_a_lead():
    """The defect this guards, found on the reader's first run.

    A school re-reasons whenever the QUESTION changes, and the question
    includes the prompt. The Sept 2026 re-seed re-asked every school under
    an edited prompt on identical filings, and the first version of this
    reader reported all 59 resulting changes as flips. Following them would
    have meant chasing our own prompt edit through twenty companies.
    """
    rows = [
        _row("akre", "AAA", "bullish", 70, "2026-09-15", "2026-08-01"),
        _row("akre", "AAA", "neutral", 50, "2026-09-20", "2026-08-01"),  # same filing
    ]
    assert flips(rows) == []
    assert len(flips(rows, require_new_filing=False)) == 1


def test_prompt_induced_changes_are_counted_and_named_in_the_output():
    rows = [
        _row("akre", "AAA", "bullish", 70, "2026-09-15", "2026-08-01"),
        _row("akre", "AAA", "neutral", 50, "2026-09-20", "2026-08-01"),
    ]
    real = flips(rows)
    hidden = len(flips(rows, require_new_filing=False)) - len(real)
    text = render_flips(real, prompt_induced=hidden)
    assert "none" in text
    assert "prompt edit rather than a" in text, "the reader must say why it is quiet"


def test_restating_the_same_signal_is_not_a_flip():
    """Confidence moving is not changing your mind."""
    rows = [
        _row("akre", "AAA", "bullish", 70, "2026-09-01", "2026-08-01"),
        _row("akre", "AAA", "bullish", 40, "2026-11-01", "2026-10-30"),
    ]
    assert flips(rows) == []


def test_reversals_are_marked_and_sorted_ahead_of_drifts_through_neutral():
    rows = [
        _row("akre", "AAA", "bullish", 70, "2026-09-01", "2026-08-01"),
        _row("akre", "AAA", "bearish", 65, "2026-11-01", "2026-10-30"),
        _row("fisher", "BBB", "bullish", 70, "2026-09-01", "2026-08-01"),
        _row("fisher", "BBB", "neutral", 50, "2026-11-01", "2026-10-30"),
    ]
    found = flips(rows)
    assert [f.reversal for f in found] == [True, False]
    assert "!!" in found[0].render() and "!!" not in found[1].render()


def test_since_filters_by_the_date_of_the_change():
    rows = [
        _row("akre", "AAA", "bullish", 70, "2026-09-01", "2026-08-01"),
        _row("akre", "AAA", "bearish", 65, "2026-10-01", "2026-09-28"),
    ]
    assert len(flips(rows, since="2026-09-15")) == 1
    assert flips(rows, since="2026-11-01") == []


def test_a_carried_name_is_labelled_as_carried():
    """Forward-compatible with carrying held names past a screen exit: the
    column reads 'screen' until the flag exists."""
    rows = [
        _row("akre", "AAA", "bullish", 70, "2026-09-01", "2026-08-01"),
        _row("akre", "AAA", "bearish", 65, "2026-11-01", "2026-10-30", carried=True),
    ]
    assert "carried" in flips(rows)[0].render()


# ---------------------------------------------------------------------------
# Per-ticker history
# ---------------------------------------------------------------------------


def test_ticker_history_gathers_every_school_in_time_order():
    rows = [
        _row("akre", "AAA", "bullish", 70, "2026-09-01", "2026-08-01"),
        _row("fisher", "AAA", "neutral", 50, "2026-09-01", "2026-08-01"),
        _row("akre", "BBB", "bullish", 70, "2026-09-01", "2026-08-01"),
    ]
    got = ticker_history(rows, "aaa")  # case-insensitive
    assert [v.school for v in got] == ["akre", "fisher"]
    assert all(v.ticker == "AAA" for v in got)


def test_same_day_verdicts_order_by_ingest_not_arbitrarily():
    """A cohort re-asked the same day yields two rows sharing an
    event_date; the later ingest is the later view, and showing them the
    other way round reads as a school reversing itself backwards."""
    rows = [
        _row("akre", "AAA", "bullish", 70, "2026-09-20", "2026-08-01", logged="2026-09-21T02:00:00+00:00"),
        _row("akre", "AAA", "neutral", 50, "2026-09-20", "2026-08-01", logged="2026-09-21T01:00:00+00:00"),
    ]
    assert [v.signal for v in ticker_history(rows, "AAA")] == ["neutral", "bullish"]


def test_ticker_history_renders_theses_and_can_omit_them():
    rows = [_row("akre", "AAA", "bullish", 70, "2026-09-01", "2026-08-01", thesis="a long thesis here")]
    assert "a long thesis here" in render_ticker_history(ticker_history(rows, "AAA"), "AAA")
    terse = render_ticker_history(ticker_history(rows, "AAA"), "AAA", theses=False)
    assert "a long thesis here" not in terse and "bullish" in terse


def test_an_unknown_ticker_says_so_rather_than_rendering_nothing():
    assert "no verdicts" in render_ticker_history(ticker_history([], "ZZZ"), "ZZZ")


@pytest.mark.parametrize("reader", [flips, lambda r: ticker_history(r, "AAA")])
def test_the_readers_never_mutate_the_ledger_rows(reader):
    rows = [_row("akre", "AAA", "bullish", 70, "2026-09-01", "2026-08-01")]
    before = [dict(r) for r in rows]
    reader(rows)
    assert rows == before
