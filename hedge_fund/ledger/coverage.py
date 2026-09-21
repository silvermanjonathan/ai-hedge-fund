"""Coverage: what the scorecard is entitled to say about each school.

A scorecard that lists only the schools with calls is a trap. It prints a
clean ranking and says nothing about the schools that were never asked, and
a reader — including the person deciding which schools keep their seat —
cannot tell a school that performed badly from one that never ran. In
Sept 2026 the weekly rotation covered 9 of 18 registered schools, and
nothing in the output said so.

So every school in the roster appears, carrying one of five states:

    scored       enough scored calls at the anchor horizon; rank it
    provisional  staffed and accumulating; numbers shown, not yet a result
    ad-hoc       has calls, but no mandate here staffs it — the numbers come
                 from one-off runs and are not a ranking
    unstaffed    no staffing mandate and no calls in the ledger
    blocked      deliberately held back pending a dependency (see roster.py)

An important caveat on "unstaffed": the ledger holds only what was ingested.
A school that was run ad-hoc, but whose CycleRecord never went through
`aihf-ledger ingest`, leaves prompt-cache entries on disk and no trace here,
so it reads as unstaffed. That is what the ledger knows, not necessarily
what happened. `ad-hoc` is the state for a school whose calls WERE ingested
but which no running mandate staffs.

Staffing is read from mandate files, because the ledger cannot tell you.
It records new verdicts only, and a school re-reasons only when a filing
changes — so a school squarely in the weekly rotation can go a full quarter
without adding a row. Recency of calls therefore says nothing about whether
a school is still being asked, and only the mandates do.

Which mandates count is the caller's decision, and it matters: pointing at
every file in ~/.hedge-fund/mandates/ marks a school staffed because some
mandate could run it, while pointing at the desks the weekly loop actually
runs marks it staffed because it will accumulate calls. The second is the
useful question, so scripts/weekly.sh passes its three desks explicitly.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from hedge_fund.paths import MANDATES_DIR
from hedge_fund.roster import BLOCKED

logger = logging.getLogger(__name__)

SCORED = "scored"
PROVISIONAL = "provisional"
AD_HOC = "ad-hoc"
UNSTAFFED = "unstaffed"
BLOCKED_STATE = "blocked"

# Order used for grouping in the rendered summary: most to least to say.
STATE_ORDER = (SCORED, PROVISIONAL, AD_HOC, BLOCKED_STATE, UNSTAFFED)

LEGEND = {
    SCORED: "at or past min_calls at the anchor horizon — the ranking below is meaningful "
    "(it does not imply the school still runs; check whether it is also staffed)",
    PROVISIONAL: "staffed and accumulating; numbers shown are not yet a result",
    AD_HOC: "has calls, but no mandate scanned here staffs it — one-off runs, not a ranking",
    BLOCKED_STATE: "deliberately unstaffed pending a dependency; not a judgment on the school",
    UNSTAFFED: "no mandate scanned here staffs it, and the ledger holds no calls for it",
}


def mandate_paths(explicit: list[str] | None = None) -> list[Path]:
    """The mandates to read staffing from: the ones given, else every
    mandate in ~/.hedge-fund/mandates/."""
    if explicit:
        return [Path(p).expanduser() for p in explicit]
    if not MANDATES_DIR.exists():
        return []
    return sorted(p for p in MANDATES_DIR.glob("*.yaml"))


def staffed_schools(paths: list[Path]) -> set[str]:
    """Every model name staffed by any of these mandates.

    Parses the YAML directly rather than going through fund.load_spec: that
    would import hedge_fund.signals for registry validation, and the ledger
    is deliberately free of the LLM stack. A malformed or missing mandate is
    warned about and skipped — a coverage report must not be the thing that
    stops a scorecard from printing.
    """
    staffed: set[str] = set()
    for path in paths:
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("coverage: skipping mandate %s: %s", path, exc)
            continue
        for strategy in data.get("strategies") or []:
            for model in (strategy or {}).get("models") or []:
                name = (model or {}).get("name")
                if name:
                    staffed.add(name)
    return staffed


def classify(school: str, n_at_anchor: int, has_calls: bool, staffed: set[str], min_calls: int) -> str:
    """One school's coverage state.

    Order matters. Blocked wins over everything: it is a standing decision,
    not a function of how many calls happen to exist, and a blocked school
    that somehow accumulated a sample still must not be ranked as though
    that sample answered the question it is blocked on.

    Then the sample size, before staffing. Whether a sample is big enough
    to rank is a fact about the ledger; staffing decides whether it will
    keep GROWING, which is a different question. A school with 20 scored
    calls is rankable even if nothing runs it any more — and a scorecard
    that withheld the ranking because no mandate file mentioned it would be
    unusable on any machine without mandates.
    """
    if school in BLOCKED:
        return BLOCKED_STATE
    if n_at_anchor >= min_calls:
        return SCORED
    if school in staffed:
        return PROVISIONAL
    return AD_HOC if has_calls else UNSTAFFED
