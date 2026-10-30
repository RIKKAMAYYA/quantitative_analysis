"""组合持仓/交易流水名称补全契约：portfolio 相关端点每行带 name。

名称源：StockNameRegistry（stock_basic 优先，TickFlow 兜底，进程内缓存）。
Issue 0001 同族的可用性增量：风险管理/投资组合页只见代码不见名称。
"""

from __future__ import annotations

import pytest
from flask import Flask

from app.api.ml_factor_api import ml_factor_bp

pytestmark = pytest.mark.module_portfolio


class FakeRegistry:
    def __init__(self, names):
        self.names = names

    def merge_names(self, rows):
        for row in rows:
            if row.get("name") is None:
                row["name"] = self.names.get(str(row.get("ts_code")))
        return rows


FAKE_POSITIONS = [
    {"id": 6, "portfolio_id": "t1", "ts_code": "000001.SZ", "position_size": 1000.0,
     "avg_cost": 10.0, "current_price": 10.98, "market_value": 10980.0,
     "unrealized_pnl": 980.0, "weight": 0.6, "sector": "银行", "is_active": True,
     "created_at": "2026-06-04T10:53:10"},
    {"id": 7, "portfolio_id": "t1", "ts_code": "300305.SZ", "position_size": 1000.0,
     "avg_cost": 8.0, "current_price": 7.55, "market_value": 7550.0,
     "unrealized_pnl": -450.0, "weight": 0.4, "sector": None, "is_active": True,
     "created_at": "2026-06-04T10:53:34"},
]

FAKE_TRADES = [
    {"id": 1, "portfolio_id": "t1", "ts_code": "000001.SZ", "action": "buy",
     "quantity": 1000.0, "price": 10.0, "traded_at": "2026-06-04"},
]


@pytest.fixture()
def client(monkeypatch):
    import app.api.ml_factor_api as mod

    class FakeRepo:
        def list_positions(self, pid, active_only=True):
            return [dict(p) for p in FAKE_POSITIONS]

        def calculate_metrics(self, pid):
            return {"total_positions": 2, "total_market_value": 18530.0,
                    "total_unrealized_pnl": 530.0, "total_pnl_percentage": 2.94,
                    "max_position_weight": 0.59}

        def list_portfolio_ids(self, active_only=True):
            return ["t1"]

        def list_trades(self, pid, limit=200):
            return [dict(t) for t in FAKE_TRADES]

    monkeypatch.setattr(mod, "_portfolio_repo", FakeRepo())
    monkeypatch.setattr(
        "app.services.stock_name_registry.get_stock_name_registry",
        lambda: FakeRegistry({"000001.SZ": "平安银行", "300305.SZ": "裕兴股份"}),
    )
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(ml_factor_bp)
    return app.test_client()


class TestPositionNames:
    def test_detail_positions_carry_name(self, client):
        resp = client.get("/api/ml-factor/portfolio/t1").get_json()
        names = [p.get("name") for p in resp["portfolio"]["positions"]]
        assert names == ["平安银行", "裕兴股份"]

    def test_unknown_code_name_is_none_not_crash(self, client):
        import app.api.ml_factor_api as mod
        FAKE_POSITIONS[1]["ts_code"] = "999999.SZ"
        try:
            resp = client.get("/api/ml-factor/portfolio/t1").get_json()
            assert resp["portfolio"]["positions"][1]["name"] is None
        finally:
            FAKE_POSITIONS[1]["ts_code"] = "300305.SZ"

    def test_trades_carry_name(self, client):
        resp = client.get("/api/ml-factor/portfolio/t1/trades").get_json()
        assert resp["trades"][0]["name"] == "平安银行"
