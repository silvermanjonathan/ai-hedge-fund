"""CachedDataClient tests — counting fake, no network."""

from hedge_fund.data.cached import CachedDataClient
from hedge_fund.data.models import CompanyFacts, Price


class CountingClient:
    """Counts calls; returns canned data."""

    def __init__(self, facts=None):
        self.calls = 0
        self._facts = facts

    def get_prices(self, ticker, start_date, end_date, interval="day", interval_multiplier=1):
        self.calls += 1
        return [Price(open=1.0, close=2.0, high=2.0, low=1.0, volume=100, time=f"{start_date}T00:00:00Z")]

    def get_company_facts(self, ticker):
        self.calls += 1
        return self._facts

    def get_market_cap(self, ticker, end_date):
        self.calls += 1
        return 3.0e12


def test_cache_hit_skips_wrapped_client(tmp_path):
    inner = CountingClient()
    fd = CachedDataClient(inner, cache_dir=tmp_path)

    first = fd.get_prices("AAPL", "2024-01-01", "2024-12-31")
    second = fd.get_prices("AAPL", "2024-01-01", "2024-12-31")

    assert inner.calls == 1
    assert isinstance(second[0], Price)  # re-hydrated to the pydantic model
    assert second[0].close == first[0].close


def test_different_params_different_entries(tmp_path):
    inner = CountingClient()
    fd = CachedDataClient(inner, cache_dir=tmp_path)

    fd.get_prices("AAPL", "2024-01-01", "2024-12-31")
    fd.get_prices("AAPL", "2024-01-01", "2025-12-31")  # different end date

    assert inner.calls == 2


def test_refresh_busts_cache(tmp_path):
    inner = CountingClient()
    CachedDataClient(inner, cache_dir=tmp_path).get_prices("AAPL", "2024-01-01", "2024-12-31")
    CachedDataClient(inner, cache_dir=tmp_path, refresh=True).get_prices("AAPL", "2024-01-01", "2024-12-31")

    assert inner.calls == 2


def test_none_item_is_cached(tmp_path):
    """A cached None (ticker without facts) must not re-hit the API."""
    inner = CountingClient(facts=None)
    fd = CachedDataClient(inner, cache_dir=tmp_path)

    assert fd.get_company_facts("ZZZZ") is None
    assert fd.get_company_facts("ZZZZ") is None
    assert inner.calls == 1


def test_item_rehydrates(tmp_path):
    inner = CountingClient(facts=CompanyFacts(ticker="AAPL", sector="Tech"))
    fd = CachedDataClient(inner, cache_dir=tmp_path)

    fd.get_company_facts("AAPL")
    facts = fd.get_company_facts("AAPL")

    assert inner.calls == 1
    assert isinstance(facts, CompanyFacts)
    assert facts.sector == "Tech"


def test_scalar_cached(tmp_path):
    inner = CountingClient()
    fd = CachedDataClient(inner, cache_dir=tmp_path)

    assert fd.get_market_cap("AAPL", "2024-12-31") == 3.0e12
    assert fd.get_market_cap("AAPL", "2024-12-31") == 3.0e12
    assert inner.calls == 1


def test_two_cache_dirs_do_not_collide(tmp_path):
    """Keys carry method and params, not the provider: two sources must be
    kept apart by directory (as open_data_client does)."""
    a = CachedDataClient(CountingClient(), cache_dir=tmp_path / "a")
    b = CachedDataClient(CountingClient(), cache_dir=tmp_path / "b")
    a.get_company_facts("AAPL")
    assert b._client.calls == 0
    b.get_company_facts("AAPL")
    assert b._client.calls == 1


def test_the_cache_key_carries_the_derivation_version(tmp_path):
    """A mapping change must invalidate the cache by itself.

    The Sept 2026 tag fix recovered nothing until the cache was refreshed
    by hand: metrics are cached per (method, params), and a warm cache
    served rows built by the old mapping. A run that forgot --refresh-data
    re-measured stale data at full price and reported success. Folding the
    derivation version into the key makes the flag non-load-bearing, and
    protects every future mapping change rather than this one.
    """
    from hedge_fund.data import cached as cached_mod

    client = CachedDataClient(CountingClient(), cache_dir=tmp_path)
    before = client._key("get_financial_metrics", {"ticker": "AAPL"})

    monkey = cached_mod.DERIVATION_VERSION + 1
    original = cached_mod.DERIVATION_VERSION
    try:
        cached_mod.DERIVATION_VERSION = monkey
        after = client._key("get_financial_metrics", {"ticker": "AAPL"})
    finally:
        cached_mod.DERIVATION_VERSION = original

    assert before != after, "bumping DERIVATION_VERSION must change the cache key"


def test_the_key_still_separates_methods_and_params(tmp_path):
    client = CachedDataClient(CountingClient(), cache_dir=tmp_path)
    assert client._key("get_prices", {"t": "A"}) != client._key("get_prices", {"t": "B"})
    assert client._key("get_prices", {"t": "A"}) != client._key("get_news", {"t": "A"})
