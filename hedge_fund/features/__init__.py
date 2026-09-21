"""v2 feature engineering.

Point-in-time fundamentals snapshots for LLM analysts; later: earnings
surprise features, KPI momentum, cross-sector lead-lag, feature importance
(MDA/MDI/SFI).
"""

from hedge_fund.features.snapshot import (
    build_snapshot,
    FundamentalsSnapshot,
    InsufficientData,
    PeriodFundamentals,
)

__all__ = [
    "FundamentalsSnapshot",
    "InsufficientData",
    "PeriodFundamentals",
    "build_snapshot",
]
