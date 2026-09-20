"""Every persona is a school applying a framework, not an impersonation.

What keeps that true is not the class name — it is the tail every system
prompt carries. CLAUDE.md requires new personas to "end with the common
hard-rules tail copied from buffett.py". Nothing checked that, and the
prompts are the one part of this system with no other test coverage: they
are strings, so a missing clause cannot fail at import or at runtime. It
shows up as a persona quietly reasoning from pre-training instead of from
the snapshot.

Two clauses matter most, and both are about point-in-time discipline:

    Reason ONLY from the data provided. Treat the most recent filing date
    shown as the present day; do not use any knowledge of anything that
    happened after it. Do not invent numbers.

Without the first sentence a persona answers from what the model already
knows about the company. Without the second it answers as of today rather
than as of the filing — lookahead that no amount of care in the data layer
can undo, since the leak is inside the model.

Editing a prompt to satisfy these tests is not free: prompt_key hashes the
system prompt, so any change orphans that persona's entire cache under
~/.hedge-fund/cache/llm/ and the next run re-reasons every name it covers
at full price. Change prompts deliberately, not to make a test green.
"""

from __future__ import annotations

import pytest

from hedge_fund.signals import ALPHA_MODEL_REGISTRY
from hedge_fund.signals.llm_agent import LLMAgent


class StubLLM:
    """Personas must be constructible without an API key or a network."""

    model = "stub"

    def complete(self, system: str, user: str) -> str:  # pragma: no cover
        raise AssertionError("a prompt-contract test must never call an LLM")


PERSONAS = sorted(n for n, c in ALPHA_MODEL_REGISTRY.items() if issubclass(c, LLMAgent))

# Present in all 18 today. Each is load-bearing, so each is asserted by name
# rather than as one opaque blob: a failure should say which guarantee went.
REQUIRED = {
    "reason only from the given data": "Reason ONLY from the data provided",
    "treat the latest filing as today": "Treat the most recent filing date",
    "ignore anything after that filing": "do not use any knowledge of anything that",
    "do not fabricate figures": "Do not invent numbers",
    "answer as JSON only": "Respond with JSON only",
}

# The abstain clause, which three older personas predate. See
# test_abstain_clause_coverage for the full reasoning; this set is a record
# of a known gap, not a licence to grow. Adding a persona here requires a
# reason better than "the test failed".
ABSTAIN_CLAUSE = "If the data is insufficient to judge"
ABSTAIN_EXEMPT = {"druckenmiller", "lynch", "munger"}


def prompt_for(name: str) -> str:
    return ALPHA_MODEL_REGISTRY[name](llm=StubLLM()).get_system_prompt()


def test_every_registered_llm_persona_is_covered():
    """Guards the guard: a new persona must not slip past by not being here."""
    assert len(PERSONAS) == 18, (
        f"persona count changed ({len(PERSONAS)}); confirm the new persona "
        "carries the hard-rules tail, then update this number"
    )


@pytest.mark.parametrize("persona", PERSONAS)
@pytest.mark.parametrize("guarantee,clause", sorted(REQUIRED.items()))
def test_persona_carries_hard_rule(persona, guarantee, clause):
    assert clause in prompt_for(persona), (
        f"{persona} does not {guarantee}.\n"
        f"Missing from its system prompt: {clause!r}\n"
        "Copy the hard-rules tail from hedge_fund/signals/buffett.py. Note "
        "that changing the prompt invalidates this persona's LLM cache."
    )


@pytest.mark.parametrize("persona", PERSONAS)
def test_persona_emits_the_verdict_schema(persona):
    """The three fields AnalystVerdict validates must be spelled out, or the
    model has to guess the shape and LLMAgent._parse abstains."""
    prompt = prompt_for(persona)
    for field in ('"signal"', '"confidence"', '"reasoning"'):
        assert field in prompt, f"{persona} never names {field} in its schema block"
    for verdict in ("bullish", "bearish", "neutral"):
        assert verdict in prompt, f"{persona} never offers {verdict!r} as a signal"


@pytest.mark.parametrize("persona", sorted(set(PERSONAS) - ABSTAIN_EXEMPT))
def test_persona_says_to_abstain_on_thin_data(persona):
    """Go neutral rather than guess when the snapshot cannot support a call.

    LLMAgent.predict already abstains when build_snapshot raises
    InsufficientData (under MIN_PERIODS filed rows), so the structural case
    is handled in code. This clause covers the softer one: enough periods to
    build a snapshot, but too many blank columns to judge. Without it a
    persona can return a confident-looking call on thin data, and confidence
    is what sizes the position.
    """
    assert ABSTAIN_CLAUSE in prompt_for(persona)


def test_abstain_clause_coverage():
    """Pins exactly which personas lack the abstain clause.

    druckenmiller, lynch, and munger predate it — all three are from the
    original five and carry a different second bullet instead (munger "be
    blunt", lynch "plain language", druckenmiller a scope note about having
    no macro data). Closing the gap is a deliberate prompt change that
    invalidates those three personas' caches, so it is recorded here rather
    than quietly fixed.

    This test fails if the gap GROWS or if it is closed, so neither can
    happen silently.
    """
    actual = {p for p in PERSONAS if ABSTAIN_CLAUSE not in prompt_for(p)}
    assert actual == ABSTAIN_EXEMPT, (
        f"abstain-clause gap changed.\nexpected: {sorted(ABSTAIN_EXEMPT)}\n"
        f"actual:   {sorted(actual)}\n"
        "If you closed it, shrink ABSTAIN_EXEMPT in the same commit."
    )
