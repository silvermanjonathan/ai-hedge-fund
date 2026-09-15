"""Facts-lag check: is a candidate's verdict a quarter behind the filings?

EDGAR's submissions index lists a filing the moment it is disseminated; the
XBRL companyfacts API that the free data source reads can trail it (CTSH's
Q2-2026 10-Q sat in submissions for weeks with none of its facts in
companyfacts). When that happens every school reasons on the prior quarter
and nothing in the snapshot says so. This compares the latest original
10-K/10-Q in submissions with the filing_date the verdicts carry and
attaches a warning when submissions are newer. Read-only; it never changes
a verdict.
"""

from __future__ import annotations

from collections.abc import Iterable

from hedge_fund.data.edgar import EdgarClient
from hedge_fund.ledger.rules import Candidate

REPORT_FORMS = frozenset({"10-K", "10-Q", "10-KT", "10-QT"})  # originals only; an amendment is not a new quarter
WARNING = "facts lag: {form} filed {date} not yet in EDGAR companyfacts; verdicts reflect the prior quarter"


def latest_report_filing(edgar: EdgarClient, ticker: str) -> tuple[str, str] | None:
    """(form, filing date) of the ticker's most recent original 10-K/10-Q per
    EDGAR submissions, or None when the ticker has no CIK or no such filing."""
    cik = edgar.cik_for(ticker)
    if cik is None:
        return None
    sub = edgar.submissions(cik) or {}
    recent = sub.get("filings", {}).get("recent", {})
    filings = [(d, f) for f, d in zip(recent.get("form", []), recent.get("filingDate", [])) if f in REPORT_FORMS and d]
    if not filings:
        return None
    date, form = max(filings)
    return form, date


def facts_lag_warning(edgar: EdgarClient, ticker: str, verdict_filing_date: str | None) -> str | None:
    """The warning text when submissions show a report newer than the one
    the verdicts reasoned on; None otherwise (including when the verdict
    carries no filing date, which cannot be compared)."""
    if not verdict_filing_date:
        return None
    latest = latest_report_filing(edgar, ticker)
    if latest is None:
        return None
    form, date = latest
    return WARNING.format(form=form, date=date) if date > verdict_filing_date else None


def annotate_staleness(candidates: Iterable[Candidate], edgar: EdgarClient) -> list[str]:
    """Append a facts-lag warning to each stale candidate; returns their tickers."""
    stale: list[str] = []
    for c in candidates:
        warning = facts_lag_warning(edgar, c.ticker, c.filing_date)
        if warning:
            c.warnings.append(warning)
            stale.append(c.ticker)
    return stale
