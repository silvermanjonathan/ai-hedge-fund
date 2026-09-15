"""v2 data pipeline — data provider protocol, the clients, and response models.

Two providers behind one protocol: FDClient (Financial Datasets, paid key)
and FreeDataClient (SEC EDGAR + Yahoo Finance, no key). open_data_client()
picks one from HEDGE_FUND_DATA and wraps it in the disk cache.
"""

from hedge_fund.data.cached import CachedDataClient
from hedge_fund.data.client import FDClient, FDClientError
from hedge_fund.data.edgar import SEC_USER_AGENT_ENV, EdgarClient, EdgarError
from hedge_fund.data.errors import DataClientError
from hedge_fund.data.factory import (
    DATA_SOURCE_ENV,
    DATA_SOURCES,
    DEFAULT_DATA_SOURCE,
    cache_dir_for,
    data_source,
    make_data_client,
    missing_data_key,
    open_data_client,
    provider_label,
    required_env_var,
    unsupported_model_names,
)
from hedge_fund.data.free import FreeDataClient
from hedge_fund.data.models import (
    CompanyFacts,
    CompanyNews,
    Earnings,
    EarningsData,
    EarningsRecord,
    Filing,
    FinancialMetrics,
    InsiderTrade,
    Price,
)
from hedge_fund.data.prices import PriceSource, PriceSourceError, YFinancePrices
from hedge_fund.data.protocol import DataClient

__all__ = [
    "CachedDataClient",
    "CompanyFacts",
    "CompanyNews",
    "DATA_SOURCE_ENV",
    "DATA_SOURCES",
    "DEFAULT_DATA_SOURCE",
    "DataClient",
    "DataClientError",
    "Earnings",
    "EarningsData",
    "EarningsRecord",
    "EdgarClient",
    "EdgarError",
    "FDClient",
    "FDClientError",
    "Filing",
    "FinancialMetrics",
    "FreeDataClient",
    "InsiderTrade",
    "Price",
    "PriceSource",
    "PriceSourceError",
    "SEC_USER_AGENT_ENV",
    "YFinancePrices",
    "cache_dir_for",
    "data_source",
    "make_data_client",
    "missing_data_key",
    "open_data_client",
    "provider_label",
    "required_env_var",
    "unsupported_model_names",
]
