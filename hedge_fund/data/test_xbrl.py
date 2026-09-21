"""XBRL → FinancialMetrics derivation, against a hand-computable synthetic filer.

The fixture is a calendar-year filer with two fiscal years of 10-Qs and
10-Ks, quarterly plus year-to-date income facts, year-to-date-only cash-flow
facts, balance-sheet instants, cover-page share counts, and every prior-year
fact repeated as a comparative (value + 1, later filing) — the shape EDGAR
actually returns. No network.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from hedge_fund.data import xbrl
from hedge_fund.data.prices import DailyHistory
from hedge_fund.data.xbrl import (
    build_rows,
    FactBook,
    first_filed,
    point_in_time,
    shares_outstanding,
)

# (period end, filed, form, quarter start) for two calendar fiscal years.
FILINGS = [
    ("2023-03-31", "2023-05-05", "10-Q", "2023-01-01"),
    ("2023-06-30", "2023-08-04", "10-Q", "2023-04-01"),
    ("2023-09-30", "2023-11-03", "10-Q", "2023-07-01"),
    ("2023-12-31", "2024-02-20", "10-K", "2023-10-01"),
    ("2024-03-31", "2024-05-05", "10-Q", "2024-01-01"),
    ("2024-06-30", "2024-08-04", "10-Q", "2024-04-01"),
    ("2024-09-30", "2024-11-03", "10-Q", "2024-07-01"),
    ("2024-12-31", "2025-02-20", "10-K", "2024-10-01"),
]

REVENUE = [100, 110, 120, 130, 140, 150, 160, 170]
QUARTERLY = {  # quarterly + year-to-date in 10-Qs, full year in the 10-K
    "Revenues": REVENUE,
    "NetIncomeLoss": [r * 0.10 for r in REVENUE],
    "GrossProfit": [r * 0.40 for r in REVENUE],
    "OperatingIncomeLoss": [r * 0.20 for r in REVENUE],
}
EPS = {"EarningsPerShareDiluted": [r * 0.10 / 100 for r in REVENUE]}  # NI / 100 shares
WEIGHTED = {"WeightedAverageNumberOfDilutedSharesOutstanding": [100] * 8}
YTD_ONLY = {  # cash-flow statement: cumulative within the fiscal year only
    "NetCashProvidedByUsedInOperatingActivities": [12, 13, 14, 15, 16, 17, 18, 19],
    "PaymentsToAcquirePropertyPlantAndEquipment": [2] * 8,
}
INSTANTS = {
    "StockholdersEquity": [500, 510, 520, 530, 540, 550, 560, 570],
    "Assets": [1000, 1010, 1020, 1030, 1040, 1050, 1060, 1070],
    "AssetsCurrent": [300] * 8,
    "LiabilitiesCurrent": [150] * 8,
    "LongTermDebtNoncurrent": [200] * 8,
    "LongTermDebtCurrent": [20] * 8,
}
DEI_SHARES = 100


def synthetic_companyfacts(drop: set[str] = frozenset(), extra_quarterly: dict[str, list[float]] | None = None) -> dict:
    """An EDGAR-shaped companyfacts payload for the filer above."""
    quarterly = {**QUARTERLY, **(extra_quarterly or {})}
    own: list[list[tuple[str, str, dict]]] = []  # per filing: (taxonomy/unit key, tag, entry)
    for i, (end, filed, form, q_start) in enumerate(FILINGS):
        fy_start = f"{end[:4]}-01-01"
        qn = i % 4
        accn = f"0000000000-{i:02d}"
        entries: list[tuple[str, str, dict]] = []

        def add(key, tag, val, start=None, end_=end):
            entry = {
                "end": end_,
                "val": val,
                "accn": accn,
                "fy": int(end[:4]),
                "fp": "FY" if form == "10-K" else f"Q{qn + 1}",
                "form": form,
                "filed": filed,
            }
            if start is not None:
                entry["start"] = start
            entries.append((key, tag, entry))

        for tag, values in {**quarterly, **EPS, **WEIGHTED}.items():
            unit = "USD/shares" if tag in EPS else ("shares" if tag in WEIGHTED else "USD")
            # A weighted share count is an average, not a flow: it does not add up.
            cumulative = (lambda lo, hi: values[hi]) if tag in WEIGHTED else (lambda lo, hi: sum(values[lo : hi + 1]))
            if form == "10-Q":
                add(unit, tag, values[i], start=q_start)
                if qn > 0:
                    add(unit, tag, cumulative(i - qn, i), start=fy_start)
            else:
                add(unit, tag, cumulative(i - 3, i), start=fy_start)
        for tag, values in YTD_ONLY.items():
            add("USD", tag, sum(values[i - qn : i + 1]), start=fy_start)
        for tag, values in INSTANTS.items():
            add("USD", tag, values[i])
        add(
            "dei",
            "EntityCommonStockSharesOutstanding",
            DEI_SHARES,
            end_=(date.fromisoformat(filed) - timedelta(days=10)).isoformat(),
        )
        own.append(entries)

    facts: dict[str, dict] = {"dei": {}, "us-gaap": {}}

    def put(key, tag, entry):
        taxonomy = "dei" if key == "dei" else "us-gaap"
        unit = "shares" if key in ("dei", "shares") else key
        if tag in drop:
            return
        facts[taxonomy].setdefault(tag, {"units": {}})["units"].setdefault(unit, []).append(entry)

    for i, entries in enumerate(own):
        for key, tag, entry in entries:
            put(key, tag, entry)
        if i >= 4:  # this year's filing repeats last year's numbers as comparatives
            _, filed, form, _ = FILINGS[i]
            for key, tag, entry in own[i - 4]:
                put(
                    key,
                    tag,
                    {**entry, "val": entry["val"] + 1, "filed": filed, "form": form, "accn": f"0000000000-{i:02d}"},
                )
    return {"cik": 1, "entityName": "Test Co", "facts": facts}


def fake_history(close: float = 20.0) -> DailyHistory:
    """A flat close on every weekday, with a 2:1 split on 2024-06-10."""
    closes = {}
    d = date(2023, 1, 2)
    while d <= date(2025, 3, 31):
        if d.weekday() < 5:
            closes[d] = close
        d += timedelta(days=1)
    return DailyHistory(closes=closes, splits={date(2024, 6, 10): 2.0})


def approx(value):
    return pytest.approx(value, abs=1e-6)


def rows_by_period(rows):
    return {r.report_period: r for r in rows}


@pytest.fixture
def rows():
    return build_rows(synthetic_companyfacts(), ticker="TEST", history=fake_history())


# ---------------------------------------------------------------------------
# Quarter and TTM derivation
# ---------------------------------------------------------------------------


def test_first_filed_wins_over_restatement():
    book = FactBook(synthetic_companyfacts())
    (q1_2023,) = [f for f in book.durations("Revenues") if f.end == date(2023, 3, 31) and f.start == date(2023, 1, 1)]
    assert q1_2023.val == 100  # not the 101 comparative filed in 2024
    assert q1_2023.filed == date(2023, 5, 5)
    assert len([f for f in book.all_facts("Revenues") if f.end == date(2023, 3, 31)]) == 2


def test_first_filed_ties_break_on_accession():
    a = {"end": "2024-03-31", "val": 1, "filed": "2024-05-05", "accn": "b", "form": "10-Q"}
    b = {**a, "val": 2, "accn": "a"}
    (kept,) = first_filed(FactBook({"facts": {"us-gaap": {"X": {"units": {"USD": [a, b]}}}}}).all_facts("X"))
    assert kept.val == 2


def test_q4_from_fy_minus_9m_and_ttm_sums_four_quarters(rows):
    row = rows_by_period(rows)["2024-09-30"]
    # Q4 2023 = FY 460 − 9M 330 = 130; TTM = 130 + 140 + 150 + 160
    assert row.gross_margin == approx(232 / 580)
    assert row.net_margin == approx(58 / 580)
    assert row.operating_margin == approx(116 / 580)


def test_cash_flow_from_ytd_only_facts(rows):
    row = rows_by_period(rows)["2024-09-30"]
    # CFO quarters: Q4'23 = 54 − 39 = 15, Q1'24 = 16, Q2 = 33 − 16 = 17, Q3 = 51 − 33 = 18 -> 66; capex 8; 100 shares
    assert row.free_cash_flow_per_share == approx((66 - 8) / 100)


def test_eps_is_ttm_of_diluted_eps(rows):
    assert rows_by_period(rows)["2024-09-30"].earnings_per_share == approx(0.13 + 0.14 + 0.15 + 0.16)


def test_eps_falls_back_to_net_income_over_weighted_shares():
    rows = build_rows(synthetic_companyfacts(drop={"EarningsPerShareDiluted"}), ticker="TEST", history=fake_history())
    assert rows_by_period(rows)["2024-09-30"].earnings_per_share == approx(58 / 100)


def test_ttm_is_none_when_a_quarter_is_missing():
    facts = synthetic_companyfacts()
    # Lose every revenue fact ending 2023-06-30 (own quarterly and 6M, and the
    # 2024 comparatives): any trailing year that needs Q2-2023 revenue is None.
    facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"] = [
        e for e in facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"] if e["end"] != "2023-06-30"
    ]
    rows = rows_by_period(build_rows(facts, ticker="TEST", history=fake_history()))
    assert rows["2024-03-31"].net_margin is None  # window Q2'23..Q1'24: revenue TTM broke
    assert rows["2024-03-31"].return_on_equity is not None  # net income did not
    assert rows["2024-06-30"].net_margin == approx(54 / 540)  # window Q3'23..Q2'24 is whole


def test_balance_sheet_ratios(rows):
    row = rows_by_period(rows)["2024-09-30"]
    assert row.return_on_equity == approx(58 / 560)
    assert row.debt_to_equity == approx(220 / 560)
    assert row.current_ratio == approx(2.0)
    assert row.book_value_per_share == approx(5.6)


def test_revenue_growth_is_ttm_over_ttm_a_year_earlier(rows):
    by = rows_by_period(rows)
    assert by["2024-09-30"].revenue_growth is None  # no TTM a year earlier yet
    assert by["2024-12-31"].revenue_growth == approx(620 / 460 - 1)


# ---------------------------------------------------------------------------
# Point-in-time
# ---------------------------------------------------------------------------


def test_row_filing_date_is_the_first_filing_of_its_balance_sheet(rows):
    by = rows_by_period(rows)
    assert by["2023-12-31"].filing_date == "2024-02-20"  # the 10-K, not the 2025 comparative
    assert by["2024-09-30"].filing_date == "2024-11-03"


def test_point_in_time_filters_on_filing_date_newest_first(rows):
    visible = point_in_time(rows, "2025-01-15", limit=20)
    assert [r.report_period for r in visible][:2] == ["2024-09-30", "2024-06-30"]
    assert all(r.filing_date <= "2025-01-15" for r in visible)
    assert "2024-12-31" not in {r.report_period for r in visible}  # filed 2025-02-20
    assert len(point_in_time(rows, "2025-01-15", limit=3)) == 3


def test_rows_are_identical_across_end_dates(rows):
    """The LLM-cache property: a later as_of only appends rows."""
    early = {r.report_period: r.model_dump() for r in point_in_time(rows, "2024-12-01", 20)}
    late = {r.report_period: r.model_dump() for r in point_in_time(rows, "2025-03-01", 20)}
    assert early and set(early) < set(late)
    assert all(late[p] == early[p] for p in early)


def test_late_tagged_comparative_is_not_visible_to_earlier_rows():
    facts = synthetic_companyfacts()
    usd = facts["facts"]["us-gaap"]["OperatingIncomeLoss"]["units"]["USD"]
    # Operating income for Q1-2023 first appears in the 2024 filing (as a comparative).
    facts["facts"]["us-gaap"]["OperatingIncomeLoss"]["units"]["USD"] = [
        e for e in usd if not (e["end"] == "2023-03-31" and e["filed"] == "2023-05-05")
    ]
    by = rows_by_period(build_rows(facts, ticker="TEST", history=fake_history()))
    assert by["2023-12-31"].operating_margin is None  # cutoff 2024-02-20: Q1'23 OI unknown
    assert by["2024-03-31"].operating_margin == approx((22 + 24 + 26 + 28) / 500)  # Q2'23..Q1'24


# ---------------------------------------------------------------------------
# Tags, fallbacks, units
# ---------------------------------------------------------------------------


def test_gross_profit_falls_back_to_revenue_minus_cost_of_revenue():
    facts = synthetic_companyfacts(drop={"GrossProfit"}, extra_quarterly={"CostOfRevenue": [r * 0.6 for r in REVENUE]})
    assert rows_by_period(build_rows(facts, ticker="TEST", history=fake_history()))[
        "2024-09-30"
    ].gross_margin == approx(0.4)


def test_tag_priority_resolves_per_period_end():
    """A filer that switched revenue tags mid-history keeps a continuous series."""
    facts = synthetic_companyfacts()
    usd = facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"]
    facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"] = [e for e in usd if e["end"] < "2024-01-01"]
    facts["facts"]["us-gaap"]["RevenueFromContractWithCustomerExcludingAssessedTax"] = {
        "units": {"USD": [e for e in usd if e["end"] >= "2024-01-01"]}
    }
    assert rows_by_period(build_rows(facts, ticker="TEST", history=fake_history()))["2024-09-30"].net_margin == approx(
        58 / 580
    )


def test_bank_like_filer_has_rows_with_none_margins():
    facts = synthetic_companyfacts(
        drop={"Revenues", "GrossProfit", "OperatingIncomeLoss", "AssetsCurrent", "LiabilitiesCurrent"}
    )
    row = rows_by_period(build_rows(facts, ticker="BANK", history=fake_history()))["2024-09-30"]
    assert row.gross_margin is None and row.current_ratio is None and row.revenue_growth is None
    assert row.return_on_equity == approx(58 / 560)
    assert row.book_value_per_share == approx(5.6)


def test_non_usd_units_and_other_forms_are_ignored():
    facts = synthetic_companyfacts()
    facts["facts"]["us-gaap"]["Revenues"]["units"]["EUR"] = [
        {"start": "2024-07-01", "end": "2024-09-30", "val": 9e9, "filed": "2024-11-03", "accn": "x", "form": "10-Q"}
    ]
    facts["facts"]["us-gaap"]["Revenues"]["units"]["USD"].append(
        {"start": "2024-07-01", "end": "2024-09-30", "val": 9e9, "filed": "2024-10-01", "accn": "y", "form": "8-K"}
    )
    assert rows_by_period(build_rows(facts, ticker="TEST", history=fake_history()))["2024-09-30"].net_margin == approx(
        58 / 580
    )


def test_debt_composition_priority():
    base = synthetic_companyfacts()
    assert rows_by_period(build_rows(base, ticker="T"))["2024-09-30"].debt_to_equity == approx(220 / 560)
    # DebtCurrent present -> used instead of the pieces
    with_current = synthetic_companyfacts(extra_quarterly={})
    with_current["facts"]["us-gaap"]["DebtCurrent"] = {
        "units": {"USD": [{"end": "2024-09-30", "val": 50, "filed": "2024-11-03", "accn": "z", "form": "10-Q"}]}
    }
    assert rows_by_period(build_rows(with_current, ticker="T"))["2024-09-30"].debt_to_equity == approx(250 / 560)
    # no noncurrent tag -> LongTermDebt is the whole, current pieces skipped
    total_only = synthetic_companyfacts(drop={"LongTermDebtNoncurrent"})
    total_only["facts"]["us-gaap"]["LongTermDebt"] = {
        "units": {"USD": [{"end": "2024-09-30", "val": 300, "filed": "2024-11-03", "accn": "z", "form": "10-Q"}]}
    }
    assert rows_by_period(build_rows(total_only, ticker="T"))["2024-09-30"].debt_to_equity == approx(300 / 560)


# ---------------------------------------------------------------------------
# Shares, price join, rounding
# ---------------------------------------------------------------------------


def test_market_cap_uses_last_close_on_or_before_filing_date_times_dei_shares(rows):
    # Filed Sunday 2024-11-03 -> Friday 2024-11-01 close 20 × 100 shares
    row = rows_by_period(rows)["2024-09-30"]
    assert row.market_cap == approx(2000)
    assert row.price_to_earnings_ratio == approx(round(2000 / 58, 1))  # 3 significant figures


def test_market_cap_undoes_later_splits(rows):
    # Filed 2024-05-05, before the 2:1 split on 2024-06-10: quoted price was 40, not 20
    assert rows_by_period(rows)["2024-03-31"].market_cap == approx(4000)


def test_market_cap_none_without_history():
    row = rows_by_period(build_rows(synthetic_companyfacts(), ticker="T"))["2024-09-30"]
    assert row.market_cap is None and row.price_to_earnings_ratio is None
    assert row.net_margin is not None


def test_multi_class_cover_page_counts_are_summed():
    facts = synthetic_companyfacts()
    dei = facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
    class_b = [{**e, "val": 25} for e in dei if e["filed"] == "2024-11-03"]
    dei.extend(class_b)
    assert shares_outstanding(FactBook(facts), date(2024, 9, 30), date(2024, 11, 3)) == 125
    assert shares_outstanding(FactBook(facts), date(2024, 6, 30), date(2024, 8, 4)) == 100


def test_shares_fall_back_to_balance_sheet_count():
    facts = synthetic_companyfacts(drop={"EntityCommonStockSharesOutstanding"})
    facts["facts"]["us-gaap"]["CommonStockSharesOutstanding"] = {
        "units": {"shares": [{"end": "2024-09-30", "val": 80, "filed": "2024-11-03", "accn": "z", "form": "10-Q"}]}
    }
    assert rows_by_period(build_rows(facts, ticker="T"))["2024-09-30"].book_value_per_share == approx(560 / 80)


def test_rounding_policy():
    rows = build_rows(synthetic_companyfacts(), ticker="T", history=fake_history(close=12.3456))
    row = rows_by_period(rows)["2024-09-30"]
    assert row.market_cap == 1230  # 1234.56 -> 3 significant figures
    assert row.return_on_equity == round(58 / 560, 6)


def test_merge_companyfacts_keeps_first_reported_values():
    from hedge_fund.data.xbrl import merge_companyfacts

    predecessor = synthetic_companyfacts()
    # The successor reports only 2024-09-30 onward, repeating 2023-09-30 as a comparative (+1).
    successor = {"cik": 2, "entityName": "Successor", "facts": {"us-gaap": {}, "dei": {}}}
    for taxonomy in ("us-gaap", "dei"):
        for tag, body in predecessor["facts"][taxonomy].items():
            for unit, entries in body["units"].items():
                kept = [
                    dict(
                        e,
                        accn="succ",
                        filed="2024-11-03",
                        **({"val": e["val"] + 1} if e["end"] == "2023-09-30" else {}),
                    )
                    for e in entries
                    if e["end"] >= "2024-09-30" or e["end"] == "2023-09-30"
                ]
                if kept:
                    successor["facts"][taxonomy].setdefault(tag, {"units": {}})["units"][unit] = kept
    merged = merge_companyfacts([successor, predecessor])
    rows = rows_by_period(build_rows(merged, ticker="T", history=fake_history()))
    assert "2023-03-31" in rows and "2024-09-30" in rows  # history from both filers
    assert rows["2023-09-30"].filing_date == "2023-11-03"  # predecessor's original filing, not the comparative
    assert rows["2024-09-30"].net_margin == approx(58 / 580)


# ---------------------------------------------------------------------------
# Debt assembly — named for the filers that break it, not the lines
# ---------------------------------------------------------------------------


def _debt_book(**tags):
    """A FactBook carrying only the given instant tags, one fact each."""
    facts = {
        tag: {"units": {"USD": [{"end": "2026-06-30", "val": val, "filed": "2026-08-01", "accn": "a", "form": "10-Q"}]}}
        for tag, val in tags.items()
    }
    return xbrl.FactBook({"facts": {"us-gaap": facts}})


END, CUTOFF = date(2026, 6, 30), date(2026, 9, 1)


def test_filer_with_only_short_term_borrowings():
    """The case the original fallthrough missed.

    total_debt skipped the current pieces whenever there was no noncurrent
    tag, because LongTermDebt is the whole when it exists and adding the
    pieces would double count. But when LongTermDebt is absent TOO, that
    returned None — so a filer whose only borrowing is short-term read as
    having no debt at all, and its debt/equity came back blank. Two of the
    78 screened names were in exactly this state.
    """
    book = _debt_book(ShortTermBorrowings=250.0, StockholdersEquity=1000.0)
    assert xbrl.total_debt(book, END, CUTOFF) == 250.0


def test_filer_with_only_a_current_debt_tag():
    book = _debt_book(DebtCurrent=400.0)
    assert xbrl.total_debt(book, END, CUTOFF) == 400.0


def test_long_term_debt_is_still_the_whole_and_pieces_are_not_added():
    """The behaviour the fallthrough was protecting. LongTermDebt already
    includes the current portion for these filers, so adding
    LongTermDebtCurrent on top would double count it."""
    book = _debt_book(LongTermDebt=900.0, LongTermDebtCurrent=100.0)
    assert xbrl.total_debt(book, END, CUTOFF) == 900.0


def test_noncurrent_plus_current_when_both_are_reported():
    book = _debt_book(LongTermDebtNoncurrent=800.0, DebtCurrent=150.0)
    assert xbrl.total_debt(book, END, CUTOFF) == 950.0


def test_a_filer_with_no_borrowings_tagged_still_reports_nothing():
    """Absence is not zero. Five of 34 names carried a real balance under a
    tag the mapping did not read, so an untagged filer cannot be assumed
    debt-free — the snapshot says 'not reported', never '0'."""
    assert xbrl.total_debt(_debt_book(StockholdersEquity=1000.0), END, CUTOFF) is None


@pytest.mark.parametrize(
    "tag",
    ["SeniorNotes", "UnsecuredLongTermDebt", "UnsecuredDebtCurrent", "ConvertibleDebtCurrent", "LinesOfCreditCurrent"],
)
def test_debt_tags_added_from_the_september_survey(tag):
    """Each was found carrying a real borrowing balance for a screened name
    whose debt/equity was blank. Named individually so removing one fails
    loudly rather than silently re-blanking a filer."""
    assert xbrl.total_debt(_debt_book(**{tag: 500.0}), END, CUTOFF) == 500.0


def test_cost_of_revenue_reads_the_excluding_dda_tag():
    """NYT and four others file cost of revenue only under this tag, so
    gross margin was blank although the filing carried it."""
    assert "CostOfGoodsAndServiceExcludingDepreciationDepletionAndAmortization" in xbrl.COST_OF_REVENUE
