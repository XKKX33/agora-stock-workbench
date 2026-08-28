# AGORA 股票量化工作台

## 项目简介

从 Hermes 股票分析技能迁出的本地量化工作台。当前主流程固定为三步：方法论选股初筛、对应板块舆情抓取、多 Agent 多方辩论输出候选结论。另具备 A 股数据入库、收益验证、复盘、K 线行情、全市场筛选、滚动回测与因子机器学习体检。界面收敛为六个工作入口：总览、方法论选股、板块舆情、多 Agent 辩论、自选与行情、设置；页面数据全部来自 DuckDB 实时查询。

当前状态是**可运行的本地闭环**：FastAPI 服务同时提供接口和页面，页面数据全部来自 DuckDB 实时查询。

为避免前视偏差，系统默认启用「防前视：隐藏最近 20 个交易日」：选股、AI 研判、实验记录、收益回填只看到「最新交易日往前退 20 个交易日」以及更早的数据。K 线和行情查询不受影响，仍能查看最新行情。用户或接口如果请求更新的日期，会被直接拒绝（错误码 `lookahead_blocked`），不会偷偷改成别的日期。

## 技术架构

- Python：数据采集、因子计算、扫描、复盘与调度。
- DuckDB：单文件保存行情、资金流、交易日历、选股台账、任务状态与舆情。
- Tushare：联网更新 A 股行情、交易日历与资金流。
- FastAPI + Uvicorn：接口层与页面托管。
- HTML/CSS/原生 JavaScript + ECharts：六个工作台入口；统一任务面板显示后台阶段、日志、进度和终态，缺数据一律按三态显示，不补零。
- `app/services/kline.py`：个股搜索与 K 线指标计算（MA / MACD / KDJ / RSI / BOLL），全部在后端算出、前端只画图。
- `app/services/screener.py`：全市场横截面筛选，支持涨跌幅、量比、行业过滤与排序分页。
- `app/services/watchlist.py`：自选股维护与行情列表（搜索/行业/排序筛选，添加移除幂等，股票不存在返回 404 而不是静默吞掉）。
- `engine/methodology.py`：短线方法论正文与七个角色职责的**唯一来源**，随每次请求下发给 Pi Agent；改方法论只改这一处。
- `engine/agents.py` + `app/services/agents.py` + `app/api/agents.py`：多 agent 短线研判（粗筛 → 三分析师 → 方法论/舆情/走势公开消息交接 → 多空辩论 → 风控），公开结构化事件写入 `agent_events`，结果落 `agent_runs` / `agent_judgments` 表；前端把选股批次 `run_id`、信号日和通过候选显式传入，后端按指定批次冻结输入。
- `engine/returns.py` + `app/services/returns.py` + `app/api/returns.py`：独立 `experiment_returns` 收益验证，提供 `t1_close` 与 `t2_open`…`t10_open` 详情和汇总；不改写旧 `picks.ret1/ret3/ret5/ret10` 口径。
- `engine/visibility.py`：防前视日期闸门，统一算出当前允许用于选股、研判、实验与收益验证的最新可见交易日。
- `engine/backtest.py` + `app/services/backtest.py` + `app/api/backtest.py`：滚动回测与多策略对比（净值曲线、回撤、胜率、换手、成本假设、覆盖率），默认非重叠调仓口径。
- `engine/ml/`：因子机器学习体检（样本外 IC / AUC / 分桶单调性 / 过拟合缺口），诊断级结果如实展示。

## 本地运行

工作目录：

```powershell
cd C:\Users\xuan\Desktop\桌面\股票\workbench
```

启动工作台（一条命令，接口与页面同一进程）：

```powershell
C:\Users\xuan\anaconda3\python.exe serve.py
```

启动后：

- 工作台页面 <http://127.0.0.1:8765>
- 接口文档 <http://127.0.0.1:8765/docs>

端口默认 **8765**（`app/config.py` 的 `AppSettings.port`）。若本机该端口已被别的程序占用，加 `--port 8788` 换一个，页面地址同步改成新端口。`serve.py` 启动时先加载已被 Git 忽略的 `workbench/.env`，再报告数据库状态；显式进程环境变量优先。**数据库文件不存在时不会自动建空库**，只打印警告——凭空造空库会把「还没采过数据」伪装成「有库但全空」。

其他入口：

```powershell
# 六个入口都必须用 anaconda 全路径：裸 `python` 在本机指向无关的 venv，不保证依赖齐全。
$py = "C:\Users\xuan\anaconda3\python.exe"

& $py -m engine.run_scan --offline                  # 只用本地数据跑一次扫描（截面自动取可见日）
& $py -m engine.run_scan --trade-date 20260706      # 指定截面交易日（必须 <= 可见日，否则拒绝执行）
& $py -m engine.close_pipeline --offline            # 手动执行一次收盘后任务链（截面=可见日）
& $py -m engine.postmortem                          # 回填 T+N 收益并做 IC 自检（只回填到可见日）
& $py -m engine.review --trade-date 20260706        # 查看某交易日的复盘（必须 <= 可见日）
& $py tools/train_ml.py --dry-run                   # 因子体检训练（采样截止日自动取可见日）
```

`close_pipeline` 支持 `--db` 指向别的 DuckDB 文件，想先在副本上试跑就用它。

## 配置

- `ai.enabled`：AI 复盘叙述。已实现 `openai_compatible` 提供方（OpenAI 兼容接口）；当前 `settings.yaml` endpoint 为 `https://api.pie-xian.com/v1`，模型为 `minimax-m3`。
- `agent.enabled`：多 agent 短线研判。当前 endpoint 为 `https://api.pie-xian.com/v1`，模型字段为空并按当前配置状态报告；面板默认候选 20 / 深度 20 / 最终 3，后端上限同为 20 / 20 / 3。

- `data.visibility_delay_sessions`：防前视隐藏窗口，默认 `20`，表示只让选股、AI 研判、实验记录、收益回填看到「最新交易日往前退 20 个交易日」以及更早的数据；设为 `0` 可关闭隐藏。配置位置是 `workbench/config/settings.yaml` 的 `data:` 段。

每日一键工作流按九步顺序执行：`preflight` → `calendar` → `market_data` → `backfill_returns` → `integrity` → `scan` → `collect_news` → `agents` → `persist_experiment`。单个步骤失败会记录为 `warning`（警告），依赖它的步骤标记为 `skipped`（跳过），其他独立步骤继续；校验失败的数据不会写入。智能体不可用时只原子保存规则组和基准组，不伪造 AI 结果；九步全部尝试后若仍没有可原子提交的实验结果，任务终态为失败，不会显示假成功。

补齐最近可见交易日可调用 `POST /api/pipelines/backfill`，请求体示例：

```json
{"count": 20, "strategy": null, "online": null, "force": false}
```

它会按日期由旧到新串行跑完整九步流程；已经成功的日期会复用，不重跑；某一天失败会记录日期和原因，然后继续后续日期。补历史时不采集当天舆情，避免把今天的热榜挂到过去的交易日上。进度用 `GET /api/pipelines/{job_id}` 查询，历史列表用 `GET /api/pipelines?kind=one_click_backfill`。

Agent 公开事件可通过 `GET /api/agents/jobs/{job_id}/events?after_seq=0&limit=500` 断点读取，或通过 `GET /api/agents/jobs/{job_id}/stream?after_seq=0` 以 SSE 读取；p13 先加载历史事件，再按最后序号续接实时流。公开消息会在角色之间交接，隐藏思维链不落库、不下发。

收益验证使用 `POST /api/returns/calculate`、`GET /api/returns` 和 `GET /api/returns/summary`。T+1 买入次日开盘、T+1 收盘卖出；T+2 至 T+10 使用后续交易日开盘卖出，v1 不计手续费、印花税和滑点。缺行情、未来未到或未成交保留 `status/reason`，不写真实 0；汇总的未成交资金槽位才按现金收益 0 处理。

收益语义上，T+1 到 T+10 的目标日期如果还落在隐藏窗口内，状态会记为 `future_not_visible` 并计入 pending：它不会被当成 0 收益，也不会去读取那些日期的行情；等可见窗口继续往前推，后续计算会自然补出来。

已知边界：`stock_basic` 里的股票名称与行业分类是当期状态，历史回放会沿用当期分类，这是 Tushare 数据源的限制；行情、资金流、涨跌停数据都是按日期存取的，不受这个限制影响。

凭据从 `workbench/.env` 加载为环境变量（`TUSHARE_TOKEN`、`WORKBENCH_AI_API_KEY`）；`.env` 已被 Git 忽略，密钥不写入 YAML、数据库或日志。

## 部署

仅面向本地单机运行，默认监听 `127.0.0.1`。**接口层没有任何认证**，改绑 `0.0.0.0` 或对外暴露前必须先加访问控制。

## 测试

```powershell
cd C:\Users\xuan\Desktop\桌面\股票\workbench
C:\Users\xuan\anaconda3\python.exe -m pytest tests -q
```

Pi Agent（TypeScript）侧：

```powershell
cd C:\Users\xuan\Desktop\桌面\股票\workbench\pi_agent
npm test
```

注意（本机环境约束）：

- 唯一可用 Python 是 `C:\Users\xuan\anaconda3\python.exe`（无 venv，禁止新建环境）；裸 `python` 在本机指向无关的 venv。
- `--import-mode=importlib` 已写进 `pytest.ini` 的 `addopts`，不用手加：`tests/` 与 `tests/api/` 存在同名文件（`test_ai.py`、`test_news.py`），默认 prepend 模式会冲突。
- 必须从 `workbench/` 目录运行：`pytest.ini` 的 `testpaths` 与 `pythonpath` 都相对它。
- 测试全部使用 `tmp_path` 隔离数据库，不会读写 `data/market.duckdb`；Pi Agent 相关用例现取空闲端口，本机开着工作台也能跑。
- 接口测试会自己删掉 `ai` / `agent` 段 `api_key_env` 声明的凭据环境变量（默认 `WORKBENCH_AI_API_KEY`）：「未配置要报错」的用例不受开发机环境影响，也不会真的去打模型接口。

## 搜索记录

- 迁移阶段：既有 Hermes 模块迁移，无新增技术选型，未检索 `skills.sh` 或 GitHub。
- 舆情阶段：经 `gh` CLI 调研，选定 **TrendRadar**（sansan0/TrendRadar，GPL-3.0）作为第一个采集器，已于 **2026-08-01** 按项目地址、许可证、维护活跃度、数据源、反爬与合规风险逐项核验并写进 `compliance_note`，注册进 `engine/news_config.FETCHER_REGISTRY["trendradar"]`。GPL 源码原样留在 `workbench/vendor/`、仅按单文件 `importlib` 加载，隔离方式见 ARCHITECTURE.md「GPL 源码隔离」。合规闸门仍在：未核验的来源不进白名单。
- UI 设计阶段：调研 TradingView / 富途 / 同花顺 / 雪球 / Linear / QuantConnect 等，结论沉淀在 `workbench/docs/ui-design-reference.md`（设计令牌、色值、布局、图表交互全部数值化，可直接照抄）。K 线图表采用 ECharts（本地文件，无 CDN 依赖，离线可运行）。
- UI 重构阶段（2026-08-01）：按 Figma 风格重做 `theme.css`——新增统一圆角（10/14/18px/胶囊）、柔和阴影、easeOutQuint 缓动令牌；卡片入场错峰浮起、按钮按压缩放、悬停轻抬；核心色值（品牌青 `#3ec6ff`、语义红涨绿跌）全部保留，A 股红涨绿跌由页面 `.up/.down` 负责不受影响；不引入 CDN 字体与依赖，离线可运行。
- UI 蓝紫渐变阶段（2026-08-02）：沿用既有设计令牌，主体背景改为青蓝→紫双色径向渐变 + 极淡科技网格，页头标题渐变字、面板悬停紫光；核心色值令牌（`--bg`/`--surface`/`--navy`/`--text`）保持不变，测试锁定的四个令牌未动。
- 多 agent 研判阶段（2026-08-02）：调研 **hsliuping/TradingAgents-CN**（TradingAgents 中文增强版）。只借鉴其「多源舆情评估 + 分析师/辩论/风控协作」的编排思路；不引入 LangGraph 依赖，不改长线基本面口径——提示词按短线操作重写，方法论沿用本工作台自己的波浪周期 + MACD + 量价体系。

## 已完成

- A 股行情、资金流、交易日历入库，写入幂等。
- 候选池过滤、行业热度、十四个因子计算。
- 综合打分、门槛过滤、行业去重、资金后置确认。
- 选股台账与按市场交易日口径的 T+N 收益回填。
- 收盘后九步任务链：预检 → 确认交易日 → 更新行情 → 回填独立收益 → 完整性检查 → 扫描 → 采集舆情 → Agent 公开研判 → 持久化实验，状态落库、支持手动触发与幂等重跑。
- 流程页（p3）：显示自动调度与闸门结论，支持手动运行和按可见交易日补齐；九步可逐项展开真实数据与中文说明，警告步骤显示具体原因，依赖缺失或历史舆情步骤明确标为「未执行」；当前可用实验组支持分页并写明总条数与数据截止时间。
- 舆情存储结构（来源、条目、实体关联）与去重、股票行业关联、事件分类、情绪方向、时间衰减、来源可追溯。
- 一键采集舆情：`POST /api/news/collect` 后台起采集任务，`GET /api/news/collect/{job_id}` 轮询进度；已接入 TrendRadar 全网热榜作为第一个采集器。舆情页可看条目、按来源追溯、按股票/行业过滤，并直接点按钮触发采集。
- 舆情按行业板块分组（2026-08-02）：`GET /api/news/industries` 返回当日各板块新闻数与情绪分布，`GET /api/news/industries/{行业}` 支持 `trade_date` 下钻到当日；页面以板块胶囊分组展示，命中依据（正文点名行业 / 关联股票所属行业）可追溯。聚合只认 `news_links` 里的真实关联，没有行业关联的条目如实显示「未匹配行业」，不硬塞进任何板块。
- 带三级标注的复盘装配：`fact`（事实）/ `derived`（规则计算结果）/ `unverified`（待验证判断）。
- 行情 K 线页：个股搜索、日 K 图（MA5/10/20/60、MACD、KDJ、RSI、BOLL），后端算指标、前端只渲染。
- 全市场筛选接口：`GET /api/screener`，涨跌幅/量比/行业过滤、多字段排序、分页。
- 六入口页面动态读取真实数据；方法论选股、板块舆情和多 Agent 页面共享当前工作上下文，刷新后可恢复扫描/采集/研判任务，候选批次不会静默切换到另一批次。
- 独立收益验证：`experiment_returns` 按 `(run_id, group_name, ts_code, horizon)` 幂等保存 `t1_close`、`t2_open`…`t10_open`，汇总明确区分可测、缺失和真实零收益。
- 实验台账收敛为一份口径（2026-08-10）：`experiment_decisions` 只存决策（11 列），成交价与各期收益只存 `experiment_returns`；`GET /api/experiments` 每行挂 `entry_status` 与 `returns.{horizon}`，汇总统一走 `GET /api/returns/summary`（原 `/api/experiments/summary` 已删除）。顺带修掉一个真实错误:涨停封板或缺涨跌停价时旧代码仍把开盘价写进 `entry_price`，导致「到底买到没买到」分不清；现在只有真的买到才有成交价。回测与复盘用的 `picks.ret1/ret3/ret5/ret10` 是另一条链路，未受影响。
- `picks` 主键已对齐业务幂等键 `(as_of, strategy, ts_code)`：旧库启动时自动迁移去重，保留最新 `run_date`。
- Windows + DuckDB 文件锁竞态加固：打开库短重试 3 次（20~50ms 退避），连续失败仍上抛，不吞错；API 层测试原 7 个失败由此清零。
- 舆情快照归属修复：TrendRadar 热榜无权威发布时间，采集当天若不在交易日历则归属最近已收盘日（晚于日历末尾则归日历最后一天），不再把合法快照当未来数据拒收；`allow_future=False` 的未来数据防线对非快照来源保持原样。
- 自选股功能（2026-08-02）：`GET/POST /api/watchlist`、`DELETE /api/watchlist/{ts_code}`，支持搜索/行业/排序/分页；行情页新增「自选股行情」面板，可添加、移除、按代码名称与行业筛选、点击行跳转 K 线，个股信息栏一键星标切换自选。添加不存在的股票返回 404，重复添加幂等。
- 行业资金流向（2026-08-02）：`/api/sentiment` 新增 `industry_moneyflow` 段，按最新资金流交易日聚合各行业净流入/大单净额/超大单净额/覆盖股票数，附数据区间与股票数说明；无数据时如实返回 `availability="unavailable"` + 原因，不编造。情绪页新增「行业资金流向」面板，支持行业筛选，涨红跌绿。
- UI 蓝紫渐变科技感（2026-08-02）：全站主题背景改蓝紫双色渐变 + 科技网格，导航高亮、按钮、页头、滚动条统一青紫渐变光效，行情页与情绪页面板同步换紫色调。
- 选股台布局重构（2026-08-02）：选股台改为左主列（候选池 → 个股详情+决策依据 → 最近行情）+ 右栏（自选股行情 + 行业资金流向）；候选池每行星标一键加自选，自选面板支持输入代码/名称添加、移除、点击行跳 K 线；行业资金流向展示净流入 TOP12、覆盖区间说明、涨红跌绿。
- 独立自选页（2026-08-02）：`p10_watchlist.html` + `watchlist.js`，侧栏导航新增「自选」；自选列表 + 最新行情 + 搜索/行业筛选/添加/移除/点击跳 K 线/空态引导 + 概览统计卡。
- AI 研判面板（2026-08-02，选股台 p1）：候选/深度/最终三个参数面板可改并本地保存，后台线程分阶段执行（粗筛 → 深度学习 → 多空辩论），进度条 + 结果卡片（排名/理由/风险/来源引用）+ 一键加入自选 + 最近研判列表；AI 未配置时按钮禁用并如实显示原因。
- 多 agent 研判引擎（2026-08-02）：`engine/agents.py` 两级混合编排（方法论文本粗筛 → 方法论分析师 0.4 + 舆情分析师 0.3 + 走势分析师 0.3 → 多方/空方陈述 + 中性风控）；舆情沿用 TrendRadar 数据只做过滤评估，不新增采集器；结果落 `agent_runs` / `agent_judgments`，同参数成功批次幂等复用。
- OpenAI 兼容接入（2026-08-02）：`engine/ai.py` 新增 `openai_compatible` 提供方（httpx 调 `{base_url}/chat/completions`），凭据由 `workbench/.env` 加载到 `WORKBENCH_AI_API_KEY`，当前默认模型为 `grok-4.5`。
- 防前视可见日期闸门（2026-08-10）：默认隐藏最近 20 个交易日，选股、AI 研判、实验记录、收益回填只使用可见日及更早数据；显式请求隐藏日期直接返回 `lookahead_blocked`，不静默改写；新增 `one_click_backfill` 补齐入口按旧到新串行补最近可见交易日。
- 闸门收口到命令行与旧台账（2026-08-10）：`run_scan` / `close_pipeline` / `postmortem` / `review` 四个命令行入口都先算可见日，显式日期超限直接拒绝；旧台账 `picks.retN` 回填加 `visible_max` 上限，目标交易日落在隐藏窗口时记 `future_not_visible` 且不读那天的行情——库里行情已摄取到最新，不设上限等于用当时看不到的价格做评估标签。
- 因子训练也收进闸门（2026-08-10）：`tools/train_ml.py` 的采样截止日默认取可见日，`--end` 超过可见日直接拒绝；产物里记 `dataset.end_day`，事后能看出这份模型看到哪天为止。顺带修掉该脚本 `--strategy` 默认值写成不存在的 `default`（不带参数跑必然 `FileNotFoundError`），改成与 `run_scan` 同一口径的 `settings.engine.default_strategy`。

## 待办事项

- 完成舆情来源合规调研并注册更多采集器（newsnow 等来源待补）；舆情正文级定向采集调研完成后，把真实新闻正文喂给舆情分析师。
- 回测成本口径：买卖不对称暂未建模（印花税 5bp 只在卖出端，现按单一 `cost_bps` 对换手部分双边计价）。换手率已改为等权权重变化口径 `sum|w_new - w_old| / 2`。


## 独立多 Agent 页面 + 设置页（2026-08-03）

- 新增 `p11_agents.html`：AI Agent 页面，支持**个股研判**（`POST /api/agents/single`）和**选股流程**（`POST /api/agents/judge`：候选池 → 粗筛 → 三位分析师 → 多空辩论 → 最优 N 只）。
- 新增 `p12_settings.html`：API 设置页，可填写 OpenAI 兼容接口 `base_url / model / temperature / max_tokens / 默认参数`，落盘到 `config/settings.local.yaml`；密钥固定从 `workbench/.env` 加载的只读环境变量 `WORKBENCH_AI_API_KEY` 获取，页面不接收或保存密钥。
- 舆情双源：`engine/agents.py` 的舆情分析师输入标明「TrendRadar 热榜 + TradingAgents-CN 质量评估口径（相关性/时效/可信度/情绪）」，`_news_brief` 输出带 `source / source_kind / credibility / relevance / quality_score`。
- 路由：`app/main.py` 白名单新增 `p11_agents.html`、`p12_settings.html`；`app/api/settings.py` 提供 `/api/settings` 读写。
- 测试：`tests/api/test_settings.py` 覆盖设置读写；引擎新增 `run_single` 单股研判。（2026-08-03 历史记录：当时全量回归 424 passed。）
## 2026-08-17 Tushare 重试调整

- `settings.yaml` 的 Tushare 请求重试次数已从 3 次改为 5 次。
- 摄取层测试验证前 4 次失败、第 5 次成功时会正常继续；摄取层回归 31 项通过。
- 五次重试后真实流程已越过资金流接口失败。历史数据标准仍为每只股票至少 150 根 K 线；不足的单只股票会被明确排除，其余有效股票继续。
## 2026-08-17 真实库生产式验收

- 已创建真实库备份：`data/market.duckdb.bak-20260817-184226-production-acceptance`。
- 前端 14 页面均真实加载并发出对应 API 请求；修复回测成本/成交规则控件、选股扫描按钮和流程终态按钮三个实际问题。
- 前两次在线九步流程分别暴露资金流接口临时失败和单只股票历史不足；修正为局部隔离后，任务 `d442e819cd7a4443b1d90e060e604051` 完整成功。
- 260 只初始候选中，`001399.SZ` 只有 `17/150` 根历史，被排除；其余 259 只完成评分，31 只通过规则，冻结 20 只送审。
- Agent 对 20 只候选逐只严格校验，2 只非法输出被记录并排除，18 只有效结果继续完成辩论，最终选出 3 只。没有把纯文本或非法字段合成为有效结论。
- 真实库已落下规则 3、AI 3、混合 3、基准 20；实验、Agent、扫描记录均为 `succeeded`，错误字段为空。
- 最终全量测试 **759 passed / 0 failed**；Pi Agent 测试 **17 passed / 0 failed**，类型检查通过，页面脚本 **18 passed / 0 failed**。

## 2026-08-23 辩论链路改造收尾

- **混合组不再是规则组副本**：原先它的「AI 那一半」取的是深度分析阶段的分数，而那个分数就是
  规则分本身，加权等于没加——实测混合组选出的三只与规则组完全相同。现改用辩论评分。
- **AI 说「看空」的股票不再进买入名单**：这些名单会按次日开盘买入回填收益，拿去和规则组比谁
  赚得多。把模型明确说「别买」的股票记成买入，那组数字什么都不代表。全部看空时如实给空名单，
  不凑数、不报错——「都不该追」本身就是有效结论。
- **进度条不再提前跑满**：排序环节删除后 20 只全部参辩，总步数按全部候选算。
- 真实流程实跑三次验证：修复前 AI 组前三里 2 只是看空的（缺陷复现）；修复后 20 只全判看空，
  只落规则 3 / 基准 20，`agents` 步如实记「最终 0/3」；换信号日再跑一次拿到 1 只看多，AI 组与
  混合组各落 1 行且 `ai_score` 是辩论分不是规则分——买入路径也验证过了，不只验证了空名单。
- 历史 6 个批次里 17 条「看空 / 中性」落在买入组的脏决策已按确认清理（删决策 34 行、收益 340 行，
  规则与基准全保留），删前备份 `data/market.duckdb.bak-20260824-002814`。
- **辩论矩阵不再拼接不同股票**：浏览器逐屏核对时发现，Agent 看板一屏六格看起来是一场完整
  辩论，实际方法论/舆情/走势讲一只、多方讲另一只、空方与反驳讲第三只——20 只候选共用同一批
  角色名，渲染时后跑完的覆盖了先跑完的，页面毫无异常迹象。现在看板加了「研判股票」选择器，
  选哪只就只显示那只的六格与风控结论；总览页每格加代码前缀标明归属。
- **台账能分清每一次的入选结果**：同一信号日可以跑多次（实测 8/21 一天跑了 6 次），原先全挤
  在一条「信号日」分隔线下，同一只票重复出现却看不出区别。现在每批一条分隔线写明运行时刻与
  批次号，信号日下方也标运行时间；筛选栏新增「运行批次」下拉框，选一次就只看那一次。
- 测试基线：Python **895 passed / 0 failed**；Pi Agent **33 passed / 0 failed**，类型检查通过。
