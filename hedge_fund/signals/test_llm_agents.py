"""LLMAgent + BuffettAgent tests — fake LLM and data client, no network."""

import json

import pytest

from hedge_fund.data.client import FDClientError
from hedge_fund.data.models import FinancialMetrics
from hedge_fund.llm import extract_json, LLMRefusal, PromptCache
from hedge_fund.llm.client import LLMParseError
from hedge_fund.models import Signal
from hedge_fund.signals import BuffettAgent

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeLLM:
    """Canned-response LLM; counts calls; can raise instead."""

    model = "fake-model"

    def __init__(self, response="", error=None):
        self._response = response
        self._error = error
        self.calls = 0

    def complete(self, system, user):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._response


class MockDataClient:
    def __init__(self, metrics=None, error=None):
        self._metrics = metrics or []
        self._error = error

    def get_financial_metrics(self, ticker, end_date, period="ttm", limit=10):
        if self._error is not None:
            raise self._error
        return self._metrics

    def get_company_facts(self, ticker):
        return None


def _history(n=8):
    quarters = [
        "2024-12-31",
        "2024-09-30",
        "2024-06-30",
        "2024-03-31",
        "2023-12-31",
        "2023-09-30",
        "2023-06-30",
        "2023-03-31",
    ]
    return [
        FinancialMetrics(
            ticker="TEST",
            report_period=q,
            period="ttm",
            filing_date=q,
            return_on_equity=0.2,
            gross_margin=0.4,
            book_value_per_share=10.0,
            market_cap=1e9,
        )
        for q in quarters[:n]
    ]


BULLISH = json.dumps({"signal": "bullish", "confidence": 80, "reasoning": "Wonderful business."})


def _agent(tmp_path, llm, cache=None):
    return BuffettAgent(llm=llm, cache=cache or PromptCache(tmp_path / "llm"))


# ---------------------------------------------------------------------------
# Signal folding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "signal,confidence,expected",
    [
        ("bullish", 80, 0.8),
        ("bearish", 60, -0.6),
        ("neutral", 90, 0.0),
    ],
)
def test_value_folding(tmp_path, signal, confidence, expected):
    response = json.dumps({"signal": signal, "confidence": confidence, "reasoning": "r"})
    agent = _agent(tmp_path, FakeLLM(response))

    sig = agent.predict("TEST", "2025-01-15", MockDataClient(metrics=_history()))

    assert isinstance(sig, Signal)
    assert sig.model_name == "buffett"
    assert sig.value == pytest.approx(expected)
    assert sig.metadata["abstained"] is False


# ---------------------------------------------------------------------------
# Failure contract
# ---------------------------------------------------------------------------


def test_malformed_json_abstains(tmp_path):
    agent = _agent(tmp_path, FakeLLM("I am bullish, trust me."))
    sig = agent.predict("TEST", "2025-01-15", MockDataClient(metrics=_history()))
    assert sig.value == 0.0
    assert sig.metadata["abstained"] is True


@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("llm timed out"),
        ConnectionError("connection reset"),
        RuntimeError("401 authentication_error: invalid x-api-key"),
        RuntimeError("400 invalid_request_error: credit balance is too low"),
        RuntimeError("429 rate_limit_error"),
    ],
)
def test_infrastructure_failure_propagates_rather_than_abstaining(tmp_path, error):
    """The bug this guards is silent, not loud.

    Until Sept 2026 predict() caught bare Exception here, so an expired key
    or an exhausted budget produced a Signal(abstained=True) — identical in
    the record to a school declining a name. Ledger.ingest drops abstained
    rows, so the evidence never reached the ledger either: an unattended run
    reported success with most of its universe silently missing, and the
    scorecard read it as schools passing on names nobody had asked about.

    Nothing is caught here now. run_cycle's fan-out cancels the remaining
    futures and the cycle dies before a CycleRecord exists, so there is no
    partial cohort to mis-read.
    """
    agent = _agent(tmp_path, FakeLLM(error=error))
    with pytest.raises(type(error)):
        agent.predict("TEST", "2025-01-15", MockDataClient(metrics=_history()))


def test_an_unrecognised_failure_propagates(tmp_path):
    """The default is propagate, not abstain.

    Enumerating provider exception types would only ever cover Anthropic;
    every other provider raises its own. So anything not positively
    identified as model-side is treated as infrastructure — the safe
    direction, and the one that does not rot as providers change."""

    class SomeNewProviderError(Exception):
        pass

    agent = _agent(tmp_path, FakeLLM(error=SomeNewProviderError("who knows")))
    with pytest.raises(SomeNewProviderError):
        agent.predict("TEST", "2025-01-15", MockDataClient(metrics=_history()))


def test_a_failed_call_is_not_cached_as_a_verdict(tmp_path):
    """A propagating failure must leave no trace a rerun would trust."""
    from hedge_fund.llm import PromptCache

    cache_dir = tmp_path / "llm"
    agent = _agent(tmp_path, FakeLLM(error=TimeoutError("boom")), cache=PromptCache(cache_dir))
    with pytest.raises(TimeoutError):
        agent.predict("TEST", "2025-01-15", MockDataClient(metrics=_history()))
    assert not list(cache_dir.glob("*.json")), "a failed call wrote a cache entry"


def test_refusal_abstains_with_reason_refusal(tmp_path):
    """A refusal is a non-view: abstain, tagged so the record can tell it
    apart from a transport failure. No fallback model."""
    agent = _agent(tmp_path, FakeLLM(error=LLMRefusal("declined")))
    sig = agent.predict("TEST", "2025-01-15", MockDataClient(metrics=_history()))
    assert sig.value == 0.0
    assert sig.metadata["abstained"] is True
    assert sig.metadata["abstain_reason"] == "refusal"


def test_insufficient_data_abstains(tmp_path):
    agent = _agent(tmp_path, FakeLLM(BULLISH))
    sig = agent.predict("TEST", "2025-01-15", MockDataClient(metrics=_history(2)))
    assert sig.value == 0.0
    assert sig.metadata["abstained"] is True


def test_data_layer_error_propagates(tmp_path):
    """Fail loud: an infrastructure failure must NOT become a neutral view."""
    client = MockDataClient(error=FDClientError("API down", status_code=500))
    agent = _agent(tmp_path, FakeLLM(BULLISH))
    with pytest.raises(FDClientError):
        agent.predict("TEST", "2025-01-15", client)


# ---------------------------------------------------------------------------
# Cache = persistence
# ---------------------------------------------------------------------------


def test_cache_hit_skips_llm_call(tmp_path):
    llm = FakeLLM(BULLISH)
    client = MockDataClient(metrics=_history())
    agent = _agent(tmp_path, llm)

    first = agent.predict("TEST", "2025-01-15", client)
    second = agent.predict("TEST", "2025-01-15", client)

    assert llm.calls == 1  # second predict served from cache
    assert first.value == second.value
    assert first.metadata["cached"] is False
    assert second.metadata["cached"] is True


def test_new_as_of_same_data_hits_cache(tmp_path):
    """A new date with unchanged fundamentals must be free: the snapshot
    renders identically, so the prompt cache hits — no second LLM call."""
    llm = FakeLLM(BULLISH)
    client = MockDataClient(metrics=_history())
    agent = _agent(tmp_path, llm)

    first = agent.predict("TEST", "2025-01-15", client)
    second = agent.predict("TEST", "2025-02-20", client)

    assert llm.calls == 1
    assert second.metadata["cached"] is True
    assert second.date == "2025-02-20"  # Signal date is the predict arg, not the cache's
    assert first.metadata["snapshot_hash"] == second.metadata["snapshot_hash"]


def test_new_filing_forces_new_llm_call(tmp_path):
    """A new filing changes the snapshot — the agent must re-reason."""
    llm = FakeLLM(BULLISH)
    agent = _agent(tmp_path, llm)

    first = agent.predict("TEST", "2025-01-15", MockDataClient(metrics=_history(7)))
    second = agent.predict("TEST", "2025-02-20", MockDataClient(metrics=_history(8)))

    assert llm.calls == 2
    assert first.metadata["snapshot_hash"] != second.metadata["snapshot_hash"]


def test_prompt_and_response_persisted(tmp_path):
    agent = _agent(tmp_path, FakeLLM(BULLISH))
    agent.predict("TEST", "2025-01-15", MockDataClient(metrics=_history()))

    records = list((tmp_path / "llm").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["agent"] == "buffett"
    assert "You are Warren Buffett" in record["system"]
    assert "2024-12-31" in record["user"]  # the rendered snapshot
    assert record["response"] == BULLISH
    assert record["parsed"]["signal"] == "bullish"


def test_failed_parse_still_persists_response(tmp_path):
    agent = _agent(tmp_path, FakeLLM("garbage"))
    agent.predict("TEST", "2025-01-15", MockDataClient(metrics=_history()))

    records = list((tmp_path / "llm").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text())
    assert record["response"] == "garbage"
    assert "parse_error" in record


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_registry_names_match_keys(tmp_path):
    """Every registry entry instantiates and reports its own key as name."""
    from hedge_fund.signals import ALPHA_MODEL_REGISTRY, LLMAgent

    for key, cls in ALPHA_MODEL_REGISTRY.items():
        if issubclass(cls, LLMAgent):
            model = cls(llm=FakeLLM(), cache=PromptCache(tmp_path / "llm"))
        else:
            model = cls()
        assert model.name == key


def test_llm_personas_share_the_contract(tmp_path):
    """Every persona prompt keeps the PIT rule and the JSON schema."""
    from hedge_fund.signals import ALPHA_MODEL_REGISTRY, LLMAgent

    for cls in ALPHA_MODEL_REGISTRY.values():
        if not issubclass(cls, LLMAgent):
            continue
        prompt = cls(llm=FakeLLM(), cache=PromptCache(tmp_path / "llm")).get_system_prompt()
        assert "most recent filing date" in prompt  # the point-in-time hard rule
        assert '"signal"' in prompt and '"confidence"' in prompt  # the schema


SCHOOLS = [
    "akre",
    "chanos",
    "dalio_resilience",
    "damodaran",
    "dreman",
    "earnings_quality_skeptic",
    "fisher",
    "fundsmith",
    "greenblatt",
    "klarman",
    "pabrai",
    "quality_compounder",
    "schloss",
]


@pytest.mark.parametrize("slug", SCHOOLS)
def test_school_personas_keep_the_school_framing(tmp_path, slug):
    """A school is "an analyst applying X's framework": it carries a scope
    note about what the snapshot cannot supply, ends with the common hard
    rules, and never opens as a named person."""
    import re

    from hedge_fund.signals import ALPHA_MODEL_REGISTRY

    prompt = ALPHA_MODEL_REGISTRY[slug](llm=FakeLLM(), cache=PromptCache(tmp_path / "llm")).get_system_prompt()
    assert "Scope note" in prompt
    assert "Reason ONLY from the data provided" in prompt
    assert prompt.startswith("You are an analyst")
    assert re.search(r"You are [A-Z]", prompt) is None  # never "You are <Person>"
    assert "in the voice of a" in prompt or "in the voice of an" in prompt  # the school, not a person


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------


def test_extract_json_fenced():
    assert extract_json('here:\n```json\n{"a": 1}\n```\ndone') == {"a": 1}


def test_extract_json_bare():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_embedded():
    assert extract_json('Sure! {"a": {"b": 2}} hope that helps') == {"a": {"b": 2}}


def test_extract_json_raises_on_garbage():
    with pytest.raises(LLMParseError):
        extract_json("no json here at all")
