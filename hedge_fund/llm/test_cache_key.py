"""prompt_key must stay stable: every file already in ~/.hedge-fund/cache/llm
is keyed by the pre-effort formula, and a replay must still find it."""

from hedge_fund.llm import prompt_key

_AGENT, _MODEL = "buffett", "claude-opus-5"
_SYSTEM, _USER = "You are Warren Buffett.", "Ticker: TEST"

# sha256("buffett|claude-opus-5|You are Warren Buffett.|Ticker: TEST")[:24],
# as computed before effort existed.
_PRE_EFFORT_KEY = "71deebf8901b9d983dd262fe"


def test_key_without_effort_is_unchanged():
    assert prompt_key(_AGENT, _MODEL, _SYSTEM, _USER) == _PRE_EFFORT_KEY
    assert prompt_key(_AGENT, _MODEL, _SYSTEM, _USER, effort=None) == _PRE_EFFORT_KEY


def test_effort_is_part_of_the_key():
    low = prompt_key(_AGENT, _MODEL, _SYSTEM, _USER, effort="low")
    high = prompt_key(_AGENT, _MODEL, _SYSTEM, _USER, effort="high")
    assert low != _PRE_EFFORT_KEY
    assert low != high
