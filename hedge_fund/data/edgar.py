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

import html
import json
import logging
import os
import re
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
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accn_nodash}/{document}"
COMPANY_SEARCH_URL = "https://www.sec.gov/cgi-bin/browse-edgar"

DEFAULT_CACHE_DIR = CACHE_DIR / "edgar"

# Seconds between requests, process-wide: 8/s, under the SEC's 10/s.
_MIN_INTERVAL = 0.125
_RETRY_DELAYS = (5, 15, 30)
_DAY = 24 * 3600
TTL_TICKERS = 7 * _DAY
TTL_COMPANYFACTS = 1 * _DAY
TTL_SUBMISSIONS = 7 * _DAY
TTL_PREDECESSOR = 7 * _DAY
# How many successions to follow (a holding-company reorganisation on top of
# an earlier one) before giving up.
_MAX_SUCCESSION_DEPTH = 3

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
            raise EdgarError(
                f"{SEC_USER_AGENT_ENV} is not set. The SEC requires every automated "
                f"client to identify itself with a contact, e.g. "
                f'{SEC_USER_AGENT_ENV}="Jane Doe jane@example.com" — export it or '
                f"add it to ~/.hedge-fund/.env."
            )
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
        return self._cached(
            self._dir / "submissions" / f"CIK{cik:010d}.json", TTL_SUBMISSIONS, SUBMISSIONS_URL.format(cik=cik)
        )

    def predecessor_cik(self, cik: int) -> int | None:
        """The registrant this filer succeeded, or None.

        A holding-company reorganisation gives the business a new CIK and
        the ticker moves with it; XBRL history stays under the old one. The
        successor announces itself on Form 8-K12B (or 8-K12G3), whose text
        names the predecessor — "successor registrant of X" / "successor
        issuer to X", with X either the legal name or a term defined earlier
        as "Legal Name, a … corporation (“X”)". That name is resolved to a
        CIK through EDGAR company search, by exact normalised name. Cached
        for a week; None when the filer has no succession filing or the text
        does not fit the pattern.
        """
        path = self._dir / "predecessors" / f"CIK{cik:010d}.json"
        payload = self._cached_compute(path, TTL_PREDECESSOR, lambda: {"predecessor": self._find_predecessor(cik)})
        return payload.get("predecessor") if payload else None

    def cik_chain(self, cik: int) -> list[int]:
        """*cik* followed by its predecessors, oldest last, at most a few deep."""
        chain = [cik]
        while len(chain) <= _MAX_SUCCESSION_DEPTH:
            prev = self.predecessor_cik(chain[-1])
            if prev is None or prev in chain:
                break
            chain.append(prev)
        return chain

    def _find_predecessor(self, cik: int) -> int | None:
        sub = self.submissions(cik)
        if not sub:
            return None
        filing = successor_filing(sub)
        if filing is None:
            return None
        accn, document = filing
        text = self._get_text(ARCHIVE_URL.format(cik=cik, accn_nodash=accn.replace("-", ""), document=document))
        if not text:
            return None
        name = predecessor_name(text)
        if not name:
            logger.info("CIK %d filed %s but its text names no predecessor", cik, document)
            return None
        candidates = self._company_search(strip_corporate_suffix(name))
        wanted = normalize_company_name(name)
        matches = [c for c, conformed in candidates if c != cik and normalize_company_name(conformed) == wanted]
        if len(matches) == 1:
            return matches[0]
        logger.info("CIK %d: predecessor %r resolved to %s, not one CIK", cik, name, matches or candidates)
        return None

    def _company_search(self, name_prefix: str) -> list[tuple[int, str]]:
        """EDGAR company search (prefix match on the conformed name) as
        (cik, conformed name) pairs."""
        params = {"action": "getcompany", "company": name_prefix, "output": "atom"}
        text = self._get_text(COMPANY_SEARCH_URL, params=params, accept="application/atom+xml, */*")
        if not text:
            return []
        ciks = re.findall(r"<cik>(\d+)</cik>", text)
        names = [html.unescape(n) for n in re.findall(r"<conformed-name>(.*?)</conformed-name>", text)]
        return [(int(c), n) for c, n in zip(ciks, names)]

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
        return {
            str(row["ticker"]).upper(): int(row["cik_str"])
            for row in payload.values()
            if isinstance(row, dict) and "ticker" in row and "cik_str" in row
        }

    def _cached(self, path: Path, ttl: float, url: str) -> dict | None:
        """Memo → disk (within *ttl* of its mtime) → network. None for a 404,
        which is cached like any other answer."""
        return self._cached_compute(path, ttl, lambda: self._get_json(url))

    def _cached_compute(self, path: Path, ttl: float, compute) -> dict | None:
        now = time.time()
        with _MEMO_LOCK:
            hit = _MEMO.get(path)
        if hit is not None and now - hit[0] < ttl:
            payload = hit[1]
        else:
            payload = self._read(path, ttl, now)
            if payload is None:
                payload = compute()
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
        resp = self._get(url)
        if resp is None:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise EdgarError(
                f"GET {url} returned non-JSON: {resp.text[:200]}", status_code=resp.status_code, path=url
            ) from exc

    def _get_text(self, url: str, params: dict | None = None, accept: str = "text/html, */*") -> str | None:
        """GET a document (a filing, a search feed) as text. None only for 404."""
        resp = self._get(url, params=params, headers={"Accept": accept})
        return None if resp is None else resp.text

    def _get(self, url: str, params: dict | None = None, headers: dict | None = None):
        """One paced, retried request. None only for 404."""
        for attempt, delay in enumerate((*_RETRY_DELAYS, None)):
            _LIMITER.wait()
            try:
                resp = self._session.request("GET", url, params=params, headers=headers, timeout=self._timeout)
            except requests.RequestException as exc:
                raise EdgarError(f"GET {url} failed: {exc}", path=url) from exc

            if resp.status_code == 404:
                return None

            if resp.status_code in (403, 429) or resp.status_code >= 500:
                if delay is not None:
                    logger.info(
                        "EDGAR returned %d for %s, retrying in %ds (attempt %d/%d)",
                        resp.status_code,
                        url,
                        delay,
                        attempt + 1,
                        len(_RETRY_DELAYS),
                    )
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
                raise EdgarError(
                    f"GET {url} returned {resp.status_code}: {resp.text[:200]}", status_code=resp.status_code, path=url
                )

            return resp

        raise EdgarError(f"GET {url} failed", path=url)  # pragma: no cover - loop always returns or raises


# ---------------------------------------------------------------------------
# Succession parsing (pure)
# ---------------------------------------------------------------------------

_SUCCESSION_FORMS = ("8-K12B", "8-K12G3")
_CORPORATE_SUFFIX = r"(?:Corporation|Corp\.?|Incorporated|Inc\.?|Company|Co\.?|Limited|Ltd\.?|LLC|L\.L\.C\.|L\.P\.|LP|plc|PLC|N\.V\.|S\.A\.|Holdings|Group|Trust)"
_LEGAL_NAME_RE = re.compile(r"((?:[A-Z][\w&.'\-]*\s+){0,6}" + _CORPORATE_SUFFIX + r")(?![\w.])")
_SUCCESSOR_RE = re.compile(
    r"successor\s+(?:registrant|issuer)\s+(?:of|to)(?:\s+the)?\s+([A-Z][\w&.'\-]*(?:\s+[A-Z&][\w&.'\-]*)*)(?=[’']s\b|,|\.|\s+pursuant|\s+under|\s+common|\s+in\b|\s+for\b|\s*\()"
)
_NAME_WORDS = {"CORPORATION": "CORP", "INCORPORATED": "INC", "COMPANY": "CO", "LIMITED": "LTD", "THE": ""}


def successor_filing(submissions: dict) -> tuple[str, str] | None:
    """(accession, primary document) of the filer's earliest successor-issuer
    filing (Form 8-K12B / 8-K12G3), or None."""
    recent = submissions.get("filings", {}).get("recent", {})
    rows = list(
        zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
            recent.get("primaryDocument", []),
        )
    )
    hits = sorted((r for r in rows if str(r[0]).startswith(_SUCCESSION_FORMS) and r[2] and r[3]), key=lambda r: r[1])
    return (hits[0][2], hits[0][3]) if hits else None


def filing_text(document_html: str) -> str:
    """A filing's HTML as one line of plain text."""
    text = html.unescape(re.sub(r"<[^>]+>", " ", document_html))
    return re.sub(r"\s+", " ", text).strip()


def predecessor_name(document_html: str) -> str | None:
    """The predecessor's legal name from a successor-issuer filing's text.

    Finds "successor registrant of X" / "successor issuer to X". If X is a
    term defined earlier as `Legal Name, a … corporation (“X”)`, returns the
    legal name; otherwise X itself when it already ends in a corporate suffix.
    """
    text = filing_text(document_html)
    hit = _SUCCESSOR_RE.search(text)
    if not hit:
        return None
    term = hit.group(1).strip()
    for quote_open, quote_close in (("“", "”"), ('"', '"')):
        for m in re.finditer(
            re.escape(quote_open) + r"\s*" + re.escape(term) + r"\s*" + re.escape(quote_close) + r"\s*\)", text
        ):
            before = text[max(0, m.start() - 200) : m.start()]
            names = _LEGAL_NAME_RE.findall(before)
            if names:
                return names[-1].strip()
    if _LEGAL_NAME_RE.fullmatch(term):
        return term
    return None


def strip_corporate_suffix(name: str) -> str:
    """ "Exxon Mobil Corporation" -> "Exxon Mobil" (EDGAR search is a prefix match)."""
    return re.sub(r"[,\s]+" + _CORPORATE_SUFFIX + r"\s*$", "", name).strip()


def normalize_company_name(name: str) -> str:
    """Comparable form: upper case, punctuation dropped, suffixes conformed
    ("Exxon Mobil Corporation" and "EXXON MOBIL CORP" agree)."""
    words = re.sub(r"[^\w\s&]", " ", name.upper()).split()
    return " ".join(w for w in (_NAME_WORDS.get(w, w) for w in words) if w)
