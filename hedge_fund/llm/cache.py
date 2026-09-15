"""Prompt cache — one JSON file per LLM decision.

This is deliberately three things at once (a locked design decision):
1. a cache: a backtest re-running an agent over an unchanged snapshot costs $0;
2. the persistence record: the EXACT prompt + response behind every Signal,
   for replay and audit;
3. the debug trail: failed parses keep the raw response on disk.

Files live under ~/.hedge-fund/cache/llm/, keyed by a hash of
(agent, model, prompt).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from hedge_fund.paths import CACHE_DIR

DEFAULT_CACHE_DIR = CACHE_DIR / "llm"


def prompt_key(agent: str, model: str, system: str, user: str, effort: str | None = None) -> str:
    """Cache key for one (agent, model, prompt) combination.

    *effort*, when given, is part of the key — the same prompt at a
    different effort is a different decision. None leaves the payload
    exactly as it always was, so every existing cache file keeps its key.
    """
    payload = f"{agent}|{model}|{system}|{user}"
    if effort is not None:
        payload += f"|{effort}"
    return hashlib.sha256(payload.encode()).hexdigest()[:24]


class PromptCache:
    def __init__(self, cache_dir: Path | str = DEFAULT_CACHE_DIR) -> None:
        self._dir = Path(cache_dir)

    def get(self, key: str) -> dict | None:
        path = self._dir / f"{key}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None  # corrupt cache entry -> treat as miss, will be rewritten

    def put(self, key: str, record: dict) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        record = {**record, "created_at": datetime.now(timezone.utc).isoformat()}
        path = self._dir / f"{key}.json"
        # Atomic: parallel analysts can produce the same key at the same
        # moment, and a reader must never see a half-written file. Write
        # beside the target, then rename over it.
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=f".{key}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(record, indent=2))
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
