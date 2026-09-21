"""Coverage states: no registry school may be silently absent.

The failure this guards against is not a crash. It is a scorecard that
prints a tidy ranking of the schools that happen to run and says nothing
about the rest, so the person deciding which schools keep their seat cannot
tell a school that did badly from one that was never asked. In Sept 2026
that was 9 of 18, with nothing in the output to say so.
"""

from __future__ import annotations

import pytest
import yaml

from hedge_fund.ledger.coverage import (
    AD_HOC,
    BLOCKED_STATE,
    classify,
    LEGEND,
    PROVISIONAL,
    SCORED,
    staffed_schools,
    STATE_ORDER,
    UNSTAFFED,
)
from hedge_fund.ledger.score import scorecard
from hedge_fund.ledger.store import Ledger
from hedge_fund.roster import BLOCKED, SCHOOLS


class NoPrices:
    """Forward prices are irrelevant to coverage; refuse them all."""

    def get_prices(self, ticker, start_date, end_date, **kw):
        return []


def _mandate(tmp_path, name, models):
    path = tmp_path / f"{name}.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "name": name,
                "strategies": [{"name": "s", "models": [{"name": m} for m in models]}],
                "risk": {"max_position_pct": 0.25, "max_gross_exposure": 1.0},
            }
        )
    )
    return path


def _ledger(tmp_path, rows):
    path = tmp_path / "verdicts.jsonl"
    path.write_text("".join(__import__("json").dumps(r) + "\n" for r in rows))
    return Ledger(path)


def _row(school, ticker="AAPL", signal="bullish"):
    return {
        "key": f"{school}|{ticker}|h",
        "school": school,
        "ticker": ticker,
        "snapshot_hash": "h",
        "filing_date": "2025-01-15",
        "signal": signal,
        "confidence": 70.0,
        "value": 0.7,
        "desk": "d/s",
        "event_date": "2025-01-20",
        "logged_at": "2025-01-20T00:00:00+00:00",
        "entry_close": 100.0,
        "spy_close": 400.0,
        "thesis": "t",
    }


# ---------------------------------------------------------------------------
# The requirement
# ---------------------------------------------------------------------------


def test_every_registry_school_appears_in_the_scorecard(tmp_path):
    """The one that matters. A school missing from the output entirely is
    indistinguishable from a school that scored badly."""
    ledger = _ledger(tmp_path, [_row("akre")])
    card = scorecard(ledger, NoPrices(), "2025-06-01", horizons=(63,), staffed={"akre"})

    present = {r.school for r in card.rows}
    missing = sorted(set(SCHOOLS) - present)
    assert not missing, f"registry schools absent from the scorecard: {missing}"

    rendered = card.render()
    for school in SCHOOLS:
        assert school in rendered, f"{school} is in the registry but not in the rendered output"


def test_every_school_carries_a_known_coverage_state(tmp_path):
    ledger = _ledger(tmp_path, [_row("akre")])
    card = scorecard(ledger, NoPrices(), "2025-06-01", horizons=(63,), staffed={"akre"})
    for r in card.rows:
        assert r.coverage in STATE_ORDER, f"{r.school} has unknown coverage {r.coverage!r}"


def test_coverage_summary_accounts_for_the_whole_registry(tmp_path):
    ledger = _ledger(tmp_path, [_row("akre")])
    card = scorecard(ledger, NoPrices(), "2025-06-01", horizons=(63,), staffed={"akre"})
    grouped = card.coverage_by_state
    counted = sum(len(v) for v in grouped.values())
    assert counted == len(SCHOOLS)
    assert sorted(n for v in grouped.values() for n in v) == sorted(SCHOOLS)


def test_json_output_carries_coverage(tmp_path):
    """The JSON is the machine-readable form; it must not drop the caveat."""
    import json

    ledger = _ledger(tmp_path, [_row("akre")])
    card = scorecard(ledger, NoPrices(), "2025-06-01", horizons=(63,), staffed={"akre"})
    payload = json.loads(card.to_json())
    assert {r["school"] for r in payload["rows"]} >= set(SCHOOLS)
    assert all("coverage" in r for r in payload["rows"])


# ---------------------------------------------------------------------------
# State assignment
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "n,has_calls,staffed,expected",
    [
        (25, True, True, SCORED),
        (20, True, True, SCORED),  # threshold is inclusive
        (19, True, True, PROVISIONAL),
        (0, False, True, PROVISIONAL),  # staffed, nothing scored yet
        (5, True, False, AD_HOC),  # calls, but nothing staffs it now
        (0, False, False, UNSTAFFED),
    ],
)
def test_classify(n, has_calls, staffed, expected):
    got = classify("akre", n_at_anchor=n, has_calls=has_calls, staffed={"akre"} if staffed else set(), min_calls=20)
    assert got == expected


def test_blocked_beats_every_other_state():
    """Blocked is a standing decision, not a function of the call count. If
    a blocked school somehow accumulates calls, it still must not be ranked
    as though those calls answered the question it is blocked on."""
    school = next(iter(BLOCKED))
    for n, has_calls, staffed in [(0, False, set()), (99, True, {school})]:
        assert classify(school, n_at_anchor=n, has_calls=has_calls, staffed=staffed, min_calls=20) == BLOCKED_STATE


def test_blocked_school_is_never_given_a_verdict(tmp_path):
    school = next(iter(BLOCKED))
    ledger = _ledger(tmp_path, [_row(school) for _ in range(1)])
    card = scorecard(ledger, NoPrices(), "2025-06-01", horizons=(63,), staffed={school})
    rows = [r for r in card.rows if r.school == school]
    assert rows and all(r.coverage == BLOCKED_STATE for r in rows)
    assert all(r.status == "-" for r in rows), "a blocked school must not carry earned/probation"


def test_status_is_only_a_verdict_when_coverage_is_scored(tmp_path):
    """The point of the two columns: a thin sample gets no word that could
    be read as a ranking."""
    ledger = _ledger(tmp_path, [_row("akre")])
    card = scorecard(ledger, NoPrices(), "2025-06-01", horizons=(63,), staffed={"akre"})
    for r in card.rows:
        if r.coverage != SCORED:
            assert r.status == "-", f"{r.school} ({r.coverage}) offers a verdict {r.status!r}"


def test_a_school_in_the_ledger_but_not_the_roster_still_appears(tmp_path):
    """A renamed or retired school must not vanish from its own history."""
    ledger = _ledger(tmp_path, [_row("retired_school")])
    card = scorecard(ledger, NoPrices(), "2025-06-01", horizons=(63,), staffed=set())
    assert "retired_school" in {r.school for r in card.rows}


def test_every_state_has_a_legend_entry():
    for state in STATE_ORDER:
        assert LEGEND.get(state), f"{state} is rendered with no explanation"


# ---------------------------------------------------------------------------
# Staffing comes from mandates, because the ledger cannot supply it
# ---------------------------------------------------------------------------


def test_staffed_schools_reads_every_mandate_given(tmp_path):
    a = _mandate(tmp_path, "quality", ["fundsmith", "akre"])
    b = _mandate(tmp_path, "value", ["schloss"])
    assert staffed_schools([a, b]) == {"fundsmith", "akre", "schloss"}


def test_staffed_schools_survives_a_broken_mandate(tmp_path):
    """A coverage report must never be the thing that stops a scorecard."""
    good = _mandate(tmp_path, "good", ["akre"])
    bad = tmp_path / "bad.yaml"
    bad.write_text("{{{ not yaml")
    assert staffed_schools([bad, good, tmp_path / "missing.yaml"]) == {"akre"}


def test_unknown_staffing_reports_calls_as_ad_hoc(tmp_path):
    """staffed=None means 'rotation unknown'. A school with calls is then
    ad-hoc rather than provisional — the honest reading, since nothing has
    established it is still being asked."""
    ledger = _ledger(tmp_path, [_row("akre")])
    card = scorecard(ledger, NoPrices(), "2025-06-01", horizons=(63,), staffed=None)
    assert next(r.coverage for r in card.rows if r.school == "akre") == AD_HOC


# ---------------------------------------------------------------------------
# --since: a universe change makes older verdicts incomparable
# ---------------------------------------------------------------------------


def _dated(school, ticker, event_date, signal="bullish", basis="judged"):
    row = _row(school, ticker, signal)
    row["key"] = f"{school}|{ticker}|{event_date}"
    row["event_date"] = event_date
    row["basis"] = basis
    return row


def test_since_excludes_verdicts_from_before_the_cutoff(tmp_path):
    ledger = _ledger(tmp_path, [_dated("akre", "OLD", "2026-09-15"), _dated("akre", "NEW", "2026-11-02")])

    card = scorecard(ledger, NoPrices(), "2026-12-01", horizons=(63,), staffed={"akre"}, since="2026-10-01")

    assert len(ledger.rows()) == 2, "nothing is deleted; history stays"
    assert len(ledger.rows("2026-10-01")) == 1
    assert card.since == "2026-10-01"
    assert "on or after 2026-10-01" in card.render()


def test_since_moves_a_school_with_only_old_calls_to_unstaffed(tmp_path):
    """Its verdicts grade a universe that no longer exists, so it reads as
    no result rather than as a thin one."""
    ledger = _ledger(tmp_path, [_dated("dreman", "OLD", "2026-09-15")])

    before = scorecard(ledger, NoPrices(), "2026-12-01", horizons=(63,), staffed=set())
    after = scorecard(ledger, NoPrices(), "2026-12-01", horizons=(63,), staffed=set(), since="2026-10-01")

    assert next(r.coverage for r in before.rows if r.school == "dreman") == AD_HOC
    assert next(r.coverage for r in after.rows if r.school == "dreman") == UNSTAFFED


def test_since_reaches_the_candidates_playbook(tmp_path):
    """The must-fix. The playbook is the output that gets acted on: without
    the cutoff a name that has left the universe keeps surfacing as an entry
    candidate on the strength of a verdict from a screen that is gone."""
    ledger = _ledger(
        tmp_path,
        [_dated(s, "GONE", "2026-09-15") for s in ("akre", "fisher", "fundsmith")]
        + [_dated(s, "CURRENT", "2026-11-02") for s in ("akre", "fisher", "fundsmith")],
    )

    assert {t for t, _ in ledger.latest_per_ticker_school()} == {"GONE", "CURRENT"}
    assert {t for t, _ in ledger.latest_per_ticker_school("2026-10-01")} == {"CURRENT"}


def test_the_scorecard_reports_how_much_neutrality_was_ignorance(tmp_path):
    """basis splits "I declined this name" from "I could not tell", which
    was previously only recoverable by pattern-matching thesis text."""
    rows = [
        _dated("akre", "A", "2026-11-02", signal="neutral", basis="judged"),
        _dated("akre", "B", "2026-11-02", signal="neutral", basis="insufficient"),
        _dated("akre", "C", "2026-11-02", signal="bullish", basis="judged"),
    ]
    card = scorecard(_ledger(tmp_path, rows), NoPrices(), "2026-12-01", horizons=(63,), staffed={"akre"})
    row = next(r for r in card.rows if r.school == "akre")

    assert row.neutral_share == pytest.approx(2 / 3)
    assert row.insufficient_share == pytest.approx(1 / 3)


def test_rows_without_a_basis_report_unknown_not_zero(tmp_path):
    """Rows written before the field existed said nothing; 0.0 would claim
    they said "judged"."""
    row = _row("akre", "A", "neutral")
    row.pop("basis", None)
    card = scorecard(_ledger(tmp_path, [row]), NoPrices(), "2026-12-01", horizons=(63,), staffed={"akre"})
    assert next(r.insufficient_share for r in card.rows if r.school == "akre") is None
