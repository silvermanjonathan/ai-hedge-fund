"""The verdict ledger: every distinct verdict, logged once, priced on its day.

A verdict's identity is (school, ticker, snapshot_hash). The first cycle that
carries it appends one row, dated by that cycle's as_of; every later cycle
that repeats it — a cache hit between filings — is skipped, so a verdict is
never double-counted. The entry price is the last close on or before the
event date, from the data client; if it cannot be fetched the row is kept
with a null price and the scorer leaves it out. No price is ever estimated.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from hedge_fund.data.protocol import DataClient
from hedge_fund.paths import USER_DIR

logger = logging.getLogger(__name__)

DEFAULT_LEDGER_PATH = USER_DIR / "ledger" / "verdicts.jsonl"
BENCHMARK = "SPY"
_PRICE_LOOKBACK_DAYS = 7

FIELDS = (
    "key",
    "school",
    "ticker",
    "snapshot_hash",
    "filing_date",
    "signal",
    "confidence",
    "value",
    "desk",
    "event_date",
    "logged_at",
    "entry_close",
    "spy_close",
    "thesis",
)


@dataclass(frozen=True)
class IngestResult:
    added: int
    skipped: int
    abstained: int

    def __str__(self) -> str:
        return f"added={self.added} skipped={self.skipped} abstained={self.abstained}"


class Ledger:
    """Append-only JSONL of verdict rows with an in-memory key index."""

    def __init__(self, path: Path | str = DEFAULT_LEDGER_PATH) -> None:
        self.path = Path(path)
        self._rows: list[dict] = []
        self._keys: set[str] = set()
        self._lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def rows(self) -> list[dict]:
        return list(self._rows)

    def __len__(self) -> int:
        return len(self._rows)

    def latest_per_ticker_school(self) -> dict[tuple[str, str], dict]:
        """For each (ticker, school), the row with the greatest filing_date
        (ties and missing dates fall back to event_date, then log order)."""
        latest: dict[tuple[str, str], dict] = {}
        for row in self._rows:
            key = (row["ticker"], row["school"])
            if key not in latest or _recency(row) > _recency(latest[key]):
                latest[key] = row
        return latest

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def ingest(self, record_path: Path | str, data_client: DataClient) -> IngestResult:
        """Log every new non-abstained LLM verdict in one CycleRecord file."""
        record = json.loads(Path(record_path).read_text())
        as_of = record["as_of"]
        fund = record["fund"]
        logged_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        added = skipped = abstained = 0
        closes: dict[str, float | None] = {}

        def close_for(ticker: str) -> float | None:
            if ticker not in closes:
                closes[ticker] = _close_on_or_before(data_client, ticker, as_of)
            return closes[ticker]

        with self._lock:
            new_rows: list[dict] = []
            for strategy in record.get("strategies", []):
                desk = f"{fund}/{strategy['name']}"
                for signal in strategy.get("signals", []):
                    meta = signal.get("metadata") or {}
                    if meta.get("abstained"):
                        abstained += 1
                        continue
                    snapshot_hash = signal.get("snapshot_hash") or meta.get("snapshot_hash")
                    label = meta.get("signal")
                    if not snapshot_hash or label not in ("bullish", "bearish", "neutral"):
                        skipped += 1  # not an LLM verdict (a quant model's signal)
                        continue
                    key = f"{signal['model_name']}|{signal['ticker']}|{snapshot_hash}"
                    if key in self._keys:
                        skipped += 1
                        continue
                    confidence = signal.get("confidence")
                    if confidence is None:
                        confidence = meta.get("confidence")
                    row = {
                        "key": key,
                        "school": signal["model_name"],
                        "ticker": signal["ticker"],
                        "snapshot_hash": snapshot_hash,
                        "filing_date": signal.get("filing_date"),
                        "signal": label,
                        "confidence": float(confidence) if confidence is not None else None,
                        "value": float(signal.get("value", 0.0)),
                        "desk": desk,
                        "event_date": as_of,
                        "logged_at": logged_at,
                        "entry_close": close_for(signal["ticker"]),
                        "spy_close": close_for(BENCHMARK),
                        "thesis": signal.get("reasoning"),
                    }
                    self._keys.add(key)
                    new_rows.append(row)
                    added += 1
            if new_rows:
                self._rows.extend(new_rows)
                self._flush()
        return IngestResult(added=added, skipped=skipped, abstained=abstained)

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                logger.warning("ledger: skipping a corrupt line in %s", self.path)
                continue
            self._rows.append(row)
            self._keys.add(row["key"])

    def _flush(self) -> None:
        """Rewrite the whole file atomically (temp file, then os.replace)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in self._rows))
        os.replace(tmp, self.path)


def _recency(row: dict) -> tuple[str, str, str]:
    return (row.get("filing_date") or "", row.get("event_date") or "", row.get("logged_at") or "")


def _close_on_or_before(data_client: DataClient, ticker: str, day: str) -> float | None:
    """The last close on or before *day* (the get_market_cap rule), or None
    with a warning when the data client cannot supply one."""
    start = (date.fromisoformat(day) - timedelta(days=_PRICE_LOOKBACK_DAYS)).isoformat()
    try:
        bars = [b for b in data_client.get_prices(ticker, start, day) if b.time[:10] <= day]
    except Exception as exc:  # the ledger keeps the verdict; the scorer skips the row
        logger.warning("ledger: no entry price for %s on %s: %s", ticker, day, exc)
        return None
    if not bars:
        logger.warning("ledger: no close for %s on or before %s", ticker, day)
        return None
    return max(bars, key=lambda b: b.time).close
