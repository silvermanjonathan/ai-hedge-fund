"""Universe selection — which names to point the desk at.

Standalone by design: the analysts reason point-in-time from EDGAR, while a
screener reflects today. So this package only picks tickers; the result is
composed into a run with --tickers, never fed to a model.
"""

from hedge_fund.universe.edgar_check import edgar_filter, files_domestic_reports
from hedge_fund.universe.finviz import (
    BASE,
    EXPORT_URL,
    fetch,
    FINVIZ_TOKEN_ENV,
    FinvizError,
    PRESETS,
    resolve,
)

__all__ = ["BASE", "EXPORT_URL", "FINVIZ_TOKEN_ENV", "PRESETS", "FinvizError", "edgar_filter", "fetch", "files_domestic_reports", "resolve"]
