"""v2 backtesting — simulate a fund (or a single alpha model) over history."""

from hedge_fund.backtesting.engine import BacktestEngine
from hedge_fund.backtesting.fund import (
    backtest_fund,
    DEFAULT_BACKTEST_WEEKS,
    FundBacktestMetrics,
    FundBacktestResult,
    rebalance_grid,
)
from hedge_fund.backtesting.models import BacktestResult, PerformanceMetrics, Trade

__all__ = [
    "BacktestEngine",
    "DEFAULT_BACKTEST_WEEKS",
    "BacktestResult",
    "FundBacktestMetrics",
    "FundBacktestResult",
    "PerformanceMetrics",
    "Trade",
    "backtest_fund",
    "rebalance_grid",
]
