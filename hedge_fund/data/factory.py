"""Which data source a run uses, and one place that builds it.

HEDGE_FUND_DATA (or `aihf --data`) picks the source, the same seam pattern as
HEDGE_FUND_LLM_MODEL: the CLI flag writes the variable, and every layer —
CLI, TUI workers, scripts — reads it here.

    free  SEC EDGAR fundamentals + Yahoo Finance prices; needs
          HEDGE_FUND_SEC_USER_AGENT (the SEC's required contact). Default.
    fd    Financial Datasets; needs FINANCIAL_DATASETS_API_KEY.

Each source gets its own CachedDataClient directory: the cache keys carry
method and params but not the provider, so two sources sharing one directory
would silently serve each other's answers.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path

from hedge_fund.data.cached import CachedDataClient
from hedge_fund.data.client import FDClient
from hedge_fund.data.edgar import SEC_USER_AGENT_ENV
from hedge_fund.data.free import FreeDataClient
from hedge_fund.paths import CACHE_DIR

DATA_SOURCE_ENV = "HEDGE_FUND_DATA"
# Truthy: ignore the per-source disk cache for this process and rewrite it
# (`aihf --refresh-data`). The raw EDGAR and Yahoo payloads keep their TTLs.
DATA_REFRESH_ENV = "HEDGE_FUND_DATA_REFRESH"
DATA_SOURCES = ("free", "fd")
DEFAULT_DATA_SOURCE = "free"

_LABELS = {"free": "SEC EDGAR + Yahoo Finance", "fd": "Financial Datasets"}
_REQUIRED_ENV = {"free": SEC_USER_AGENT_ENV, "fd": "FINANCIAL_DATASETS_API_KEY"}
_CACHE_DIRS = {"free": CACHE_DIR / "data-free", "fd": CACHE_DIR / "data"}
# Alpha models whose data the free source cannot serve (they need earnings
# history with consensus surprises). Names, not classes: importing
# hedge_fund.signals here would close an import cycle.
_UNSUPPORTED_MODELS = {"free": frozenset({"pead"}), "fd": frozenset()}


def data_source() -> str:
    """The selected source: HEDGE_FUND_DATA, else the default."""
    source = os.environ.get(DATA_SOURCE_ENV, "").strip().lower() or DEFAULT_DATA_SOURCE
    if source not in DATA_SOURCES:
        raise ValueError(f"{DATA_SOURCE_ENV}={source!r} is not a data source; choose one of {', '.join(DATA_SOURCES)}")
    return source


def data_refresh() -> bool:
    """Whether HEDGE_FUND_DATA_REFRESH asks for the disk cache to be bypassed."""
    return os.environ.get(DATA_REFRESH_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def provider_label(source: str | None = None) -> str:
    return _LABELS[source or data_source()]


def required_env_var(source: str | None = None) -> str:
    return _REQUIRED_ENV[source or data_source()]


def missing_data_key(source: str | None = None) -> str | None:
    """The environment variable the source needs and does not have, or None."""
    env_var = required_env_var(source)
    return None if os.environ.get(env_var, "").strip() else env_var


def unsupported_model_names(names: Iterable[str], source: str | None = None) -> list[str]:
    """Which of these alpha-model names the source cannot feed, in order."""
    blocked = _UNSUPPORTED_MODELS[source or data_source()]
    seen: list[str] = []
    for name in names:
        if name in blocked and name not in seen:
            seen.append(name)
    return seen


def cache_dir_for(source: str | None = None) -> Path:
    return _CACHE_DIRS[source or data_source()]


def make_data_client(source: str | None = None):
    """The raw (uncached) client for a source; a context manager either way."""
    source = source or data_source()
    if source == "fd":
        return FDClient()
    return FreeDataClient()


@contextmanager
def open_data_client(source: str | None = None, refresh: bool | None = None) -> Iterator[CachedDataClient]:
    """The cached client for a source, closing the raw one on exit::

    with open_data_client() as fd:
        record = run_cycle(fund, as_of, broker, fd, universe)

    *refresh* None defers to HEDGE_FUND_DATA_REFRESH. A cached answer that
    has since become wrong (an empty result from before a data fix, say) is
    otherwise served forever, by design.
    """
    source = source or data_source()
    if refresh is None:
        refresh = data_refresh()
    with make_data_client(source) as raw:
        yield CachedDataClient(raw, cache_dir=cache_dir_for(source), refresh=refresh)
