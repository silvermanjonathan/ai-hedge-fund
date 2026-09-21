"""Regenerate the snapshot golden fixture and print its content hash.

    poetry run python -m hedge_fund.features.regolden

Run this ONLY when a change to FundamentalsSnapshot.render() or its model is
intended. Regenerating is not a formality: every prompt-cache entry under
~/.hedge-fund/cache/llm/ is keyed on the old prompt text, so the next desk
run re-reasons every position from scratch at full price. Commit the new
fixture and the new GOLDEN_CONTENT_HASH together with the change that caused
them, so review sees the cost.

The fixture data comes from test_snapshot.golden_snapshot(), the same
function the test checks against — the fixture cannot be generated from
different inputs than it verifies.
"""

from __future__ import annotations

import re
from pathlib import Path


def main() -> None:
    from hedge_fund.features.test_snapshot import GOLDEN_PATH, golden_snapshot

    snapshot = golden_snapshot()
    rendered, digest = snapshot.render(), snapshot.content_hash

    previous = GOLDEN_PATH.read_text() if GOLDEN_PATH.exists() else None
    GOLDEN_PATH.write_text(rendered)

    test_file = Path(__file__).resolve().parent / "test_snapshot.py"
    source = test_file.read_text()
    updated, n = re.subn(
        r'^GOLDEN_CONTENT_HASH = "[^"]*"$',
        f'GOLDEN_CONTENT_HASH = "{digest}"',
        source,
        count=1,
        flags=re.MULTILINE,
    )
    if n != 1:
        raise SystemExit(f"could not find GOLDEN_CONTENT_HASH in {test_file}")
    test_file.write_text(updated)

    print(f"wrote {GOLDEN_PATH.relative_to(Path.cwd())} ({len(rendered)} bytes)")
    print(f"content_hash = {digest}")
    if previous is None:
        print("\nFirst generation — no cache impact.")
    elif previous == rendered:
        print("\nPrompt text unchanged; the existing LLM cache stays valid.")
    else:
        print(
            "\nPROMPT TEXT CHANGED. Every entry under ~/.hedge-fund/cache/llm/ "
            "is now unreachable and the next run will re-reason from scratch "
            "at full price. Commit this fixture with the change that caused it."
        )


if __name__ == "__main__":
    main()
