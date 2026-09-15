"""Finviz Elite screener export → a ticker universe.

Finviz's Screener API is the screener URL with ``screener`` replaced by
``export/screener``; it answers a GET with ``v`` (view), ``f`` (filters),
``c`` (columns), and ``auth`` (the Elite token) as CSV. The legacy
``export.ashx`` path answers with a 301, so it is never used here.

Two honesty notes, both deliberate:

- The filter TOKENS in the presets below are verified Finviz filter ids; the
  numeric THRESHOLDS in them (ROE over 15, gross margin over 40, ...) are
  editorial choices, meant to be tuned for candidate count, not a published
  method.
- Finviz values are a CURRENT snapshot. This module selects a universe; it
  never feeds the analysts, who reason point-in-time from EDGAR filings.
  Composing today's screen with a historical backtest is a mild look-ahead
  in universe selection (survivorship), and the user should know that.

Results are cached under ~/.hedge-fund/cache/finviz for a day, keyed by the
filter string, with the atomic-write pattern of hedge_fund/data/edgar.py.
Fail-loud: a non-200 raises FinvizError; an empty export is an empty list.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import threading
import time
from pathlib import Path

import requests

from hedge_fund.paths import CACHE_DIR

logger = logging.getLogger(__name__)

FINVIZ_TOKEN_ENV = "FINVIZ_AUTH_TOKEN"
EXPORT_URL = "https://elite.finviz.com/export/screener"
DEFAULT_CACHE_DIR = CACHE_DIR / "finviz"
TTL = 24 * 3600
# Finviz column ids for the export's ``c`` parameter: 1 is Ticker. The CSV is
# parsed by header name, never by position, so a wrong id shows up as a
# missing "Ticker" header (FinvizError), not as silently wrong data.
TICKER_COLUMN = "1"
USER_AGENT = "aihf universe module (https://github.com/virattt/ai-hedge-fund)"

# US-listed common stocks, mid-cap and up, public five years, liquid, not a
# penny stock: the population a filing-driven fundamentals desk can analyse.
BASE = "geo_usa,ind_stocksonly,cap_midover,ipodate_more5,sh_avgvol_o500,sh_price_o5"
PRESETS = {
    "quality": BASE + ",fa_roe_o15,fa_grossmargin_o40,fa_debteq_u0.5,fa_pfcf_u50,fa_curratio_o1.5",
    "value": BASE + ",fa_pe_u15,fa_pe_profitable,fa_pb_u2,fa_debteq_u0.5,fa_curratio_o1.5",
    "garp": BASE + ",fa_epsqoq_o10,fa_salesqoq_o10,fa_eps5years_pos,fa_sales5years_pos,fa_pe_u25,fa_pe_profitable",
    "bearish": BASE + ",fa_debteq_o1,fa_opermargin_neg,fa_curratio_u1,fa_roa_neg",
    # Schloss territory: below book, single-digit-ish earnings, clean balance sheet.
    "deep_value": BASE + ",fa_pe_u12,fa_pe_profitable,fa_pb_u1,fa_debteq_u0.5,fa_curratio_o1.5",
}

_MEMO: dict[str, tuple[float, list[str]]] = {}
_MEMO_LOCK = threading.Lock()


class FinvizError(Exception):
    """The export could not be fetched or was not a screener CSV."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def resolve(preset_or_filters: str) -> tuple[str | None, str]:
    """(preset name or None, filter string) for a preset name or a raw filter string."""
    key = preset_or_filters.strip()
    if key in PRESETS:
        return key, PRESETS[key]
    return None, key


def token(explicit: str | None = None) -> str:
    """The Elite token, or a failure naming the variable to set."""
    value = (explicit or os.environ.get(FINVIZ_TOKEN_ENV, "")).strip()
    if not value:
        raise FinvizError(f"{FINVIZ_TOKEN_ENV} is not set. It is the auth= value Finviz Elite appends to Screener export URLs; export it or add it to ~/.hedge-fund/.env.")
    return value


def download(filters: str, *, auth: str, session=None, timeout: float = 30.0):
    """One export request; the raw response (the live test inspects it)."""
    http = session if session is not None else requests
    return http.get(
        EXPORT_URL,
        params={"v": "111", "f": filters, "c": TICKER_COLUMN, "auth": auth},
        headers={"User-Agent": USER_AGENT},
        timeout=timeout,
        allow_redirects=True,
    )


def parse(text: str) -> list[str]:
    """Tickers from an export CSV, by the "Ticker" header. Empty body -> []."""
    if not text.strip():
        return []
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or "Ticker" not in reader.fieldnames:
        raise FinvizError(f"export has no Ticker column (header: {reader.fieldnames}); a login page or a changed column id")
    return [row["Ticker"].strip().upper() for row in reader if (row.get("Ticker") or "").strip()]


def fetch(
    preset_or_filters: str,
    *,
    limit: int | None = None,
    refresh: bool = False,
    auth: str | None = None,
    session=None,
    cache_dir: Path | str = DEFAULT_CACHE_DIR,
) -> list[str]:
    """Tickers matching a preset or a raw Finviz filter string, in export order.

    Cached on disk for a day unless *refresh*; *limit* truncates.
    """
    preset, filters = resolve(preset_or_filters)
    key = hashlib.sha1(filters.encode()).hexdigest()
    path = Path(cache_dir) / f"{key}.csv"
    now = time.time()

    tickers: list[str] | None = None
    if not refresh:
        with _MEMO_LOCK:
            hit = _MEMO.get(key)
        if hit is not None and now - hit[0] < TTL:
            tickers = hit[1]
        else:
            text = _read(path, now)
            if text is not None:
                tickers = parse(text)
    if tickers is None:
        resp = download(filters, auth=token(auth), session=session)
        if resp.status_code != 200:
            raise FinvizError(f"Finviz export returned {resp.status_code} for {preset or 'custom filters'}", status_code=resp.status_code)
        tickers = parse(resp.text)
        _write(path, resp.text)
        logger.info("finviz %s: %d tickers (%s)", preset or "custom", len(tickers), filters)
    with _MEMO_LOCK:
        _MEMO[key] = (now, tickers)
    return tickers[:limit] if limit is not None else list(tickers)


def _read(path: Path, now: float) -> str | None:
    try:
        if now - path.stat().st_mtime >= TTL:
            return None
        return path.read_text()
    except OSError:
        return None


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(text)
    os.replace(tmp, path)
