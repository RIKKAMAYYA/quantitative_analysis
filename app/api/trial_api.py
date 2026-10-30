"""试用功能 JSON API — 与对应 Flask 页面共用 trial_analytics / heatmap_service 的计算结果。"""
from flask import jsonify, request
from loguru import logger

from app.api import api_bp
from app.services.board_market_service import get_board_market_service
from app.services.fund_service import get_fund_service
from app.services.heatmap_service import HeatmapService
from app.services.market_snapshot_service import get_market_snapshot_service
from app.services.trial_analytics import (
    financial_health_payload,
    market_brief_payload,
    moneyflow_payload,
    stock_panorama_payload,
    stock_radar_payload,
)


def _parse_ts_codes(raw):
    codes = [code.strip().upper() for code in (raw or '').split(',') if code.strip()]
    seen = set()
    return [code for code in codes if not (code in seen or seen.add(code))]


def _ok(data):
    return jsonify({'code': 200, 'message': '成功', 'data': data})


def _error(label: str, exc: Exception, status: int = 500):
    logger.error(f"{label}API错误: {exc}")
    return jsonify({'code': status, 'message': str(exc), 'data': None}), status


@api_bp.route('/trial/market-brief', methods=['GET'])
def api_market_brief():
    try:
        return _ok(market_brief_payload())
    except Exception as e:
        return _error('每日市场简报', e)


@api_bp.route('/trial/financial-health', methods=['GET'])
def api_financial_health():
    try:
        return _ok(financial_health_payload())
    except Exception as e:
        return _error('财务健康度', e)


@api_bp.route('/trial/financial-anomalies', methods=['GET'])
def api_financial_anomalies():
    try:
        from app.services.financial_quality_service import FinancialQualityService

        return _ok(FinancialQualityService().anomaly_scan())
    except Exception as e:
        return _error('财务异动扫描', e)


@api_bp.route('/trial/daily-alerts', methods=['GET'])
def api_daily_alerts():
    """每日预警扫描记录（?refresh=1 立即扫描一次）。"""
    try:
        from app.services.daily_alert_service import DailyAlertService

        limit = request.args.get('limit', type=int) or 30
        if request.args.get('refresh') == '1':
            DailyAlertService().run_scan(force=False)
        return _ok({'records': DailyAlertService().list_records(limit=limit)})
    except Exception as e:
        return _error('每日预警记录', e)


@api_bp.route('/trial/daily-alerts/push-config', methods=['GET'])
def api_alert_push_config_get():
    """预警推送配置（渠道列表 + 启用状态 + 最低推送级别）。"""
    try:
        from app.services.alert_push_service import AlertPushService

        return _ok(AlertPushService().get_config())
    except Exception as e:
        return _error('读取推送配置', e)


@api_bp.route('/trial/daily-alerts/push-config', methods=['PUT'])
def api_alert_push_config_put():
    """更新预警推送配置。body: {enabled, min_level, channels: [{type, url|send_key}]}"""
    try:
        from app.services.alert_push_service import AlertPushService

        payload = request.get_json(silent=True) or {}
        return _ok(AlertPushService().update_config(payload))
    except Exception as e:
        return _error('保存推送配置', e)


@api_bp.route('/trial/daily-alerts/push-test', methods=['POST'])
def api_alert_push_test():
    """向已配置渠道发送测试推送。"""
    try:
        from app.services.alert_push_service import AlertPushService

        return _ok(AlertPushService().send_test())
    except Exception as e:
        return _error('推送测试', e)


@api_bp.route('/trial/daily-alerts/push-log', methods=['GET'])
def api_alert_push_log():
    """推送投递日志（最近 N 条）。"""
    try:
        from app.services.alert_push_service import AlertPushService

        limit = request.args.get('limit', type=int) or 20
        return _ok({'records': AlertPushService().list_log(limit=limit)})
    except Exception as e:
        return _error('推送日志', e)


# ================= 自选股（服务端持久化） =================

@api_bp.route('/trial/watchlist', methods=['GET'])
def api_watchlist_get():
    """自选股清单（服务端持久化，含分组/备注/价格提醒）。"""
    try:
        from app.services.watchlist_service import WatchlistService

        return _ok({'items': WatchlistService().list_items()})
    except Exception as e:
        return _error('读取自选股', e)


@api_bp.route('/trial/watchlist', methods=['PUT'])
def api_watchlist_put():
    """整体替换自选股清单（localStorage 迁移/批量编辑用）。"""
    try:
        from app.services.watchlist_service import WatchlistService

        payload = request.get_json(silent=True) or {}
        return _ok({'items': WatchlistService().replace_items(payload.get('items') or [])})
    except Exception as e:
        return _error('保存自选股', e)


@api_bp.route('/trial/watchlist', methods=['POST'])
def api_watchlist_add():
    """添加自选标的。body: {ts_code, group?, note?, asset_type?}

    asset_type 缺省时服务端做「股票优先」消歧（裸码猜错后缀会被纠正）。
    """
    try:
        from app.services.watchlist_service import WatchlistService

        payload = request.get_json(silent=True) or {}
        item = WatchlistService().add_item(
            payload.get('ts_code', ''), payload.get('group') or '', payload.get('note') or '',
            asset_type=payload.get('asset_type'))
        return _ok({'item': item})
    except ValueError as e:
        return jsonify({'code': 400, 'message': str(e), 'data': None}), 400
    except Exception as e:
        return _error('添加自选标的', e)


@api_bp.route('/trial/watchlist/search', methods=['GET'])
def api_watchlist_search():
    """自选添加联想：A 股 + 基金（ETF/LOF/场外）合并候选，带 asset_type。

    只服务自选页；/api/market/ticker-search（选股/回测共用）保持纯股票，
    避免回测标的选择器混入无 Parquet 数据的基金（Issue 0001 决策4）。
    """
    query = (request.args.get('q') or '').strip()
    if len(query) < 2:
        return jsonify({'code': 400, 'message': 'q 参数至少 2 个字符', 'data': None}), 400
    try:
        limit = min(20, max(1, int(request.args.get('limit', 10))))
    except ValueError:
        return jsonify({'code': 400, 'message': 'limit 须为整数', 'data': None}), 400

    items = []
    try:
        stock_payload = get_board_market_service().search_tickers(query, limit=limit)
        for row in stock_payload.get('items') or []:
            items.append({**row, 'asset_type': 'stock'})
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"自选联想-股票侧失败: {exc}")
    try:
        for row in get_fund_service().search_funds(query, limit=limit):
            items.append({
                'ts_code': row.get('thscode'),
                'ticker': row.get('ticker'),
                'name': row.get('name'),
                'exchange': row.get('exchange'),
                'asset_type': row.get('asset_type'),
            })
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"自选联想-基金侧失败: {exc}")
    items = items[:limit]
    return _ok({'query': query, 'items': items})


@api_bp.route('/trial/watchlist/quotes', methods=['GET'])
def api_watchlist_quotes():
    """自选行情混合源：股票走扶摇快照，ETF/LOF 走基金实时价，场外用最新净值。

    取不到价的代码从结果省略（不造假），前端按缺省渲染 '--'。
    """
    codes = _parse_ts_codes(request.args.get('codes'))
    if not codes:
        return jsonify({'code': 400, 'message': '缺少 codes 参数（逗号分隔）', 'data': None}), 400

    def _looks_like_fund(code: str) -> bool:
        upper = code.upper()
        if upper.endswith('.OF'):
            return True
        # 沪市 5 开头为基金区间（股票是 60/68）；深市 15/16/18 为基金区间
        if upper.endswith('.SH') and upper.startswith('5'):
            return True
        if upper.endswith('.SZ') and upper[:2] in ('15', '16', '18'):
            return True
        return False

    fund_codes = [c for c in codes if _looks_like_fund(c)]
    stock_codes = [c for c in codes if c not in fund_codes]

    quotes = {}
    if stock_codes:
        try:
            stock_quotes = get_market_snapshot_service().get_quotes(stock_codes)
            for code, row in stock_quotes.items():
                quotes[code] = {**row, 'asset_type': row.get('asset_type') or 'stock'}
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"自选行情-股票快照失败: {exc}")

    fund_service = get_fund_service()

    def _fund_name(code: str):
        # 基金快照不返回名称；profile 服务层缓存 600s，逐只补全可接受
        try:
            return (fund_service.get_profile(code) or {}).get('fund_name')
        except Exception:  # noqa: BLE001
            return None

    for code in fund_codes:
        bare_type = 'fund-otc' if code.upper().endswith('.OF') else None
        try:
            if bare_type == 'fund-otc':
                rows = fund_service.get_nav_series(code) or []
                dated = [r for r in rows if r.get('nav_date_ymd')]
                if not dated:
                    continue
                latest = max(dated, key=lambda r: str(r.get('nav_date_ymd')))
                quotes[code] = {
                    'ts_code': code,
                    'asset_type': 'fund-otc',
                    'name': _fund_name(code),
                    'unit_nav': latest.get('unit_nav'),
                    'nav_date': _ymd_dash(latest.get('nav_date_ymd')),
                }
                continue
            snap = fund_service.get_snapshot(code)
            if snap:
                quotes[code] = {
                    'ts_code': code,
                    'asset_type': bare_type or ('fund-etf' if code.upper().startswith('5')
                                                else 'fund-lof'),
                    'name': snap.get('fund_name') or snap.get('name') or _fund_name(code),
                    'last_price': snap.get('last_price'),
                    'prev_close': snap.get('prev_price'),
                    'pct_chg': snap.get('price_change_ratio_pct'),
                    'open': snap.get('open_price'),
                    'high': snap.get('high_price'),
                    'low': snap.get('low_price'),
                    'volume': snap.get('volume'),
                    'turnover': snap.get('turnover'),
                }
                continue
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"自选行情-基金失败({code}): {exc}")
        # 场内快照失败（如 LOF 无行情）→ 净值兜底
        try:
            rows = fund_service.get_nav_series(code) or []
            dated = [r for r in rows if r.get('nav_date_ymd')]
            if dated:
                latest = max(dated, key=lambda r: str(r.get('nav_date_ymd')))
                quotes[code] = {
                    'ts_code': code,
                    'asset_type': bare_type or 'fund-lof',
                    'name': _fund_name(code),
                    'unit_nav': latest.get('unit_nav'),
                    'nav_date': _ymd_dash(latest.get('nav_date_ymd')),
                }
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"自选行情-基金净值兜底失败({code}): {exc}")

    return _ok({'quotes': quotes, 'updated_at': None})


def _ymd_dash(ymd):
    text = str(ymd or '')
    if len(text) == 8 and text.isdigit():
        return f'{text[:4]}-{text[4:6]}-{text[6:8]}'
    return text or None


@api_bp.route('/trial/watchlist/item', methods=['PUT'])
def api_watchlist_update():
    """更新单个自选股字段（分组/备注/提醒价）。body: {ts_code, ...patch}"""
    try:
        from app.services.watchlist_service import WatchlistService

        payload = request.get_json(silent=True) or {}
        item = WatchlistService().update_item(payload.get('ts_code', ''),
                                              {k: v for k, v in payload.items() if k != 'ts_code'})
        if item is None:
            return jsonify({'code': 404, 'message': '自选股不存在', 'data': None}), 404
        return _ok({'item': item})
    except Exception as e:
        return _error('更新自选股', e)


@api_bp.route('/trial/watchlist', methods=['DELETE'])
def api_watchlist_delete():
    """删除自选股（?ts_code=）。"""
    try:
        from app.services.watchlist_service import WatchlistService

        removed = WatchlistService().remove_item(request.args.get('ts_code', ''))
        return _ok({'removed': removed})
    except Exception as e:
        return _error('删除自选股', e)


@api_bp.route('/trial/watchlist/alerts', methods=['GET'])
def api_watchlist_alerts():
    """价格提醒触发记录。"""
    try:
        from app.services.watchlist_service import WatchlistAlertService

        return _ok({'alerts': WatchlistAlertService().list_alerts()})
    except Exception as e:
        return _error('价格提醒记录', e)


@api_bp.route('/trial/watchlist/alerts/check', methods=['POST'])
def api_watchlist_alerts_check():
    """立即跑一轮价格提醒检查。"""
    try:
        from app.services.watchlist_service import WatchlistAlertService

        return _ok(WatchlistAlertService().check())
    except Exception as e:
        return _error('价格提醒检查', e)


@api_bp.route('/trial/industry-rotation', methods=['GET'])
def api_industry_rotation():
    """行业轮动评分：动量 + 主力资金 + 估值分位三维截面 z-score。"""
    try:
        from app.services.industry_rotation_service import IndustryRotationService

        months = request.args.get('months', type=int) or 12
        force = request.args.get('refresh') == '1'
        return _ok(IndustryRotationService().run(months=months, force_refresh=force))
    except Exception as e:
        return _error('行业轮动评分', e)


@api_bp.route('/trial/financial-anomaly-backtest', methods=['GET'])
def api_financial_anomaly_backtest():
    """财务异动名单按披露日的前向收益（异动组 vs 全市场等权）。"""
    try:
        from app.services.signal_backtest_service import AnomalyForwardService

        force = request.args.get('refresh') == '1'
        return _ok(AnomalyForwardService().run(force_refresh=force))
    except Exception as e:
        return _error('财务异动前向收益', e)


@api_bp.route('/trial/moneyflow', methods=['GET'])
def api_moneyflow():
    try:
        return _ok(moneyflow_payload())
    except Exception as e:
        return _error('资金流统计', e)


@api_bp.route('/trial/stock-radar', methods=['GET'])
def api_stock_radar():
    try:
        ts_codes = _parse_ts_codes(request.args.get('ts_codes', ''))
        return _ok(stock_radar_payload(ts_codes))
    except Exception as e:
        return _error('个股对比雷达', e)


@api_bp.route('/trial/stock-panorama', methods=['GET'])
def api_stock_panorama():
    try:
        ts_code = request.args.get('ts_code', '').strip().upper()
        if not ts_code:
            return jsonify({'code': 400, 'message': '请提供股票代码 ts_code', 'data': None}), 400
        return _ok(stock_panorama_payload(ts_code))
    except Exception as e:
        return _error('个股全景', e)


@api_bp.route('/trial/heatmap', methods=['GET'])
def api_heatmap():
    try:
        sectors, stocks = HeatmapService().get_heatmap_data()
        return _ok({
            'sectors': sectors,
            'stocks': stocks,
            'trade_date': sectors[0]['trade_date'] if sectors else '',
        })
    except Exception as e:
        return _error('板块热力图', e)
