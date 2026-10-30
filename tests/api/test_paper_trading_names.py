"""模拟盘持仓/挂单/成交名称补全契约（与组合持仓同族，Issue 0001 可用性增量）。"""

from __future__ import annotations

import pytest
from flask import Flask

from app.api.paper_trading_api import paper_trading_bp

pytestmark = pytest.mark.module_trial


class FakeRegistry:
    def merge_names(self, rows):
        for row in rows:
            if row.get("name") is None:
                row["name"] = {"000001.SZ": "平安银行", "300750.SZ": "宁德时代"}.get(str(row.get("ts_code")))
        return rows


FAKE_STATUS = {
    "account_id": "a1", "cash": 1000.0, "nav": 2000.0,
    "positions": [{"ts_code": "000001.SZ", "qty": 100, "avg_cost": 10.0,
                   "last_close": 11.0, "market_value": 1100.0, "pnl": 100.0,
                   "pnl_pct": 0.1}],
    "pending_orders": [{"order_id": "o1", "ts_code": "300750.SZ", "side": "buy",
                        "status": "pending", "target_value": 500.0}],
    "closed_orders": [],
    "stats": {},
}

FAKE_HISTORY = {
    "nav_history": [],
    "trades": [{"trade_id": 1, "date": "2026-10-29", "ts_code": "000001.SZ",
                "side": "buy", "qty": 100, "price": 10.0, "value": 1000.0}],
}


@pytest.fixture()
def client(monkeypatch):
    import app.api.paper_trading_api as mod

    class FakeSvc:
        def status(self):
            return FAKE_STATUS
        def history(self, limit):
            return FAKE_HISTORY

    monkeypatch.setattr(mod, "_service", lambda: FakeSvc())
    monkeypatch.setattr(
        "app.services.stock_name_registry.get_stock_name_registry",
        lambda: FakeRegistry(),
    )
    app = Flask(__name__)
    app.config.update(TESTING=True)
    app.register_blueprint(paper_trading_bp, url_prefix="/api/paper-trading")
    return app.test_client()


class TestPaperNames:
    def test_status_positions_carry_name(self, client):
        d = client.get("/api/paper-trading/status").get_json()["data"]
        assert d["positions"][0]["name"] == "平安银行"

    def test_pending_orders_carry_name(self, client):
        d = client.get("/api/paper-trading/status").get_json()["data"]
        assert d["pending_orders"][0]["name"] == "宁德时代"

    def test_history_trades_carry_name(self, client):
        d = client.get("/api/paper-trading/history").get_json()["data"]
        assert d["trades"][0]["name"] == "平安银行"
