"""Backtest-validation framework: CPCV and PBO. Placeholder, not abandoned.

Combinatorial Purged Cross-Validation and Probability of Backtest
Overfitting (López de Prado). Deliberately empty as of September 2026.

Both measure how much of a backtest's edge is selection rather than signal,
and neither says anything useful until there is a track record to test. The
verdict ledger (hedge_fund/ledger/) is what will supply it: the scorecard
reads `provisional` below 20 scored calls per school, and a call is not
scored until its horizon elapses — 21, 63, or 126 trading days after the
verdict. Planned for once the ledger clears that bar, expected around
December 2026.

Implementing it earlier would mean validating a backtest against a handful
of calls, which is precisely the overfitting these methods exist to detect.

This package is empty on purpose. It is not a stub someone forgot to fill.
"""
