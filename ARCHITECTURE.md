# 架构说明

## 分层

```text
engine/     领域逻辑与数据访问,不依赖 FastAPI
app/        接口层:repositories → services → api
ui_mockups/v2/   页面(index + p1..p12,共十三个,原生 JS,动态取数)
tests/      隔离数据库测试
vendor/     外部 GPL 源码(TrendRadar),不入本仓库版本历史
```

依赖方向单向：`api → services → repositories → engine`。`engine` 不反向依赖 `app`。

## 模块职责

### engine（领域层）

- `config.py`：读取 `settings.yaml` 与策略配置，路径统一相对 workbench 根解析。
- `db.py` / `schema.py` / `db_news.py`：DuckDB 存储层，按**表族**拆成三个文件（原 `db.py` 1182 行超出自定的 800 行上限）。`schema.py` 只放建表 DDL 常量；`db_news.py` 是 `NewsAgentMixin`，装 news_sources / news_items / news_links / agent_runs / agent_judgments 的读写；`db.py` 留连接管理、行情与台账，`class Store(NewsAgentMixin)`。用 mixin 而不是组合，是因为全项目 30 处都写 `from engine.db import Store` 并直接调 `store.news_by_trade_date(...)`——mixin 让 `Store` 的方法集合与拆分前逐个相等，调用侧一行不动。表结构、幂等写入与查询口径不变。自选股表 `watchlist`（ts_code 主键 + 添加时间/排序）与增删查方法（`add_watchlist` / `remove_watchlist` / `watchlist_quotes`），资金流按行业聚合（`moneyflow_date_range` / `moneyflow_industry_summary`）。所有 `Store` 方法是唯一的数据访问出口。`Store.__init__` 打开库带 3 次短重试（20~50ms 退避）：Windows 下同进程并发打开同一 DuckDB 文件偶发文件句柄占用（WinError 32），重试失败仍上抛，不吞错。`ensure_schema=True` 路径会自动迁移 `picks` 旧主键（见「幂等」）。
- `ingest_tushare.py`：更新行情、交易日历与资金流。
- `universe.py`：硬过滤、行业热度、候选召回。
- `factors/`：结构、趋势、MACD、成交量、题材、资金六类因子。
- `score.py`：归一化、综合打分、门槛过滤、行业去重。
- `run_scan.py`：编排扫描并写入选股台账。
- `postmortem.py`：按市场交易日口径回填 T+N 收益并做 IC 自检。
- `backtest.py`：滚动回测。`run_backtest()` 返回四段结果（指标/假设/覆盖率/逐期明细），默认 `non_overlap` 非重叠调仓防收益虚高；`horizons()` 按持仓天数排序。
- `ml/`：因子机器学习体检（`dataset` 数据装配、`labels` 标签、`splits` 时序切分、`metrics` IC/AUC/分桶、`model` 训练与降级、`registry` 产物登记、`train` 编排），模型缺失/未达标时如实报诊断、不给预测。
- `schedule.py`：交易日判定与「是否该跑」的纯决策函数，不含副作用。
- `close_pipeline.py`：收盘后任务链编排（六步）。
- `news_config.py` / `news_text.py` / `news.py`：舆情来源配置、文本处理、采集与入库。`news_text.py` 的 `resolve_snapshot_trade_date()` 负责把无权威发布时间的快照条目归属到交易日（见「舆情快照归属」）。
- `news_trendradar.py`：TrendRadar 热榜采集适配器（我们的代码），实现 `NewsFetcher` 协议并注册进 `FETCHER_REGISTRY["trendradar"]`。仅按单文件路径 `importlib` 加载 vendor 的 `DataFetcher`，不 `import trendradar`——见「GPL 源码隔离」。
- `review.py`：装配带三级标注的复盘结果。
- `ai.py`：AI 叙述接口。`NARRATOR_REGISTRY` 已注册 `openai_compatible`（OpenAI 兼容 `/chat/completions`，base_url 必填不猜默认地址）；缺凭据时三态明确，不编造。
- `agents.py`：多 agent 短线研判编排（粗筛打分 → 三分析师深度分析 → 多空辩论），纯 prompt 组装与 JSON 容错解析，不直接碰数据库。

### app（接口层）

- `repositories/market.py`：唯一的数据读取入口，读路径一律 `ensure_schema=False`。
- `services/`：业务装配与可用性判定（`overview` `stocks` `analytics` `news` `reviews` `ai` `scans` `pipelines` `scheduler` `tasks` `kline` `screener` `backtest` `watchlist` `agents`）。
- `services/kline.py`：个股搜索 + 日 K 数据装配，MA5/10/20/60、EMA12/26、MACD、KDJ、RSI6/12/24、BOLL 全部后端计算；`quote` 附带换手率、量比、总市值/流通市值。
- `services/screener.py`：全市场横截面筛选（涨跌幅、量比、行业过滤，字段排序 + 分页），读最近一次扫描结果。
- `services/agents.py` / `services/agents_data.py`：多 agent 研判管理器，按**职责**拆成两个文件（原 `agents.py` 883 行超出自定的 800 行上限）。`agents_data.py` 是 `AgentDataMixin`，只做「库里的行情/技术指标/资金流/舆情 → 喂给模型的紧凑快照」（含 `_round` 安全取整：算不出留 None，不用 0 冒充）；`agents.py` 留任务编排、幂等抢占、AI 客户端与落库，`class AgentJudgeManager(AgentDataMixin)`。同样用 mixin 而不是组合——外部只 import `AgentJudgeManager`（`app/api/agents.py` 与 `app/main.py`），方法集合与拆分前逐个相等，调用侧不动。
- `api/`：路由与参数校验，不含业务逻辑。
- `errors.py`：`WorkbenchError` 统一错误信封。
- `main.py`：应用装配、启动时表结构迁移、调度线程生命周期、十三页面白名单托管（`index.html` + `p1..p12`），`AgentJudgeManager` 生命周期。

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
                        postmortem.py（T+N 回填）
                                ↓
                          review.py（三级标注装配）
                                ↓
        app/repositories → app/services → app/api
                                ↓
                      ui_mockups/v2（十页动态取数）

backtest.py：picks 台账 → 滚动回测（非重叠调仓）→ /api/backtest → 回测页
ml/：picks 台账 → 因子体检（IC/AUC/分桶/过拟合缺口）→ /api/analytics/factors → 因子页
agents.py：候选池/自选 → 行情+技术指标+资金流+舆情快照 → 粗筛 → 三分析师+决策 → 多空辩论 → agent_runs/agent_judgments → /api/agents/* → 选股台 AI 研判面板
close_pipeline.py 串联：确认交易日 → 更新行情 → 扫描 → 回填 T+N → 采集舆情 → 生成复盘
```

## 关键设计决定

### 数据可信度

- 复盘每条结论标注来源类别：`fact`（原始事实）/ `derived`（规则计算结果）/ `unverified`（待验证判断）。三者不混排，`label_legend` 随结果一起返回。
- 缺数据的小节返回 `available: False` 加 `missing_reason`，不补零、不省略。**「没有数据」和「数据是 0」是两件事**，任何一处把它们合并都算缺陷。
- 舆情缺失区分三态：`no_source_registered`（来源没登记）/ `never_collected`（登记了没采过）/ `no_news_on_date`（采过但当天没条目）。关联查询另有 `no_linked_news`。
- 行业板块聚合（`db.news_industry_summary` → `NewsService.industry_overview` → `GET /api/news/industries`）：只统计 `news_links` 里的真实行业关联，一条新闻命中多个板块各计各的（COUNT DISTINCT 保证板块内不重复）；没有任何行业关联的条目由 `news_unlinked_industry_count` 单独计数，页面如实显示「未匹配行业」，绝不编造板块归属。板块下钻走 `GET /api/news/industries/{industry}?trade_date=`，只取指定交易日的关联，仍带命中依据。
- 情绪判定的两种「中性」分开计数：`neutral` 是有依据判出的中性，`undecided` 是判不出来。合并会把「没结论」讲成「没倾向」。

### 防未来数据泄漏

- 所有历史查询限制在 `as_of` 及以前。
- T+N 按 `trade_cal` 的市场交易日定位，不数个股自己的 K 线——否则停牌票与正常票口径不一致，会污染 IC 与胜率。
- 舆情按 `close_cutoff` 归属交易日：收盘后发布的归到下一开市日。关联查询用 `as_of` 过滤 `trade_date <= as_of`。

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

### 失败显式暴露

- 不吞异常、不静默降级、不返回假数据。配置加载器遇到非法配置直接抛错。
- 未配置的能力（AI、舆情来源）返回明确的 `unconfigured` / `unavailable` 状态并列出缺什么，绝不返回看起来像真结论的占位内容。
- `FETCHER_REGISTRY` 是白名单式的合规闸门：只有经过核验的采集器工厂才能进表，未登记的 `fetcher` 名字会让配置加载直接抛错。目前登记了 `trendradar` 一个（TrendRadar 热榜，已于 2026-08-01 核验并写明 compliance_note）。`NARRATOR_REGISTRY` 已注册 `openai_compatible`（OpenAI 兼容接口）——没有真实凭据（base_url + model + api_key）的叙述器一律不可用，接口返回 `disabled` / `unconfigured` 并列出缺什么。
- 前端三态渲染与后端契约一一对应：舆情（`available` 已接入 / `missing_reason` 未接入并附原因）、复盘（按 `available_sections` 数量显示已生成/部分生成/待生成）、AI（`disabled` 未启用 / `unconfigured` 未配置 / `available` 已配置）。九个页面通过 `app-shell.js` 共享数据链路状态条，总览页另有明细行。

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
- p12_settings.html：独立设置页，读写 config/settings.local.yaml（OpenAI 兼容接口 base_url/api_key_env/model + 研判默认参数）。
  - 密钥只存环境变量名，不落明文；环境变量已设置则在页面显示「已检测到」。
  - `engine.config.load_settings_with_local()` 合并本地覆盖，`app/services/settings_store.py` 负责白名单落盘。
- 舆情双源：舆情分析师输入带 `source_note` 说明「TrendRadar 已入库 + TradingAgents-CN 风格质量评估字段（相关性/时效/可信度/情绪）」，每条舆情带 source_kind / credibility / relevance / quality_score。

### 多 agent 短线研判


- 两级混合结构：① 粗筛用方法论文本 prompt 对候选池全体打分（单次模型调用）；② 深度分析对前 N 只并行跑方法论/舆情/走势三位分析师（加权 0.4/0.3/0.3）+ 决策汇总；③ 最终 M 只做多方/空方陈述 + 中性风控一轮精简辩论。
- 短线口径：提示词淡化中线基本面，强调情绪阶段/题材热度/量价/资金；输入全部来自已入库数据（`/api/stocks`、`/api/kline` 指标、`/api/news/*`）。舆情不新增采集器，只做相关性/时效/来源可信度过滤与评估，未匹配行业的新闻如实标注。
- 参数钳制：面板可改但后端按 `settings.yaml` 的 `agent:` 段上限钳制（候选 200 / 深度 30 / 最终 10），防止超长任务打爆模型。
- 落库与幂等：结果写 `agent_runs`（run_id/参数/状态）与 `agent_judgments`（股票/阶段/分数/理由/风险/原始 JSON）；同一 as_of + 同一组参数已成功则复用并带 `reused=True`，抢占失败带冲突行。
- 未配置即显式失败：AI 未启用/未配置时 `POST /api/agents/judge` 与 `POST /api/agents/single` 返回 503，绝不走规则模板降级、绝不编造结论；已完成结果可用 `GET /api/agents/results` 按 `as_of` 过滤查看。

## 已知待办

- 页面固定 `run_id` 与数据截止时间的能力尚未提供，所有页面查询最新全局扫描。
- 舆情来源扩展（TrendRadar 之外更多来源待核验）；多 agent 研判需真实 OpenAI 兼容凭据（agent.enabled + base_url + model + WORKBENCH_AI_API_KEY）才可运行。