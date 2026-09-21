"""backtest_fund tests — fake data client + fake analysts, real broker + pipeline."""

import math

import pytest

from hedge_fund.backtesting.fund import (
    _metrics,
    backtest_fund,
    rebalance_grid,
    running_metrics,
)
from hedge_fund.data.models import Price
from hedge_fund.fund.spec import Fund, FundSpec
from hedge_fund.models import Signal

# ---------------------------------------------------------------------------
# Fakes (date-aware variants of the run_cycle test fakes)
# ---------------------------------------------------------------------------


class FakeDataClient:
    """Canned closes per ticker per date: {ticker: {date: close}}."""

    def __init__(self, series):
        self._series = series

    def get_prices(self, ticker, start_date, end_date, **kwargs):
        days = self._series.get(ticker, {})
        return [
            Price(open=close, close=close, high=close, low=close, volume=1000, time=f"{day}T00:00:00Z")
            for day, close in sorted(days.items())
            if start_date <= day <= end_date
        ]


class FakeAnalyst:
    """Fixed conviction per ticker, on every date."""

    def __init__(self, name, views=None):
        self._name = name
        self._views = views or {}

    @property
    def name(self):
        return self._name

    def predict(self, ticker, date, data_client):
        return Signal(model_name=self._name, ticker=ticker, date=date, value=self._views.get(ticker, 0.0))


def _spec(**overrides):
    base = dict(
        name="test-fund",
        strategies=[{"name": "solo", "models": [{"name": "a"}]}],
        risk={"max_position_pct": 1.0, "max_gross_exposure": 1.0},
        capital=100_000.0,
        rebalance="weekly",
    )
    return FundSpec(**{**base, **overrides})


# Three trading weeks (Mon–Fri). Weekly grid = each Friday.
WEEKDAYS = [
    "2024-06-03",
    "2024-06-04",
    "2024-06-05",
    "2024-06-06",
    "2024-06-07",
    "2024-06-10",
    "2024-06-11",
    "2024-06-12",
    "2024-06-13",
    "2024-06-14",
    "2024-06-17",
    "2024-06-18",
    "2024-06-19",
    "2024-06-20",
    "2024-06-21",
]
FRIDAYS = ["2024-06-07", "2024-06-14", "2024-06-21"]

# Closes chosen so 100k always targets exactly 500 AAPL shares — the fund
# buys once and then correctly has nothing to trade.
SERIES = {
    "SPY": {day: close for day, close in zip(FRIDAYS, [100.0, 102.0, 101.0])},
    "AAPL": {day: close for day, close in zip(FRIDAYS, [200.0, 210.0, 190.0])},
}


def _run(series=SERIES, spec=None):
    spec = spec or _spec()
    fund = Fund(spec, models={"solo": [FakeAnalyst("a", views={"AAPL": 1.0})]})
    return backtest_fund(fund, "2024-06-03", "2024-06-21", FakeDataClient(series), ["AAPL"])


# ---------------------------------------------------------------------------
# rebalance_grid
# ---------------------------------------------------------------------------


def test_grid_daily_is_identity():
    assert rebalance_grid(WEEKDAYS, "daily") == WEEKDAYS


def test_grid_weekly_takes_last_trading_day_of_each_iso_week():
    # A short holiday week (no Friday) still contributes its last day.
    days = ["2024-06-27", "2024-06-28", "2024-07-01", "2024-07-02", "2024-07-05"]
    assert rebalance_grid(days, "weekly") == ["2024-06-28", "2024-07-05"]
    assert rebalance_grid(WEEKDAYS, "weekly") == FRIDAYS


def test_grid_monthly_splits_where_weekly_does_not():
    # Dec 30 2024 – Jan 3 2025 is ONE ISO week but TWO calendar months.
    days = ["2024-12-30", "2024-12-31", "2025-01-02", "2025-01-03"]
    assert rebalance_grid(days, "weekly") == ["2025-01-03"]
    assert rebalance_grid(days, "monthly") == ["2024-12-31", "2025-01-03"]


def test_grid_unknown_cadence_raises():
    with pytest.raises(ValueError, match="cadence"):
        rebalance_grid(WEEKDAYS, "hourly")


# ---------------------------------------------------------------------------
# backtest_fund
# ---------------------------------------------------------------------------


def test_happy_path_hand_computed():
    result = _run()

    assert result.dates == FRIDAYS
    assert len(result.records) == 3
    # Week 1: buy 500 @ 200 (full conviction, 100% cap). Weeks 2-3: the
    # closes are chosen so the target stays exactly 500 shares — no churn.
    assert result.records[0].positions == {"AAPL": 500}
    assert result.nav == [100_000.0, 105_000.0, 95_000.0]
    assert result.metrics.n_orders == 1
    # Benchmark scaled to starting capital off its first grid close.
    assert result.benchmark_nav == [100_000.0, 102_000.0, 101_000.0]

    m = result.metrics
    assert m.total_return_pct == pytest.approx(-0.05)
    assert m.benchmark_return_pct == pytest.approx(0.01)
    assert m.excess_return_pct == pytest.approx(-0.06)
    # Peak 105k -> trough 95k.
    assert m.max_drawdown_pct == pytest.approx(10_000 / 105_000, abs=1e-6)
    assert m.n_cycles == 3


def test_positions_carry_across_cycles_not_restart():
    result = _run()
    # Same book all three weeks; only the marks moved.
    assert [r.positions for r in result.records] == [{"AAPL": 500}] * 3
    assert result.records[1].orders == []
    assert result.records[2].orders == []


def test_deterministic_json_round_trip():
    first, second = _run(), _run()
    assert first.model_dump_json() == second.model_dump_json()
    from hedge_fund.backtesting.fund import FundBacktestResult

    assert FundBacktestResult.model_validate_json(first.model_dump_json()) == first


def test_on_cycle_fires_per_tick_in_order():
    seen = []
    spec = _spec()
    fund = Fund(spec, models={"solo": [FakeAnalyst("a", views={"AAPL": 1.0})]})
    backtest_fund(
        fund,
        "2024-06-03",
        "2024-06-21",
        FakeDataClient(SERIES),
        ["AAPL"],
        on_cycle=lambda i, n, record: seen.append((i, n, record.as_of)),
    )
    assert seen == [(0, 3, FRIDAYS[0]), (1, 3, FRIDAYS[1]), (2, 3, FRIDAYS[2])]


def test_universe_round_trips_onto_the_result():
    """The study's tickers are recorded — the mandate never held them."""
    result = _run()
    assert result.universe == ["AAPL"]
    assert all(r.universe == ["AAPL"] for r in result.records)


def test_missing_benchmark_raises():
    series = {"AAPL": SERIES["AAPL"]}  # no SPY bars at all
    with pytest.raises(ValueError, match="trading grid"):
        _run(series=series)


def test_grid_follows_mandate_cadence():
    spec = _spec(rebalance="monthly")
    result = _run(spec=spec)
    assert result.dates == ["2024-06-21"]  # one June rebalance
    assert result.rebalance == "monthly"


# ---------------------------------------------------------------------------
# running_metrics — one implementation, two callers
# ---------------------------------------------------------------------------
#
# Sharpe and max drawdown were computed twice until Sept 2026: once in
# _metrics with numpy for the final record, once in tui/app.py with
# `statistics` for the live tiles. They agreed, but nothing held them
# together, the TUI copy was untested, and the TUI reached across a package
# boundary for the private _PERIODS_PER_YEAR to do it.
#
# These tests pin the formula and assert the two call sites cannot diverge.


def test_running_metrics_constant_return_is_zero_sharpe_not_infinity():
    """Zero dispersion must give 0.0, not a division by zero.

    Doubling each period keeps the returns exactly 1.0 in binary floating
    point. A "+10% every period" curve would NOT work here: 133.1 / 121 is
    1.1000000000000001, and that last-bit dispersion produces a Sharpe in
    the hundreds. Real curves always have dispersion, so this is a property
    of the ratio rather than a bug — but it makes a near-constant series a
    bad way to test the zero branch.
    """
    sharpe, max_dd = running_metrics(100.0, [200.0, 400.0, 800.0], "weekly")
    assert sharpe == 0.0
    assert max_dd == 0.0


def test_running_metrics_matches_a_hand_computation():
    capital, nav = 100.0, [110.0, 99.0, 118.8]
    returns = [0.10, -0.10, 0.20]
    mean_r = sum(returns) / 3
    var = sum((r - mean_r) ** 2 for r in returns) / 2  # ddof=1
    expected = mean_r / math.sqrt(var) * math.sqrt(52)

    sharpe, max_dd = running_metrics(capital, nav, "weekly")
    assert sharpe == pytest.approx(expected, rel=1e-9)
    assert max_dd == pytest.approx((110.0 - 99.0) / 110.0, rel=1e-9)


def test_running_metrics_annualizes_by_cadence():
    capital, nav = 100.0, [110.0, 99.0, 118.8]
    weekly, _ = running_metrics(capital, nav, "weekly")
    daily, _ = running_metrics(capital, nav, "daily")
    monthly, _ = running_metrics(capital, nav, "monthly")
    assert daily == pytest.approx(weekly * math.sqrt(252 / 52), rel=1e-9)
    assert monthly == pytest.approx(weekly * math.sqrt(12 / 52), rel=1e-9)


def test_running_metrics_counts_the_first_period():
    """Capital is prepended inside the function. A first-tick move must show
    up in the drawdown, which it cannot if the opening value is dropped."""
    sharpe, max_dd = running_metrics(100.0, [80.0, 80.0], "weekly")
    assert max_dd == pytest.approx(0.20, rel=1e-9)


@pytest.mark.parametrize("nav", [[], [100.0]])
def test_running_metrics_degenerate_curves_are_zero_not_nan(nav):
    """Under two periods there is no dispersion to divide by. The TUI calls
    this after the FIRST cycle, so this path renders on every replay."""
    sharpe, max_dd = running_metrics(100.0, nav, "weekly")
    assert sharpe == 0.0
    assert math.isfinite(max_dd)


def test_live_tiles_and_final_record_cannot_diverge():
    """The fitness function for F3.

    The TUI calls running_metrics after each cycle with the NAV curve so
    far; the engine calls it once at the end with the whole curve. Feeding
    the engine's final curve through the incremental path must land on the
    engine's own reported numbers, or the tiles lie at the end of a replay.
    """
    capital = 100_000.0
    nav = [101_000.0, 99_500.0, 104_250.0, 98_900.0, 107_400.0]

    # What the TUI shows after the last cycle.
    live_sharpe, live_dd = running_metrics(capital, nav, "weekly")

    # What _metrics puts on the record, through the real code path.
    metrics = _metrics(
        capital=capital,
        grid=["2025-01-06", "2025-01-13", "2025-01-20", "2025-01-27", "2025-02-03"],
        nav=nav,
        benchmark_nav=[100_500.0, 100_200.0, 101_000.0, 100_800.0, 102_000.0],
        cadence="weekly",
        records=[],
    )

    assert live_sharpe == pytest.approx(metrics.sharpe_ratio, abs=5e-5)  # record rounds to 4dp
    assert live_dd == pytest.approx(metrics.max_drawdown_pct, abs=5e-7)  # record rounds to 6dp
