"""EdgarClient: required headers, pacing, retries, caching, CIK lookup. No network —
the session's request method is swapped for a closure, as in test_client_contract."""

from __future__ import annotations

import json

import pytest
import requests

from hedge_fund.data import edgar as edgar_module
from hedge_fund.data.edgar import EdgarClient, EdgarError, SEC_USER_AGENT_ENV
from hedge_fund.data.errors import DataClientError

TICKERS = {"0": {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"}, "1": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, "2": {"cik_str": 1067983, "ticker": "BRK-B", "title": "BERKSHIRE HATHAWAY INC"}}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text

    def json(self):
        return self._payload


def _stub(client, responses):
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append({"method": method, "url": url, **kwargs})
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    client._session.request = fake_request
    return calls


@pytest.fixture
def agent(monkeypatch):
    monkeypatch.setenv(SEC_USER_AGENT_ENV, "Test Runner test@example.com")
    monkeypatch.setattr(edgar_module.time, "sleep", lambda s: None)
    edgar_module._MEMO.clear()
    edgar_module._LIMITER._last = float("-inf")


@pytest.fixture
def client(agent, tmp_path):
    c = EdgarClient(cache_dir=tmp_path)
    yield c
    c.close()


def test_missing_user_agent_raises_naming_the_variable(monkeypatch, tmp_path):
    monkeypatch.delenv(SEC_USER_AGENT_ENV, raising=False)
    with pytest.raises(EdgarError, match=SEC_USER_AGENT_ENV):
        EdgarClient(cache_dir=tmp_path)


def test_headers_declare_the_client(client):
    assert client._session.headers["User-Agent"] == "Test Runner test@example.com"
    assert client._session.headers["Accept-Encoding"] == "gzip, deflate"


def test_rate_limiter_paces_every_request(agent, tmp_path, monkeypatch):
    clock = {"now": 100.0}
    sleeps = []
    monkeypatch.setattr(edgar_module.time, "monotonic", lambda: clock["now"])

    def sleep(seconds):
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(edgar_module.time, "sleep", sleep)
    a, b = EdgarClient(cache_dir=tmp_path / "a"), EdgarClient(cache_dir=tmp_path / "b")
    _stub(a, [_FakeResponse(payload={"ok": 1})])
    _stub(b, [_FakeResponse(payload={"ok": 2})])
    a._get_json("https://data.sec.gov/x")
    b._get_json("https://data.sec.gov/y")  # a different instance shares the limiter
    assert sleeps == [pytest.approx(edgar_module._MIN_INTERVAL)]


def test_403_retries_then_raises_with_hint(client):
    calls = _stub(client, [_FakeResponse(403, text="Undeclared Automated Tool")] * 4)
    with pytest.raises(EdgarError, match="User-Agent") as exc:
        client._get_json("https://data.sec.gov/x")
    assert exc.value.status_code == 403
    assert len(calls) == len(edgar_module._RETRY_DELAYS) + 1
    assert isinstance(exc.value, DataClientError)


def test_429_then_success_recovers(client):
    _stub(client, [_FakeResponse(429), _FakeResponse(payload={"ok": True})])
    assert client._get_json("https://data.sec.gov/x") == {"ok": True}


def test_500_and_network_errors_raise(client):
    _stub(client, [_FakeResponse(500)] * 4)
    with pytest.raises(EdgarError):
        client._get_json("https://data.sec.gov/x")
    _stub(client, [requests.ConnectionError("dns")])
    with pytest.raises(EdgarError):
        client._get_json("https://data.sec.gov/y")


def test_404_means_missing_and_is_cached(client, tmp_path):
    calls = _stub(client, [_FakeResponse(404)])
    assert client.company_facts(999) is None
    assert client.company_facts(999) is None
    assert len(calls) == 1
    assert json.loads((tmp_path / "companyfacts" / "CIK0000000999.json").read_text()) == edgar_module._MISSING


def test_cik_lookup_normalises_and_pads(client):
    calls = _stub(client, [_FakeResponse(payload=TICKERS)])
    assert client.cik_for("aapl") == 320193
    assert client.cik_for("BRK.B") == 1067983
    assert client.cik_for("BRK-B") == 1067983
    assert client.cik_for("SPY") is None
    assert len(calls) == 1
    assert calls[0]["url"] == edgar_module.TICKERS_URL
    _stub(client, [_FakeResponse(payload={"facts": {}})])
    client.company_facts(320193)
    assert client._session.request  # padded URL asserted through the path on disk
    assert (client._dir / "companyfacts" / "CIK0000320193.json").exists()


def test_disk_cache_serves_within_ttl_and_refetches_after(client, monkeypatch):
    calls = _stub(client, [_FakeResponse(payload={"v": 1}), _FakeResponse(payload={"v": 2})])
    assert client.submissions(1)["v"] == 1
    edgar_module._MEMO.clear()
    assert client.submissions(1)["v"] == 1  # disk hit, no second request
    assert len(calls) == 1
    edgar_module._MEMO.clear()
    monkeypatch.setattr(edgar_module.time, "time", lambda: 1e12)  # far past the TTL
    assert client.submissions(1)["v"] == 2
    assert len(calls) == 2


def test_corrupt_cache_file_is_refetched(client, tmp_path):
    path = tmp_path / "submissions" / "CIK0000000001.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    _stub(client, [_FakeResponse(payload={"v": 3})])
    assert client.submissions(1)["v"] == 3
    assert not [p for p in path.parent.iterdir() if p.suffix == ".tmp"]


# ---------------------------------------------------------------------------
# Succession: predecessor CIK from the 8-K12B
# ---------------------------------------------------------------------------

from hedge_fund.data.edgar import (  # noqa: E402
    normalize_company_name,
    predecessor_name,
    strip_corporate_suffix,
    successor_filing,
)

_8K12B = "<html><body><p>On July 1, 2026, Exxon Mobil Corporation, a New Jersey corporation and the " "predecessor registrant (&#8220;ExxonMobil&#8221;), merged with Ensign LLC. This Current Report " "on Form 8-K is being filed for the purpose of establishing ExxonMobil Holdings Corporation as the " "successor registrant of ExxonMobil&#8217;s common stock pursuant to Rule 12g-3(a).</p></body></html>"
_SUBMISSIONS = {
    "name": "ExxonMobil Holdings Corp",
    "filings": {
        "recent": {
            "form": ["10-Q", "8-K12B", "8-K"],
            "filingDate": ["2026-08-03", "2026-07-01", "2026-07-02"],
            "accessionNumber": ["0001193125-26-300000", "0001193125-26-291990", "0001193125-26-292000"],
            "primaryDocument": ["q.htm", "d71068d8k12b.htm", "k.htm"],
        }
    },
}
_ATOM = '<?xml version="1.0"?><feed><company-info><cik>0000034088</cik>' "<conformed-name>EXXON MOBIL CORP</conformed-name></company-info></feed>"


def test_predecessor_name_via_defined_term_and_direct_form():
    assert predecessor_name(_8K12B) == "Exxon Mobil Corporation"
    assert predecessor_name("<p>Newco Inc. is the successor issuer to Oldco Holdings Corp. pursuant to Rule 12g-3.</p>") == "Oldco Holdings Corp."
    assert predecessor_name("<p>An ordinary 8-K about a dividend.</p>") is None


def test_company_name_normalisation():
    assert normalize_company_name("Exxon Mobil Corporation") == normalize_company_name("EXXON MOBIL CORP")
    assert normalize_company_name("The Coca-Cola Company") == "COCA COLA CO"
    assert strip_corporate_suffix("Exxon Mobil Corporation") == "Exxon Mobil"
    assert strip_corporate_suffix("Oldco Holdings Corp.") == "Oldco Holdings"


def test_successor_filing_picks_the_earliest_8k12():
    assert successor_filing(_SUBMISSIONS) == ("0001193125-26-291990", "d71068d8k12b.htm")
    assert successor_filing({"filings": {"recent": {"form": ["10-K"], "filingDate": ["2025-02-01"], "accessionNumber": ["a"], "primaryDocument": ["b"]}}}) is None


def test_predecessor_cik_end_to_end_and_cached(client, tmp_path):
    plain = {"name": "EXXON MOBIL CORP", "filings": {"recent": {"form": ["10-K"], "filingDate": ["2025-02-26"], "accessionNumber": ["a"], "primaryDocument": ["b"]}}}
    calls = _stub(
        client,
        [
            _FakeResponse(payload=_SUBMISSIONS),  # successor's submissions
            _FakeResponse(text=_8K12B),  # the 8-K12B primary document
            _FakeResponse(text=_ATOM),  # company search
            _FakeResponse(payload=plain),  # predecessor's submissions: no succession of its own
        ],
    )
    assert client.predecessor_cik(2115436) == 34088
    assert "d71068d8k12b.htm" in calls[1]["url"] and "000119312526291990" in calls[1]["url"]
    assert calls[2]["params"]["company"] == "Exxon Mobil"
    assert client.cik_chain(2115436) == [2115436, 34088]
    assert client.predecessor_cik(2115436) == 34088  # memo
    assert len([c for c in calls if "browse-edgar" in c["url"]]) == 1
    assert (tmp_path / "predecessors" / "CIK0002115436.json").exists()


def test_predecessor_none_without_succession_filing(client):
    _stub(client, [_FakeResponse(payload={"name": "Plain Co", "filings": {"recent": {"form": ["10-K"], "filingDate": ["2025-02-01"], "accessionNumber": ["a"], "primaryDocument": ["b"]}}})])
    assert client.predecessor_cik(1) is None
    assert client.cik_chain(1) == [1]


def test_predecessor_none_when_search_is_ambiguous(client):
    two = _ATOM.replace("</feed>", "<company-info><cik>0000099999</cik><conformed-name>EXXON MOBIL CORP</conformed-name></company-info></feed>")
    _stub(client, [_FakeResponse(payload=_SUBMISSIONS), _FakeResponse(text=_8K12B), _FakeResponse(text=two)])
    assert client.predecessor_cik(2115436) is None
