"""The data layer's failure type.

Every provider raises a DataClientError subclass on infrastructure failure
(auth, rate limit, network, server error). Nothing in hedge_fund/signals or
hedge_fund/pipeline catches it: a broken data source must crash a cycle, not
read as "no signal" (see hedge_fund/data/protocol.py for the contract).
"""

from __future__ import annotations


class DataClientError(Exception):
    """A data request failed for infrastructure reasons. Distinct from "no
    data exists" — that returns empty. A backtest must crash on this."""

    def __init__(self, message: str, *, status_code: int | None = None, path: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.path = path
