"""FreeDataClient and YFinancePrices over fakes: protocol shape, the unsupported
methods, the snapshot end to end. No network."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from hedge_fund.data import (
    CachedDataClient,
    DataClient,
    DataClientError,
    FreeDataClient,
)
from hedge_fund.data import prices as prices_module
from hedge_fund.data.free import sic_division
from hedge_fund.data.prices import DailyHistory, PriceSourceError, YFinancePrices
from hedge_fund.data.test_xbrl import fake_history, synthetic_companyfacts
from hedge_fund.features.snapshot import build_snapshot

SUBMISSIONS = {"name": "Test Co", "sic": "3571", "sicDescription": "Electronic Computers", "exchanges": ["Nasdaq"]}


class FakeEdgar:
    def __init__(self, facts=None, submissions=SUBMISSIONS, ciks=None):
        self._facts = facts if facts is not None else synthetic_companyfacts()
        self._submissions = submissions
        self._ciks = ciks or {"TEST": 1}
        self.closed = False

    def cik_for(self, ticker):
        return self._ciks.get(ticker.upper().replace(".", "-"))

    def company_facts(self, cik):
        return self._facts

    def company_facts_version(self, cik):
        return 1.0

    def submissions(self, cik):
        return self._submissions

    def close(self):
        self.closed = True


class FakePrices:
    def __init__(self, history=None):
        self._history = history if history is not None else fake_history()
        self.calls = []

    def daily_bars(self, ticker, start_date, end_date):
        self.calls.append((ticker, start_date, end_date))
        return []

    def history(self, ticker):
        return self._history


def _client(**kw):
    return FreeDataClient(prices=kw.pop("prices", FakePrices()), edgar=kw.pop("edgar", FakeEdgar(**kw)))


@pytest.fixture(autouse=True)
def _fresh_memos():
    from hedge_fund.data import free as free_module

    free_module._ROWS.clear()
    prices_module._HISTORY_MEMO.clear()


# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------


def test_satisfies_the_runtime_protocol():
    assert isinstance(_client(), DataClient)


def test_unsupported_methods_raise_not_implemented_never_empty():
    client = _client()
    for call in (lambda: client.get_news("TEST", "2025-01-15"), lambda: client.get_insider_trades("TEST", "2025-01-15"), lambda: client.get_earnings_history("TEST"), lambda: client.get_earnings("TEST")):
        with pytest.raises(NotImplementedError, match="free data source") as exc:
            call()
        assert not isinstance(exc.value, DataClientError)


def test_non_daily_prices_not_implemented():
    with pytest.raises(NotImplementedError):
        _client().get_prices("TEST", "2025-01-01", "2025-01-15", "minute", 5)


def test_get_prices_delegates_positionally_like_cached_client():
    prices = FakePrices()
    _client(prices=prices).get_prices("TEST", "2025-01-01", "2025-01-15", "day", 1)
    assert prices.calls == [("TEST", "2025-01-01", "2025-01-15")]


# ---------------------------------------------------------------------------
# Fundamentals
# ---------------------------------------------------------------------------


def test_metrics_are_point_in_time_and_newest_first():
    rows = _client().get_financial_metrics("TEST", "2025-01-15", period="ttm", limit=20)
    assert [r.report_period for r in rows][:2] == ["2024-09-30", "2024-06-30"]
    assert all(r.filing_date <= "2025-01-15" for r in rows)
    assert rows[0].period == "ttm" and rows[0].ticker == "TEST"
    assert rows[0].market_cap == pytest.approx(2000)


def test_unknown_ticker_has_no_metrics_and_no_facts():
    client = _client()
    assert client.get_financial_metrics("SPY", "2025-01-15") == []
    assert client.get_company_facts("SPY") is None
    assert client.get_market_cap("SPY", "2025-01-15") is None


def test_non_ttm_period_not_implemented():
    with pytest.raises(NotImplementedError):
        _client().get_financial_metrics("TEST", "2025-01-15", period="annual")


def test_company_facts_from_sic():
    facts = _client().get_company_facts("test")
    assert facts.ticker == "TEST" and facts.cik == "0000000001"
    assert facts.sector == "Manufacturing" and facts.industry == "Electronic Computers"
    assert facts.sic_code == "3571" and facts.exchange == "Nasdaq"
    assert sic_division(6021) == "Finance, Insurance & Real Estate"
    assert sic_division(None) is None and sic_division("9999") is None


def test_get_market_cap_is_point_in_time():
    # 2024-06-01 is a Saturday, before the 2:1 split: Friday close 20 × factor 2 × 100 shares
    assert _client().get_market_cap("TEST", "2024-06-01") == pytest.approx(4000)
    assert _client().get_market_cap("TEST", "2024-11-05") == pytest.approx(2000)


def test_rows_are_built_once_per_facts_version():
    edgar = FakeEdgar()
    client = FreeDataClient(prices=FakePrices(), edgar=edgar)
    first = client.get_financial_metrics("TEST", "2025-01-15")
    second = client.get_financial_metrics("TEST", "2025-03-01")
    assert len(second) > len(first)
    assert {r.report_period: r.model_dump() for r in first} == {r.report_period: r.model_dump() for r in second if r.filing_date <= "2025-01-15"}


def test_build_snapshot_over_the_free_client():
    client = _client()
    snap = build_snapshot("TEST", "2025-01-15", client)
    assert len(snap.periods) >= 4
    assert snap.sector == "Manufacturing"
    text = snap.render()
    assert "2024-09-30" in text and "2025-01-15" not in text
    # Between two filings the snapshot, and so its LLM cache key, does not move.
    assert snap.content_hash == build_snapshot("TEST", "2024-12-01", client).content_hash
    assert snap.content_hash != build_snapshot("TEST", "2025-03-01", client).content_hash


def test_cached_wrapper_round_trips(tmp_path):
    edgar = FakeEdgar()
    cached = CachedDataClient(FreeDataClient(prices=FakePrices(), edgar=edgar), cache_dir=tmp_path)
    first = cached.get_financial_metrics("TEST", "2025-01-15", "ttm", 20)
    edgar._facts = {"facts": {}}  # a second call must come from disk, not EDGAR
    from hedge_fund.data import free as free_module

    free_module._ROWS.clear()
    assert cached.get_financial_metrics("TEST", "2025-01-15", "ttm", 20) == first


def test_context_manager_closes_edgar():
    edgar = FakeEdgar()
    with FreeDataClient(prices=FakePrices(), edgar=edgar):
        pass
    assert edgar.closed


# ---------------------------------------------------------------------------
# YFinancePrices over a fake yfinance Ticker
# ---------------------------------------------------------------------------


def _frame(days, close=20.0, splits=None):
    index = pd.DatetimeIndex([pd.Timestamp(d, tz="America/New_York") for d in days], name="Date")
    data = {"Open": [close] * len(days), "High": [close] * len(days), "Low": [close] * len(days), "Close": [close] * len(days), "Volume": [1000] * len(days), "Dividends": [0.0] * len(days), "Stock Splits": [(splits or {}).get(d, 0.0) for d in days]}
    return pd.DataFrame(data, index=index)


class FakeTicker:
    def __init__(self, frame=None, error=None):
        self.frame = frame
        self.error = error
        self.kwargs = []

    def history(self, **kwargs):
        self.kwargs.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.frame


def _source(ticker, tmp_path):
    return YFinancePrices(ticker_factory=lambda symbol: ticker, cache_dir=tmp_path)


def test_daily_bars_map_the_frame_and_the_end_is_inclusive(tmp_path):
    ticker = FakeTicker(_frame(["2025-01-13", "2025-01-14"]))
    bars = _source(ticker, tmp_path).daily_bars("brk.b", "2025-01-13", "2025-01-14")
    assert [b.time for b in bars] == ["2025-01-13T00:00:00Z", "2025-01-14T00:00:00Z"]
    assert bars[0].close == 20.0 and bars[0].volume == 1000
    assert ticker.kwargs[0]["end"] == "2025-01-15"  # yfinance's end is exclusive
    assert ticker.kwargs[0]["auto_adjust"] is False and ticker.kwargs[0]["interval"] == "1d"


def test_empty_frame_and_missing_price_errors_mean_no_data(tmp_path):
    from yfinance import exceptions as yfe

    assert _source(FakeTicker(_frame([])), tmp_path).daily_bars("X", "2025-01-13", "2025-01-14") == []
    assert _source(FakeTicker(error=yfe.YFPricesMissingError("X", {})), tmp_path).daily_bars("X", "2025-01-13", "2025-01-14") == []


def test_http_404_means_the_symbol_does_not_exist(tmp_path):
    class NotFound(Exception):
        def __init__(self):
            super().__init__("HTTP Error 404: ")
            self.response = type("R", (), {"status_code": 404})()

    assert _source(FakeTicker(error=NotFound()), tmp_path).daily_bars("ZZZZ", "2025-01-13", "2025-01-14") == []
    assert _source(FakeTicker(error=NotFound()), tmp_path).history("ZZZZ").closes == {}


def test_rate_limit_and_unexpected_errors_raise(tmp_path):
    from yfinance import exceptions as yfe

    with pytest.raises(PriceSourceError) as exc:
        _source(FakeTicker(error=yfe.YFRateLimitError()), tmp_path).daily_bars("X", "2025-01-13", "2025-01-14")
    assert exc.value.status_code == 429 and isinstance(exc.value, DataClientError)
    with pytest.raises(PriceSourceError):
        _source(FakeTicker(error=RuntimeError("boom")), tmp_path).daily_bars("X", "2025-01-13", "2025-01-14")


def test_history_records_splits_and_is_cached_on_disk(tmp_path):
    ticker = FakeTicker(_frame(["2024-06-07", "2024-06-10", "2024-06-11"], splits={"2024-06-10": 2.0}))
    history = _source(ticker, tmp_path).history("X")
    assert history.splits == {date(2024, 6, 10): 2.0}
    assert history.unadjusted_close_on_or_before(date(2024, 6, 8)) == 40.0  # Friday's close, pre-split
    assert history.unadjusted_close_on_or_before(date(2024, 6, 11)) == 20.0
    prices_module._HISTORY_MEMO.clear()
    again = _source(FakeTicker(error=RuntimeError("must not be called")), tmp_path).history("X")
    assert again.closes == history.closes and again.splits == history.splits


def test_daily_history_lookback_window():
    history = DailyHistory(closes={date(2024, 1, 2): 10.0})
    assert history.unadjusted_close_on_or_before(date(2024, 1, 9)) == 10.0
    assert history.unadjusted_close_on_or_before(date(2024, 1, 10)) is None
