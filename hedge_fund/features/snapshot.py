"""Point-in-time fundamentals snapshot — the shared input for LLM analysts.

A `FundamentalsSnapshot` is everything an investor agent is allowed to know
about a company as of a given date: a history of financial metrics (each row
provably public by `as_of` — the data layer filters on filing_date, not
report_period) plus a few derived aggregates computed here in Python so the
LLM reasons over facts instead of re-deriving arithmetic.

The snapshot is pure data: build it once, hash it, feed it to any persona.
`content_hash` is the cache key for LLM calls — an agent only re-reasons
when a new filing changes its snapshot. Both the hash and `render()` exclude
`as_of`: two dates between filings see identical data, and identical data
must produce an identical prompt (a cache hit), not two paid LLM calls.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel

from hedge_fund.data.protocol import DataClient

# An agent can't say anything defensible about a company with less history
# than this (one year of ttm rows).
MIN_PERIODS = 4

# A blank cell used to be a single dash, which collapsed three different
# situations into one: a figure we could not read, a figure the filer does
# not publish, and a line that does not exist for this kind of business.
# Personas abstained on all three, and a bank with several structurally
# inapplicable columns read as a company with missing data.
NOT_REPORTED = "n/r"
NOT_APPLICABLE = "n/a"

# Fields a sector does not report in a form comparable to other sectors.
# Banks, insurers and asset managers publish no gross profit line and no
# classified balance sheet, so gross margin and the current ratio have
# nothing behind them; operating margin exists in some form but not one
# that compares to an industrial's.
#
# Keyed on FundamentalsSnapshot.sector, which comes from the data source
# (Finviz for screened names, EDGAR SIC otherwise). It is third-party and
# coarse: a diversified holding company classified Financial gets n/a on a
# gross margin it genuinely does not report, which is right by accident.
# If that classification ever moves, this mapping moves with it.
SECTOR_NOT_APPLICABLE: dict[str, frozenset[str]] = {
    "Financial": frozenset({"gross_margin", "operating_margin", "current_ratio"}),
}


class InsufficientData(ValueError):
    """Not enough point-in-time history to build a snapshot."""


class PeriodFundamentals(BaseModel):
    """One reporting period's key metrics, compacted for prompting."""

    report_period: str
    filing_date: str | None = None
    market_cap: float | None = None
    price_to_earnings_ratio: float | None = None
    return_on_equity: float | None = None
    gross_margin: float | None = None
    operating_margin: float | None = None
    net_margin: float | None = None
    debt_to_equity: float | None = None
    current_ratio: float | None = None
    revenue_growth: float | None = None
    earnings_per_share: float | None = None
    book_value_per_share: float | None = None
    free_cash_flow_per_share: float | None = None


class FundamentalsSnapshot(BaseModel):
    """What an analyst may know about *ticker* as of *as_of*. Newest first."""

    ticker: str
    as_of: str
    sector: str | None = None
    industry: str | None = None
    periods: list[PeriodFundamentals]

    # Derived aggregates (computed in build_snapshot, not by the LLM)
    roe_avg: float | None = None
    net_margin_avg: float | None = None
    gross_margin_trend: float | None = None  # latest minus oldest
    bvps_cagr: float | None = None
    debt_to_equity_latest: float | None = None
    market_cap_latest: float | None = None

    @property
    def content_hash(self) -> str:
        """Stable hash of the fundamentals content — the LLM cache key.

        Excludes `as_of` so two dates between filings share one hash: an
        unchanged snapshot must be free, not a fresh LLM call per date.
        """
        canonical = self.model_dump_json(exclude={"as_of"})
        return hashlib.sha256(canonical.encode()).hexdigest()[:24]

    def not_applicable(self, field: str) -> bool:
        """Whether *field* is structurally absent for this company's sector
        rather than merely unreported. See SECTOR_NOT_APPLICABLE."""
        return field in SECTOR_NOT_APPLICABLE.get(self.sector or "", frozenset())

    def render(self) -> str:
        """Compact text block for the LLM prompt.

        Deliberately date-free (no `as_of`): the prompt cache keys on exact
        prompt text, so the same fundamentals must render identically on any
        date. It also keeps the LLM from anchoring on a calendar date it
        could associate with post-date world events.

        A missing figure renders as n/r or n/a rather than a bare dash, and
        the legend tells the reader what to DO with each. Saying only that
        the two are different leaves a persona free to treat n/r as either
        a zero or a reason to abstain, which is the ambiguity this replaces.
        """
        lines = [
            f"Company: {self.ticker}"
            + (f"  |  Sector: {self.sector}" if self.sector else "")
            + (f"  |  Industry: {self.industry}" if self.industry else ""),
            "All figures below were publicly filed by their filing dates. "
            "Treat the most recent filing shown as the present.",
            "",
            "Two kinds of blank appear below and they mean different things:",
            f"  {NOT_REPORTED}  Not reported: the filing carries no figure we could read for " "this period.",
            "      It does NOT mean zero. A company with no borrowings and one whose debt is",
            "      tagged in a way we do not read both show this, and these figures cannot",
            "      tell them apart. Treat the measure as UNKNOWN: either say so and leave it",
            "      out of your reasoning, or abstain on that dimension and judge on what is",
            f"      visible. Never substitute a number, and never read {NOT_REPORTED} as a low value.",
            f"  {NOT_APPLICABLE}  Not applicable: this line is not reported comparably by this sector.",
            "      Banks, insurers and asset managers publish no gross profit and no classified",
            "      balance sheet, so gross margin and current ratio have nothing behind them,",
            "      and operating margin is not comparable to an industrial's. The question does",
            "      not arise for this business — its absence is not a weakness, and the",
            "      remaining columns are not a fuller picture for being fewer.",
            "",
            "Summary:",
            f"  Market cap (latest filed): {_fmt(self.market_cap_latest, blank=self._blank('market_cap'))}",
            f"  ROE avg: {_fmt(self.roe_avg, blank=self._blank('return_on_equity'))}"
            f"  |  Net margin avg: {_fmt(self.net_margin_avg, blank=self._blank('net_margin'))}",
            "  Gross margin trend (latest-oldest): "
            f"{_fmt(self.gross_margin_trend, blank=self._blank('gross_margin'))}",
            "  Book value/share CAGR: " f"{_fmt(self.bvps_cagr, blank=self._blank('book_value_per_share'))}",
            f"  Debt/equity (latest): {_fmt(self.debt_to_equity_latest, blank=self._blank('debt_to_equity'))}",
            "",
            "History (trailing-twelve-month periods, newest first):",
            "period | filed | mktcap | P/E | ROE | gross_m | op_m | net_m | D/E "
            "| curr | rev_gr | EPS | BVPS | FCF/sh",
        ]

        def cell(period: PeriodFundamentals, field: str) -> str:
            return _fmt(getattr(period, field), blank=self._blank(field))

        for p in self.periods:
            lines.append(
                f"{p.report_period} | {p.filing_date or '?'} | {cell(p, 'market_cap')} "
                f"| {cell(p, 'price_to_earnings_ratio')} | {cell(p, 'return_on_equity')} "
                f"| {cell(p, 'gross_margin')} | {cell(p, 'operating_margin')} "
                f"| {cell(p, 'net_margin')} | {cell(p, 'debt_to_equity')} "
                f"| {cell(p, 'current_ratio')} | {cell(p, 'revenue_growth')} "
                f"| {cell(p, 'earnings_per_share')} | {cell(p, 'book_value_per_share')} "
                f"| {cell(p, 'free_cash_flow_per_share')}"
            )
        return "\n".join(lines)

    def _blank(self, field: str) -> str:
        return NOT_APPLICABLE if self.not_applicable(field) else NOT_REPORTED


def build_snapshot(
    ticker: str,
    as_of: str,
    data_client: DataClient,
    periods: int = 20,
) -> FundamentalsSnapshot:
    """Build the point-in-time snapshot for (ticker, as_of).

    Raises InsufficientData if fewer than MIN_PERIODS filed periods exist.
    Data-layer failures propagate (fail loud) — a broken snapshot must never
    silently become a neutral view.
    """
    metrics = data_client.get_financial_metrics(
        ticker,
        as_of,
        period="ttm",
        limit=periods,
    )
    if len(metrics) < MIN_PERIODS:
        raise InsufficientData(f"{ticker} as of {as_of}: only {len(metrics)} filed periods " f"(need {MIN_PERIODS})")

    # Market cap comes from the most recent FILED metrics row. Deliberately
    # NOT data_client.get_market_cap(): that prefers company_facts.market_cap,
    # which is latest-only — lookahead in a backtest.
    facts = data_client.get_company_facts(ticker)

    rows = [PeriodFundamentals(**m.model_dump(include=set(PeriodFundamentals.model_fields))) for m in metrics]

    return FundamentalsSnapshot(
        ticker=ticker,
        as_of=as_of,
        # Sector/industry are slow-moving company attributes; using latest
        # facts here is an accepted, documented PIT approximation.
        sector=facts.sector if facts else None,
        industry=facts.industry if facts else None,
        periods=rows,
        roe_avg=_avg([m.return_on_equity for m in metrics]),
        net_margin_avg=_avg([m.net_margin for m in metrics]),
        gross_margin_trend=_trend([m.gross_margin for m in metrics]),
        bvps_cagr=_cagr([m.book_value_per_share for m in metrics]),
        debt_to_equity_latest=metrics[0].debt_to_equity,
        market_cap_latest=metrics[0].market_cap,
    )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _fmt(v: float | None, blank: str = NOT_REPORTED) -> str:
    if v is None:
        return blank
    if abs(v) >= 1e9:
        return f"{v / 1e9:.1f}B"
    if abs(v) >= 1e6:
        return f"{v / 1e6:.1f}M"
    return f"{v:.2f}"


def _avg(values: list[float | None]) -> float | None:
    xs = [v for v in values if v is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def _trend(values: list[float | None]) -> float | None:
    """Latest minus oldest (values arrive newest first)."""
    xs = [v for v in values if v is not None]
    return round(xs[0] - xs[-1], 4) if len(xs) >= 2 else None


def _cagr(values: list[float | None]) -> float | None:
    """Annualized growth from oldest to latest (ttm rows are quarter-spaced)."""
    xs = [v for v in values if v is not None]
    if len(xs) < 2 or xs[-1] is None or xs[-1] <= 0 or xs[0] <= 0:
        return None
    years = (len(xs) - 1) / 4  # quarter-spaced ttm periods
    if years <= 0:
        return None
    return round((xs[0] / xs[-1]) ** (1 / years) - 1, 4)
