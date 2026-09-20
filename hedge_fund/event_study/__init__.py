"""Event study framework: CARs around earnings. Complete, fd-only, unwired.

CARs around earnings, a market model for expected returns, cross-sectional
t-tests and bootstrap confidence intervals.

Status as of September 2026 — read this before concluding it is abandoned:

- **The library is finished.** `compute_car()` is the entry point and does
  the whole job: fetches the market benchmark once, computes per-event CARs
  per ticker, then aggregates cross-sectionally by source type with t-tests
  and bootstrap CIs, returning an EventStudyResult. stats.py has the market
  model, abnormal returns, CAR summation, t-test and bootstrap. 14 tests
  cover it offline; 2 more are skipped without a Financial Datasets key.

- **It needs `--data fd`.** Earnings events come from earnings history with
  consensus surprises, which the free EDGAR + Yahoo source does not serve —
  the same constraint that blocks the `pead` model. Nothing here runs on the
  default data source.

- **Nothing imports it, on purpose.** It is analysis, not part of the
  trading pipeline: no entry point, no mandate, and no strategy reaches it.
  `python -m hedge_fund.event_study` is the only way in, and that demo has
  drifted from this library (see its own NOTE) — it hand-rolls what
  compute_car already does and never renders the aggregates.

Kept rather than finished. Wiring the demo back to compute_car() is perhaps
an hour, but it would produce a CLI that cannot run without paid data, so
it is not worth doing until that changes. What was missing was this note:
a vestigial discarded call in __main__.py read as a half-built feature
because nothing recorded that the library behind it was done.
"""

from hedge_fund.event_study.engine import compute_car
from hedge_fund.event_study.models import (
    AggregateResult,
    BootstrapCI,
    EventCAR,
    EventStudyResult,
    MarketModelFit,
    WindowStats,
)
from hedge_fund.event_study.plot import (
    plot_car_by_source,
    plot_car_distribution,
    plot_cumulative_ar,
)

__all__ = [
    "compute_car",
    "AggregateResult",
    "BootstrapCI",
    "EventCAR",
    "EventStudyResult",
    "MarketModelFit",
    "WindowStats",
    "plot_car_by_source",
    "plot_car_distribution",
    "plot_cumulative_ar",
]
