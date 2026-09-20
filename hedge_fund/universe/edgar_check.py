"""Keep only tickers the free data source can analyse.

A screener returns US-listed names, not US filers. Foreign private issuers
(20-F / 40-F filers such as AngloGold Ashanti) have no US-GAAP company facts
on EDGAR, so every analyst abstains on them with "0 filed periods". This
post-filter drops a ticker with no CIK in the SEC ticker table, or whose
recent submissions contain no 10-K or 10-Q, before the desk wastes a cycle
on it. It reuses EdgarClient — its disk cache, User-Agent, and the shared
rate limiter — so a few dozen lookups cost a few dozen cached reads.
"""

from __future__ import annotations

from collections.abc import Iterable

from hedge_fund.data.edgar import EdgarClient

DOMESTIC_FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "10-QT"})


def files_domestic_reports(edgar: EdgarClient, ticker: str) -> tuple[bool, str | None]:
    """(keep?, reason when dropped) for one ticker."""
    cik = edgar.cik_for(ticker)
    if cik is None:
        return False, "no CIK in the SEC ticker table"
    sub = edgar.submissions(cik) or {}
    forms = [str(f) for f in sub.get("filings", {}).get("recent", {}).get("form", [])]
    if not any(f in DOMESTIC_FORMS for f in forms):
        seen = sorted({f for f in forms if f.startswith(("20-F", "40-F", "6-K"))}) or sorted(set(forms))[:4]
        return False, f"no 10-K/10-Q on file (forms seen: {', '.join(seen) or 'none'})"
    return True, None


def edgar_filter(
    tickers: Iterable[str], *, edgar: EdgarClient, limit: int | None = None
) -> tuple[list[str], list[tuple[str, str]]]:
    """Tickers that file 10-K/10-Q, in the given order, stopping once *limit*
    are kept; plus (ticker, reason) for each one dropped along the way."""
    kept: list[str] = []
    dropped: list[tuple[str, str]] = []
    for ticker in tickers:
        if limit is not None and len(kept) >= limit:
            break
        ok, reason = files_domestic_reports(edgar, ticker)
        if ok:
            kept.append(ticker)
        else:
            dropped.append((ticker, reason or "dropped"))
    return kept, dropped
