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

import re

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
    "abstain on thin data": "cannot support a call either way",
    "read a figure against its sector": "normal for ITS sector",
    "treat multiples as of the filing": "not a live quote",
    "confidence is conviction, not liking": "understating it on a short",
    "distinguish declining from not knowing": '"basis": "judged" | "insufficient"',
}

# All 18 carry this verbatim. Asserting the exact sentence, not just the
# idea, keeps it from drifting into eighteen paraphrases that are hard to
# compare when a school's scorecard looks off.
ABSTAIN_CLAUSE = 'go neutral and set basis to "insufficient"'


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
    for field in ('"signal"', '"confidence"', '"basis"', '"reasoning"'):
        assert field in prompt, f"{persona} never names {field} in its schema block"
    for verdict in ("bullish", "bearish", "neutral"):
        assert verdict in prompt, f"{persona} never offers {verdict!r} as a signal"


@pytest.mark.parametrize("persona", PERSONAS)
def test_persona_says_to_abstain_on_thin_data(persona):
    """Go neutral rather than guess when the snapshot cannot support a call.

    LLMAgent.predict already abstains when build_snapshot raises
    InsufficientData (under MIN_PERIODS filed rows), so the structural case
    is handled in code. This clause covers the softer one: enough periods to
    build a snapshot, but too many blank columns to judge. Without it a
    persona can return a confident-looking call on thin data, and confidence
    is what sizes the position.

    druckenmiller, lynch, and munger predated this clause and were brought
    into line in Sept 2026, at the cost of their prompt caches.
    """
    assert ABSTAIN_CLAUSE in prompt_for(persona)


def test_the_abstain_clause_is_worded_identically_everywhere():
    """Eighteen schools are compared against each other on the scorecard.
    They have to be told to abstain in the same words, or a difference in
    neutral_share is a difference in phrasing rather than in judgment."""
    offenders = sorted(p for p in PERSONAS if ABSTAIN_CLAUSE not in prompt_for(p))
    assert not offenders, (
        f"{offenders} word the abstain rule differently. Copy the line from " "hedge_fund/signals/buffett.py verbatim."
    )


# ---------------------------------------------------------------------------
# Byte-identity, not just presence
# ---------------------------------------------------------------------------

SHARED_CLAUSES = {
    "point-in-time": r"- Reason ONLY from the data provided.*?Do not invent numbers\.\n",
    "sector comparability": r"- The sector and industry.*?last one you saw\.\n",
    "multiples as of filing": r"- Market cap and P/E.*?live quote\.\n",
    "abstain on thin data": r"- If the facts shown cannot support.*?nothing\n  exciting\.\n",
    "confidence semantics": r"- Confidence is how sure.*?understates the short\.\n",
}


@pytest.mark.parametrize("label,pattern", sorted(SHARED_CLAUSES.items()))
def test_a_shared_clause_is_byte_identical_everywhere(label, pattern):
    """Presence is not enough — the wording has to match exactly.

    Three personas drifted on the abstain clause and it was found only by
    going looking. The same drift on confidence semantics would reintroduce
    the bug §11.10 records: eight prompts never defined confidence, and
    confidence sizes the position, so chanos and earnings_quality_skeptic
    were under-sizing the shorts they exist to make. A paraphrase that
    loses "a bearish call you are certain of is HIGH confidence" brings
    that back silently.

    Eighteen schools are also compared against each other on one
    scorecard. A difference between them should be a difference in method,
    never in the instructions they were given.
    """
    seen = {}
    for persona in PERSONAS:
        match = re.search(pattern, prompt_for(persona), re.S)
        assert match, f"{persona} is missing the {label} clause entirely"
        seen.setdefault(match.group(0), []).append(persona)
    assert len(seen) == 1, f"the {label} clause has {len(seen)} different wordings:\n" + "\n".join(
        f"  {sorted(who)}: {text[:90]!r}…" for text, who in seen.items()
    )


def test_the_shared_tail_appears_in_the_same_order_everywhere():
    """A reader comparing two personas should find the common rules in the
    same place, and a persona's own bullet after them rather than mixed in."""
    order = None
    for persona in PERSONAS:
        prompt = prompt_for(persona)
        positions = [(prompt.index(re.search(p, prompt, re.S).group(0)), label) for label, p in SHARED_CLAUSES.items()]
        got = [label for _, label in sorted(positions)]
        if order is None:
            order = got
        assert got == order, f"{persona} orders the shared clauses differently: {got} != {order}"
