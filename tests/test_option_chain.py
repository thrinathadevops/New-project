"""Tests for the Angel One market-data parsing (LTP + open interest).

Uses a stub broker client so no network/credentials are needed.
"""

from intraday_advisor.nse_option_chain import _fetch_market_data_angel


class _StubAPI:
    def __init__(self, fetched, status=True):
        self._fetched = fetched
        self._status = status
        self.calls = []

    def getMarketData(self, mode, payload):
        self.calls.append((mode, list(payload.get("NFO", []))))
        return {"status": self._status, "data": {"fetched": self._fetched}}


def test_market_data_parses_ltp_and_oi():
    api = _StubAPI([
        {"symbolToken": "111", "ltp": "50.5", "opnInterest": "12000"},
        {"symbolToken": "222", "ltp": 100, "opnInterest": 5000},
    ])
    md = _fetch_market_data_angel(["111", "222"], api)
    # Asserting the fields this test is about, rather than exact dict equality:
    # the parser also returns the bid/ask book now, and a test that breaks
    # whenever a field is ADDED is testing the signature, not the behaviour.
    assert md["111"]["ltp"] == 50.5
    assert md["111"]["oi"] == 12000.0
    assert md["222"]["oi"] == 5000.0


def test_a_payload_without_depth_reports_no_spread_rather_than_a_free_one():
    """These payloads carry no book at all, which is the pre-existing shape.
    The spread keys must come back None -- a 0 would read as a free entry on
    the one number that decides whether a trade clears its costs."""
    api = _StubAPI([{"symbolToken": "111", "ltp": 50.5, "opnInterest": 12000}])
    md = _fetch_market_data_angel(["111"], api)

    assert md["111"]["half_spread_pct"] is None
    assert md["111"]["bid"] is None and md["111"]["ask"] is None
    assert md["111"]["ask_qty"] == 0.0


def test_market_data_batches_in_50s():
    api = _StubAPI([])
    _fetch_market_data_angel([str(i) for i in range(60)], api)
    assert len(api.calls) == 2
    assert len(api.calls[0][1]) == 50
    assert len(api.calls[1][1]) == 10


def test_market_data_accepts_openInterest_alias():
    api = _StubAPI([{"symbolToken": "1", "ltp": 10, "openInterest": 777}])
    assert _fetch_market_data_angel(["1"], api)["1"]["oi"] == 777.0


def test_market_data_safe_on_empty_or_none():
    assert _fetch_market_data_angel([], None) == {}
    assert _fetch_market_data_angel(["1"], None) == {}


def test_market_data_skips_failed_status():
    api = _StubAPI([{"symbolToken": "1", "ltp": 10, "opnInterest": 1}], status=False)
    assert _fetch_market_data_angel(["1"], api) == {}
