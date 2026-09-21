"""What each school has said about a name, and where it changed its mind.

Two readers over the ledger, sharing one traversal. Neither writes, neither
calls a model, and neither touches a prompt or the rendered snapshot — so
they cost nothing in re-seed terms.

A FLIP is the most precise research lead this system produces — but only
when a NEW FILING caused it. A school re-reasons whenever the question
changes, and the question includes the prompt. So a changed signal has two
possible causes, and they are not remotely the same thing:

    new filing, changed view   the numbers moved enough to reverse a
                               considered opinion. A research lead.
    same filing, changed view  we edited a prompt and the school answered
                               differently. Says something about the
                               prompt; says nothing about the company.

The first version of this reader did not distinguish them and reported 59
flips on its first run, every one of them the Sept 2026 pilot against the
re-seed on identical filings. Reporting those as leads would have sent a
reader chasing our own prompt edit through twenty companies. `flips()`
therefore requires a new filing by default, and the same-filing changes
are reported separately and labelled for what they are.

A TICKER HISTORY is the other way to cut the same data — everything every
school has said about one name over time, with the theses — which is what
you want once a flip, or a candidate, has caught your attention.
"""

from __future__ import annotations

from dataclasses import dataclass

DIRECTIONAL = ("bullish", "bearish")


@dataclass(frozen=True)
class Verdict:
    """One ledger row, narrowed to what these readers need."""

    school: str
    ticker: str
    event_date: str
    signal: str
    confidence: float | None
    basis: str | None
    filing_date: str | None
    desk: str
    logged_at: str
    carried: bool
    thesis: str | None

    @classmethod
    def of(cls, row: dict) -> "Verdict":
        return cls(
            school=row["school"],
            ticker=row["ticker"],
            event_date=row.get("event_date") or "",
            signal=row.get("signal") or "",
            confidence=row.get("confidence"),
            basis=row.get("basis"),
            filing_date=row.get("filing_date"),
            desk=row.get("desk") or "",
            logged_at=row.get("logged_at") or "",
            # Set once names are carried past a screen exit; absent today,
            # so everything reads as on-screen.
            carried=bool(row.get("carried")),
            thesis=row.get("thesis"),
        )

    @property
    def source(self) -> str:
        return "carried" if self.carried else "screen"


@dataclass(frozen=True)
class Flip:
    """A school changing its mind about a name."""

    school: str
    ticker: str
    before: Verdict
    after: Verdict

    @property
    def reversal(self) -> bool:
        """bullish <-> bearish, as opposed to moving through neutral."""
        return self.before.signal in DIRECTIONAL and self.after.signal in DIRECTIONAL

    @property
    def filing_changed(self) -> bool:
        """Whether a new filing caused this, or only a changed question."""
        return (self.before.filing_date or "") != (self.after.filing_date or "")

    def render(self) -> str:
        conf = f"{self.before.confidence or 0:.0f}->{self.after.confidence or 0:.0f}"
        mark = "!!" if self.reversal else "  "
        return (
            f"{mark} {self.school:24} {self.ticker:6} "
            f"{self.before.signal:8} -> {self.after.signal:8} "
            f"conf {conf:8} filing {self.after.filing_date or '?':11} "
            f"{self.after.source}"
        )


def _by_pair(rows: list[dict]) -> dict[tuple[str, str], list[Verdict]]:
    """Verdicts grouped by (school, ticker), oldest first. The single
    traversal both readers share."""
    out: dict[tuple[str, str], list[Verdict]] = {}
    for row in rows:
        v = Verdict.of(row)
        out.setdefault((v.school, v.ticker), []).append(v)
    for verdicts in out.values():
        # logged_at is the tiebreak, the same one rule 3 uses for
        # supersession: two verdicts can share an event_date when a cohort
        # is re-asked the same day, and the later ingest is the later view.
        verdicts.sort(key=lambda v: (v.event_date, v.logged_at))
    return out


def flips(rows: list[dict], *, since: str | None = None, require_new_filing: bool = True) -> list[Flip]:
    """Changes of signal, newest first.

    *require_new_filing* keeps only changes a new filing can explain, which
    is what makes a flip a research lead. Pass False to see the ones a
    prompt edit produced — useful for judging an edit, useless for
    judging a company.

    A repeat of the same signal is not a flip even when the confidence
    moves — the school did not change its mind, it re-stated it. Confidence
    drift is visible in the rendered line for the flips that do qualify.
    """
    found: list[Flip] = []
    for (school, ticker), verdicts in _by_pair(rows).items():
        for before, after in zip(verdicts, verdicts[1:]):
            if before.signal == after.signal:
                continue
            if since and after.event_date < since:
                continue
            flip = Flip(school=school, ticker=ticker, before=before, after=after)
            if require_new_filing and not flip.filing_changed:
                continue
            found.append(flip)
    # Reversals first within a date: a bullish->bearish is a louder signal
    # than a drift through neutral, and should not be buried below it.
    return sorted(found, key=lambda f: (f.after.event_date, f.reversal), reverse=True)


def render_flips(found: list[Flip], *, since: str | None = None, prompt_induced: int = 0) -> str:
    window = f" since {since}" if since else ""
    note = (
        f"\n  ({prompt_induced} further signal changes came from a prompt edit rather than a\n"
        "   filing, and are not leads — see `aihf-ledger flips --include-prompt-changes`.)"
        if prompt_induced
        else ""
    )
    if not found:
        return (
            f"Flips{window}: none.\n"
            "  A school re-reasons only when a filing changes the facts, so a quiet\n"
            "  week is the normal case rather than a failure." + note
        )
    reversals = sum(1 for f in found if f.reversal)
    lines = [
        f"Flips{window}: {len(found)} ({reversals} outright reversals, marked !!)",
        "   school                   ticker before   -> after    confidence  filing      source",
        "   " + "-" * 92,
    ]
    lines += [f"  {f.render()}" for f in found]
    return "\n".join(lines) + note


def ticker_history(rows: list[dict], ticker: str) -> list[Verdict]:
    """Everything every school has said about one name, oldest first."""
    wanted = ticker.strip().upper()
    out = [v for (_, t), vs in _by_pair(rows).items() if t == wanted for v in vs]
    return sorted(out, key=lambda v: (v.event_date, v.logged_at, v.school))


def render_ticker_history(verdicts: list[Verdict], ticker: str, *, theses: bool = True) -> str:
    wanted = ticker.strip().upper()
    if not verdicts:
        return f"{wanted}: no verdicts in the ledger."
    schools = sorted({v.school for v in verdicts})
    dates = sorted({v.event_date for v in verdicts})
    lines = [
        f"{wanted}: {len(verdicts)} verdicts from {len(schools)} schools, " f"{dates[0]} to {dates[-1]}",
        "",
    ]
    for day in dates:
        same_day = [v for v in verdicts if v.event_date == day]
        lines.append(f"{day}  ({len(same_day)} verdicts, filing {same_day[0].filing_date or '?'})")
        for v in sorted(same_day, key=lambda v: (v.school, v.logged_at)):
            basis = "" if v.basis in (None, "judged") else f" [{v.basis}]"
            lines.append(f"    {v.school:24} {v.signal:8} conf {v.confidence or 0:>3.0f}{basis}" f"  ({v.source})")
            if theses and v.thesis:
                text = " ".join(v.thesis.split())
                lines.append(f"        {text[:300]}{'…' if len(text) > 300 else ''}")
        lines.append("")
    return "\n".join(lines).rstrip()
