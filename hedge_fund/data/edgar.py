"""SEC EDGAR transport: ticker → CIK, XBRL companyfacts, submissions.

Free, no key — but the SEC has rules for automated clients, and this module
is where they are obeyed so no caller has to think about them:

- every request declares who is calling. The SEC requires a User-Agent of
  the form "Name contact@example.com"; it comes from HEDGE_FUND_SEC_USER_AGENT
  and the client refuses to start without it (never a fabricated contact);
- at most 10 requests per second per IP, across every SEC host. One
  process-wide limiter paces every EdgarClient instance and thread to 8/s;
- an undeclared or too-fast client gets a 403 "Undeclared Automated Tool"
  page, and the IP recovers once the rate stays under the limit for about
  ten minutes. 403/429/5xx are retried with backoff, then raise.

Payloads are cached on disk under ~/.hedge-fund/cache/edgar with a TTL, so a
backtest downloads a filer's facts once a day, not once per date.

Fail-loud contract: infrastructure failures raise EdgarError; a 404 returns
None ("this CIK has no such data" is a fact, not a failure).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import requests

from hedge_fund.data.errors import DataClientError
from hedge_fund.paths import CACHE_DIR

logger = logging.getLogger(__name__)

SEC_USER_AGENT_ENV = "HEDGE_FUND_SEC_USER_AGENT"

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

DEFAULT_CACHE_DIR = CACHE_DIR / "edgar"

# Seconds between requests, process-wide: 8/s, under the SEC's 10/s.
_MIN_INTERVAL = 0.125
_RETRY_DELAYS = (5, 15, 30)
_DAY = 24 * 3600
TTL_TICKERS = 7 * _DAY
TTL_COMPANYFACTS = 1 * _DAY
TTL_SUBMISSIONS = 7 * _DAY

# On-disk stand-in for a 404, so an unknown CIK is not re-requested.
_MISSING = {"__missing__": True}


class EdgarError(DataClientError):
    """An EDGAR request failed for infrastructure reasons."""


class _RateLimiter:
    """Paces calls to at most one per *min_interval* seconds, across threads."""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._lock = threading.Lock()
        self._last = float("-inf")

    def wait(self) -> None:
        with self._lock:
            delay = self._last + self._min_interval - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            self._last = time.monotonic()


# One per process: the SEC counts per IP, not per client or thread.
_LIMITER = _RateLimiter(_MIN_INTERVAL)

# path -> (fetched_at, payload). Shared across instances (the TUI builds one
# client per worker thread) so a payload is parsed once per process.
_MEMO: dict[Path, tuple[float, dict]] = {}
_MEMO_LOCK = threading.Lock()


class EdgarClient:
    """SEC EDGAR fetcher with the required headers, pacing, and a disk cache.

    Usage::

        with EdgarClient() as edgar:
            cik = edgar.cik_for("AAPL")
            facts = edgar.company_facts(cik)
    """

    def __init__(
        self,
        user_agent: str | None = None,
        cache_dir: Path | str = DEFAULT_CACHE_DIR,
        timeout: float = 30.0,
    ) -> None:
        agent = (user_agent or os.environ.get(SEC_USER_AGENT_ENV, "")).strip()
        if not agent:
            raise EdgarError(f"{SEC_USER_AGENT_ENV} is not set. The SEC requires every automated " f"client to identify itself with a contact, e.g. " f'{SEC_USER_AGENT_ENV}="Jane Doe jane@example.com" — export it or ' f"add it to ~/.hedge-fund/.env.")
        if "@" not in agent:
            logger.warning("%s has no email address; the SEC asks for one: %r", SEC_USER_AGENT_ENV, agent)
        self.user_agent = agent
        self._dir = Path(cache_dir)
        self._timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "User-Agent": agent,
                "Accept-Encoding": "gzip, deflate",
                "Accept": "application/json",
            }
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> EdgarClient:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    def close(self) -> None:
        self._session.close()

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def cik_for(self, ticker: str) -> int | None:
        """The filer's CIK, or None if the SEC lists no such ticker (an ETF,
        a foreign non-filer, a typo). Yahoo's BRK-B and BRK.B both map."""
        return self._ticker_table().get(ticker.strip().upper().replace(".", "-"))

    def company_facts(self, cik: int) -> dict | None:
        """The filer's complete XBRL facts (every reported value of every tag,
        with the filing that reported it). Several MB for a large filer."""
        return self._cached(self._facts_path(cik), TTL_COMPANYFACTS, COMPANYFACTS_URL.format(cik=cik))

    def company_facts_version(self, cik: int) -> float:
        """A token that changes when the cached facts change (the file mtime)."""
        try:
            return self._facts_path(cik).stat().st_mtime
        except OSError:
            return 0.0

    def submissions(self, cik: int) -> dict | None:
        """Filer profile: name, SIC code and description, exchanges, tickers."""
        return self._cached(self._dir / "submissions" / f"CIK{cik:010d}.json", TTL_SUBMISSIONS, SUBMISSIONS_URL.format(cik=cik))

    # ------------------------------------------------------------------
    # Cache mechanics
    # ------------------------------------------------------------------

    def _facts_path(self, cik: int) -> Path:
        return self._dir / "companyfacts" / f"CIK{cik:010d}.json"

    def _ticker_table(self) -> dict[str, int]:
        payload = self._cached(self._dir / "company_tickers.json", TTL_TICKERS, TICKERS_URL)
        if payload is None:
            raise EdgarError(f"{TICKERS_URL} returned 404", status_code=404, path=TICKERS_URL)
        # {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        return {str(row["ticker"]).upper(): int(row["cik_str"]) for row in payload.values() if isinstance(row, dict) and "ticker" in row and "cik_str" in row}

    def _cached(self, path: Path, ttl: float, url: str) -> dict | None:
        """Memo → disk (within *ttl* of its mtime) → network. None for a 404,
        which is cached like any other answer."""
        now = time.time()
        with _MEMO_LOCK:
            hit = _MEMO.get(path)
        if hit is not None and now - hit[0] < ttl:
            payload = hit[1]
        else:
            payload = self._read(path, ttl, now)
            if payload is None:
                payload = self._get_json(url)
                if payload is None:
                    payload = _MISSING
                self._write(path, payload)
            with _MEMO_LOCK:
                _MEMO[path] = (now, payload)
        return None if payload == _MISSING else payload

    def _read(self, path: Path, ttl: float, now: float) -> dict | None:
        try:
            if now - path.stat().st_mtime >= ttl:
                return None
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None  # absent, stale, or corrupt -> refetch

    def _write(self, path: Path, payload: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, path)

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------

    def _get_json(self, url: str) -> dict | None:
        """GET with the SEC's pacing and retry. None only for 404."""
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            _LIMITER.wait()
            try:
                resp = self._session.request("GET", url, timeout=self._timeout)
            except requests.RequestException as exc:
                raise EdgarError(f"GET {url} failed: {exc}", path=url) from exc

            if resp.status_code == 404:
                return None

            if resp.status_code in (403, 429) or resp.status_code >= 500:
                if delay is not None:
                    logger.info("EDGAR returned %d for %s, retrying in %ds (attempt %d/%d)", resp.status_code, url, delay, attempt + 1, len(_RETRY_DELAYS))
                    time.sleep(delay)
                    continue
                hint = ""
                if resp.status_code == 403:
                    hint = f" — a 403 from the SEC usually means a missing or unusual User-Agent ({SEC_USER_AGENT_ENV}) or too many requests; the IP recovers after ~10 minutes under the limit"
                raise EdgarError(
                    f"GET {url} returned {resp.status_code} after {len(_RETRY_DELAYS)} retries{hint}",
                    status_code=resp.status_code,
                    path=url,
                )

            if resp.status_code >= 400:
                raise EdgarError(f"GET {url} returned {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code, path=url)

            try:
                return resp.json()
            except ValueError as exc:
                raise EdgarError(f"GET {url} returned non-JSON: {resp.text[:200]}", status_code=resp.status_code, path=url) from exc

        raise EdgarError(f"GET {url} failed", path=url)  # pragma: no cover - loop always returns or raises
