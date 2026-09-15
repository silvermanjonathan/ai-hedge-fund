"""Finviz universe module over a fake session — no network."""

from __future__ import annotations

import os

import pytest

from hedge_fund.universe import finviz
from hedge_fund.universe.finviz import (
    BASE,
    fetch,
    FINVIZ_TOKEN_ENV,
    FinvizError,
    parse,
    PRESETS,
    resolve,
)

CSV = "No.,Ticker\r\n1,AAPL\r\n2,msft\r\n3,\r\n4,GOOGL\r\n"


class _Resp:
    def __init__(self, status_code=200, text=CSV, history=()):
        self.status_code = status_code
        self.text = text
        self.history = list(history)


class FakeSession:
    def __init__(self, response=None):
        self.response = response or _Resp()
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append({"url": url, **kwargs})
        return self.response


@pytest.fixture(autouse=True)
def token(request, monkeypatch):
    """A fake token for the offline tests; the gated live test keeps the real one."""
    finviz._MEMO.clear()
    if "live" not in request.node.name:
        monkeypatch.setenv(FINVIZ_TOKEN_ENV, "test-token")


def test_missing_token_raises_naming_the_variable(monkeypatch, tmp_path):
    monkeypatch.delenv(FINVIZ_TOKEN_ENV, raising=False)
    with pytest.raises(FinvizError, match=FINVIZ_TOKEN_ENV):
        fetch("quality", session=FakeSession(), cache_dir=tmp_path)


def test_presets_all_start_with_base():
    assert set(PRESETS) == {"quality", "value", "garp", "bearish"}
    assert all(f.startswith(BASE + ",") for f in PRESETS.values())
    assert resolve("value") == ("value", PRESETS["value"])
    assert resolve("geo_usa,cap_midover") == (None, "geo_usa,cap_midover")


def test_request_shape(tmp_path):
    session = FakeSession()
    fetch("quality", session=session, cache_dir=tmp_path)
    (call,) = session.calls
    assert call["url"] == finviz.EXPORT_URL
    assert call["params"] == {"v": "111", "f": PRESETS["quality"], "c": "1", "auth": "test-token"}
    assert call["allow_redirects"] is True
    assert "ft" not in call["params"]


def test_csv_parses_by_header_and_respects_limit(tmp_path):
    assert fetch("quality", session=FakeSession(), cache_dir=tmp_path) == ["AAPL", "MSFT", "GOOGL"]
    finviz._MEMO.clear()
    assert fetch("quality", limit=2, session=FakeSession(), cache_dir=tmp_path) == ["AAPL", "MSFT"]
    assert parse("Ticker,No.\r\nAAPL,1\r\n") == ["AAPL"]  # column order does not matter


def test_empty_body_returns_empty_and_bad_header_raises(tmp_path):
    assert fetch("value", session=FakeSession(_Resp(text="")), cache_dir=tmp_path) == []
    with pytest.raises(FinvizError, match="Ticker column"):
        fetch("garp", session=FakeSession(_Resp(text="<html>login</html>")), cache_dir=tmp_path)


def test_non_200_raises_with_status(tmp_path):
    with pytest.raises(FinvizError) as exc:
        fetch("quality", session=FakeSession(_Resp(status_code=403, text="")), cache_dir=tmp_path)
    assert exc.value.status_code == 403


def test_cache_hit_skips_network_and_refresh_bypasses(tmp_path):
    first = FakeSession()
    assert fetch("quality", session=first, cache_dir=tmp_path) == ["AAPL", "MSFT", "GOOGL"]
    assert len(list(tmp_path.glob("*.csv"))) == 1 and not list(tmp_path.glob("*.tmp"))
    finviz._MEMO.clear()  # disk, not memo
    second = FakeSession()
    assert fetch("quality", session=second, cache_dir=tmp_path) == ["AAPL", "MSFT", "GOOGL"]
    assert second.calls == []
    third = FakeSession(_Resp(text="No.,Ticker\r\n1,XOM\r\n"))
    assert fetch("quality", refresh=True, session=third, cache_dir=tmp_path) == ["XOM"]
    assert len(third.calls) == 1


pytestmark_live = pytest.mark.skipif(
    os.environ.get("HEDGE_FUND_LIVE_TESTS") != "1" or not os.environ.get(FINVIZ_TOKEN_ENV),
    reason="live Finviz export: set HEDGE_FUND_LIVE_TESTS=1 and FINVIZ_AUTH_TOKEN",
)


@pytestmark_live
def test_live_quality_preset_is_a_direct_200(tmp_path):
    resp = finviz.download(PRESETS["quality"], auth=finviz.token())
    assert resp.status_code == 200 and resp.history == []  # no redirect
    tickers = parse(resp.text)
    assert tickers and all(t.isupper() and t.isalnum() for t in tickers)
