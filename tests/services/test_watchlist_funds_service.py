"""自选基金支持契约：asset_type 存储、股票优先消歧、场外后缀识别。

行为 spec 以本文件为准（Issue docs/issues/0001-watchlist-funds.md）。
fake 注入 stock_lookup / fund_lookup，不碰真实 Parquet 与扶摇。
"""

from __future__ import annotations

import json

import pytest

from app.services.watchlist_service import WatchlistService

pytestmark = pytest.mark.module_trial


STOCK_POOL = {"000001.SZ", "600519.SH", "600000.SH"}


def fake_fund_lookup(code: str):
    table = {
        "515450": [{"thscode": "515450.SH", "asset_type": "fund-etf", "name": "红利低波50ETF"}],
        "161725": [{"thscode": "161725.SZ", "asset_type": "fund-lof", "name": "白酒基金LOF"}],
        "005827": [{"thscode": "005827.OF", "asset_type": "fund-otc", "name": "蓝筹精选"}],
    }
    return table.get(code, [])


@pytest.fixture()
def svc(tmp_path):
    return WatchlistService(
        data_dir=str(tmp_path),
        stock_lookup=lambda: set(STOCK_POOL),
        fund_lookup=fake_fund_lookup,
    )


@pytest.fixture()
def empty_pool_svc(tmp_path):
    return WatchlistService(
        data_dir=str(tmp_path),
        stock_lookup=lambda: set(),
        fund_lookup=fake_fund_lookup,
    )


class TestAssetTypeStorage:
    def test_new_stock_item_gets_stock_asset_type(self, svc):
        item = svc.add_item("600519.SH")
        assert item["asset_type"] == "stock"

    def test_legacy_items_without_asset_type_read_as_stock(self, tmp_path):
        # 老数据零迁移：文件里没有 asset_type，读取时补 stock
        path = tmp_path / "watchlist"
        path.mkdir(parents=True)
        (path / "watchlist.json").write_text(json.dumps({"items": [
            {"ts_code": "600000.SH", "group": "默认", "note": "", "alert_high": None,
             "alert_low": None, "added_at": "2026-10-29 18:12:43"},
        ]}), encoding="utf-8")
        svc = WatchlistService(data_dir=str(tmp_path))
        items = svc.list_items()
        assert items[0]["asset_type"] == "stock"

    def test_of_suffix_recognized_as_fund_otc(self, svc):
        item = svc.add_item("005827.OF")
        assert item["ts_code"] == "005827.OF"
        assert item["asset_type"] == "fund-otc"

    def test_explicit_asset_type_from_picker_is_trusted(self, svc):
        # 前端下拉选中候选时直接带类型，跳过消歧
        item = svc.add_item("515450.SH", asset_type="fund-etf")
        assert item["asset_type"] == "fund-etf"


class TestStockFirstResolution:
    def test_bare_code_prefers_stock_on_collision(self, svc):
        # 000001 既是平安银行也是华夏成长(000001.OF)：股票优先
        item = svc.add_item("000001")
        assert item["ts_code"] == "000001.SZ"
        assert item["asset_type"] == "stock"

    def test_bare_etf_code_resolves_to_fund_with_real_suffix(self, svc):
        # 515450 不在股票池 → 基金检索命中上交所 ETF，纠正后缀
        item = svc.add_item("515450")
        assert item["ts_code"] == "515450.SH"
        assert item["asset_type"] == "fund-etf"

    def test_wrong_suffix_corrected_by_fund_lookup(self, svc):
        # 用户手滑输 515450.SZ（旧 bug 现场）：股票池没有 → 基金命中 → 修正为 .SH
        item = svc.add_item("515450.SZ")
        assert item["ts_code"] == "515450.SH"
        assert item["asset_type"] == "fund-etf"

    def test_lof_bare_code_resolves(self, svc):
        item = svc.add_item("161725")
        assert item["ts_code"] == "161725.SZ"
        assert item["asset_type"] == "fund-lof"

    def test_unknown_code_keeps_legacy_behavior(self, svc):
        # 股票池和基金检索都没有：按旧规则原样保存为 stock
        item = svc.add_item("999999")
        assert item["ts_code"] == "999999.SZ"
        assert item["asset_type"] == "stock"

    def test_empty_stock_pool_skips_fund_fallback(self, empty_pool_svc):
        # 安全阀：数据未就绪时不做基金识别，避免把股票误判成基金
        item = empty_pool_svc.add_item("515450")
        assert item["ts_code"] == "515450.SZ"
        assert item["asset_type"] == "stock"


class TestFundItemLifecycle:
    def test_update_and_remove_work_for_funds(self, svc):
        svc.add_item("515450.SH", asset_type="fund-etf")
        updated = svc.update_item("515450.SH", {"note": "红利策略", "alert_low": 1.40})
        assert updated["note"] == "红利策略"
        assert updated["alert_low"] == 1.40
        assert updated["asset_type"] == "fund-etf"  # 更新不动类型
        assert svc.remove_item("515450.SH") is True
        assert svc.list_items() == []

    def test_dedup_across_resolved_code(self, svc):
        svc.add_item("515450")  # 消歧后为 515450.SH fund-etf
        again = svc.add_item("515450.SH", asset_type="fund-etf")
        assert len(svc.list_items()) == 1
        assert again["ts_code"] == "515450.SH"

    def test_replace_items_does_not_resolve(self, svc):
        # PUT 整体替换（localStorage 迁移语义）：纯字符串清洗，不查池
        items = svc.replace_items([{"ts_code": "515450.SZ"}])
        assert items[0]["ts_code"] == "515450.SZ"
        assert items[0]["asset_type"] == "stock"
