"""XBRL companyfacts → point-in-time FinancialMetrics rows. Pure functions.

Input is the JSON from data.sec.gov/api/xbrl/companyfacts: every value a
filer ever reported for every tag, each with the filing that reported it
(`filed`), the period it covers (`start`/`end`, or just `end` for a balance
sheet instant), and the form. The same period appears many times — once when
first reported, then again as a comparative column in later filings, and
again if restated. Three rules turn that into rows an analyst may see:

1. FIRST-REPORTED VALUES. Per tag and period, keep the entry with the
   earliest `filed`. A row is the number the market saw when the period was
   first disclosed, and it never changes when a later filing restates it —
   so a snapshot's content hash, and the LLM decision cache keyed on it, are
   stable across every later as_of.
2. ONE ROW PER FILING. A row's `report_period` is a balance-sheet date and
   its `filing_date` is the `filed` of the 10-Q/10-K that first carried that
   balance sheet. Everything in the row is computed only from facts filed on
   or before that date. get_financial_metrics(end_date) returns the rows
   whose filing_date <= end_date: point-in-time by construction.
3. QUARTERS FROM WHAT WAS FILED. Income-statement tags carry quarterly and
   year-to-date values in 10-Qs and only the full year in the 10-K; cash-flow
   tags are year-to-date only. A quarter is the quarterly fact if one exists,
   else a year-to-date fact minus the same tag's year-to-date fact one period
   earlier (Q4 = FY − 9M). Trailing twelve months is four consecutive
   quarters, or None.

Tag priority resolves per period end (a filer that switched from `Revenues`
to `RevenueFromContractWithCustomer...` keeps a continuous series), and
differencing never mixes tags.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING

from hedge_fund.data.models import FinancialMetrics

if TYPE_CHECKING:
    from hedge_fund.data.prices import DailyHistory

FORMS = frozenset({"10-K", "10-Q", "10-K/A", "10-Q/A", "10-KT", "10-QT"})

# Inclusive day counts that make a duration fact a quarter, a half, nine
# months, or a year — wide enough for 13/14-week quarters and 52/53-week years.
DURATION_CLASSES = {"Q": (80, 100), "H": (170, 190), "9M": (260, 290), "FY": (350, 380)}
_QUARTER_GAP = (80, 100)  # days between consecutive balance-sheet dates
_LONGEST_FIRST = ("FY", "9M", "H", "Q")

# us-gaap tags per input, in priority order. Resolved per period end.
REVENUE = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
)
NET_INCOME = ("NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic")
GROSS_PROFIT = ("GrossProfit",)
COST_OF_REVENUE = ("CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold")
OPERATING_INCOME = ("OperatingIncomeLoss",)
EQUITY = ("StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest")
ASSETS_CURRENT = ("AssetsCurrent",)
LIABILITIES_CURRENT = ("LiabilitiesCurrent",)
OPERATING_CASH_FLOW = (
    "NetCashProvidedByUsedInOperatingActivities",
    "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
)
CAPEX = ("PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets")
DEBT_NONCURRENT = ("LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligations")
DEBT_TOTAL = ("LongTermDebt",)  # used as the whole when no noncurrent tag exists
DEBT_CURRENT = ("DebtCurrent",)
DEBT_CURRENT_PIECES = ("LongTermDebtCurrent", "ShortTermBorrowings", "CommercialPaper")
EPS_DILUTED = ("EarningsPerShareDiluted",)  # unit USD/shares
WEIGHTED_SHARES = ("WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfSharesOutstandingBasic")
SHARES_OUTSTANDING = ("CommonStockSharesOutstanding",)
DEI_SHARES_OUTSTANDING = "EntityCommonStockSharesOutstanding"  # dei taxonomy, unit shares


@dataclass(frozen=True)
class Fact:
    start: date | None  # None for a balance-sheet instant
    end: date
    val: float
    filed: date
    accn: str
    form: str

    @property
    def klass(self) -> str | None:
        """ "Q" / "H" / "9M" / "FY" for a duration fact, else None."""
        if self.start is None:
            return None
        days = (self.end - self.start).days + 1
        for name, (lo, hi) in DURATION_CLASSES.items():
            if lo <= days <= hi:
                return name
        return None


class FactBook:
    """One filer's facts, parsed once and indexed by tag."""

    def __init__(self, companyfacts: dict) -> None:
        self._facts = companyfacts.get("facts", {}) if companyfacts else {}
        self._all: dict[tuple[str, str, str], list[Fact]] = {}
        self._first: dict[tuple[str, str, str], list[Fact]] = {}

    def all_facts(self, tag: str, unit: str = "USD", taxonomy: str = "us-gaap") -> list[Fact]:
        """Every parsed entry for *tag*, duplicates included, sorted by end."""
        key = (taxonomy, tag, unit)
        if key not in self._all:
            entries = self._facts.get(taxonomy, {}).get(tag, {}).get("units", {}).get(unit, [])
            self._all[key] = sorted(_parse(entries), key=lambda f: (f.end, f.start or f.end, f.filed, f.accn))
        return self._all[key]

    def facts(self, tag: str, unit: str = "USD", taxonomy: str = "us-gaap") -> list[Fact]:
        """First-reported facts: one per (start, end), the earliest filed."""
        key = (taxonomy, tag, unit)
        if key not in self._first:
            self._first[key] = first_filed(self.all_facts(tag, unit, taxonomy))
        return self._first[key]

    def durations(self, tag: str, unit: str = "USD") -> list[Fact]:
        return [f for f in self.facts(tag, unit) if f.start is not None]

    def instants(self, tag: str, unit: str = "USD") -> list[Fact]:
        return [f for f in self.facts(tag, unit) if f.start is None]


def _parse(entries: Iterable[dict]) -> list[Fact]:
    facts: list[Fact] = []
    for e in entries:
        try:
            form = str(e.get("form", ""))
            if form not in FORMS:
                continue
            start = date.fromisoformat(e["start"]) if e.get("start") else None
            facts.append(
                Fact(
                    start=start,
                    end=date.fromisoformat(e["end"]),
                    val=float(e["val"]),
                    filed=date.fromisoformat(e["filed"]),
                    accn=str(e.get("accn", "")),
                    form=form,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue  # a malformed entry is not evidence
    return facts


def first_filed(facts: Iterable[Fact]) -> list[Fact]:
    """One fact per (start, end): the earliest filed (ties broken by accession)."""
    best: dict[tuple[date | None, date], Fact] = {}
    for f in facts:
        key = (f.start, f.end)
        cur = best.get(key)
        if cur is None or (f.filed, f.accn) < (cur.filed, cur.accn):
            best[key] = f
    return sorted(best.values(), key=lambda f: (f.end, f.start or f.end))


# ---------------------------------------------------------------------------
# Values at one period end, as known on one cutoff date
# ---------------------------------------------------------------------------


def instant_at(book: FactBook, tags: Iterable[str], end: date, cutoff: date, unit: str = "USD") -> float | None:
    """Balance-sheet value at *end* from the first tag that has one filed by *cutoff*."""
    for tag in tags:
        for f in book.instants(tag, unit):
            if f.end == end and f.filed <= cutoff:
                return f.val
    return None


def quarter_at(
    book: FactBook, tag: str, end: date, prev_end: date | None, cutoff: date, unit: str = "USD"
) -> float | None:
    """One quarter of *tag* ending at *end*: a quarterly fact, else a
    year-to-date fact minus the same tag's year-to-date fact ending at
    *prev_end* with the same start (Q4 = FY − 9M). Same tag only."""
    ending = [f for f in book.durations(tag, unit) if f.end == end and f.filed <= cutoff]
    for f in ending:
        if f.klass == "Q":
            return f.val
    if prev_end is None:
        return None
    for f in ending:
        if f.klass in ("H", "9M", "FY"):
            for g in book.durations(tag, unit):
                if g.end == prev_end and g.start == f.start and g.filed <= cutoff:
                    return f.val - g.val
    return None


def quarter_first(
    book: FactBook, tags: Iterable[str], end: date, prev_end: date | None, cutoff: date, unit: str = "USD"
) -> float | None:
    """The quarter from the first tag that yields one at this period end."""
    for tag in tags:
        value = quarter_at(book, tag, end, prev_end, cutoff, unit)
        if value is not None:
            return value
    return None


class Periods:
    """The filer's balance-sheet dates in order, with quarter-gap checks."""

    def __init__(self, ends: list[date]) -> None:
        self.ends = sorted(ends)
        self._index = {e: i for i, e in enumerate(self.ends)}

    def prev(self, end: date) -> date | None:
        """The period one quarter before *end*, or None if the gap is not a quarter."""
        i = self._index[end]
        if i == 0:
            return None
        prev = self.ends[i - 1]
        lo, hi = _QUARTER_GAP
        return prev if lo <= (end - prev).days <= hi else None

    def window(self, end: date, n: int) -> list[date] | None:
        """The *n* consecutive quarters ending at *end*, or None if any gap is off."""
        i = self._index[end]
        if i - n + 1 < 0:
            return None
        window = self.ends[i - n + 1 : i + 1]
        lo, hi = _QUARTER_GAP
        for a, b in zip(window, window[1:]):
            if not lo <= (b - a).days <= hi:
                return None
        return window

    def back(self, end: date, quarters: int) -> date | None:
        """The period *quarters* before *end* along an unbroken chain."""
        window = self.window(end, quarters + 1)
        return window[0] if window else None


def ttm(quarter: Callable[[date, date | None], float | None], periods: Periods, end: date) -> float | None:
    """Sum of *quarter*(period, previous period) over the four quarters ending
    at *end*; None if the chain is broken or any quarter is missing."""
    window = periods.window(end, 4)
    if window is None:
        return None
    total = 0.0
    for e in window:
        value = quarter(e, periods.prev(e))
        if value is None:
            return None
        total += value
    return total


# ---------------------------------------------------------------------------
# Shares, debt
# ---------------------------------------------------------------------------


def shares_outstanding(book: FactBook, end: date, cutoff: date) -> float | None:
    """Common shares outstanding as last reported on a cover page filed by
    *cutoff* (dei:EntityCommonStockSharesOutstanding). A multi-class filer
    reports one count per class in the same filing; they are summed. Falls
    back to the balance-sheet count at *end*."""
    facts = [f for f in book.all_facts(DEI_SHARES_OUTSTANDING, "shares", "dei") if f.filed <= cutoff]
    if facts:
        latest = max(f.end for f in facts)
        group = [f for f in facts if f.end == latest]
        first = min(f.filed for f in group)
        seen: set[tuple[str, float]] = set()
        total = 0.0
        for f in group:
            if f.filed == first and (f.accn, f.val) not in seen:
                seen.add((f.accn, f.val))
                total += f.val
        return total
    return instant_at(book, SHARES_OUTSTANDING, end, cutoff, unit="shares")


def weighted_shares(book: FactBook, end: date, cutoff: date) -> float | None:
    """Diluted weighted-average shares for the longest period ending at *end*
    (the year-to-date figure is the closest proxy for a trailing year)."""
    for tag in WEIGHTED_SHARES:
        ending = [f for f in book.durations(tag, "shares") if f.end == end and f.filed <= cutoff]
        for klass in _LONGEST_FIRST:
            for f in ending:
                if f.klass == klass:
                    return f.val
    return shares_outstanding(book, end, cutoff)


def total_debt(book: FactBook, end: date, cutoff: date) -> float | None:
    """Noncurrent plus current borrowings. With no noncurrent tag, LongTermDebt
    is taken as the whole and the current pieces are skipped (they would
    double count). None when nothing is tagged."""
    noncurrent = instant_at(book, DEBT_NONCURRENT, end, cutoff)
    if noncurrent is None:
        return instant_at(book, DEBT_TOTAL, end, cutoff)
    current = instant_at(book, DEBT_CURRENT, end, cutoff)
    if current is None:
        current = sum(instant_at(book, (tag,), end, cutoff) or 0.0 for tag in DEBT_CURRENT_PIECES)
    return noncurrent + current


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


def build_rows(companyfacts: dict, *, ticker: str, history: DailyHistory | None = None) -> list[FinancialMetrics]:
    """Every point-in-time ttm row for a filer, newest first.

    *history* supplies split-unadjusted closes for the market-cap join (the
    last close on or before the row's filing date, matching get_market_cap);
    without it market_cap and price_to_earnings_ratio are None.
    """
    book = FactBook(companyfacts)

    equity_at: dict[date, Fact] = {}
    for tag in EQUITY:
        for f in book.instants(tag):
            equity_at.setdefault(f.end, f)
    flow_ends = {f.end for tag in REVENUE + NET_INCOME for f in book.durations(tag)}
    periods = Periods([e for e in equity_at if e in flow_ends])

    rows: list[FinancialMetrics] = []
    for end in periods.ends:
        cutoff = equity_at[end].filed

        def q(tags, unit="USD"):
            return lambda e, prev: quarter_first(book, tags, e, prev, cutoff, unit)

        def q_gross(e, prev):
            gp = quarter_first(book, GROSS_PROFIT, e, prev, cutoff)
            if gp is not None:
                return gp
            rev = quarter_first(book, REVENUE, e, prev, cutoff)
            cogs = quarter_first(book, COST_OF_REVENUE, e, prev, cutoff)
            return None if rev is None or cogs is None else rev - cogs

        revenue = ttm(q(REVENUE), periods, end)
        net_income = ttm(q(NET_INCOME), periods, end)
        gross_profit = ttm(q_gross, periods, end)
        operating_income = ttm(q(OPERATING_INCOME), periods, end)
        cfo = ttm(q(OPERATING_CASH_FLOW), periods, end)
        capex = ttm(q(CAPEX), periods, end)
        eps = ttm(q(EPS_DILUTED, "USD/shares"), periods, end)
        year_ago = periods.back(end, 4)
        revenue_year_ago = ttm(q(REVENUE), periods, year_ago) if year_ago else None

        equity = equity_at[end].val
        assets_current = instant_at(book, ASSETS_CURRENT, end, cutoff)
        liabilities_current = instant_at(book, LIABILITIES_CURRENT, end, cutoff)
        debt = total_debt(book, end, cutoff)
        shares = shares_outstanding(book, end, cutoff)
        wsh = weighted_shares(book, end, cutoff)
        price = history.unadjusted_close_on_or_before(cutoff) if history is not None else None
        market_cap = price * shares if price is not None and shares else None

        if eps is None and net_income is not None and wsh:
            eps = net_income / wsh
        fcf = cfo - (capex or 0.0) if cfo is not None else None

        row = FinancialMetrics(
            ticker=ticker,
            report_period=end.isoformat(),
            period="ttm",
            currency="USD",
            filing_date=cutoff.isoformat(),
            market_cap=_sig(market_cap),
            price_to_earnings_ratio=_sig(
                _ratio(market_cap, net_income) if net_income is not None and net_income > 0 else None
            ),
            return_on_equity=_r6(_ratio(net_income, equity) if equity > 0 else None),
            gross_margin=_r6(_ratio(gross_profit, revenue)),
            operating_margin=_r6(_ratio(operating_income, revenue)),
            net_margin=_r6(_ratio(net_income, revenue)),
            debt_to_equity=_r6(_ratio(debt, equity) if equity > 0 else None),
            current_ratio=_r6(_ratio(assets_current, liabilities_current)),
            revenue_growth=_r6(
                _ratio(revenue, revenue_year_ago) - 1
                if revenue is not None and revenue_year_ago and revenue_year_ago > 0
                else None
            ),
            earnings_per_share=_r6(eps),
            book_value_per_share=_r6(_ratio(equity, shares)),
            free_cash_flow_per_share=_r6(_ratio(fcf, shares)),
        )
        if any(getattr(row, name) is not None for name in _COMPUTED):
            rows.append(row)

    rows.reverse()
    return rows


def merge_companyfacts(payloads: list[dict]) -> dict:
    """One facts payload from several filers' payloads — a successor and the
    registrants it succeeded. Entries are concatenated per taxonomy, tag,
    and unit; first-filed dedup then keeps the original disclosure wherever
    the successor repeated a predecessor period as a comparative."""
    merged: dict = {"facts": {}}
    for payload in payloads:
        if not payload:
            continue
        merged.setdefault("cik", payload.get("cik"))
        merged.setdefault("entityName", payload.get("entityName"))
        for taxonomy, tags in payload.get("facts", {}).items():
            for tag, body in tags.items():
                target = merged["facts"].setdefault(taxonomy, {}).setdefault(tag, {"units": {}})
                for unit, entries in body.get("units", {}).items():
                    target["units"].setdefault(unit, []).extend(entries)
    return merged


def point_in_time(rows: list[FinancialMetrics], end_date: str, limit: int) -> list[FinancialMetrics]:
    """The newest *limit* rows that were public by *end_date* (filing_date <= end_date)."""
    return [r for r in rows if r.filing_date is not None and r.filing_date <= end_date][:limit]


_COMPUTED = (
    "market_cap",
    "price_to_earnings_ratio",
    "return_on_equity",
    "gross_margin",
    "operating_margin",
    "net_margin",
    "debt_to_equity",
    "current_ratio",
    "revenue_growth",
    "earnings_per_share",
    "book_value_per_share",
    "free_cash_flow_per_share",
)


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator is None or denominator == 0:
        return None
    return numerator / denominator


def _r6(value: float | None) -> float | None:
    return None if value is None or not math.isfinite(value) else round(value, 6)


def _sig(value: float | None, figures: int = 3) -> float | None:
    """Round to *figures* significant figures — price-derived fields would
    otherwise jitter with every fetch and break the snapshot hash."""
    if value is None or not math.isfinite(value) or value == 0:
        return value
    return round(value, figures - 1 - int(math.floor(math.log10(abs(value)))))
