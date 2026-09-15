"""Source selection: env var, per-source cache dir, credential and model gates."""

from __future__ import annotations

import pytest

from hedge_fund.data import factory
from hedge_fund.data.factory import (
    cache_dir_for,
    data_refresh,
    DATA_REFRESH_ENV,
    data_source,
    DATA_SOURCE_ENV,
    missing_data_key,
    open_data_client,
    unsupported_model_names,
)


@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv(DATA_SOURCE_ENV, raising=False)
    monkeypatch.delenv(DATA_REFRESH_ENV, raising=False)
    monkeypatch.delenv("FINANCIAL_DATASETS_API_KEY", raising=False)
    monkeypatch.delenv("HEDGE_FUND_SEC_USER_AGENT", raising=False)


def test_default_is_free(clean_env):
    assert data_source() == "free"


def test_env_selects_fd_case_insensitively(clean_env, monkeypatch):
    monkeypatch.setenv(DATA_SOURCE_ENV, "FD")
    assert data_source() == "fd"


def test_invalid_source_raises_listing_options(clean_env, monkeypatch):
    monkeypatch.setenv(DATA_SOURCE_ENV, "bloomberg")
    with pytest.raises(ValueError, match="free, fd"):
        data_source()


def test_cache_dirs_differ_per_source():
    assert cache_dir_for("free") != cache_dir_for("fd")
    assert cache_dir_for("fd").name == "data"  # the pre-existing Financial Datasets cache


def test_missing_data_key_names_each_sources_variable(clean_env, monkeypatch):
    assert missing_data_key("free") == "HEDGE_FUND_SEC_USER_AGENT"
    assert missing_data_key("fd") == "FINANCIAL_DATASETS_API_KEY"
    monkeypatch.setenv("HEDGE_FUND_SEC_USER_AGENT", "Name n@example.com")
    assert missing_data_key("free") is None


def test_unsupported_models_only_pead_under_free():
    assert unsupported_model_names(["buffett", "pead", "pead", "munger"], "free") == ["pead"]
    assert unsupported_model_names(["buffett", "pead"], "fd") == []


def test_open_data_client_wraps_and_closes(clean_env, monkeypatch, tmp_path):
    class Raw:
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    raw = Raw()
    monkeypatch.setattr(factory, "make_data_client", lambda source=None: raw)
    monkeypatch.setattr(factory, "cache_dir_for", lambda source=None: tmp_path)
    with open_data_client("free") as fd:
        assert fd._client is raw and fd._dir == tmp_path
    assert raw.closed


def test_refresh_comes_from_the_env_unless_given(clean_env, monkeypatch, tmp_path):
    class Raw:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(factory, "make_data_client", lambda source=None: Raw())
    monkeypatch.setattr(factory, "cache_dir_for", lambda source=None: tmp_path)
    assert data_refresh() is False
    with open_data_client("free") as fd:
        assert fd._refresh is False
    monkeypatch.setenv(DATA_REFRESH_ENV, "1")
    assert data_refresh() is True
    with open_data_client("free") as fd:
        assert fd._refresh is True
    with open_data_client("free", refresh=False) as fd:
        assert fd._refresh is False  # an explicit argument wins
