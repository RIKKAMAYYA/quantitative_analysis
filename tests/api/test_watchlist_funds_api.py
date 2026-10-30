"""自选基金 API 契约（Issue docs/issues/0001-watchlist-funds.md 决策4/5）。

- GET  /api/trial/watchlist/search  股票+基金合并联想（原 /api/market/ticker-search 不动）
- GET  /api/trial/watchlist/quotes  股票走快照、ETF/LOF 走实时价、场外用最新净值
fake 注入 provider 边界，不碰扶摇网络。
"""

from __future__ import annotations

import pytest
from flask import Flask

from app.api.trial_api import api_bp

pytestmark = pytest.mark.module_trial


class FakeBoardService:
    def __init__(self, rows=None, fail=False):
        self.rows = rows if rows is not None else [
            {"ts_code": "600519.SH", "ticker": "600519", "name": "贵州茅台", "exchange": "SH"},
        ]
        self.fail = fail

    def search_tickers(self, query, limit=10):
        if self.fail:
            raise RuntimeError("fuyao network")
        return {"query": query, "items": self.rows[:limit]}


class FakeFundService:
    def __init__(self, search_rows=None, snapshot=None, nav=None, profile=None, search_fail=False):
        self.search_rows = search_rows if search_rows is not None else [
            {"thscode": "515450.SH", "ticker": "515450", "name": "红利低波50ETF",
             "exchange": "SH", "asset_type": "fund-etf"},
        ]
        self.snapshot = snapshot or {}
        self.nav = nav or {}
        self.profile = profile or {}
        self.search_fail = search_fail

    def search_funds(self, query, asset_type=None, limit=20):
        if self.search_fail:
            raise RuntimeError("fuyao 500")
        return self.search_rows[:limit]

    def get_profile(self, thscode):
        return self.profile.get(thscode, {})

    def get_snapshot(self, thscode):
        if thscode not in self.snapshot:
            raise RuntimeError(f"fuyao 3001: {thscode}")
        return self.snapshot[thscode]

    def get_nav_series(self, thscode, range="month", nav_type="unit,adj"):
        return self.nav.get(thscode, [])


class FakeSnapshotService:
    def __init__(self, quotes=None):
        self.quotes = quotes or {}

    def get_quotes(self, codes):
        return {c: self.quotes[c] for c in codes if c in self.quotes}


@pytest.fixture()
def make_app(monkeypatch):
    def _make(board=None, fund=None, stock_quotes=None):
        board = board or FakeBoardService()
        fund = fund or FakeFundService()
        snap = FakeSnapshotService(stock_quotes or {})
        monkeypatch.setattr("app.api.trial_api.get_board_market_service", lambda: board)
        monkeypatch.setattr("app.api.trial_api.get_fund_service", lambda: fund)
        monkeypatch.setattr("app.api.trial_api.get_market_snapshot_service", lambda: snap)
        app = Flask(__name__)
        app.config.update(TESTING=True)
        app.register_blueprint(api_bp, url_prefix="/api")  # 与应用注册方式一致
        return app
    return _make


@pytest.fixture()
def client(make_app):
    return make_app().test_client()


class TestWatchlistSearch:
    def test_merges_stocks_and_funds_with_asset_type(self, client):
        resp = client.get("/api/trial/watchlist/search?q=红利")
        assert resp.status_code == 200
        items = resp.get_json()["data"]["items"]
        assert {i["asset_type"] for i in items} == {"stock", "fund-etf"}
        by_code = {i["ts_code"]: i for i in items}
        assert by_code["515450.SH"]["name"] == "红利低波50ETF"

    def test_stocks_come_first(self, client):
        items = client.get("/api/trial/watchlist/search?q=xx").get_json()["data"]["items"]
        types = [i["asset_type"] == "stock" for i in items]
        assert types == sorted(types, reverse=True)  # stock 段在前

    def test_short_query_rejected(self, client):
        assert client.get("/api/trial/watchlist/search?q=红").status_code == 400

    def test_fund_side_failure_degrades_to_stocks(self, make_app):
        app = make_app(fund=FakeFundService(search_fail=True))
        items = app.test_client().get("/api/trial/watchlist/search?q=600").get_json()["data"]["items"]
        assert [i["ts_code"] for i in items] == ["600519.SH"]

    def test_stock_side_failure_degrades_to_funds(self, make_app):
        app = make_app(board=FakeBoardService(fail=True))
        items = app.test_client().get("/api/trial/watchlist/search?q=515").get_json()["data"]["items"]
        assert [i["ts_code"] for i in items] == ["515450.SH"]

    def test_total_respects_limit(self, make_app):
        big = FakeBoardService(rows=[
            {"ts_code": f"60{i:04d}.SH", "ticker": "x", "name": "n", "exchange": "SH"} for i in range(10)
        ])
        app = make_app(board=big)
        items = app.test_client().get("/api/trial/watchlist/search?q=60&limit=3").get_json()["data"]["items"]
        assert len(items) == 3


class TestWatchlistQuotes:
    @pytest.fixture()
    def qclient(self, make_app):
        return make_app(
            stock_quotes={"600519.SH": {"name": "贵州茅台", "last_price": 1400.0}},
            fund=FakeFundService(
                snapshot={"515450.SH": {"last_price": 1.462, "prev_price": 1.449,
                                        "price_change_ratio_pct": 0.897}},
                nav={"005827.OF": [{"nav_date_ymd": "20261029", "unit_nav": 1.4461}]},
                profile={"515450.SH": {"fund_name": "红利低波50ETF南方"},
                         "005827.OF": {"fund_name": "蓝筹精选"}},
            ),
        ).test_client()

    def test_mixed_codes_return_mixed_quotes(self, qclient):
        quotes = qclient.get(
            "/api/trial/watchlist/quotes?codes=600519.SH,515450.SH,005827.OF"
        ).get_json()["data"]["quotes"]
        assert quotes["600519.SH"]["asset_type"] == "stock"
        assert quotes["600519.SH"]["last_price"] == 1400.0
        assert quotes["515450.SH"]["asset_type"] == "fund-etf"
        assert quotes["515450.SH"]["last_price"] == 1.462
        # 基金快照不返回名称，行必须带 profile 补全的 fund_name（Issue 0001 增量）
        assert quotes["515450.SH"]["name"] == "红利低波50ETF南方"
        assert quotes["005827.OF"]["name"] == "蓝筹精选"
        assert quotes["515450.SH"]["prev_close"] == 1.449
        assert quotes["515450.SH"]["pct_chg"] == pytest.approx(0.897)
        assert quotes["005827.OF"]["asset_type"] == "fund-otc"
        assert quotes["005827.OF"]["unit_nav"] == 1.4461
        assert quotes["005827.OF"]["nav_date"] == "2026-10-29"

    def test_failed_fund_snapshot_omitted(self, qclient):
        quotes = qclient.get(
            "/api/trial/watchlist/quotes?codes=600519.SH,999999.OF"
        ).get_json()["data"]["quotes"]
        assert "600519.SH" in quotes
        assert "999999.OF" not in quotes  # 无净值数据 → 省略，不造假

    def test_missing_codes_param(self, qclient):
        assert qclient.get("/api/trial/watchlist/quotes").status_code == 400

    def test_dedupe_and_cap(self, qclient):
        codes = ",".join(["600519.SH"] * 3 + [f"{i:06d}.SZ" for i in range(300)])
        quotes = qclient.get(f"/api/trial/watchlist/quotes?codes={codes}").get_json()["data"]["quotes"]
        assert "600519.SH" in quotes  # 去重后仍取到
