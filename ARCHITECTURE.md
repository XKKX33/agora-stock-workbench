# 架构说明

## 分层

```text
engine/     领域逻辑与数据访问,不依赖 FastAPI
app/        接口层:repositories → services → api
ui_mockups/v2/   页面(index + p1..p13,共十四个,原生 JS,动态取数)
tests/      隔离数据库测试
vendor/     外部 GPL 源码(TrendRadar),不入本仓库版本历史
```

依赖方向单向：`api → services → repositories → engine`。`engine` 不反向依赖 `app`。

## 模块职责

### engine（领域层）

- `config.py`：读取 `settings.yaml` 与策略配置，路径统一相对 workbench 根解析；`load_workspace_env()` 用 `python-dotenv` 加载根目录 `.env`，不覆盖显式进程环境变量。
- `visibility.py`：防前视日期闸门，计算“此刻允许看到的最新交易日”；只读 `trade_cal`，不写库、不联网，窗口算不出来就报明确原因，绝不回退成最新日。
- `db.py` / `schema.py` / `db_news.py` / `db_agents.py` / `db_experiments.py`：DuckDB 存储层按表族拆分。`schema.py` 只放建表 DDL；`db_news.py` 管理舆情与 Agent 运行记录；`db_agents.py` 管理 `agent_events` 公开结构化事件及按序号续读；`db_experiments.py` 管理实验台账与独立 `experiment_returns`（台账只存决策，成交与收益只存 `experiment_returns`，每个 horizon 一行、可单独重试）；`db.py` 留连接管理、行情与台账。所有 `Store` 方法是唯一的数据访问出口。
- `ingest_tushare.py`：更新行情、交易日历与资金流。
- `universe.py`：硬过滤、行业热度、候选召回。
- `factors/`：结构、趋势、MACD、成交量、题材、资金六类因子。
- `score.py`：归一化、综合打分、门槛过滤、行业去重。
- `run_scan.py`：编排扫描并写入选股台账。`as_of` 由调用方传入（应用层的 `ScanManager`、一键流程、命令行都先算可见日）；不传时只取「本地/在线最新交易日」这一纯数据口径，不含可见性判断。命令行 `_cli()` 默认取可见日，`--trade-date` 必须 <= 可见日，否则直接退出。
- `postmortem.py`：按市场交易日口径回填旧台账的 T+N 字段并做 IC 自检。`backfill_returns(..., visible_max=)` 与 `returns.py` 同一口径：目标交易日比可见日更新时记 `future_not_visible` 并且**不读那天的行情**（库里通常已摄取到最新，读了就是拿当时看不到的价格做标签）；`visible_max=None` 只用于纯历史离线补全。命令行 `_cli()` 固定取可见日。
- `returns.py`：独立收益域，按 `t1_close`、`t2_open`…`t10_open` 计算并持久化实验收益；缺数据保留状态与原因。
- `backtest.py`：滚动回测。`run_backtest()` 返回四段结果（指标/假设/覆盖率/逐期明细），默认 `non_overlap` 非重叠调仓防收益虚高；回测只消费旧 `ret1/ret3/ret5/ret10` 兼容字段。
- `ml/`：因子机器学习体检（`dataset` 数据装配、`labels` 标签、`splits` 时序切分、`metrics` IC/AUC/分桶、`model` 训练与降级、`registry` 产物登记、`train` 编排），模型缺失/未达标时如实报诊断、不给预测。`build_dataset(end=)` 与 `train_from_store(end=)` 是采样截止日；不传就取库里最新交易日的**纯数据口径**，防前视由入口 `tools/train_ml.py` 先算可见日再传入，产物里记 `dataset.end_day`（这份模型看到哪天为止）。
- `schedule.py`：交易日判定与「是否该跑」的纯决策函数，不含副作用。
- `close_pipeline.py`：由应用层 `OneClickRunner` 实现的九步收盘工作流的领域操作，另含手动跑一次五步闭环的 `run_close_pipeline()`。`trade_date` 就是扫描截面，原样传给 `run_scan(as_of=)`——不传 run_scan 会自取「最新交易日」，那正是隐藏窗口里的日期；回填步同步带上 `visible_max`。
- `one_click.py`：收盘九步工作流编排，`calendar` 定定 `as_of` 并拒绝隐藏日期，`market_data` 继续摄取最新交易日以推动窗口前移，`backfill_returns` 负责把收益回填限定在可见日及更早数据。
- `news_config.py` / `news_text.py` / `news.py`：舆情来源配置、文本处理、采集与入库。`news_text.py` 的 `resolve_snapshot_trade_date()` 负责把无权威发布时间的快照条目归属到交易日（见「舆情快照归属」）。
- `news_trendradar.py`：TrendRadar 热榜采集适配器（我们的代码），实现 `NewsFetcher` 协议并注册进 `FETCHER_REGISTRY["trendradar"]`。仅按单文件路径 `importlib` 加载 vendor 的 `DataFetcher`，不 `import trendradar`。
- `review.py`：装配带三级标注的复盘结果。`backfill=True` 时把 `visible_max` 透传给 `run_postmortem`。命令行 `_cli()` 的 `--trade-date` 必须 <= 可见日，与接口层同一条纪律。
- `ai.py`：AI 叙述接口。`NARRATOR_REGISTRY` 已注册 `openai_compatible`（OpenAI 兼容 `/chat/completions`）；base URL 配置为 `https://grok.xuan.christmas/v1`，最终请求 `/v1/chat/completions`，模型保持配置可编辑；缺凭据时三态明确，不编造。
- `agents.py`：多 agent 短线研判编排，公开消息按固定角色顺序交接；隐藏思维链不落库、不下发。

### app（接口层）

- `repositories/market.py`：唯一的数据读取入口，读路径一律 `ensure_schema=False`。
- `services/`：业务装配与可用性判定（`overview` `stocks` `analytics` `news` `reviews` `ai` `scans` `pipelines` `scheduler` `tasks` `kline` `screener` `backtest` `watchlist` `agents` `returns`）。
- `services/agents.py` / `services/agents_data.py`：多 agent 研判管理器，负责任务、事件发布、历史读取与 SSE 订阅。
- `services/returns.py`：面向接口的收益服务，内部解析可见窗口并把 `visible_max` 传给领域层。
- `services/pipelines.py`：提交闸门与补齐协调器，负责 `one_click_backfill` 的串行补齐、逐日失败记录、任务历史与进度暴露。
- `services/one_click.py`：九步业务操作与固定顺序编排；逐步骤隔离异常，失败记警告、缺依赖则跳过，规则扫描成功后把当前可用实验组与任务终态原子提交。
- `api/`：路由与参数校验，不含业务逻辑。Agent 事件提供 `/api/agents/jobs/{job_id}/events` 历史读取和 `/api/agents/jobs/{job_id}/stream` SSE 断点续接；收益提供 `/api/returns/calculate`、`/api/returns`、`/api/returns/summary`。
- `errors.py`：`WorkbenchError` 统一错误信封。
- `main.py`：应用装配、启动时表结构迁移、调度线程生命周期、十四页面白名单托管（`index.html` + `p1..p13`），`AgentJudgeManager` 生命周期。

## 调用关系

```text
Tushare ──→ ingest_tushare.py ──┐
舆情来源 ──→ news.py ───────────┤
                                ↓
                          DuckDB ← db.py
                                ↓
              universe.py → factors/ → score.py
                                ↓
                    run_scan.py → picks 台账
                                ↓
                        postmortem.py（旧台账 T+N 回填）
                                ↓
                        returns.py（experiment_returns）
                                ↓
                          review.py（三级标注装配）
                                ↓
        app/repositories → app/services → app/api
                                ↓
                   p11 启动/配置 → p13 实时/历史报告

agents.py：候选池/自选 → 行情+技术指标+资金流+舆情快照 → 粗筛 → 三分析师公开消息 → 多方/空方/反驳/风控 → agent_runs/agent_judgments + agent_events → `/api/agents/*` → p13
OneClickRunner 串联：`preflight` → `calendar` → `market_data` → `backfill_returns` → `integrity` → `scan` → `collect_news` → `agents` → `persist_experiment`。每步异常转成带原因的 `warning`，只跳过依赖失败步骤；舆情、收益回填和智能体失败不阻断规则扫描。哈希或事务校验失败的数据仍不提交，但任务结果会保留警告而不是让整个编排器退出。

流程页 p3 只按后端步骤契约渲染：成功步骤展示 `detail + data`，`warning` 展示具体原因，`skipped` 映射为「未执行」；任务历史可展开指定批次，可用实验组按信号日和组名分页读取。

`OneClickRunner` 的 `calendar`、`market_data`、`backfill_returns`、`agents`、`persist_experiment` 共用同一条可见性边界：`calendar` 只接受可见的 `as_of`，`market_data` 负责把最新交易日持续摄入，`backfill_returns` 只回填可见日及更早的数据，`agents` / `reviews` 读取时默认跟随同一闸门。
```

## 关键设计决定

### 数据可信度

- 缺数据的小节返回 `available: False` 加 `missing_reason`，不补零、不省略。Agent 事件按 `seq` 单调递增，SSE 首先重放 `after_seq` 之后的持久事件，再监听实时发布；重连沿用最后序号。
- `experiment_returns` 每个 horizon 独立记录 `status`、`reason`、价格和日期。`future_not_reached`、`entry_bar_missing`、`limit_up_locked`、`target_bar_missing` 等不可用状态不写真实 `0`。组合汇总 `portfolio_gross_return` 只在该期限全部槽位都可测时给值,否则为 `null`——把买不到的槽位当成现金 0 收益会让组合看起来比实际稳。
- 实验成交与收益只有一份口径：`experiment_decisions` 只存决策本身（11 列，无 `entry_*` / `ret*`），成交价与各期收益一律存 `experiment_returns`。`entry_price` 只在真的买到时才有值——涨停封板、缺涨跌停价、当日无行情都留空，`status` 说明原因。行级成交状态由收益明细推导（`db_experiments.classify_entry_status`）：有成交价 `filled`，判定买不到 `entry_unavailable`，算过但还没定 `pending_entry`，从没算过收益返回 `None`（「没算」和「买不到」不合并）。筛选侧 `entry_status_predicate` 提供同一套行级 SQL 条件，台账与收益接口共用，两处口径不可能漂移。
- 行业板块聚合（`db.news_industry_summary` → `NewsService.industry_overview` → `GET /api/news/industries`）：只统计 `news_links` 里的真实行业关联，一条新闻命中多个板块各计各的（COUNT DISTINCT 保证板块内不重复）；没有任何行业关联的条目由 `news_unlinked_industry_count` 单独计数，页面如实显示「未匹配行业」，绝不编造板块归属。板块下钻走 `GET /api/news/industries/{industry}?trade_date=`，只取指定交易日的关联，仍带命中依据。
- 情绪判定的两种「中性」分开计数：`neutral` 是有依据判出的中性，`undecided` 是判不出来。合并会把「没结论」讲成「没倾向」。

### 防未来数据泄漏

- T+N 独立收益按 `trade_cal` 市场交易日定位：T+1 开盘买入，T+1 收盘卖出得到 `t1_close`；后续交易日开盘分别得到 `t2_open`…`t10_open`。v1 不计手续费、印花税和滑点。
- 为给 T+1~T+10 的收益留出完整落地空间，系统默认隐藏最近 20 个交易日；这样历史批次才能被真实评估，避免把“还没来得及确认”的目标日过早算进去。
- 请求隐藏日期时直接拒绝，不静默改写成别的日期；静默改写会让调用方误以为拿到的就是自己要的那天，是最坏的降级。
- 在线仍要持续摄取最新交易日，因为可见日 = 基准日往前退 20 个交易日；只有最新截面不断进来，可见窗口才会往前推，历史可见范围才会逐步释放。

### 舆情快照归属

- TrendRadar 热榜没有权威发布时间，`raw.time_basis="first_seen_at_collect"` 显式标注「首次抓取时刻」而非发布时间（合规语义妥协，可审计，不是造假）。
- 快照条目不适用「未来数据」拒收逻辑：采集当天在交易日历 → 归属当天；采集日不在日历 → 归属最近已收盘日；采集日晚于日历末尾 → 归属日历最后一天；日历为空 → 抛错而不是猜。
- 非快照来源（带权威 `published_at` 的）仍走原路径，`time_decay(..., allow_future=False)` 的未来数据防线不变。

### 读写分离

- 读路径不执行 DDL（`ensure_schema=False`），也不写库。`build_review()` 在读路径固定 `backfill=False`，打开页面不该产生写入。
- 表结构迁移集中在启动时执行一次。数据库文件不存在时不建空库。

### 幂等

- `Store.upsert()` 是 DELETE+INSERT。
- 扫描按 `(as_of, strategy)` 整组替换，同一交易日重跑不累积冗余批次。
- 任务以 `(kind, trade_date, strategy)` 为抢占键，状态落 `task_runs` 表，服务重启后仍可查询；心跳超时判为僵死并允许重试。
- `task_runs` 的读写统一收在 `app/services/tasks.py` 的 `TaskTracker`：抢占、心跳、落终态、按 `kind` 查历史、JSON 列解析与字段装饰只有一份实现。`ScanManager`（`kind="scan"`）与 `PipelineManager`（`kind="close_pipeline"`）都走这一层，只保留各自的执行体与 HTTP 语义（错误码、僵死阈值）。两个 Manager 各抄一份装饰逻辑必然漂移，而漂移的表现是页面上某类任务少一个字段，很难定位。
- `picks` 主键为业务幂等键 `(as_of, strategy, ts_code)`（`as_of` 是横截面日期，`run_date` 只是写入时间）。旧库启动时自动迁移：去重保留最新 `run_date`，全程事务，失败回滚。
- `experiment_decisions` 的 16 个旧列（`entry_date` / `entry_price` / `entry_status` / `entry_reason` / `ret{1,3,5,10}` 及其 `_target_date` / `_status` / `_reason`）已从 DDL 删除。`Store(ensure_schema=True)` 每次开库执行 `_drop_legacy_decision_columns()`：只 DROP 还残留的旧列，一个事务、幂等，决策数据不动（已在真实库副本与带数据的合成老库上验证）。留着旧列的代价是两套收益口径并存、页面读哪一套都可能对不上。

### 失败显式暴露

- 不吞异常、不静默降级、不返回假数据。配置加载器遇到非法配置直接抛错。
- 未配置的能力（AI、舆情来源）返回明确的 `unconfigured` / `unavailable` 状态并列出缺什么，绝不返回看起来像真结论的占位内容。
- `FETCHER_REGISTRY` 是白名单式的合规闸门：只有经过核验的采集器工厂才能进表，未登记的 `fetcher` 名字会让配置加载直接抛错。目前登记了 `trendradar` 一个（TrendRadar 热榜，已于 2026-08-01 核验并写明 compliance_note）。`NARRATOR_REGISTRY` 已注册 `openai_compatible`（OpenAI 兼容接口）——没有真实凭据（base_url + model + api_key）的叙述器一律不可用，接口返回 `disabled` / `unconfigured` 并列出缺什么。
- 前端三态渲染与后端契约一一对应：舆情（`available` 已接入 / `missing_reason` 未接入并附原因）、复盘（按 `available_sections` 数量显示已生成/部分生成/待生成）、AI（`disabled` 未启用 / `unconfigured` 未配置 / `available` 已配置）。九个页面通过 `app-shell.js` 共享数据链路状态条，总览页另有明细行。
- 窗口算不出来时，不能拿最新交易日顶替；调用方必须拿到明确原因，才能知道是日历缺失、基准日不足，还是窗口本身不可用。

### GPL 源码隔离

- TrendRadar（sansan0/TrendRadar）是 GPL-3.0，原样保留在 `workbench/vendor/TrendRadar/`，不改一行、不拷贝进本包。
- `news_trendradar.py` 只用 `importlib.util.spec_from_file_location` 按**单文件路径**加载它的 `DataFetcher`（`trendradar/crawler/fetcher.py`，仅依赖 requests），刻意不 `import trendradar`——走包入口会拽出 litellm/boto3 等重依赖，也会把 GPL 代码更深地耦合进来。适配器本身是我们的代码，只调用一个外部类。

### 前端与图表

- K 线页用本地 ECharts（无 CDN、离线可用），指标全部后端算好返回，前端只负责渲染与交互（十字光标、指标副图）。
- 设计令牌与参考对象调研沉淀在 `workbench/docs/ui-design-reference.md`，页面主题统一深色科技感，涨红跌绿符合 A 股习惯。
- 主题令牌分层：`theme.css` 定义基础令牌（`--bg`/`--surface`/`--navy`/`--text` 为测试锁定值，不可改）与渐变光效（青蓝→紫双色 radial-gradient、科技网格、紫光悬停），各页私有样式只叠加页面级渐变，不覆盖基础令牌——保证换肤不动四令牌、UI 测试稳定。
- 自选股：`watchlist` 表以 ts_code 为主键，增删幂等（重复添加/删除不报错）；列表接口把自选与行情、行业、最新交易日联表，`sort_order` 保持手工添加顺序；股票不存在时添加返回 404，不让假数据进自选。
- 行业资金流：`/api/sentiment` 的 `industry_moneyflow` 取资金流最新交易日按行业聚合（净流入=买-卖，大单/超大单净额分别计算），覆盖区间与股票数随结果返回；没有任何资金流数据时返回 `availability="unavailable"` + `reason`，页面如实显示「暂无数据」并附原因，不伪造行业资金分布。
- 选股台（p1）布局：左主列选股工作流（候选池 → 个股详情+决策依据 → 最近行情），右栏自选股行情 + 行业资金流向；`desk.js` 内 `renderWatchStars()` 负责把候选池星标与自选列表同步，`loadWatchlist()` 完成后必须调用它刷新星标状态（曾因调用不存在的 `refreshWatchStars()` 导致星标不刷新，已修复）。
- 自选页（p10）：独立页面集中管理自选（列表 + 最新行情 + 搜索/行业筛选/添加/移除/点击跳 K 线/空态引导 + 概览统计卡），与行情页、选股台自选面板共用 `/api/watchlist`。
- AI 研判面板（选股台 p1）：参数候选 200 / 深度 8 / 最终 3 面板可改并存 localStorage；后台线程分阶段执行并回报进度（粗筛 → 深度学习 → 多空辩论）；结果卡片可一键加入自选；未配置模型时按钮禁用并显示原因。

### 独立多 Agent 页面与设置页

- p11_agents.html：独立「AI Agent」页面，支持两种模式：
  - 个股研判（POST /api/agents/single）：直接对单只股票跑三位分析师 + 多空辩论 + 风控，不走候选池/粗筛；
  - 选股流程（POST /api/agents/judge）：候选池 → 粗筛 → 深度学习 → 辩论 → 最优 N 只。
- p12_settings.html：独立设置页，读写 config/settings.local.yaml（OpenAI 兼容接口 base_url/model + 研判默认参数）；`WORKBENCH_AI_API_KEY` 从 Git 忽略的 `workbench/.env` 加载并保持只读，不由页面修改。
  - 页面、YAML、数据库和日志都不保存密钥明文；加载成功后页面显示「已检测到」。
  - `engine.config.load_settings_with_local()` 合并本地覆盖，`app/services/settings_store.py` 负责白名单落盘。
- 舆情双源：舆情分析师输入带 `source_note` 说明「TrendRadar 已入库 + TradingAgents-CN 风格质量评估字段（相关性/时效/可信度/情绪）」，每条舆情带 source_kind / credibility / relevance / quality_score。

### 多 agent 短线研判

- 两级混合结构：① 粗筛用方法论文本 prompt 对候选池全体打分（单次模型调用）；② 深度分析对前 N 只并行跑方法论/舆情/走势三位分析师（加权 0.4/0.3/0.3）+ 决策汇总；③ 最终 M 只做多方/空方陈述 + 中性风控一轮精简辩论。
- 短线口径：提示词淡化中线基本面，强调情绪阶段/题材热度/量价/资金；输入全部来自已入库数据（`/api/stocks`、`/api/kline` 指标、`/api/news/*`）。舆情不新增采集器，只做相关性/时效/来源可信度过滤与评估，未匹配行业的新闻如实标注。
- 参数钳制：面板可改但后端按 `settings.yaml` 的 `agent:` 段上限钳制（候选 200 / 深度 30 / 最终 10），防止超长任务打爆模型。
- 落库与幂等：结果写 `agent_runs`（run_id/参数/状态）与 `agent_judgments`（股票/阶段/分数/理由/风险/原始 JSON）；同一 as_of + 同一组参数已成功则复用并带 `reused=True`，抢占失败带冲突行。
- 未配置即显式失败：AI 未启用/未配置时 `POST /api/agents/judge` 与 `POST /api/agents/single` 返回 503，绝不走规则模板降级、绝不编造结论；已完成结果可用 `GET /api/agents/results` 按 `as_of` 过滤查看。

## 已知待办

- 除流程页可从任务历史展开指定批次外，其余页面仍主要查询最新全局扫描，尚未统一固定 `run_id`。
- 舆情来源扩展（TrendRadar 之外更多来源待核验）。
