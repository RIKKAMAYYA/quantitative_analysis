# Issue 0001：自选模块不支持 ETF / LOF / 场外基金

- 日期：2026-10-30
- 状态：实现中（本 issue 即改动依据，行为契约以测试用例为准）
- 模块：module_trial（自选）+ 基金数据链路（扶摇）

## 现象（复现步骤）

1. 打开自选行情页 `/market/watchlist`，搜索框输入 `515450`（红利低波50ETF）
2. 下拉联想显示「没有匹配的 A 股标的」；直接回车提交则被存成 `515450.SZ`（猜错交易所）
3. 列表中该行价格列显示 `--`，价格提醒形同虚设

## 根因

三处均为「股票中心制」的既有设计，非缺陷代码：

1. `app/services/watchlist_service.py` `_normalize()`：6 位纯数字按股票规则猜后缀
   （`6→SH`、`8/4→BJ`、其余 `→SZ`），无法表达基金；
2. `app/api/market_api.py` `/ticker-search`：扶摇检索硬编码 `asset_type="a-share"`
   （`board_market_service.search_tickers`），基金天然搜不到；
3. 自选行情取数 `/api/market/snapshot` 与价格提醒 `WatchlistAlertService.check()`
   分别只读扶摇股票快照与 Parquet `daily_history`，两者都不含基金数据。

## 影响面（实测证据）

- `data/stock_basic.parquet` 前缀分布 60/00/30/68/92，**零只 ETF**；
  `daily_history` 中 `515450` 命中 0 行（2026-10-29 实测）。
- 基金数据能力分档（扶摇实测）：

  | 品种 | 实时价 | 日线 | 净值 |
  |---|---|---|---|
  | fund-etf | ✅ | ✅（前复权，窗口≤5年） | ✅ |
  | fund-lof | ✅ | ❌ | ✅ |
  | fund-otc | ❌（3004） | ❌ | ✅（T+1 公布） |

- 代码撞号：`000001` 同时是平安银行（000001.SZ）与华夏成长（000001.OF）。
- 现有消费方回归风险：`/api/market/ticker-search` 被 StockPicker 与
  BacktestPage 共用，回测引擎读 Parquet（无基金数据），给它混入基金候选
  会产生「选了就是死单」的标的——故本改动不动该端点。

## 修复方案（决策记录）

1. 范围：显示价格 + 价格提醒；ETF/LOF/场外全支持。
2. 存储：item 新增 `asset_type`（stock/fund-etf/fund-lof/fund-otc）；
   缺省（老数据）视为 stock，零迁移。
3. 消歧：股票优先——裸码先查本地股票池，命中即 stock；未命中查扶摇基金
   检索，命中存真实 thscode；股票池为空时跳过基金兜底（安全阀）。
4. 搜索：新增自选专用端点 `/api/trial/watchlist/search`（合并 a-share +
   fund-*，带类型标签）；原 `/api/market/ticker-search` 不动。
5. 行情：新增 `/api/trial/watchlist/quotes`（股票走现有快照，基金走
   FundService；场外返回最新净值 + 净值日期）。
6. 提醒：盘后确认口径——ETF 用日线收盘、LOF/场外用最新净值，18:30 调度
   统一触发，去重键沿用 (ts_code, kind, 数据日期)；文案区分「收盘/净值」。
   盘中越线只做前端行内高亮，不产生正式告警。

## 验收合约（测试即文档）

- `tests/services/test_watchlist_funds_service.py`（module_trial）
- `tests/services/test_watchlist_fund_alerts.py`（module_trial）
- `tests/api/test_watchlist_funds_api.py`（module_trial）
- 回归底线：`tests/services/test_watchlist_service.py` 既有用例一字不改必须全绿。
