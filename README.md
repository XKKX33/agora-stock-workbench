# Hermes 股票量化工作台

## 项目简介

从 Hermes 股票分析技能迁出的本地量化工作台。当前具备 A 股数据入库、候选池筛选、因子计算、综合打分、资金确认、选股台账、T+N 收益回填、收盘后自动任务链、舆情存储与带标注复盘、K 线行情与全市场筛选、滚动回测与因子机器学习体检，以及一套动态读取真实数据的十一页面界面。界面为蓝紫渐变科技感主题：行情页支持自选股（添加/管理/按代码名称与行业筛选，点击即跳 K 线），独立自选页集中管理自选与行情，情绪页提供行业资金流向（按最新资金流交易日聚合，涨红跌绿，附覆盖区间说明），选股台内置「AI 研判」面板——用多 agent 协作按短线口径（波浪周期 + 情绪 + 量价 + 舆情）从候选池筛出最有潜力的几只。

当前状态是**可运行的本地闭环**：FastAPI 服务同时提供接口和页面，页面数据全部来自 DuckDB 实时查询。

## 技术架构

- Python：数据采集、因子计算、扫描、复盘与调度。
- DuckDB：单文件保存行情、资金流、交易日历、选股台账、任务状态与舆情。
- Tushare：联网更新 A 股行情、交易日历与资金流。
- FastAPI + Uvicorn：接口层与页面托管。
- HTML/CSS/原生 JavaScript + ECharts：十三个工作台页面（总览、选股台、情绪、流程、因子、台账、行情 K 线、舆情、AI 复盘、回测、自选、AI Agent、设置），Figma 风格暗色主题（圆角分块、柔和阴影、丝滑动效），缺数据一律按三态显示，不补零。
- `app/services/kline.py`：个股搜索与 K 线指标计算（MA / MACD / KDJ / RSI / BOLL），全部在后端算出、前端只画图。
- `app/services/screener.py`：全市场横截面筛选，支持涨跌幅、量比、行业过滤与排序分页。
- `app/services/watchlist.py`：自选股维护与行情列表（搜索/行业/排序筛选，添加移除幂等，股票不存在返回 404 而不是静默吞掉）。
- `engine/agents.py` + `app/services/agents.py` + `app/api/agents.py`：多 agent 短线研判（粗筛 → 方法论/舆情/走势三分析师加权 → 多空辩论），结果落 `agent_runs` / `agent_judgments` 表。
- `engine/ai.py`：AI 边界，`NARRATOR_REGISTRY` 已注册 `openai_compatible`（OpenAI 兼容 `/chat/completions`，支持任意 base_url/api_key/model）。
- `engine/backtest.py` + `app/services/backtest.py` + `app/api/backtest.py`：滚动回测与多策略对比（净值曲线、回撤、胜率、换手、成本假设、覆盖率），默认非重叠调仓口径。
- `engine/ml/`：因子机器学习体检（样本外 IC / AUC / 分桶单调性 / 过拟合缺口），诊断级结果如实展示。

## 本地运行

工作目录：

```powershell
cd C:\Users\xuan\Desktop\桌面\股票\workbench
```

启动工作台（一条命令，接口与页面同一进程）：

```powershell
C:\Users\xuan\anaconda3\python.exe serve.py --port 8788
```

启动后：

- 工作台页面 <http://127.0.0.1:8788>
- 接口文档 <http://127.0.0.1:8788/docs>

注意：默认端口 8765 在本机被 Codex WebUI 占用，工作台固定用 **8788**（8765 不要动）。`serve.py` 启动前会报告数据库状态。**数据库文件不存在时不会自动建空库**，只打印警告——凭空造空库会把「还没采过数据」伪装成「有库但全空」。

其他入口：

```powershell
python -m engine.run_scan --offline        # 只用本地数据跑一次扫描
python -m engine.close_pipeline            # 手动执行一次收盘后任务链
python -m engine.postmortem                # 回填 T+N 收益并做 IC 自检
python -m engine.review                    # 查看某交易日的复盘结果
```

## 配置

`workbench/config/settings.yaml`。三个开关默认关闭，需要时再开：

- `schedule.enabled`：收盘后自动任务链。关闭时调度器仍上报状态，只是不触发。
- `news.enabled` 与 `news.sources`：舆情采集。已接入 **TrendRadar 全网热榜**（`fetcher: trendradar`，默认 `enabled: true`）；`options.platforms` 列出全部热榜平台，改源/加源只改配置不动代码。**未配置来源时采集步明确报未配置**，不会假装采到 0 条正常新闻。热榜无权威发布时间，`published_at` 记为采集时刻并在 `raw.time_basis` 标注，快照按「采集日归属最近已收盘交易日」入库，详见 ARCHITECTURE.md「GPL 源码隔离」与「舆情快照归属」。
- `ai.enabled`：AI 复盘叙述。已实现 `openai_compatible` 提供方（OpenAI 兼容接口）；缺凭据时接口返回 `unconfigured` 并列出缺什么，不返回编造的摘要。
- `agent.enabled`：多 agent 短线研判。接入方式与 `ai` 段一致（provider/base_url/model/api_key_env，留空回退 `ai` 段）；面板参数默认候选 200 / 深度 8 / 最终 3，后端按上限钳制（200/30/10）。

凭据只从环境变量读（`TUSHARE_TOKEN`、`WORKBENCH_AI_API_KEY`），不写进配置文件。

## 部署

仅面向本地单机运行，默认监听 `127.0.0.1`。**接口层没有任何认证**，改绑 `0.0.0.0` 或对外暴露前必须先加访问控制。

## 测试

```powershell
C:\Users\xuan\anaconda3\python.exe -m pytest tests -q --import-mode=importlib --basetemp=.pytest-tmp-all -p no:cacheprovider
```

注意（本机环境约束）：

- 唯一可用 Python 是 `C:\Users\xuan\anaconda3\python.exe`（无 venv，禁止新建环境）。
- 必须加 `--import-mode=importlib`：`tests/` 与 `tests/api/` 存在同名文件（`test_ai.py`、`test_news.py`），默认模式会冲突。
- 必须加 `--basetemp=项目内目录`：系统 Temp 目录权限被拒；每次用独立名字（如 `.pytest-tmp-all`、`.pytest-tmp-all2`）。
- 测试全部使用 `tmp_path` 隔离数据库，不会读写 `data/market.duckdb`。

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
- 收盘后任务链：确认交易日 → 更新行情 → 扫描 → 回填 T+N → 采集舆情 → 生成复盘，状态落库、支持手动触发与幂等重跑。
- 舆情存储结构（来源、条目、实体关联）与去重、股票行业关联、事件分类、情绪方向、时间衰减、来源可追溯。
- 一键采集舆情：`POST /api/news/collect` 后台起采集任务，`GET /api/news/collect/{job_id}` 轮询进度；已接入 TrendRadar 全网热榜作为第一个采集器。舆情页可看条目、按来源追溯、按股票/行业过滤，并直接点按钮触发采集。
- 舆情按行业板块分组（2026-08-02）：`GET /api/news/industries` 返回当日各板块新闻数与情绪分布，`GET /api/news/industries/{行业}` 支持 `trade_date` 下钻到当日；页面以板块胶囊分组展示，命中依据（正文点名行业 / 关联股票所属行业）可追溯。聚合只认 `news_links` 里的真实关联，没有行业关联的条目如实显示「未匹配行业」，不硬塞进任何板块。
- 带三级标注的复盘装配：`fact`（事实）/ `derived`（规则计算结果）/ `unverified`（待验证判断）。
- 行情 K 线页：个股搜索、日 K 图（MA5/10/20/60、MACD、KDJ、RSI、BOLL），后端算指标、前端只渲染。
- 全市场筛选接口：`GET /api/screener`，涨跌幅/量比/行业过滤、多字段排序、分页。
- 十三页面动态读取真实数据，缺数据显示为缺失而不是补零；Figma 风格暗色主题（统一圆角令牌、柔和阴影、卡片入场动效），侧栏共享数据链路状态条（舆情/复盘/AI 三态）。
- 滚动回测与多策略对比（`/api/backtest`、`/api/backtest/compare`），默认非重叠调仓防收益虚高，覆盖率与跳过期明示。
- 因子机器学习体检（`/api/analytics/factors`）：逐日 IC、分桶收益、AUC、过拟合缺口，样本不足或模型不达标时如实报告。
- AI 接口边界，未配置时明确标记。
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
- OpenAI 兼容接入（2026-08-02）：`engine/ai.py` 新增 `openai_compatible` 提供方（httpx 调 `{base_url}/chat/completions`），凭据走 `WORKBENCH_AI_API_KEY` 环境变量 + settings.yaml `agent:` 段；可对接 DeepSeek/硅基流动/OpenRouter/本地 vLLM/Ollama。

## 待办事项

- 完成舆情来源合规调研并注册更多采集器（newsnow 等来源待补）；舆情正文级定向采集调研完成后，把真实新闻正文喂给舆情分析师。
- 给 `agent` 段配置真实 base_url/model/API Key 并开启 `agent.enabled` 后，选股台 AI 研判即可用。
- 页面固定 `run_id` 与数据截止时间的能力。
- 回测成本口径：买卖不对称暂未建模（印花税 5bp 只在卖出端，现按单一 `cost_bps` 对换手部分双边计价）。换手率已改为等权权重变化口径 `sum|w_new - w_old| / 2`。


## 独立多 Agent 页面 + 设置页（2026-08-03）

- 新增 `p11_agents.html`：AI Agent 页面，支持**个股研判**（`POST /api/agents/single`）和**选股流程**（`POST /api/agents/judge`：候选池 → 粗筛 → 三位分析师 → 多空辩论 → 最优 N 只）。
- 新增 `p12_settings.html`：API 设置页，可填写 OpenAI 兼容接口 `base_url / api_key_env / model / temperature / max_tokens / 默认参数`，落盘到 `config/settings.local.yaml`；密钥只存环境变量名。
- 舆情双源：`engine/agents.py` 的舆情分析师输入标明「TrendRadar 热榜 + TradingAgents-CN 质量评估口径（相关性/时效/可信度/情绪）」，`_news_brief` 输出带 `source / source_kind / credibility / relevance / quality_score`。
- 路由：`app/main.py` 白名单新增 `p11_agents.html`、`p12_settings.html`；`app/api/settings.py` 提供 `/api/settings` 读写。
- 测试：`tests/api/test_settings.py` 覆盖设置读写；引擎新增 `run_single` 单股研判；全量回归 424 passed。
