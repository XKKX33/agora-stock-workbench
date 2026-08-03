# Hermes 盘后复盘与舆情系统任务计划

## 目标

在现有本地量化工作台中建立收盘后自动复盘、真实舆情采集、来源追溯、AI 复盘接口和页面展示的完整闭环，并把工作台升级为可看 K 线、可筛选、可看舆情、可与 AI 复盘的九页面本地工作台。

## 阶段

- [x] 审计现有代码、运行环境、测试基线和并发改动。
- [x] 调研 GitHub 与 skills.sh 的可复用舆情方案并完成来源选择。
- [x] 完成 OpenSpec 提案、设计、规格和实施计划。
- [x] 通过测试驱动实现舆情存储、采集、去重、关联和分析。
- [x] 实现交易日收盘后调度、幂等复盘和手动触发。
- [x] 接入 FastAPI、AI 复盘边界和前端页面。
- [x] Windows + DuckDB 文件锁竞态加固，API 层测试清零失败。
- [x] 舆情快照归属修复：热榜无发布时间不再被未来数据闸门拒收。
- [x] 工作台 UI 升级：九页面（总览/选股台/情绪/流程/因子/台账/行情 K 线/舆情/AI 复盘）、暗色科技感主题、K 线与全市场筛选后端接入。
- [x] 完成全量测试、浏览器验收、Bug Review 和第一性原理精简。
- [x] 更新 README、ARCHITECTURE 并归档 OpenSpec。
- [x] 自选股功能：后端 CRUD + 行情页面板（添加/移除/搜索/行业筛选/点击跳 K 线/星标切换）。
- [x] 行业资金流向：按最新交易日聚合 + 情绪页面板（覆盖区间说明/筛选/涨红跌绿）。
- [x] UI 蓝紫渐变科技感改造，核心令牌不变。
- [x] 全量测试 397 通过 + 浏览器验收 + 文档同步。

## 完成条件

- 一个明确命令可启动工作台。
- 隔离测试库可以演示完整盘后复盘流程。
- 舆情记录具备发布时间、抓取时间、来源和原始链接。
- 同一交易日重复运行不产生重复复盘或重复舆情。
- AI 未配置时明确显示未配置；配置后只能基于已存证据生成带引用的复盘。
- 页面可看个股 K 线与指标、全市场筛选、舆情明细并可一键触发采集。
- 真实数据库未被测试和开发过程修改。

## 2026-08-03 迭代计划：独立多 Agent 页面 + 双源舆情 + 设置页

### 目标
1. 独立「AI Agent」页面：支持单只股票深度研判 + 完整选股流程（候选池→粗筛→三位分析师→辩论→决出最优3只）。
2. 舆情双源互补：TrendRadar 热榜已有数据 + TradingAgents-CN 风格舆情质量评估（相关性/时效/紧急程度/可信度），不新增采集器。
3. 独立设置页面：UI 填写 base_url/api_key/model/provider/temperature/max_tokens/默认参数，持久化到 config/settings.local.yaml。
4. 导航、路由、文档、测试同步。

### 后端改动
- engine/agents.py：抽取单只研判入口 judge_single（三位分析师+辩论+风控）
- app/services/agents.py：加载单股快照 + 创建单股 job（mode=single）
- engine/agents.py：舆情快照增强（带 source/source_name/credibility/relevance/urgency/quality_score）
- app/api/agents.py：POST /api/agents/single，支持 mode=single/flow
- 新增 app/services/settings_store.py：UI 设置持久化到 config/settings.local.yaml
- app/config.py：加载 settings.local.yaml 覆盖默认值

### 前端改动
- 新增 ui_mockups/v2/p11_agents.html + assets/js/pages/agents.js
- 新增 ui_mockups/v2/p12_settings.html + assets/js/pages/settings.js
- app-shell.js 导航加「AI Agent」「设置」
- app/main.py 白名单加 p11_agents.html p12_settings.html

### 测试
- engine/test_agents.py：单股研判、舆情软件过滤（相关性/时效/可信度）、设置持久化
- api/test_agents.py：/api/agents/single、设置读写、未配置 503
