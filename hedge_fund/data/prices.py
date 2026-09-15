"""Daily price sources for the free data client.

`PriceSource` is the seam: two methods, daily bars for a window and the full
daily history (closes plus splits) used to put a market cap on a filing date.
`YFinancePrices` is the default. A Finviz Elite export could replace it with
no other change — see the note at the bottom.

Yahoo's `Close` is split-adjusted while SEC share counts are as reported, so
`DailyHistory.unadjusted_close_on_or_before` undoes later splits before a
market-cap join; without that a pre-split row understates market cap by the
split ratio (Apple's 4:1 in 2020, for one).

Fail-loud contract: "Yahoo has no bars for this symbol" is data and returns
empty; a rate limit or any other failure raises PriceSourceError.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Protocol, runtime_checkable

from hedge_fund.data.errors import DataClientError
from hedge_fund.data.models import Price
from hedge_fund.paths import CACHE_DIR

DEFAULT_CACHE_DIR = CACHE_DIR / "yfinance"
_DAY = 24 * 3600
_PRICE_LOOKBACK_DAYS = 7


class PriceSourceError(DataClientError):
    """A price request failed for infrastructure reasons."""


@dataclass
class DailyHistory:
    """A symbol's full daily closes and split events."""

    closes: dict[date, float] = field(default_factory=dict)
    splits: dict[date, float] = field(default_factory=dict)  # date -> ratio (4.0 for 4:1)

    def split_factor(self, day: date) -> float:
        """Product of every split ratio after *day*: multiplies an adjusted
        close back to the price actually quoted on *day*."""
        factor = 1.0
        for split_day, ratio in self.splits.items():
            if split_day > day and ratio:
                factor *= ratio
        return factor

    def unadjusted_close_on_or_before(self, day: date, lookback_days: int = _PRICE_LOOKBACK_DAYS) -> float | None:
        """The last quoted close on or before *day*, within *lookback_days*."""
        for back in range(lookback_days + 1):
            d = day - timedelta(days=back)
            close = self.closes.get(d)
            if close is not None:
                return close * self.split_factor(d)
        return None

    def to_json(self) -> dict:
        return {"closes": {d.isoformat(): c for d, c in self.closes.items()}, "splits": {d.isoformat(): r for d, r in self.splits.items()}}

    @classmethod
    def from_json(cls, payload: dict) -> DailyHistory:
        return cls(closes={date.fromisoformat(d): float(c) for d, c in payload.get("closes", {}).items()}, splits={date.fromisoformat(d): float(r) for d, r in payload.get("splits", {}).items()})


@runtime_checkable
class PriceSource(Protocol):
    def daily_bars(self, ticker: str, start_date: str, end_date: str) -> list[Price]:
        ...

    def history(self, ticker: str) -> DailyHistory:
        ...


# ticker symbol -> an object with .history(**kwargs) -> pandas.DataFrame
TickerFactory = Callable[[str], object]

_CONFIGURED = False
_HISTORY_MEMO: dict[str, tuple[float, DailyHistory]] = {}
_HISTORY_LOCK = threading.Lock()


class YFinancePrices:
    """Daily bars from Yahoo Finance via yfinance.

    yfinance is imported lazily, so nothing pays for it unless this source is
    used. *ticker_factory* is the test seam: anything returning an object
    with a pandas-returning ``history(**kwargs)``.
    """

    def __init__(self, ticker_factory: TickerFactory | None = None, cache_dir: Path | str = DEFAULT_CACHE_DIR, history_ttl: float = _DAY) -> None:
        self._dir = Path(cache_dir)
        self._ttl = history_ttl
        self._ticker_factory = ticker_factory or self._default_factory
        self._raise_errors_kwarg = False  # set if the installed yfinance lacks config.debug

    def daily_bars(self, ticker: str, start_date: str, end_date: str) -> list[Price]:
        # yfinance's end is exclusive; the DataClient contract is inclusive.
        end_exclusive = (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()
        frame = self._fetch(ticker, start=start_date, end=end_exclusive, interval="1d", auto_adjust=False, actions=False)
        if frame is None or len(frame) == 0:
            return []
        bars: list[Price] = []
        for stamp, row in frame.iterrows():
            close = row.get("Close")
            if close is None or close != close:  # NaN
                continue
            volume = row.get("Volume")
            bars.append(
                Price(
                    open=float(row.get("Open", close)),
                    close=float(close),
                    high=float(row.get("High", close)),
                    low=float(row.get("Low", close)),
                    volume=0 if volume is None or volume != volume else int(volume),
                    time=f"{_day_of(stamp).isoformat()}T00:00:00Z",
                )
            )
        return bars

    def history(self, ticker: str) -> DailyHistory:
        symbol = _symbol(ticker)
        now = time.time()
        with _HISTORY_LOCK:
            hit = _HISTORY_MEMO.get(symbol)
            if hit is not None and now - hit[0] < self._ttl:
                return hit[1]
        history = self._read_history(symbol, now)
        if history is None:
            frame = self._fetch(ticker, period="max", interval="1d", auto_adjust=False, actions=True)
            history = DailyHistory()
            if frame is not None and len(frame) > 0:
                history.closes = {_day_of(stamp): float(c) for stamp, c in frame["Close"].items() if c == c}
                if "Stock Splits" in frame:
                    history.splits = {_day_of(stamp): float(s) for stamp, s in frame["Stock Splits"].items() if s and s == s}
            self._write_history(symbol, history)
        with _HISTORY_LOCK:
            _HISTORY_MEMO[symbol] = (now, history)
        return history

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _default_factory(self, symbol: str):
        import yfinance as yf

        global _CONFIGURED
        if not _CONFIGURED:
            tz_dir = self._dir / "tz"
            tz_dir.mkdir(parents=True, exist_ok=True)
            yf.set_tz_cache_location(str(tz_dir))
            # yfinance hides download failures behind an empty frame unless told
            # not to; the DataClient contract needs them raised (the older
            # raise_errors=True keyword is deprecated in favour of this flag).
            try:
                yf.config.debug.hide_exceptions = False
            except AttributeError:
                self._raise_errors_kwarg = True
            _CONFIGURED = True
        return yf.Ticker(symbol)

    def _fetch(self, ticker: str, **kwargs):
        """One history() call with Yahoo's failure modes sorted into "no data"
        (None) and "infrastructure" (PriceSourceError)."""
        from yfinance import exceptions as yfe

        symbol = _symbol(ticker)
        try:
            ticker_obj = self._ticker_factory(symbol)
            if self._raise_errors_kwarg:
                kwargs["raise_errors"] = True
            return ticker_obj.history(timeout=30, **kwargs)
        except (yfe.YFPricesMissingError, yfe.YFTzMissingError, yfe.YFTickerMissingError):
            return None
        except yfe.YFRateLimitError as exc:
            raise PriceSourceError(f"Yahoo Finance rate limited {symbol}: {exc}", status_code=429, path=symbol) from exc
        except Exception as exc:
            if _is_not_found(exc):
                return None  # Yahoo answers 404 for a symbol it has never listed
            raise PriceSourceError(f"Yahoo Finance request for {symbol} failed: {exc}", path=symbol) from exc

    def _history_path(self, symbol: str) -> Path:
        return self._dir / "history" / f"{symbol}.json"

    def _read_history(self, symbol: str, now: float) -> DailyHistory | None:
        path = self._history_path(symbol)
        try:
            if now - path.stat().st_mtime >= self._ttl:
                return None
            return DailyHistory.from_json(json.loads(path.read_text()))
        except (OSError, ValueError):
            return None

    def _write_history(self, symbol: str, history: DailyHistory) -> None:
        path = self._history_path(symbol)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(history.to_json()))
        os.replace(tmp, path)


def _is_not_found(exc: BaseException) -> bool:
    """True for an HTTP 404 raised anywhere in *exc*'s cause chain — "this
    symbol does not exist" is data, not a failure (the same rule as FDClient)."""
    seen: BaseException | None = exc
    while seen is not None:
        response = getattr(seen, "response", None)
        if getattr(response, "status_code", None) == 404 or getattr(seen, "code", None) == 404:
            return True
        if "404" in str(seen).split(":")[0] or str(seen).startswith("HTTP Error 404"):
            return True
        seen = seen.__cause__ or seen.__context__
    return False


def _symbol(ticker: str) -> str:
    """Yahoo spells share classes with a dash (BRK-B), as the SEC does."""
    return ticker.strip().upper().replace(".", "-")


def _day_of(stamp) -> date:
    """The trading date of a (tz-aware) pandas Timestamp."""
    return stamp.date() if hasattr(stamp, "date") else date.fromisoformat(str(stamp)[:10])


# Finviz Elite as an alternative price source
# -------------------------------------------
# Finviz Elite's quote export returns the same daily bars as CSV:
#   https://elite.finviz.com/quote_export.ashx?t=AAPL&auth=<FINVIZ_AUTH_TOKEN>
#   Date,Open,High,Low,Close,Volume
# A `FinvizPrices(token)` implementing `daily_bars` (parse the CSV, filter
# the window) and `history` (the full CSV) is a drop-in for `YFinancePrices`
# behind `PriceSource`; nothing above this module would change. Finviz
# carries no split events, so `DailyHistory.splits` would need another
# source (or the closes treated as already unadjusted, if Finviz serves them
# that way) before the market-cap join is trustworthy across a split.
