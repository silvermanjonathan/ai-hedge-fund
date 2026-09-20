"""The free DataClient: SEC EDGAR fundamentals, Yahoo Finance prices, no key.

Selected with HEDGE_FUND_DATA=free (the default) or `aihf --data free`. Same
protocol as FDClient, so the pipeline cannot tell them apart — with these
honest differences:

- fundamentals come from XBRL companyfacts (hedge_fund/data/xbrl.py): rows
  are dated by the 10-Q/10-K that first reported them, so a quarter becomes
  visible 0–4 weeks later than an earnings release, and values are the
  first-reported ones, never restated;
- market cap is price × dei:EntityCommonStockSharesOutstanding as of the
  date, not a vendor's figure;
- sector/industry are the SIC code's division and description;
- news, insider trades, earnings history, and latest earnings have no free
  source with the fields the models expect. Those methods raise
  NotImplementedError — never an empty answer that would read as "no data".
"""

from __future__ import annotations

import threading
from datetime import date

from hedge_fund.data.edgar import EdgarClient
from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsRecord,
    FinancialMetrics,
    InsiderTrade,
    Price,
)
from hedge_fund.data.prices import PriceSource, YFinancePrices
from hedge_fund.data.xbrl import (
    build_rows,
    FactBook,
    merge_companyfacts,
    point_in_time,
    shares_outstanding,
)

# ((cik, facts version), ...) over the succession chain -> rows, shared
# across instances (one per TUI thread).
_ROWS: dict[tuple, list[FinancialMetrics]] = {}
_ROWS_LOCK = threading.Lock()
_ROWS_MAX = 64

_UNSUPPORTED = "{method} is not available on the free data source (HEDGE_FUND_DATA=free): {why}. Use --data fd."


class FreeDataClient:
    """DataClient over EDGAR + a PriceSource (yfinance by default)."""

    def __init__(self, *, prices: PriceSource | None = None, edgar: EdgarClient | None = None) -> None:
        self._prices = prices if prices is not None else YFinancePrices()
        self._edgar = edgar if edgar is not None else EdgarClient()

    def __enter__(self) -> FreeDataClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._edgar.close()

    # ------------------------------------------------------------------
    # DataClient protocol
    # ------------------------------------------------------------------

    def get_prices(
        self, ticker: str, start_date: str, end_date: str, interval: str = "day", interval_multiplier: int = 1
    ) -> list[Price]:
        if interval != "day" or interval_multiplier != 1:
            raise NotImplementedError(
                _UNSUPPORTED.format(
                    method=f"get_prices(interval={interval!r}, interval_multiplier={interval_multiplier})",
                    why="only daily bars are served",
                )
            )
        return self._prices.daily_bars(ticker, start_date, end_date)

    def get_financial_metrics(
        self, ticker: str, end_date: str, period: str = "ttm", limit: int = 10
    ) -> list[FinancialMetrics]:
        """Point-in-time ttm rows: filed on or before *end_date*, newest first."""
        if period != "ttm":
            raise NotImplementedError(
                _UNSUPPORTED.format(
                    method=f"get_financial_metrics(period={period!r})",
                    why="only trailing-twelve-month rows are computed",
                )
            )
        cik = self._edgar.cik_for(ticker)
        if cik is None:
            return []
        return point_in_time(self._rows(ticker, cik), end_date, limit)

    def get_news(
        self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000
    ) -> list[CompanyNews]:
        raise NotImplementedError(_UNSUPPORTED.format(method="get_news", why="there is no free news feed"))

    def get_insider_trades(
        self, ticker: str, end_date: str, start_date: str | None = None, limit: int = 1000
    ) -> list[InsiderTrade]:
        raise NotImplementedError(
            _UNSUPPORTED.format(method="get_insider_trades", why="Form 4 parsing is not implemented")
        )

    def get_company_facts(self, ticker: str) -> CompanyFacts | None:
        cik = self._edgar.cik_for(ticker)
        if cik is None:
            return None
        sub = self._edgar.submissions(cik)
        if sub is None:
            return None
        sic = str(sub.get("sic") or "").strip() or None
        description = (sub.get("sicDescription") or "").strip() or None
        exchanges = [e for e in (sub.get("exchanges") or []) if e]
        return CompanyFacts(
            ticker=ticker.upper(),
            name=sub.get("name") or None,
            cik=f"{cik:010d}",
            sector=sic_division(sic),
            industry=description,
            exchange=exchanges[0] if exchanges else None,
            sic_code=sic,
            sic_industry=description,
            sic_sector=sic_division(sic),
        )

    def get_earnings(self, ticker: str) -> Earnings | None:
        raise NotImplementedError(
            _UNSUPPORTED.format(
                method="get_earnings",
                why="EDGAR carries no consensus estimates, so the surprise fields cannot be filled",
            )
        )

    def get_earnings_history(self, ticker: str, limit: int = 12) -> list[EarningsRecord]:
        raise NotImplementedError(
            _UNSUPPORTED.format(
                method="get_earnings_history",
                why="EDGAR carries no consensus estimates, so BEAT/MISS surprises cannot be labelled",
            )
        )

    def get_market_cap(self, ticker: str, end_date: str) -> float | None:
        """Last close on or before *end_date* × shares outstanding as last
        reported on a cover page filed by *end_date*. Point-in-time."""
        cik = self._edgar.cik_for(ticker)
        if cik is None:
            return None
        facts, _ = self._facts(cik)
        if facts is None:
            return None
        as_of = date.fromisoformat(end_date)
        shares = shares_outstanding(FactBook(facts), as_of, as_of)
        price = self._prices.history(ticker).unadjusted_close_on_or_before(as_of)
        if not shares or price is None:
            return None
        return price * shares

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _facts(self, cik: int) -> tuple[dict | None, tuple]:
        """The filer's facts merged with its predecessors' (a reorganised
        company keeps its history), plus a version key for the memo."""
        chain = self._edgar.cik_chain(cik)
        payloads = [self._edgar.company_facts(c) for c in chain]
        if not any(payloads):
            return None, ()
        version = tuple((c, self._edgar.company_facts_version(c)) for c in chain)
        if len(chain) == 1:
            return payloads[0], version
        return merge_companyfacts([p for p in payloads if p]), version

    def _rows(self, ticker: str, cik: int) -> list[FinancialMetrics]:
        """All rows for a filer, built once per cached facts payload."""
        facts, key = self._facts(cik)
        if facts is None:
            return []
        with _ROWS_LOCK:
            rows = _ROWS.get(key)
        if rows is None:
            rows = build_rows(facts, ticker=ticker.upper(), history=self._prices.history(ticker))
            with _ROWS_LOCK:
                if len(_ROWS) >= _ROWS_MAX:
                    _ROWS.pop(next(iter(_ROWS)))
                _ROWS[key] = rows
        return rows


# SIC major-group ranges -> division. A coarse sector, honestly labelled.
_SIC_DIVISIONS = (
    (100, 999, "Agriculture, Forestry & Fishing"),
    (1000, 1499, "Mining"),
    (1500, 1799, "Construction"),
    (2000, 3999, "Manufacturing"),
    (4000, 4999, "Transportation & Public Utilities"),
    (5000, 5199, "Wholesale Trade"),
    (5200, 5999, "Retail Trade"),
    (6000, 6799, "Finance, Insurance & Real Estate"),
    (7000, 8999, "Services"),
    (9100, 9729, "Public Administration"),
)


def sic_division(sic: str | int | None) -> str | None:
    """The SIC division for a code, e.g. 3571 -> Manufacturing."""
    try:
        code = int(sic)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    for lo, hi, name in _SIC_DIVISIONS:
        if lo <= code <= hi:
            return name
    return None
