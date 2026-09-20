"""Entry-point layering: headless commands must not import the TUI.

Five commands run without a terminal UI — `aihf` with a mandate, the
`aihf-ledger` and `aihf-universe` scripts, and the two `python -m` dev CLIs.
The weekly LaunchAgent run (scripts/weekly.sh, Mondays at 10:00) drives three
of them unattended.

Credentials used to live in hedge_fund/tui/keys.py and the default backtest
window in hedge_fund/tui/shared.py, so every one of those commands imported
the interactive app's package to start up. Both modules were Textual-free, so
nothing was actually loading Textual — but that held only by care. One import
added to hedge_fund/tui/__init__.py or shared.py would have pulled a terminal
UI framework into an unattended cron job, and nothing would have failed.

These tests make it structural instead. The one permitted reference is the
lazy `from hedge_fund.tui.app import HedgeFundApp` inside run.main(), which is
how the no-argument invocation launches the app; test_tui_import_in_run_is_lazy
pins it to function scope so it cannot drift back up to module level.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent

# Importable module -> the command it backs.
HEADLESS_ENTRY_POINTS = {
    "hedge_fund.run": "aihf <mandate>",
    "hedge_fund.ledger.__main__": "aihf-ledger",
    "hedge_fund.universe.__main__": "aihf-universe",
    "hedge_fund.backtesting.__main__": "python -m hedge_fund.backtesting",
    "hedge_fund.event_study.__main__": "python -m hedge_fund.event_study",
}


def _source(module: str) -> tuple[Path, ast.Module]:
    path = PACKAGE.parent / (module.replace(".", "/") + ".py")
    return path, ast.parse(path.read_text())


def _module_level_imports(tree: ast.Module) -> list[str]:
    """Dotted names imported at module scope only — not inside a function."""
    names = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


@pytest.mark.parametrize("module,command", sorted(HEADLESS_ENTRY_POINTS.items()))
def test_headless_entry_point_has_no_module_level_tui_import(module, command):
    """A headless command must not import hedge_fund.tui to start up."""
    path, tree = _source(module)
    offenders = [n for n in _module_level_imports(tree) if n.startswith("hedge_fund.tui")]
    assert not offenders, (
        f"{command} ({path.name}) imports {offenders} at module level.\n"
        "Credentials belong in hedge_fund.config and backtest defaults in "
        "hedge_fund.backtesting. A headless command must not reach into the "
        "interactive app's package to start up."
    )


@pytest.mark.parametrize("module,command", sorted(HEADLESS_ENTRY_POINTS.items()))
def test_headless_entry_point_does_not_load_textual(module, command):
    """The property that actually matters, checked by importing for real.

    A module-level import is not the only way to pull Textual in — any
    transitive dependency would do it. This imports the entry point in a
    clean interpreter and asserts Textual never reaches sys.modules.
    """
    probe = (
        f"import importlib, sys; importlib.import_module({module!r}); "
        "leaked = sorted(m for m in sys.modules if m == 'textual' or m.startswith('textual.')); "
        "print('LEAKED' if leaked else 'CLEAN', len(leaked))"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, f"{command} failed to import:\n{result.stderr}"
    assert result.stdout.startswith("CLEAN"), (
        f"{command} pulls Textual into a headless run ({result.stdout.strip()}).\n"
        "The weekly LaunchAgent job runs these without a terminal. Check what "
        "hedge_fund.config, hedge_fund.backtesting, or a new transitive import "
        "is dragging in."
    )


def test_tui_import_in_run_is_lazy():
    """run.py may import the app — but only inside the function that launches
    it, so the non-interactive path never pays for it."""
    path, tree = _source("hedge_fund.run")
    assert "hedge_fund.tui" not in _module_level_imports(tree)

    lazy = [
        node.module
        for fn in tree.body
        if isinstance(fn, ast.FunctionDef)
        for node in ast.walk(fn)
        if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("hedge_fund.tui")
    ]
    assert lazy == [
        "hedge_fund.tui.app"
    ], f"expected exactly one lazy TUI import inside a function in {path.name}, found {lazy}"


def test_config_layer_does_not_load_textual():
    """hedge_fund.config is the seam every entry point now depends on, so
    anything it drags in, all five commands inherit.

    Only Textual is forbidden. `rich` is fair game and is genuinely loaded:
    run.py prints its run summary with rich.console.Console, and tui/shared.py
    builds rich renderables the non-interactive CLI also uses. Rich writes to
    a stream; Textual drives a terminal application. Only the second one has
    no business in an unattended cron job.
    """
    probe = (
        "import importlib, sys; importlib.import_module('hedge_fund.config'); "
        "leaked = sorted(m for m in sys.modules if m == 'textual' or m.startswith('textual.')); "
        "print('LEAKED' if leaked else 'CLEAN', len(leaked))"
    )
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("CLEAN"), (
        f"hedge_fund.config now pulls in Textual ({result.stdout.strip()}); " "every headless command inherits it."
    )
