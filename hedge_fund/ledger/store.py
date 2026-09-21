"""The verdict ledger: every distinct verdict, logged once, priced on its day.

A verdict's identity is (school, ticker, prompt_key) — the school, the name,
and the exact question it was asked, which covers the persona prompt, the
model, the effort and the rendered snapshot. The first cycle that carries it
appends one row, dated by that cycle's as_of; every later cycle that repeats
it — a cache hit between filings — is skipped, so a verdict is never
double-counted.

Keying on the question rather than on the facts is deliberate (Sept 2026).
It was (school, ticker, snapshot_hash), which deduplicated by what was TRUE
rather than by what was ASKED — so improving a prompt produced fresh verdicts
that the ledger silently discarded as repeats of the old ones. Two answers on
identical facts under different instructions are different events.

The consequence is that a prompt edit re-logs the whole cohort at the current
price, so a school can hold two rows for one name weeks apart. Both are an
honest record of what it said when; whether the SCORER should treat them as
two observations is a separate question (see ARCHITECTURE.md §11.10). The entry price is the last close on or before the
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

    def rows(self, since: str | None = None) -> list[dict]:
        """Every row, or only those made on or after *since* (YYYY-MM-DD).

        The cutoff exists because a universe change makes older verdicts
        incomparable: they grade a school on names it will never be shown
        again, against a universe bar built from a different screen.
        Nothing is deleted — the rows stay as history and simply fall
        outside the window.
        """
        if since is None:
            return list(self._rows)
        return [r for r in self._rows if (r.get("event_date") or "") >= since]

    def __len__(self) -> int:
        return len(self._rows)

    def latest_per_ticker_school(self, since: str | None = None) -> dict[tuple[str, str], dict]:
        """For each (ticker, school), the row with the greatest filing_date
        (ties and missing dates fall back to event_date, then log order).

        Honours *since* for the same reason the scorecard does, and it
        matters more here: this feeds the candidates playbook, which is the
        output that gets acted on. Without the cutoff a name that has left
        the universe keeps surfacing as an entry candidate on the strength
        of a verdict from a screen that no longer exists.
        """
        latest: dict[tuple[str, str], dict] = {}
        for row in self.rows(since):
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
                    # Identity is what was ASKED, not what was true. A
                    # verdict is a school's answer to a question, and the
                    # question is the whole prompt — persona, model, effort
                    # and the rendered snapshot. Two answers on identical
                    # facts under different instructions are different
                    # events. prompt_key hashes exactly that; snapshot_hash
                    # was encoding "what was true" where this needs "what
                    # was asked", so a prompt change used to be silently
                    # deduplicated away. Older rows keyed on snapshot_hash
                    # keep their keys; both are opaque 24-char digests and
                    # cannot collide.
                    identity = meta.get("prompt_key") or snapshot_hash
                    key = f"{signal['model_name']}|{signal['ticker']}|{identity}"
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
                        # Why a neutral is neutral. Rows written before
                        # Sept 2026 have none, and read as unknown.
                        "basis": meta.get("basis"),
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
