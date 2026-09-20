"""hedge_fund.roster must stay in step with ALPHA_MODEL_REGISTRY.

roster.py duplicates the model names so the ledger can read them without
importing hedge_fund.signals — which would construct every agent class and
pull in `anthropic`, about 800ms, and make a read-only scorecard depend on
the LLM client importing cleanly.

Duplication is the price, and this is what keeps it honest. Without it a
new school is added to the registry, never reaches the roster, and is
silently missing from the scorecard — exactly the failure the coverage work
exists to prevent, reintroduced one layer down.
"""

from __future__ import annotations

from hedge_fund.roster import BLOCKED, QUANT_MODELS, SCHOOLS
from hedge_fund.signals import ALPHA_MODEL_REGISTRY
from hedge_fund.signals.llm_agent import LLMAgent


def test_roster_matches_the_registry_exactly():
    registry_schools = {n for n, c in ALPHA_MODEL_REGISTRY.items() if issubclass(c, LLMAgent)}
    missing = sorted(registry_schools - set(SCHOOLS))
    extra = sorted(set(SCHOOLS) - registry_schools)
    assert not missing, (
        f"{missing} are registered schools but absent from hedge_fund/roster.py, "
        "so the scorecard will never mention them. Add them to SCHOOLS."
    )
    assert not extra, f"{extra} are in roster.SCHOOLS but not in ALPHA_MODEL_REGISTRY"


def test_quant_models_match_the_registry():
    registry_quants = {n for n, c in ALPHA_MODEL_REGISTRY.items() if not issubclass(c, LLMAgent)}
    assert set(QUANT_MODELS) == registry_quants


def test_roster_covers_the_whole_registry():
    assert set(SCHOOLS) | set(QUANT_MODELS) == set(ALPHA_MODEL_REGISTRY)


def test_blocked_schools_are_real_schools():
    unknown = sorted(set(BLOCKED) - set(SCHOOLS))
    assert not unknown, f"{unknown} are marked blocked but are not schools"


def test_blocked_entries_explain_themselves():
    """A bare name in BLOCKED is worse than no entry: it makes a school
    inert with no record of what would unblock it."""
    for school, (reason, strategies) in BLOCKED.items():
        assert len(reason) > 60, f"{school}'s blocked reason is too thin to act on"
        assert isinstance(strategies, tuple), f"{school} must list the strategies waiting on it"


def test_roster_does_not_import_the_signals_package():
    """The whole point of the split. If roster grows a signals import, the
    ledger silently pays for the LLM stack again."""
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parent / "roster.py"
    for node in ast.walk(ast.parse(src.read_text())):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        for name in names:
            assert not name.startswith("hedge_fund.signals"), f"roster.py imports {name}"
