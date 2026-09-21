"""Names a school still holds a view on, after they leave the screen.

A school forms a new view only when the name is in that week's universe
AND has filed something since. So a name that drops off the screen before
its next 10-Q is never re-asked, and the view the school last held on it
can never change.

That matters more than the low turnover rate suggests, because of WHICH
names leave. The screens are threshold-based, so a quality company whose
margins deteriorate falls out of the quality screen at precisely the
moment a school would have turned bearish on it. The exits are
disproportionately the names where a flip was most likely — which means
the system was systematically blind to negative flips, the ones worth
most.

DINO is the case that motivated this: it left the value screen five days
after the Sept 2026 pilot carrying three directional views, and would have
kept filing 10-Qs with nobody ever asked again.

What qualifies (decided Sept 2026):

- **Directional only.** A neutral or insufficient view has nothing to
  flip from, and carrying it would pay to re-ask a name about which the
  school has already said it has no opinion.
- **Live under rule 3.** The latest verdict for that (school, ticker) —
  a superseded one is not a position.
- **Post-cutoff only.** Views from before AIHF_LEDGER_SINCE were formed on
  snapshots since found to be wrong and under prompts since replaced, and
  the scorecard already discards them. Carrying on their strength would
  mean re-asking a name because of a view we decided not to count. This
  is why DINO does NOT qualify today, despite being the example.
- **Not already on the screen.** A carried name that re-enters stops being
  carried and rejoins the screen cohort. The flag is recomputed from
  current membership each week rather than sticking, so a name never stays
  outside the universe bar on the strength of a historical exit.
"""

from __future__ import annotations

from collections.abc import Iterable

DIRECTIONAL = ("bullish", "bearish")


def live_positions(rows: list[dict], *, since: str | None = None) -> dict[tuple[str, str], dict]:
    """The standing verdict for each (school, ticker) — rule 3's definition
    of a live position, and the same one the scorer uses."""
    live: dict[tuple[str, str], dict] = {}
    for row in sorted(rows, key=lambda r: (r.get("event_date") or "", r.get("logged_at") or "")):
        if since and (row.get("event_date") or "") < since:
            continue
        live[(row["school"], row["ticker"])] = row
    return live


def carried_tickers(
    rows: list[dict],
    *,
    schools: Iterable[str],
    screen: Iterable[str],
    since: str | None = None,
) -> list[str]:
    """Names to add to this desk's run because a school still holds a
    directional view on them and the screen no longer does.

    Order is stable (alphabetical) so a run's universe does not churn for
    reasons unrelated to the screen.
    """
    wanted = {s.strip() for s in schools if s.strip()}
    on_screen = {t.strip().upper() for t in screen if t.strip()}
    carried = {
        ticker
        for (school, ticker), row in live_positions(rows, since=since).items()
        if school in wanted and row.get("signal") in DIRECTIONAL and ticker.upper() not in on_screen
    }
    return sorted(carried)


def holders(rows: list[dict], ticker: str, *, schools: Iterable[str], since: str | None = None) -> list[str]:
    """Which of *schools* hold a live directional view on *ticker*. For
    explaining why a carried name is in a run."""
    wanted = {s.strip() for s in schools if s.strip()}
    want_ticker = ticker.strip().upper()
    return sorted(
        school
        for (school, t), row in live_positions(rows, since=since).items()
        if school in wanted and t.upper() == want_ticker and row.get("signal") in DIRECTIONAL
    )
