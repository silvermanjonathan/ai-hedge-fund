"""The LLM's influence ends at Signal — enforced, not merely documented.

CLAUDE.md states the rule that makes this system auditable:

    The LLM's influence ends at Signal. blend_signals, apply_limits, and
    build_orders are pure and deterministic — never move sizing or ordering
    into a prompt.

It is the reason a hostile or malfunctioning model response cannot produce
an arbitrary book. A verdict is validated into a scalar in [-1, +1], and
every decision after that point is arithmetic no model touches: conviction
requests, risk disposes.

The rule held by discipline alone. One `from hedge_fund.llm import ...` in
risk/limits.py would quietly end it, and no test would notice, because a
prompt-driven risk limit still returns plausible numbers. These tests read
the three modules' import statements and their source, so the boundary
fails at CI rather than in a backtest nobody re-derives.

Scope: imports and I/O syscalls, checked statically. A determinism test
(same inputs, same weights) belongs with each stage's own tests, and those
exist in test_construction.py, test_limits.py, and test_execution.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1]

# The three pure stages, in pipeline order.
PURE_MODULES = {
    "portfolio/construction.py": "blend_signals",
    "risk/limits.py": "apply_limits",
    "pipeline/execution.py": "build_orders",
}

# Sibling packages that exist to talk to the outside world. A pure stage
# importing any of them means a decision moved somewhere unauditable.
FORBIDDEN_PACKAGES = (
    "hedge_fund.llm",  # the whole point: no prompt may shape sizing
    "hedge_fund.data",  # a pure stage receives data, it does not fetch it
    "hedge_fund.brokers",  # execution builds orders; the broker fills them
    "hedge_fund.signals",  # views arrive as Signal, already formed
    "hedge_fund.features",
    "hedge_fund.tui",
    "hedge_fund.backtesting",
)

# Pure data-type modules. A stage must be able to CONSTRUCT the things it
# returns — build_orders makes Order objects — so the types are allowed while
# the machinery around them is not. Each is verified inert by
# test_allowed_model_modules_are_actually_pure below, so this is not a hole.
ALLOWED_MODEL_MODULES = (
    "hedge_fund.models",
    "hedge_fund.brokers.models",
    "hedge_fund.data.models",
)

FORBIDDEN_THIRD_PARTY = (
    "requests",
    "httpx",
    "urllib",
    "socket",
    "anthropic",
    "openai",
    "langchain",
    "yfinance",
    "sqlite3",
    "subprocess",
)

# Module-level calls that would make a "pure" function read the world.
FORBIDDEN_CALLS = ("open(", "os.environ", "os.getenv", "Path(", "datetime.now", "date.today", "random.")


def _imports(path: Path) -> list[str]:
    """Every dotted module name imported anywhere in the file, any scope —
    a lazy import inside a function is still a dependency."""
    names = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("relpath,function", sorted(PURE_MODULES.items()))
def test_pure_stage_imports_no_sibling_package(relpath, function):
    """A pure stage may depend on the shared models and nothing else in the
    package. Signal in, weights or orders out."""
    offenders = sorted(
        {
            n
            for n in _imports(PACKAGE / relpath)
            if any(n == p or n.startswith(p + ".") for p in FORBIDDEN_PACKAGES) and n not in ALLOWED_MODEL_MODULES
        }
    )
    assert not offenders, (
        f"{relpath} ({function}) imports {offenders}.\n"
        "This is the boundary that keeps the LLM's influence ending at "
        "Signal. If sizing or ordering genuinely needs something from "
        "there, pass it in as an argument — do not import it here."
    )


@pytest.mark.parametrize("relpath,function", sorted(PURE_MODULES.items()))
def test_pure_stage_imports_no_io_library(relpath, function):
    offenders = sorted({n for n in _imports(PACKAGE / relpath) if n.split(".")[0] in FORBIDDEN_THIRD_PARTY})
    assert not offenders, (
        f"{relpath} ({function}) imports {offenders}. A pure stage performs "
        "no I/O; given the same inputs it must always return the same book."
    )


@pytest.mark.parametrize("relpath,function", sorted(PURE_MODULES.items()))
def test_pure_stage_reads_nothing_from_the_environment(relpath, function):
    """Determinism has a second enemy: ambient state. A stage that reads the
    clock, an env var, or a file is not replayable even with no imports."""
    source = "\n".join(
        line for line in (PACKAGE / relpath).read_text().splitlines() if not line.lstrip().startswith("#")
    )
    found = sorted({c for c in FORBIDDEN_CALLS if c in source})
    assert not found, (
        f"{relpath} ({function}) references {found}. A pure stage takes "
        "everything it needs as an argument, including the date."
    )


def test_the_guarded_set_is_the_documented_one():
    """Guards the guard. If a fourth pure stage appears, it joins this list
    deliberately rather than being silently unprotected."""
    for relpath, function in PURE_MODULES.items():
        path = PACKAGE / relpath
        assert path.exists(), f"{relpath} moved; update PURE_MODULES"
        assert f"def {function}(" in path.read_text(), (
            f"{function} is no longer defined in {relpath}; if the pure "
            "stages were reorganised, update PURE_MODULES to match"
        )


@pytest.mark.parametrize("module", ALLOWED_MODEL_MODULES)
def test_allowed_model_modules_are_actually_pure(module):
    """The allowlist above is only safe while these stay plain type
    definitions. If one grows a client or reads the environment, it becomes
    a way to smuggle I/O into a pure stage through the back door."""
    path = PACKAGE.parent / (module.replace(".", "/") + ".py")
    imported = _imports(path)
    impure = sorted(
        {
            n
            for n in imported
            if n.split(".")[0] in FORBIDDEN_THIRD_PARTY
            or (n.startswith("hedge_fund.") and n not in ALLOWED_MODEL_MODULES)
        }
    )
    assert not impure, (
        f"{module} is on the pure-core allowlist but imports {impure}. "
        "Either keep it a plain type module or drop it from "
        "ALLOWED_MODEL_MODULES."
    )
