"""自选清单服务端持久化：分组 / 备注 / 价格提醒（股票 + ETF/LOF/场外基金）。

此前自选股只存浏览器 localStorage（清缓存即丢，且无法配合定时任务）。
现落盘 data/watchlist/watchlist.json：
    {"items": [{"ts_code", "asset_type", "group", "note",
                "alert_high", "alert_low", "added_at"}]}

asset_type ∈ stock / fund-etf / fund-lof / fund-otc；老数据缺省视为 stock，
零迁移（Issue docs/issues/0001-watchlist-funds.md）。

代码消歧「股票优先」：裸 6 位码先查本地股票池（stock_basic），命中即股票；
未命中再查扶摇基金检索并采用真实 thscode（000001 撞号场景股票赢，
515450 自动纠正为 515450.SH fund-etf）。股票池为空（数据未就绪）时跳过
基金兜底，保持旧行为，防误判。

价格提醒（WatchlistAlertService）挂在每日调度里，盘后确认口径：
股票用 Parquet 最近收盘价；ETF 用扶摇历史日线收盘（前复权）；LOF/场外用
最新单位净值（净值日期随消息带出，不谎称当日）。触发记入
data/watchlist/alerts.json（同票同方向同数据日去重，保留 100 条），
并经 AlertPushService 外推（若已配置）。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set

from loguru import logger

_DIR_NAME = "watchlist"
_ITEMS_FILE = "watchlist.json"
_ALERTS_FILE = "alerts.json"
_MAX_ALERTS = 100

#: 基金资产类型（与扶摇 /api/meta/tickers/search 的 asset_type 对齐）
FUND_ASSET_TYPES = ("fund-etf", "fund-lof", "fund-otc")
_VALID_ASSET_TYPES = ("stock",) + FUND_ASSET_TYPES

_lock = threading.Lock()


def _ymd_to_dash(ymd: Any) -> str:
    """20261030 → 2026-10-30；非 8 位数字原样返回。"""
    text = str(ymd or "").strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return text


class WatchlistService:
    def __init__(
        self,
        data_dir: Optional[str] = None,
        stock_lookup: Optional[Callable[[], Set[str]]] = None,
        fund_lookup: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
    ):
        if data_dir is None:
            data_dir = os.getenv(
                "DATA_DIR",
                os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "data"),
            )
        self.data_dir = data_dir
        self._stock_lookup = stock_lookup
        self._fund_lookup = fund_lookup
        self._stock_pool_cache: Optional[Set[str]] = None

    # ---- 消歧依赖（懒加载；注入版供测试） ----

    def _stock_pool(self) -> Set[str]:
        if self._stock_pool_cache is None:
            if self._stock_lookup is not None:
                try:
                    self._stock_pool_cache = set(self._stock_lookup() or set())
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"读取股票池失败，按空池处理: {exc}")
                    self._stock_pool_cache = set()
            else:
                from app.services.data_reader import ParquetDataReader

                try:
                    df = ParquetDataReader().get_stock_basic()
                    codes = (
                        set(df["ts_code"].dropna().astype(str))
                        if df is not None and not df.empty and "ts_code" in df.columns
                        else set()
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"读取 stock_basic 失败，按空池处理: {exc}")
                    codes = set()
                self._stock_pool_cache = codes
        return self._stock_pool_cache

    def _fund_rows(self, bare: str) -> List[Dict[str, Any]]:
        if self._fund_lookup is not None:
            try:
                return list(self._fund_lookup(bare) or [])
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"基金检索失败({bare}): {exc}")
                return []
        try:
            from app.services.fund_service import get_fund_service

            rows = get_fund_service().search_funds(bare, limit=5)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"基金检索失败({bare}): {exc}")
            return []
        return [
            row for row in rows
            if str(row.get("ticker") or "") == bare
            or str(row.get("thscode") or "").upper().startswith(bare)
        ]

    # ---- 路径 ----

    @property
    def _items_path(self) -> str:
        return os.path.join(self.data_dir, _DIR_NAME, _ITEMS_FILE)

    @property
    def _alerts_path(self) -> str:
        return os.path.join(self.data_dir, _DIR_NAME, _ALERTS_FILE)

    # ------------------------------------------------------------------
    def list_items(self) -> List[Dict[str, Any]]:
        with _lock:
            return self._read_items()

    def _read_items(self) -> List[Dict[str, Any]]:
        try:
            with open(self._items_path, encoding="utf-8") as f:
                items = json.load(f).get("items", [])
        except FileNotFoundError:
            return []
        except Exception as e:
            logger.warning(f"读取自选股失败: {e}")
            return []
        for row in items:
            if not row.get("asset_type"):
                row["asset_type"] = "stock"
        return items

    def _write_items(self, items: List[Dict[str, Any]]) -> None:
        os.makedirs(os.path.dirname(self._items_path), exist_ok=True)
        tmp = self._items_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self._items_path)

    @staticmethod
    def _normalize(code: str) -> Optional[str]:
        """纯字符串清洗（股票规则猜后缀）；不做任何 IO。"""
        text = str(code or "").strip().upper()
        if len(text) == 6 and text.isdigit():
            if text.startswith("6"):
                return f"{text}.SH"
            if text.startswith(("8", "4")):
                return f"{text}.BJ"
            return f"{text}.SZ"
        return text if text else None

    def _resolve_input(
        self, code: str, asset_type: Optional[str] = None
    ) -> "tuple[Optional[str], str]":
        """股票优先消歧，返回 (ts_code, asset_type)。规则见模块 docstring。"""
        text = str(code or "").strip().upper()
        if not text:
            return None, "stock"
        # 前端下拉选中候选：显式基金类型直接信任（picker 总带完整 thscode）
        if asset_type in FUND_ASSET_TYPES:
            return text, asset_type
        if asset_type == "stock":
            return self._normalize(text) or text, "stock"
        # 场外基金 .OF 后缀本身即类型，无需检索
        if text.endswith(".OF"):
            return text, "fund-otc"
        bare = text.split(".")[0]
        is_bare6 = bare.isdigit() and len(bare) == 6
        guessed = self._normalize(bare) if is_bare6 else text
        pool = self._stock_pool()
        if pool:
            if guessed in pool:
                return guessed, "stock"
            if text in pool:
                return text, "stock"
            if is_bare6:
                for row in self._fund_rows(bare):
                    ths = str(row.get("thscode") or "").upper()
                    atype = str(row.get("asset_type") or "")
                    if ths and atype in FUND_ASSET_TYPES:
                        return ths, atype
        return guessed or text, "stock"

    def _sanitize_item(self, raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        code = self._normalize(raw.get("ts_code"))
        if not code:
            return None

        def _num(key: str) -> Optional[float]:
            v = raw.get(key)
            if v is None or v == "":
                return None
            try:
                n = float(v)
            except (TypeError, ValueError):
                return None
            return n if n > 0 else None

        atype = str(raw.get("asset_type") or "stock")
        if atype not in _VALID_ASSET_TYPES:
            atype = "stock"
        return {
            "ts_code": code,
            "asset_type": atype,
            "group": str(raw.get("group") or "").strip() or "默认",
            "note": str(raw.get("note") or "").strip(),
            "alert_high": _num("alert_high"),
            "alert_low": _num("alert_low"),
            "added_at": str(raw.get("added_at")
                            or datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        }

    def replace_items(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """整体替换（PUT 语义）；老 localStorage 迁移也走这里。

        纯字符串清洗，不做消歧（迁移数据本就带完整代码，且不得引入 IO 依赖）。
        """
        clean: List[Dict[str, Any]] = []
        seen = set()
        for raw in items or []:
            item = self._sanitize_item(raw)
            if item and item["ts_code"] not in seen:
                seen.add(item["ts_code"])
                clean.append(item)
        with _lock:
            self._write_items(clean)
        return clean

    def add_item(
        self,
        ts_code: str,
        group: str = "",
        note: str = "",
        asset_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        with _lock:
            items = self._read_items()
            resolved, atype = self._resolve_input(ts_code, asset_type)
            item = self._sanitize_item(
                {"ts_code": resolved, "asset_type": atype, "group": group, "note": note}
            )
            if item is None:
                raise ValueError("未识别的股票代码")
            existing = next((x for x in items if x["ts_code"] == item["ts_code"]), None)
            if existing:
                return existing
            items.append(item)
            self._write_items(items)
            return item

    def update_item(self, ts_code: str, patch: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        with _lock:
            items = self._read_items()
            code = self._normalize(ts_code)
            for i, item in enumerate(items):
                if item["ts_code"] != code:
                    continue
                merged = self._sanitize_item({**item, **(patch or {}), "ts_code": code,
                                              "added_at": item["added_at"]})
                if merged:
                    items[i] = merged
                    self._write_items(items)
                    return merged
            return None

    def remove_item(self, ts_code: str) -> bool:
        with _lock:
            items = self._read_items()
            code = self._normalize(ts_code)
            remaining = [x for x in items if x["ts_code"] != code]
            if len(remaining) == len(items):
                return False
            self._write_items(remaining)
            return True


class WatchlistAlertService:
    """价格提醒检查：最近收盘价/最新净值 vs alert_high / alert_low（盘后口径）。"""

    def __init__(self, data_dir: Optional[str] = None, fund_service: Any = None):
        self.svc = WatchlistService(data_dir)
        self._fund_service = fund_service

    @property
    def _alerts_path(self) -> str:
        return self.svc._alerts_path

    @property
    def fund_service(self) -> Any:
        if self._fund_service is None:
            from app.services.fund_service import get_fund_service

            self._fund_service = get_fund_service()
        return self._fund_service

    def list_alerts(self, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            with open(self._alerts_path, encoding="utf-8") as f:
                return json.load(f).get("alerts", [])[-limit:]
        except Exception:
            return []

    # ---- 价格源：按资产类型分派 ----

    def _stock_closes(self, items: List[Dict[str, Any]]) -> Dict[str, tuple]:
        """股票：Parquet 最近 14 天窗口内最新收盘价（老路径，行为不变）。"""
        from app.services.data_reader import ParquetDataReader

        reader = ParquetDataReader()
        end = datetime.now().strftime("%Y-%m-%d")
        start = (datetime.now() - timedelta(days=14)).strftime("%Y-%m-%d")
        daily = reader.get_daily(ts_codes=[x["ts_code"] for x in items],
                                 start_date=start, end_date=end)
        closes: Dict[str, tuple] = {}
        if daily is None or daily.empty:
            return closes
        d = daily.copy()
        d["trade_date"] = d["trade_date"].astype(str)
        last = d.sort_values("trade_date").groupby("ts_code").tail(1)
        for _, r in last.iterrows():
            closes[r["ts_code"]] = (str(r["trade_date"])[:10], float(r["close"]), "收盘")
        return closes

    def _etf_closes(self, items: List[Dict[str, Any]]) -> Dict[str, tuple]:
        """ETF：扶摇历史日线（前复权）最新收盘；单票失败静默跳过。"""
        closes: Dict[str, tuple] = {}
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=14)).strftime("%Y%m%d")
        for item in items:
            code = item["ts_code"]
            try:
                rows = self.fund_service.get_history(code, start, end).get("items") or []
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"ETF 提醒取历史失败({code}): {exc}")
                continue
            dated = [r for r in rows if _ymd_to_dash(r.get("date"))]
            if not dated:
                continue
            latest = max(dated, key=lambda r: _ymd_to_dash(r.get("date")))
            price = latest.get("close_price", latest.get("close"))
            if price is None:
                continue
            try:
                closes[code] = (_ymd_to_dash(latest.get("date")), float(price), "收盘")
            except (TypeError, ValueError):
                continue
        return closes

    def _nav_closes(self, items: List[Dict[str, Any]]) -> Dict[str, tuple]:
        """LOF/场外：最新单位净值 + 净值日期；缺日期跳过（不谎称当日）。"""
        closes: Dict[str, tuple] = {}
        for item in items:
            code = item["ts_code"]
            try:
                rows = self.fund_service.get_nav_series(code) or []
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"基金提醒取净值失败({code}): {exc}")
                continue
            dated = [r for r in rows if _ymd_to_dash(r.get("nav_date_ymd") or r.get("nav_date"))]
            if not dated:
                continue
            latest = max(dated, key=lambda r: _ymd_to_dash(r.get("nav_date_ymd") or r.get("nav_date")))
            nav = latest.get("unit_nav")
            if nav is None:
                continue
            try:
                closes[code] = (_ymd_to_dash(latest.get("nav_date_ymd") or latest.get("nav_date")),
                                float(nav), "净值")
            except (TypeError, ValueError):
                continue
        return closes

    def check(self) -> Dict[str, Any]:
        """对设了提醒价的自选条目跑一轮检查，返回新触发的提醒。"""
        items = [x for x in self.svc.list_items()
                 if x.get("alert_high") or x.get("alert_low")]
        if not items:
            return {"checked": 0, "triggered": []}

        stocks = [x for x in items if x.get("asset_type", "stock") == "stock"]
        etfs = [x for x in items if x.get("asset_type") == "fund-etf"]
        navs = [x for x in items if x.get("asset_type") in ("fund-lof", "fund-otc")]

        closes: Dict[str, tuple] = {}
        try:
            if stocks:
                closes.update(self._stock_closes(stocks))
        except Exception as e:
            logger.warning(f"价格提醒读取行情失败: {e}")
            return {"checked": len(items), "error": str(e), "triggered": []}
        if etfs:
            closes.update(self._etf_closes(etfs))
        if navs:
            closes.update(self._nav_closes(navs))
        if not closes:
            return {"checked": len(items), "triggered": []}

        with _lock:
            existing = self.list_alerts()
            seen = {(a["ts_code"], a["kind"], a["close_date"]) for a in existing}
            new_alerts = []
            for item in items:
                close_info = closes.get(item["ts_code"])
                if not close_info:
                    continue
                close_date, close, price_label = close_info
                for kind, threshold in (("above", item.get("alert_high")),
                                        ("below", item.get("alert_low"))):
                    if not threshold:
                        continue
                    hit = close >= threshold if kind == "above" else close <= threshold
                    if not hit or (item["ts_code"], kind, close_date) in seen:
                        continue
                    arrow = "≥" if kind == "above" else "≤"
                    new_alerts.append({
                        "ts_code": item["ts_code"],
                        "asset_type": item.get("asset_type", "stock"),
                        "kind": kind,
                        "close": close,
                        "close_date": close_date,
                        "threshold": threshold,
                        "message": f"{item['ts_code']} {price_label} {close} {arrow} 提醒价 {threshold}"
                                   f"（{close_date}）",
                        "triggered_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
            if new_alerts:
                alerts = (existing + new_alerts)[-_MAX_ALERTS:]
                os.makedirs(os.path.dirname(self._alerts_path), exist_ok=True)
                tmp = self._alerts_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump({"alerts": alerts}, f, ensure_ascii=False)
                os.replace(tmp, self._alerts_path)

        if new_alerts:
            self._push(new_alerts)
        return {"checked": len(items), "triggered": new_alerts}

    @staticmethod
    def _push(alerts: List[Dict[str, Any]]) -> None:
        try:
            from app.services.alert_push_service import AlertPushService

            text = "\n".join(f"· {a['message']}" for a in alerts)
            record = {
                "scan_date": datetime.now().strftime("%Y-%m-%d"),
                "alerts": [{"level": "warn", "message": a["message"]} for a in alerts],
            }
            svc = AlertPushService()
            cfg = svc.get_config()
            if cfg["enabled"] and cfg["channels"]:
                results = []
                for ch in cfg["channels"]:
                    ok, message = svc._send(ch, "🔔 自选价格提醒", f"**自选价格提醒**\n{text}", record)
                    results.append({"type": ch["type"], "ok": ok, "message": message})
                svc._append_log("watchlist", results)
        except Exception:
            logger.warning("价格提醒推送失败", exc_info=True)
