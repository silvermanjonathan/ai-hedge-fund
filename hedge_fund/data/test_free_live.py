"""Live smoke of the free source against the real SEC and Yahoo endpoints.

Skipped unless HEDGE_FUND_LIVE_TESTS=1 and HEDGE_FUND_SEC_USER_AGENT are set.
Asserts on figures that are public record and stable (Apple's fiscal 2024
10-K was filed 2024-11-01), not on prices.
"""

from __future__ import annotations

import os

import pytest

from hedge_fund.data import FreeDataClient
from hedge_fund.data.edgar import SEC_USER_AGENT_ENV

pytestmark = pytest.mark.skipif(
    os.environ.get("HEDGE_FUND_LIVE_TESTS") != "1" or not os.environ.get(SEC_USER_AGENT_ENV),
    reason="live SEC/Yahoo smoke: set HEDGE_FUND_LIVE_TESTS=1 and HEDGE_FUND_SEC_USER_AGENT",
)


@pytest.fixture(scope="module")
def client():
    with FreeDataClient() as c:
        yield c


def test_aapl_prices_january_2024(client):
    bars = client.get_prices("AAPL", "2024-01-01", "2024-01-31")
    assert 19 <= len(bars) <= 22
    assert bars[0].time.startswith("2024-01-02")


def test_aapl_metrics_at_year_end_2024(client):
    rows = client.get_financial_metrics("AAPL", "2024-12-31", period="ttm", limit=20)
    assert len(rows) >= 4
    assert rows[0].report_period == "2024-09-28"
    assert rows[0].filing_date == "2024-11-01"
    assert 0.40 < rows[0].gross_margin < 0.50
    assert 2e12 < rows[0].market_cap < 4.5e12
    assert all(r.filing_date <= "2024-12-31" for r in rows)


def test_jpm_has_rows_without_current_ratio(client):
    rows = client.get_financial_metrics("JPM", "2024-12-31", period="ttm", limit=8)
    assert len(rows) >= 4
    assert rows[0].current_ratio is None
    assert rows[0].return_on_equity is not None


def test_cik_lookup_and_facts(client):
    assert client._edgar.cik_for("BRK.B") == 1067983
    facts = client.get_company_facts("AAPL")
    assert facts.sector == "Manufacturing" and facts.cik == "0000320193"


def test_googl_multi_class_market_cap_is_sane(client):
    rows = client.get_financial_metrics("GOOGL", "2024-12-31", period="ttm", limit=1)
    assert 1e12 < rows[0].market_cap < 4e12
