"""Presentation names must cover every registered alpha model — an unnamed
model would render as its slug in the picker and the run board."""

from hedge_fund.signals import ALPHA_MODEL_REGISTRY
from hedge_fund.tui.shared import _SHORT_NAMES, DISPLAY_NAMES


def test_display_and_short_names_cover_the_registry():
    assert set(DISPLAY_NAMES) == set(ALPHA_MODEL_REGISTRY)
    assert set(_SHORT_NAMES) == set(ALPHA_MODEL_REGISTRY)
    assert all(DISPLAY_NAMES[k] and _SHORT_NAMES[k] for k in ALPHA_MODEL_REGISTRY)
