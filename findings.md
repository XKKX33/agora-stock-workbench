# 调研与审计发现

## 2026-08-04 一键全流程审计结论

- AI 客户端会在 `base_url` 后追加 `/chat/completions`；当前服务的地址必须保存为 `https://grok.xuan.christmas/v1`。
- `app/services/ai.py` 只读基础配置，而 Agent 与设置页读本地覆盖，必须统一为合并配置。
- 现有盘后链缺少 Agent 和四组实验落库，旧 `picks` 又以信号日收盘为收益基准，不能复用来表达“次日开盘买入”。
- 真实库目前仅 3 个扫描日、18 条选股和 11 条 T+1；没有足够样本证明策略有效。
- 交易日历更新当前位于闸门之后，日历过期时会循环阻塞；一键流程必须先刷新日历再确认最新完整交易日。
- 收益回填需要权威涨跌停价数据；现有库只有日线，无法严谨判断一字涨停不可成交。
- 实验查询不能复用旧台账接口：新接口必须从 `experiment_runs` 与 `experiment_decisions` 联查，且只统计成功批次。DuckDB 的 `COUNT(retN)` 会排除 `NULL`、保留真实 0，适合作为四期样本数的权威口径；无样本时平均收益、胜率和覆盖率均保持空值。

## 当前架构

- 数据链路为 Tushare → DuckDB → 扫描引擎 → FastAPI → 原生 JavaScript 页面。
- 扫描结果已经保存到 `scan_runs`、`scan_rows` 和 `picks`，具备盘后复盘的基础数据。
- 现有 `postmortem.py` 可以回填 T+1、T+3、T+5、T+10 收益。
- 情绪页面原先只展示候选通过率、行业热度和资金确认。2026-08-01 起舆情链路已接入后端，`/api/sentiment` 改为返回真实的 `news_sentiment` 区块（详见下文"跨会话契约变更"）。
- 现有服务进程内扫描任务只支持手动触发，没有交易日收盘后调度和持久化任务状态。

## 约束

- 不创建新的 Python 或 Conda 环境。
- 测试必须使用隔离数据库，禁止修改 `workbench/data/market.duckdb`。
- 不采集需要绕过登录、验证码、付费墙或明确禁止自动访问的数据。
- 不使用静态假数据、无来源结论或静默失败。
- AI 和机器学习能力在没有真实凭据或模型文件时必须明确标记不可用。

## 待确认事实

- 可复用 GitHub 项目的许可证、维护状态和数据源稳定性。
- 初始舆情提供方、调度方式和 AI 兼容接口的最终选择。

## 基线验证

- Conda `base` 使用 Python 3.13.9，现有 API、扫描、复盘和测试依赖齐全。
- 隔离测试 26 项通过、0 项失败；测试没有写入真实数据库。
- 当前扫描和页面已经动态连通，README 与 ARCHITECTURE 的“静态原型”说明已过期。
- 真实数据库有 8 张表、78,741 条日线、260 条扫描明细和 12 条台账记录。

## 2026-07-31 21:15 实测复核（本轮直接验证，非推断）

### 规则文件实际情况

- 项目内**不存在 `AGENTS.md`**（全目录 glob 无结果）。现有规则来源只有 `.learnings/ERRORS.md`，以及 `README.md`、`ARCHITECTURE.md`、`task_plan.md`、`findings.md`、`progress.md` 与 `docs/superpowers/`。任务描述中"项目现有 AGENTS.md"与实际不符，需要用户确认是否新建规则文件。
- 项目内**不存在 `openspec/` 或 `.openspec/` 目录**，`openspec` 命令也不在 PATH 中。OpenSpec propose/archive 流程当前没有落地载体。

### 现有环境（Conda base，可直接复用）

- Python 3.13.9（`C:\Users\xuan\anaconda3\python.exe`）。
- 已安装：`duckdb 1.5.5`、`fastapi 0.136.1`、`uvicorn 0.46.0`、`pandas 2.3.3`、`numpy 2.3.5`、`tushare 1.4.29`、`pytest 8.4.2`、`httpx 0.28.1`、`pydantic 2.12.4`、`requests 2.32.5`、`beautifulsoup4 4.13.5`、`lxml 5.3.0`、`tenacity 9.1.2`、`openai 2.45.0`。
- 舆情与调度所需但**缺失**：`akshare`、`efinance`、`feedparser`、`apscheduler`、`jieba`、`snownlp`、`anthropic`、`pytest-asyncio`。
- `.learnings/ERR-20260731-002`（缺少 duckdb）实际已不成立：`duckdb 1.5.5` 已在 base 环境可用，该条目应关闭。

### 真实数据库实测结构（只读打开，未写入）

`workbench/data/market.duckdb`，8 张表：

| 表 | 行数 | 字段 |
| --- | --- | --- |
| `daily` | 78,741 | ts_code, trade_date, open, high, low, close, pre_close, pct_chg, vol, amount |
| `daily_basic` | 5,528 | ts_code, trade_date, turnover_rate, volume_ratio, total_mv, circ_mv |
| `moneyflow` | 4,194 | ts_code, trade_date, net_mf_amount, buy_lg_amount, sell_lg_amount, buy_elg_amount, sell_elg_amount |
| `picks` | 12 | run_date, as_of, strategy, ts_code, name, industry, rank, total, money_class, one_line, contrib_json, feat_json, ret1, ret3, ret5, ret10 |
| `scan_rows` | 260 | run_id, ts_code, name, industry, rank, total, passed, selected, gate_reasons_json, cat_scores_json, money_class, one_line, contrib_json, feat_json |
| `scan_runs` | 1 | run_id, run_date, as_of, strategy, candidate_count, scored_count, passed_count, final_count, top_industries_json |
| `stock_basic` | 5,534 | ts_code, symbol, name, area, industry, market, list_date |
| `trade_cal` | 401 | exchange, cal_date, is_open |

- 所有字段为 VARCHAR/DOUBLE/INTEGER/BOOLEAN。**所有 8 张表均已声明 PRIMARY KEY**（`engine/db.py:26-121` 的 `_SCHEMA`）：`stock_basic(ts_code)`、`daily(ts_code, trade_date)`、`daily_basic(ts_code, trade_date)`、`moneyflow(ts_code, trade_date)`、`trade_cal(exchange, cal_date)`、`picks(run_date, strategy, ts_code)`、`scan_runs(run_id)`、`scan_rows(run_id, ts_code)`。`Store.upsert()` 已是 DELETE+INSERT 幂等路径。
- `trade_cal(exchange, cal_date, is_open)` 已存在，可直接作为"已确认交易日"判定与市场 T+N 基准，无需新建日历源。
- `picks` 与 `scan_rows` 之间没有 `run_id` 关联字段（`picks` 只有 `run_date`/`as_of`/`strategy`），跨表对齐同一批次目前缺少稳定键。
- 舆情相关表（文档、实体关联、分析、来源引用、AI 报告、复盘结果、任务状态）**全部不存在**，本阶段需新建。
- 存在两个历史备份 `market.duckdb.bak-20260730-193308`、`market.duckdb.bak-20260730-201430`，不得删除。

### 并行会话现状

- 另一会话（Codex，thread `019fb787-c920-7702-9024-4c3d1ff2f98d`）的最新落盘时间为 2026-07-31 18:04，改动集中在 `workbench/ui_mockups/v2/`（六个 HTML、`assets/js/pages/*.js`、`theme.css`）与 `workbench/tests/test_ui_pages.py`，最新为 17:42。
- 结论：**前端目录归属另一会话**，本任务应只新增后端模块与新的 API 端点，前端改动需与其协商后再动，避免互相覆盖。

## 2026-08-01 舆情来源 GitHub 调研（gh CLI 已恢复，本轮一手核验）

调研工具此前被 `WebSearch`/`WebFetch` 不可用阻塞；本轮确认 `gh` CLI 2.88.1 已登录（账户 XKKX33），改用 `gh search` / `gh api` 直取仓库与源码核验，不再依赖网页抓取。

### 候选比较

| 项目 | Stars | License | 维护 | 结论 |
| --- | --- | --- | --- | --- |
| `akfamily/akshare` | 21.7k | **MIT** | 2026-07-29（活跃） | **选定**。财经数据接口库，含个股新闻/快讯接口，走公开数据端点而非自建爬虫。 |
| `Micro-sheep/efinance` | 3.9k | MIT | 2026-07-17 | 备选。以行情/基金为主，新闻能力弱于 akshare。 |
| `wangys96/Bayesian-Stock-Market-Sentiment` | 42 | 无 | 陈旧 | 否。无许可证，是完整 Django 站点而非可复用采集器。 |
| 其余 `股票新闻爬虫` 类小仓库 | <10 | 无 | 陈旧 | 否。均无许可证、自建脆弱爬虫，合规与维护风险高。 |

### 选定 akshare 的理由

- **许可证干净**：MIT，可直接依赖，无 GPL 传染风险。
- **合规路径更清晰**：akshare 封装的是数据方公开接口（东方财富搜索、百度财经等），比逐站自建爬虫更容易在 `compliance_note` 里写清依据。
- **接口契合本项目 `RawNewsItem` 契约**（`engine/news.py:101`）：
  - `stock_news_em(symbol)` 源码 `akshare/news/news_stock.py:15`，端点 `https://search-api-web.eastmoney.com/search/jsonp`，返回列：`关键词 / 新闻标题 / 新闻内容 / 发布时间 / 文章来源 / 新闻链接`。逐列映射到 `RawNewsItem`：标题→`title`、内容→`summary`、发布时间→`published_at`、链接→`url`、文章来源保留进 `raw`。
  - 发布时间为 `YYYY-MM-DD HH:MM:SS`，正好落在 `news_text._TIME_FORMATS` 首个格式，`parse_published_at` 可直接解析，无需自造时间格式。
  - `news/news_baidu.py` 另有经济数据/停复牌/分红快讯，属结构化日历，非个股舆情，本期不接。

### 接入点（已核实，改动面很小）

- 采集链路已完整：`close_pipeline._collect_news_step`（`close_pipeline.py:268`）→ `build_fetchers`（`news_config.py:172`）→ `collect_news`（`news.py:155`）。去重、股票/行业关联、归属交易日、三态缺失、复盘装配全部就绪。
- **接一个新源只需两处**：① 在 `FETCHER_REGISTRY`（`news_config.py:28`）注册一个工厂；② 在 `settings.yaml` 的 `news.sources` 加一条并 `enabled: true`。无需改动 `collect_news` 及其下游。
- akshare **未安装**在 Conda base（`ModuleNotFoundError`）。接入前需 `pip install akshare`，否则 fetcher 工厂在 import 时即失败——按项目约定应显式抛错而非静默跳过。
- `stock_news_em` 是按 `symbol` 查询的个股接口，不接受时间窗；采集窗 `window_start/window_end`（`news.py:120`）需在 fetcher 内部做客户端侧过滤，落在窗口外的条目丢弃，避免把历史旧闻计入当日情绪。

## 必须先修正的基础问题

### 已修复（2026-07-31 22:00）

1. ✅ **异常显式暴露**：`run_scan()` 不再 try-except 吞掉自动复盘异常，失败会直接上抛。
2. ✅ **市场交易日 T+N 口径**：`Store.future_close()` 改为按 `trade_cal` 的市场日历定位第 N 个交易日，不再数该票自己的 K 线。停牌票的 retN 与正常票口径统一，IC/胜率不再被污染。
3. ✅ **读路径不执行 DDL**：`Store.__init__` 新增 `ensure_schema` 参数（默认 True），`MarketRepository` 所有读方法传 `ensure_schema=False`，避免读请求凭空建表。
4. ✅ **picks 业务幂等键**：`Store.record_picks()` 改为按 `(as_of, strategy)` 整组删除+插入，同一交易日重跑不产生重复横截面，retN 重算口径统一。

### 已改代码、尚未运行验证（2026-07-31 23:xx）

> ⚠️ 安全分类器不可用，`execute_bash` / PowerShell 自本轮起全程被拒，以下改动**一次都没有执行过**。
> 分类器恢复后必须先跑 `python workbench/tests/test_postmortem.py` 等测试套件，再把本节结论改为"已修复"。

5. ⏳ **任务状态持久化**：新增 `task_runs` 表（`engine/db.py:_SCHEMA`）与 `claim_task` / `mark_task_running` / `task_heartbeat` / `finish_task` / `get_task` / `recent_tasks`。`ScanManager` 不再用内存 dict，状态全部落库，服务重启后仍可查询；抢占以 `(kind, trade_date, strategy)` 为业务幂等键，跨进程由 DuckDB 单写者文件锁串行化；心跳超时的任务判为僵死并允许抢占重试。
6. ⏳ **扫描批次幂等**：`Store.record_scan()` 改为先按 `(as_of, strategy)` 删除既有批次（先明细后主表）再插入，同一交易日重跑不再累积冗余 `scan_runs` / `scan_rows`。
7. ⏳ **幂等键与真实批次对齐**：抢占时只能用本地 `latest_confirmed_date` 预解析 `trade_date`，在线模式下 Tushare 可能返回更新的交易日，实际写入的 `as_of` 会晚于抢占键。`finish_task(trade_date=...)` 在任务完成时回写真实 `as_of`，否则同 `as_of` 的下一次重跑会因键不匹配而放行，幂等失效。
8. ⏳ **HTTP 语义**：命中已完成扫描不再抛 `status_code=200` 的 `WorkbenchError`（错误处理器会把 200 包进 error 信封）。改为重读任务行、附 `reused=True` 正常返回，`POST /api/scans` 以 200 区别于新排队的 202。
9. ⏳ **启动时补表**：`app/main.py:migrate_schema()` 在 lifespan 启动阶段执行一次 DDL。读路径一律 `ensure_schema=False`，而既有 8 张表的库里没有 `task_runs`，不补表会抛 `CatalogException`。数据库文件不存在时**不建库**，只告警——凭空造空库会把"没采数据"伪装成"有库但全空"。
10. ⏳ **失败不再被 future 吞掉**：`ScanManager._run()` 记录 failed 后 `logger.exception` 并原样 `raise`。

## 跨会话契约变更：`/api/sentiment` 的舆情字段（2026-08-01）

> ⚠️ **需要拥有 `ui_mockups/v2/` 的会话（Codex thread `019fb787-c920-7702-9024-4c3d1ff2f98d`）配合修改。**
> 本会话按分工只动后端，没有编辑 `sentiment.js`，因此该文件当前与新接口不兼容。

**变更原因**：旧字段 `community_sentiment` 是一句写死的"尚未接入新闻、社区和实时舆情数据源"。舆情链路落地后这句话已经是假陈述，违反"不使用静态假数据、不静默降级"的约束，故按真实数据重写。

**旧结构（已删除）**：

```json
"community_sentiment": { "availability": "pending", "reason": "尚未接入……" }
```

**新结构**：键名改为 `news_sentiment`，`availability` 的取值从 `pending` 改为 `available` / `unavailable`，`reason` 拆成 `missing_reason`（机器可判的枚举）与 `detail`（给人看的说明）。

```json
"news_sentiment": {
  "availability": "unavailable",
  "trade_date": "20260731",
  "missing_reason": "no_source_registered",
  "detail": "尚未登记任何舆情来源,采集链路未接入",
  "coverage": { "earliest": null, "latest": null },
  "counts": null
}
```

`availability="available"` 时 `missing_reason` / `detail` 为 `null`，`counts` 为四个整数：

```json
"counts": { "positive": 0, "negative": 0, "neutral": 0, "undecided": 0 }
```

**前端必须改的两行**：`ui_mockups/v2/assets/js/pages/sentiment.js:16-17`

- 第 17 行 `data.community_sentiment.reason` 会抛 `TypeError: Cannot read properties of undefined`，因为该键已不存在。应改读 `data.news_sentiment.detail`。
- 第 16 行把状态标签写死为 `statusTag("待接入", "pending")`。现在状态由后端给出，应按 `data.news_sentiment.availability` 分支渲染。

**渲染时必须区分的三件事**（`missing_reason` 的三个取值，合并显示会把不同事实讲成同一件）：

| `missing_reason` | 含义 | 建议文案 |
| --- | --- | --- |
| `no_source_registered` | 来源都没登记，采集链路未接入 | 未接入 |
| `never_collected` | 来源已登记，但一条都没采过 | 已登记未采集 |
| `no_news_on_date` | 采过，但目标日当天没有条目 | 当日无舆情 |

**`counts` 里 `neutral` 与 `undecided` 也不能合并**：`neutral` 是有依据判出的中性，`undecided` 是判不出来（`sentiment` 为 `null`）。加在一起会把"没结论"显示成"没倾向"。

**同时新增的三个端点**（前端可按需接入，均带 loading/empty/error 三态所需字段）：`GET /api/news`、`GET /api/news/sources`、`GET /api/news/stocks/{ts_code}`、`GET /api/news/industries/{industry}`、`GET /api/reviews`、`GET /api/ai/status`、`POST /api/ai/reviews`。

`docs/superpowers/plans/2026-07-31-quant-workbench-api-ui.md:346` 里断言 `community_sentiment.availability == "pending"` 的那行属于历史计划文档，不再反映实际接口，保留作记录但不要照抄。

### 待修复

- 舆情文档、实体关联、分析结果、来源引用和 AI 报告均没有存储结构。
- 所有页面查询最新全局扫描，尚不能固定 `run_id` 和数据截止时间。
- `picks` 主键仍为 `(run_date, strategy, ts_code)`，与业务幂等键 `(as_of, strategy)` 不一致。当前靠 `record_picks()` 的整组删除保证不重复，但主键本身没有约束力——直插的调用方仍能写出同一 `as_of` 的重复横截面。
- 收盘后调度器（交易日判定 + 定时触发 + 任务链）尚未建立，`task_runs` 目前只被扫描任务使用。

## 2026-08-01 收尾发现（真实复现与修复验证）

### Windows + DuckDB 文件锁竞态（已修复）

- **现象**：同进程并发打开同一 DuckDB 文件偶发 `IOException: WinError 32 文件被占用`（API 层 7 个测试失败同根因，约 0.4% 概率）。触发路径：`run_scan` 后台执行写库期间，GET 查询打开新连接。
- **修法**：`engine/db.py` `Store.__init__` 对 `duckdb.connect` 短重试 3 次（20~50ms 随机退避），连续失败仍上抛。这是健壮性重试，不是吞错——失败次数用尽后异常原样冒泡。
- **验证**：API 层 43+7 → 50 passed；全量 297 passed。

### picks 主键不一致（已修复）

- 旧主键 `(run_date, strategy, ts_code)` 对业务幂等键 `(as_of, strategy)` 没有约束力：直插调用方仍能写出同一横截面的重复行。
- 真实库实测：6 只票各 2 行（as_of=20260730 但 run_date 跨 20260730/20260731），共 12 行。
- 修法：主键改为 `(as_of, strategy, ts_code)`，`_migrate_picks_pk()` 在启动迁移路径自动处理旧库（事务内重建表，`QUALIFY ROW_NUMBER() OVER (PARTITION BY as_of, strategy, ts_code ORDER BY run_date DESC) = 1` 去重保留最新）。临时库副本验证：12 行 → 6 行，无数据丢失。

### 端到端离线演示（临时库副本）

- 盘后链五步结果：ingest_market=skipped（离线）、scan=ok（260→68→6）、backfill_returns=ok（回填 0，pending 缺后续日历 24）、collect_news=ok（fetch 255 条，因发布时间晚于 20260730 截止被前视闸门全部拒绝入库——`rejected` 列表可见，前视纪律生效）、postmortem 完成。
- 幂等：同批次重复触发 HTTP 200 + reused=true，不重复执行。
- 六个 API 全 200：health/overview/sentiment/news/reviews/ai-status 均返回真实状态（舆情 never_collected、复盘 7/8 节可用、AI disabled）。

### 前端契约 Bug（已修复）

- `sentiment.js` 读 `data.community_sentiment.reason`，该键已在后端契约中删除（实际返回 `news_sentiment`），会导致 `TypeError` 页面情绪区块崩溃。已改为按 `news_sentiment.availability` 三态渲染并显示四类计数（positive/negative/neutral/undecided 分开）。
- `foundry.js` 在 `latest_scan` 为 null（无扫描批次）时直接访问 `scan.candidate_count` 会崩溃，已补空态保护。
- `factorlab.js` ML 状态硬编码"待训练"，已改为按接口 `availability` 渲染。

## 2026-08-01 第三阶段发现（UI 升级 + 舆情快照归属）

### 舆情快照归属缺陷（已修复）

- **现象**：TrendRadar 热榜无权威发布时间，`published_at` 记为采集时刻。采集当天（8/1）晚于 `trade_cal` 最后一天（7/31）时，255 条全部被前视闸门拒收，舆情入库为 0，复盘缺舆情节。
- **根因**：`_normalize_one` 对所有来源一视同仁套「发布时间 ≤ 截止」的前视防线。对 `time_basis="first_seen_at_collect"` 的快照条目，发布时间就是采集时刻，未来拒收规则不适用。
- **修法**：`news_text.resolve_snapshot_trade_date()`：采集日在日历 → 当天；不在 → 最近已收盘日；晚于日历末 → 日历最后一天；日历空 → 抛错。`time_decay()` 增加 `allow_future: bool = False`，快照路径传 `True` 并把负天数钳到 0（decay=1）。`news.py` 按 `raw.time_basis` 分流，快照路径不拒收、不猜衰减。非快照来源的 `allow_future=False` 防线原样保留。
- **验证**：253 条入库（9 转载/2 拒收），复盘 8/8 节全齐，`/api/news?trade_date=20260731` available=True。

### 前后端字段不一致（已修复）

- `kline._bars` 缺 `pre_close`/`pct_chg`，前端涨跌渲染拿不到基准 → 已补。
- `chart.js` 读 `boll_up/boll_low`，后端返回 `boll_upper/boll_lower` → 以前端对齐后端为准修正。
- `app/main.py` 页面白名单缺 p6/p7/p8 → 已补，三个新页面 200。

### 测试基线

- 全量 **319 passed / 0 failed**（`--import-mode=importlib --basetemp=.pytest-tmp-all2 -p no:cacheprovider`）。

## 2026-08-11：流程步骤说明链路

- 根因：`OneClickRunner` 只保存业务步骤返回的 `_detail`，但除舆情外的步骤没有提供该字段；失败步骤抛异常后也不会进入已完成步骤列表。
- 修复边界：成功说明在各业务步骤产生，前端不猜业务含义；失败步骤只展示任务错误；后端 `skipped` 保持原语义，前端显示为「未执行」。
- 当时的真实验收受两项外部条件限制：模型凭据未配置、真实数据库写入未获明确授权。

## 2026-08-11：真实模型接口地址与凭据

- 服务根路径是网页入口；OpenAI 兼容 API 位于 `/v1`。`POST /chat/completions` 返回 `404`，而 `POST /v1/chat/completions` 无凭据时进入鉴权并返回 `401`，因此默认 `base_url` 必须包含 `/v1`。
- 此前的 `WORKBENCH_AI_API_KEY` 具有 `sk-` 前缀且无首尾空白，但服务明确返回 `invalid_api_key`，问题不是模型名或请求正文。
- 用户随后提供服务认可的凭据并要求保存在 Git 忽略的 `workbench/.env`；`/v1/models` 确认正式模型 ID 为 `grok-4.5`，一次无重试最小 JSON 请求成功。

## 2026-08-17：软门控真实验收结论

- 整批停止只用于继续执行会污染数据、产生前视或无法计算的情况；单只股票、单次模型输出失败应隔离并记录。
- 150 根历史是单只股票的有效性标准，不是整批候选的通过率标准。排除 `17/150` 根的 `001399.SZ` 后，259 只有效候选可以继续评分。
- 模型输出仍执行严格 JSON 和字段校验；软门控不是降低输出标准，而是把失败范围限制在产生非法输出的候选上。
- `scan_runs.candidate_hash` 指向 259 只可评分候选，`experiment_runs.candidate_hash` 指向冻结的 20 只送审候选，两者对象不同，哈希不同不是数据漂移。
- `to_legacy_output()` 的异常来自重构时误贴代码：批次元数据进入单股字典并引用局部函数外不存在的 `run_date`。恢复原单股指标映射后，专门回归测试和全量测试均通过。
- 软门控只决定“是否继续尝试后续步骤”，不能直接决定任务成功。最终没有完成实验原子提交时必须标记失败，否则页面会把“无结果”显示成“成功”。
- Tushare 确认交易日与正式摄取会各请求一次日线；第二次实际入库行数若少于第一次确认行数，现在会留下完整性警告，但不会在中途拖停其他独立步骤。
