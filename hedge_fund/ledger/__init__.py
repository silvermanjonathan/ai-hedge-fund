"""Verdict ledger, scorecard, and playbook.

The desks produce one verdict per (school, ticker) per cycle, and a school
re-reasons only when a company's filing changes, so the same verdict recurs
unchanged across weeks. This package remembers each distinct verdict once
(the ledger), grades schools on what happened afterwards (the scorecard),
and combines the latest verdicts per ticker into a list for the user to
review (the playbook). Read-only outputs: nothing here places orders, sizes
positions, or writes back into mandates, registries, or the LLM cache.
"""

from hedge_fund.ledger.rules import (
    Candidate,
    HEADER,
    playbook,
    PlaybookConfig,
    render_candidates,
    write_candidates_csv,
)
from hedge_fund.ledger.score import Scorecard, scorecard, ScoreRow
from hedge_fund.ledger.store import DEFAULT_LEDGER_PATH, IngestResult, Ledger

__all__ = [
    "Candidate",
    "DEFAULT_LEDGER_PATH",
    "HEADER",
    "IngestResult",
    "Ledger",
    "PlaybookConfig",
    "ScoreRow",
    "Scorecard",
    "annotate_staleness",
    "facts_lag_warning",
    "playbook",
    "render_candidates",
    "scorecard",
    "write_candidates_csv",
]
