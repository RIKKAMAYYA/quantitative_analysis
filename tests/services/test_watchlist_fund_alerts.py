"""基金价格提醒契约：盘后确认口径（Issue docs/issues/0001-watchlist-funds.md 决策5）。

- ETF：扶摇历史日线收盘价（前复权）
- LOF / 场外：最新单位净值（净值日期随消息带出，不谎称当日）
- 去重键沿用 (ts_code, kind, 数据日期)；缺数据静默跳过不产告警
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.services.watchlist_service import WatchlistAlertService, WatchlistService

pytestmark = pytest.mark.module_trial


class FakeFundService:
    """按 thscode 返回日线/净值的最小 fake。"""

    def __init__(self, history=None, nav=None, fail=()):
        self._history = history or {}
        self._nav = nav or {}
        self._fail = set(fail)
        self.history_calls = []

    def get_history(self, thscode, start, end):
        self.history_calls.append((thscode, start, end))
        if thscode in self._fail:
            raise RuntimeError("fuyao 3001 标的不存在")
        return {"items": self._history.get(thscode, [])}

    def get_nav_series(self, thscode, range="month", nav_type="unit,adj"):
        return self._nav.get(thscode, [])


def make_daily(rows_by_code):
    frames = [pd.DataFrame(rows) for rows in rows_by_code.values()]
    return pd.concat(frames, ignore_index=True)


@pytest.fixture()
def stock_only_env(tmp_path, monkeypatch):
    """股票自选 + Parquet 行情（老路径），验证零回归。"""
    svc = WatchlistService(data_dir=str(tmp_path))
    svc.replace_items([{"ts_code": "600519.SH", "alert_low": 1000}])
    daily = make_daily({"600519.SH": [
        {"ts_code": "600519.SH", "trade_date": "2026-10-28", "close": 980.0},
    ]})
    import app.services.data_reader as dr_mod
    monkeypatch.setattr(dr_mod.ParquetDataReader, "get_daily",
                        lambda self, ts_codes=None, start_date=None, end_date=None: daily.copy())
    return svc


class TestStockPathUnchanged:
    def test_stock_alert_still_fires(self, stock_only_env):
        result = WatchlistAlertService(data_dir=stock_only_env.data_dir).check()
        assert result["checked"] == 1
        assert result["triggered"][0]["ts_code"] == "600519.SH"
        assert "收盘" in result["triggered"][0]["message"]


class TestEtfAlerts:
    @pytest.fixture()
    def etf_env(self, tmp_path):
        svc = WatchlistService(data_dir=str(tmp_path))
        svc.add_item("515450.SH", asset_type="fund-etf")
        svc.update_item("515450.SH", {"alert_low": 1.40})
        return svc

    def _check(self, svc, fund):
        import app.services.fund_service as fs_mod
        return WatchlistAlertService(data_dir=svc.data_dir, fund_service=fund).check()

    def test_etf_fires_on_history_close(self, etf_env):
        fund = FakeFundService(history={"515450.SH": [
            {"date": "20261029", "close_price": 1.462},
            {"date": "20261030", "close_price": 1.395},
        ]})
        result = self._check(etf_env, fund)
        hit = [a for a in result["triggered"] if a["ts_code"] == "515450.SH"]
        assert len(hit) == 1
        assert hit[0]["close"] == 1.395
        assert hit[0]["close_date"] == "2026-10-30"
        assert hit[0]["asset_type"] == "fund-etf"
        assert "收盘" in hit[0]["message"]

    def test_etf_uses_latest_bar_not_first(self, etf_env):
        fund = FakeFundService(history={"515450.SH": [
            {"date": "20261030", "close_price": 1.462},
            {"date": "20261029", "close_price": 1.300},  # 乱序输入
        ]})
        result = self._check(etf_env, fund)
        assert result["triggered"] == []  # 最新一条 1.462 未越线

    def test_etf_dedupe_same_data_date(self, etf_env):
        fund = FakeFundService(history={"515450.SH": [
            {"date": "20261030", "close_price": 1.395},
        ]})
        first = self._check(etf_env, fund)
        assert len(first["triggered"]) == 1
        second = self._check(etf_env, fund)
        assert second["triggered"] == []

    def test_fund_service_error_skips_silently(self, etf_env):
        fund = FakeFundService(fail={"515450.SH"})
        result = self._check(etf_env, fund)
        assert result["triggered"] == []
        assert result["checked"] == 1


class TestLofAndOtcAlerts:
    def test_lof_fires_on_latest_nav(self, tmp_path):
        svc = WatchlistService(data_dir=str(tmp_path))
        svc.add_item("161725.SZ", asset_type="fund-lof")
        svc.update_item("161725.SZ", {"alert_high": 1.20})
        fund = FakeFundService(nav={"161725.SZ": [
            {"nav_date_ymd": "20261029", "unit_nav": 1.18},
            {"nav_date_ymd": "20261030", "unit_nav": 1.25},
        ]})
        result = WatchlistAlertService(data_dir=str(tmp_path), fund_service=fund).check()
        hit = result["triggered"][0]
        assert hit["close"] == 1.25
        assert hit["close_date"] == "2026-10-30"
        assert "净值" in hit["message"]
        assert hit["asset_type"] == "fund-lof"

    def test_otc_fires_on_latest_nav(self, tmp_path):
        svc = WatchlistService(data_dir=str(tmp_path))
        svc.add_item("005827.OF")
        svc.update_item("005827.OF", {"alert_low": 2.0})
        fund = FakeFundService(nav={"005827.OF": [
            {"nav_date_ymd": "20261030", "unit_nav": 1.93},
        ]})
        result = WatchlistAlertService(data_dir=str(tmp_path), fund_service=fund).check()
        assert result["triggered"][0]["ts_code"] == "005827.OF"
        assert result["triggered"][0]["asset_type"] == "fund-otc"

    def test_missing_nav_skipped(self, tmp_path):
        svc = WatchlistService(data_dir=str(tmp_path))
        svc.add_item("005827.OF")
        svc.update_item("005827.OF", {"alert_low": 2.0})
        result = WatchlistAlertService(data_dir=str(tmp_path),
                                       fund_service=FakeFundService()).check()
        assert result["triggered"] == []

    def test_nav_without_date_skipped(self, tmp_path):
        # 净值缺 nav_date_ymd 时不得用今天日期冒充实盘价
        svc = WatchlistService(data_dir=str(tmp_path))
        svc.add_item("005827.OF")
        svc.update_item("005827.OF", {"alert_low": 2.0})
        fund = FakeFundService(nav={"005827.OF": [{"unit_nav": 1.93}]})
        result = WatchlistAlertService(data_dir=str(tmp_path), fund_service=fund).check()
        assert result["triggered"] == []


class TestMixedWatchlist:
    def test_stocks_and_funds_checked_in_one_pass(self, tmp_path, monkeypatch):
        svc = WatchlistService(
            data_dir=str(tmp_path),
            stock_lookup=lambda: {"600519.SH"},
            fund_lookup=lambda c: [{"thscode": "515450.SH", "asset_type": "fund-etf"}] if c == "515450" else [],
        )
        svc.add_item("600519.SH")
        svc.add_item("515450")
        svc.update_item("600519.SH", {"alert_low": 1000})
        svc.update_item("515450.SH", {"alert_low": 1.40})
        daily = make_daily({"600519.SH": [
            {"ts_code": "600519.SH", "trade_date": "2026-10-30", "close": 980.0},
        ]})
        import app.services.data_reader as dr_mod
        monkeypatch.setattr(dr_mod.ParquetDataReader, "get_daily",
                            lambda self, ts_codes=None, start_date=None, end_date=None: daily.copy())
        fund = FakeFundService(history={"515450.SH": [
            {"date": "20261030", "close_price": 1.39},
        ]})
        result = WatchlistAlertService(data_dir=str(tmp_path), fund_service=fund).check()
        codes = {a["ts_code"] for a in result["triggered"]}
        assert codes == {"600519.SH", "515450.SH"}
        assert result["checked"] == 2
