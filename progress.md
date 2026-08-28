# 进度记录

## 2026-08-17 九步流程软门控

- 九步编排改为逐步骤隔离异常：失败记为 `warning`，依赖缺失记为 `skipped`，独立步骤继续执行。
- 全市场和候选覆盖率不足改为可审计的数据质量警告；单只股票历史、快照或智能体分析失败只排除该股票。
- 舆情、历史收益和 Pi Agent 不可用不再阻断规则扫描；智能体少返回结果按实际数量保存。
- 实验落库允许保存当前可用组；无智能体结果时只保存规则组和基准组，哈希与事务校验仍阻止无效数据写入。
- 历史补齐记录失败或占用日期后继续后续日期；Tushare `None` 响应会重试，单股历史和资金流失败不会停止剩余股票。
- 验证结果：Python `757` 项通过，TypeScript `15` 项通过，TypeScript 类型检查通过。

## 2026-08-04 一键全流程阶段

- 用户确认工作台按信号日期追踪四组实验、成交状态和后续收益。
- 正式设计状态改为已确认；自动运行仅指点击一次后的业务流程，不含开机自启或计划任务。
- 完成 AI 配置、自动流程和真实证据三路审计；当前基线为 434 项通过、0 项失败。
- 已生成详细实施计划和 OpenSpec 提案，下一步进入测试驱动实现。
- DeepSeek 配置与请求契约完成：新增测试先出现 3 项预期失败，实施后相关回归 47 项通过；规格审查和代码质量复审均通过。
- 实验表与事务存储完成：质量审查发现残缺四组可能误标成功后，新增 20 项红灯回归；修复后实验存储 35 项通过，复审通过。
- 新增界面验收范围：系统选股、完整流程、Agent 选股与单股研判按钮必须区分清楚；情绪页先复现真实 UI 问题再修根因。
- 四组构造与收益回填完成：规则、AI、混合、基准统一冻结候选池；次日开盘成交、一字涨停不可成交、四期收益和历史涨跌停价补采均已用 77 项测试锁定，质量复审通过。
- 已完成 Gemini 明亮界面只读调研：采用中性近白背景、低阴影、圆形工具按钮和淡蓝主操作；工作台保留高密度布局并新增暗夜主题。
- 按日期实验查询 API 完成：列表支持信号日、组别、股票和成交状态精确筛选，批次详情保留审计元数据，四期汇总严格区分空值与真实 0；红灯 10 项失败，实施后相关回归 75 项通过。

## 2026-07-31

- 已确认目标范围为“收盘后自动复盘 + 舆情系统”，机器学习和深度学习训练不在本阶段。
- 已确认当前项目不是 Git 仓库，不创建分支或 worktree。
- 已确认新版 FastAPI、API 测试、六个动态页面和页面控制器已经存在。
- 已确认 README 与 ARCHITECTURE 仍描述旧的静态原型状态，完成实现后需要同步更新。
- 正在并行进行代码基线审计、GitHub/skills.sh 调研和架构边界审查。
- 代码与环境审计完成：隔离测试 26 项通过、0 项失败，Conda `base` 可直接复用。
- 真实数据库当前最新扫描漏斗为 260 → 260 → 68 → 6，四个收益期限均没有已回填样本。
- 已把持久化任务、业务幂等、上海时区、市场交易日 T+N 和 AI 证据引用列为本阶段基础要求。

### 21:15 本轮实测与阻塞

- 复核确认：项目内没有 `AGENTS.md`，也没有 `openspec/` 目录与 `openspec` 命令，两项都需要用户确认如何处理。
- 复核确认：Conda `base` 已有 duckdb 1.5.5、fastapi、tushare、openai 等；舆情与调度所需的 akshare、feedparser、apscheduler 等尚未安装。
- 复核确认：真实数据库 8 张表结构与行数已记录到 `findings.md`，全程只读打开，未写入。
- 复核确认：`trade_cal` 表已可用于交易日判定，无需引入新日历源。
- 复核确认：前端 `ui_mockups/v2/` 由另一会话正在修改，本任务只新增后端与新端点。
- **阻塞（等待用户处理）**：API 账户额度耗尽，剩余 $0.0617，子代理预扣费需 $0.10，三个调研/审计子代理全部返回 403 失败。在额度恢复前无法按规则用子代理完成复杂调研、实现与 Review，因此暂停实现阶段，未写入任何业务代码。

## 2026-08-01

### 已完成（代码层面）

- **舆情链路**：`engine/news_config.py`、`news_text.py`、`news.py` 与三张表（`news_sources` / `news_items` / `news_links`）。原文标题、摘要、发布时间、抓取时间、来源、原始链接全部落库，去重、股票行业关联、事件分类、情绪方向、可信度、时间衰减齐备，每条可追溯到来源。
- **收盘后任务链**：`engine/schedule.py`（纯决策）+ `engine/close_pipeline.py`（六步编排）+ `app/services/scheduler.py`。仅在确认交易日后触发，可配置运行时间、可手动触发、幂等重跑、状态落 `task_runs` 可查询。
- **复盘装配**：`engine/review.py`，三级标注 `fact` / `derived` / `unverified`，`label_legend` 随结果返回，缺数据小节以 `available=False` + `missing_reason` 明示。读路径固定 `backfill=False`。
- **接口层**：新增 `/api/news`（4 个端点）、`/api/reviews`、`/api/ai/status`、`POST /api/ai/reviews`，均已在 `app/main.py` 挂载。`table_stats()` 补入三张舆情表。
- **AI 接口边界**：`engine/ai.py` + `app/services/ai.py` + `app/api/ai.py`，`NARRATOR_REGISTRY` 刻意留空。未配置时返回 `disabled` / `unconfigured` 并列出缺什么，不返回编造摘要。
- **`/api/sentiment` 去假**：删掉写死的「尚未接入」，改为真实读取。契约变更详见 `findings.md`「跨会话契约变更」，**需要拥有 `ui_mockups/v2/` 的会话配合改 `sentiment.js:16-17`**，本会话未动该文件。
- **启动入口**：新增 `workbench/serve.py`，一条命令 `python serve.py` 同时起接口与页面。
- **文档**：README.md 与 ARCHITECTURE.md 已按实际代码重写，删去过期的「静态原型」描述。

### 修掉的缺陷

- `close_pipeline.py` 注释声称复盘步内部会调 `run_postmortem`，实际只在 `backfill=True` 时成立；已改正并显式传参。
- `Store.existing_news_ids()` / `find_dedup_originals()` 的临时视图在查询抛错时不会摘除，会挂在连接上；已用 `try/finally` 包住。
- 自己新写的 `app/services/news.py` 里误用 `pd.isna()`：遇到 list/ndarray 单元格会返回逐元素数组，放进 `if` 直接抛 `ValueError`。已改成 `value != value`，与 `review.py` 既有写法一致。
- `tests/test_ai.py` 的 `clean_registry` fixture 在 `yield` 前就把注册表恢复了，等于没隔离；已改为只在 `yield` 后恢复。
- **静默丢失来源主页（本轮静态审查发现，两条路径同时受影响）**：`Store.news_for_link()` 的 SQL 漏选 `s.home_url AS source_home_url`，而 `app/services/news.py` 与 `engine/review.py` 两处的 `_news_row()` 都读这个列名。pandas 的 `Series.get()` 对缺失键返回 `None` 而不报错，因此每条关联舆情的 `source.home_url` 都会静默变成 `null`——按本项目约定 `null` 表示"缺失"，等于把已登记的来源主页讲成没有。这类错误不抛异常、不进日志，只能靠断言守住。已在 SQL 补上该列（一处修复覆盖两条路径），并补两条回归测试：`tests/api/test_news.py::test_linked_news_carries_source_home_url`、`tests/test_review.py::test_alignment_news_keeps_full_source_info`。

### 阻塞（未解决，影响交付判定）

- ⚠️ **测试一次都没跑过**：安全分类器持续不可用，`execute_bash` / PowerShell 已连续被拒 **19 次**，提示均为「claude-opus-5 is temporarily unavailable, so auto mode cannot determine the safety of Bash right now」。因此第 8 项要求的「报告测试通过/失败数量」**目前无法给出真实数字**，本轮不编造。分类器恢复后第一条应执行 `cd workbench && python -m pytest tests -q`。
- ⚠️ 本轮新增的全部代码（约 20 个文件）**从未被导入或执行过**，静态审查通过不等于可运行。已知风险点见下节。
- 舆情来源合规调研（第 2 项）仍被 `WebSearch` / `WebFetch` 不可用阻塞。在拿到一手证据写出 `compliance_note` 之前不注册任何采集器。
- API 额度耗尽（ERR-20260731-008）未恢复，子代理不可用，Review 只能自审。

### 未验证风险点（分类器恢复后优先确认）

1. `Store.upsert_news_*` 是否接受 `tests/api/test_news.py:_register_news` 构造的 DataFrame 列形状。
2. `news_by_trade_date()` 是否真的返回 `source_name` / `source_kind` / `source_home_url` 三个别名列——`_news_row()` 直接读它们。
3. `record_picks()` 是否接受 `test_read_only_review_does_not_write` 里的 `PREV_DATE` 行。
4. `tests/api/` 下的文件能否在没有 `sys.path.insert` 的情况下导入 `engine.db`（`tests/` 根下的文件有这行，`tests/api/` 下没有）。
5. `serve.py` 的 `sys.path` 处理是否让 `python serve.py` 在任意工作目录下都能启动。

## 2026-08-01 收尾（第二阶段）

### 已完成

- **Windows + DuckDB 文件锁竞态修复**：`engine/db.py` 的 `Store.__init__` 对 `duckdb.connect` 加 3 次短重试（20~50ms 退避），注释写明是文件句柄瞬态，重试失败仍上抛。API 层测试原 7 failed（同一竞态）→ 全部通过。
- **picks 主键迁移**：`(run_date, strategy, ts_code)` → `(as_of, strategy, ts_code)`。`_migrate_picks_pk()` 在 `ensure_schema=True` 路径自动执行：检测旧主键 → 事务内重建 → `QUALIFY ROW_NUMBER() ... ORDER BY run_date DESC` 去重 → 失败回滚。真实库 12 行去重为 6 行，数据不丢。`update_pick_return` 签名同步改为按 `as_of` 定位。回归测试 `test_picks_old_pk_migrates_to_business_key`。
- **全量测试全绿**：297 passed / 0 failed（`--import-mode=importlib --basetemp=.pytest-tmp-all3`，复跑确认）。
- **前端 UI 修复**：六个页面接入 `/api/news`、`/api/reviews`、`/api/ai/status` 三态渲染。新建 `ui_mockups/v2/assets/js/data-links.js` 共享状态读取；`app-shell.js` 侧栏加数据链路状态条；`sentiment.js` 修复读已删除的 `community_sentiment` 键的崩溃并渲染 `news_sentiment` 四类计数；`overview.js` 数据仓库面板加三行；`foundry.js` 补空扫描批次保护；`factorlab.js` ML 状态改用接口值。`tests/test_ui_pages.py` 3 项全绿，全部 JS 通过 `node --check`。
- **端到端离线演示**（临时库副本，未碰真实库）：`POST /api/pipelines` 触发盘后链，`succeeded`：ingest 离线跳过 → scan 260→68→6 → backfill 回填 0 条（缺后续交易日历）→ collect_news 采 255 条但全部被前视闸门拒绝入库 → postmortem 完成。同批次重复触发返回 200 + reused=true（幂等）。六个核心 API 全部 200。
- **OpenSpec 归档**：`workbench/docs/openspec/archive/postmarket-recap-news/`（propose/spec/tasks + CHANGELOG），归档后补记 picks 迁移、竞态修复、前端三态。

### 仍待办

- `ScanManager` 迁到统一 `TaskTracker`。
- 舆情来源扩展（TrendRadar 之外更多来源）。
- 页面固定 `run_id` 与数据截止时间的能力。
- 回测、策略对比与机器学习复核（不在本阶段范围）。

## 2026-08-01 收尾（第三阶段：工作台 UI 升级 + 舆情链路打通）

### 已完成

- **5 个子代理协同交付落地**：行情 K 线页（后端 `app/services/kline.py` + 页面 `p6_chart.html`）、全市场筛选（`app/services/screener.py` + API）、舆情页（`p7_news.html`，含一键采集按钮、来源追溯、股票/行业过滤）、AI 复盘页（`p8_ai.html`）、九页面导航 + 暗色科技感主题（`app-shell.js`、`theme.css`）、UI 设计参考文档（`docs/ui-design-reference.md`）。
- **字段不一致修复**：`kline.py` 补齐 `pre_close`/`pct_chg`；`chart.js` 的 BOLL 字段名对齐后端（`boll_upper`/`boll_lower`）；`app/main.py` 页面白名单加入 p6/p7/p8。
- **舆情链路真实故障修复（核心）**：
  - 根因：TrendRadar 热榜无权威发布时间，快照时间（8/1 17:37）晚于 `trade_cal` 最后一天（7/31），255 条全部被「未来数据」闸门拒收。
  - 修法：`engine/news_text.py` 新增 `resolve_snapshot_trade_date()`（采集日→日历内/最近已收盘日/日历末/空日历抛错）；`time_decay()` 加 `allow_future=False` 参数，快照条目钳到 0（decay=1）；`engine/news.py` 识别 `raw.time_basis == "first_seen_at_collect"` 走快照路径，不拒收不猜衰减。
  - 验证：重跑盘后链 collect_news 入库 253 条（9 转载/2 拒收），复盘 8/8 节全齐；`/api/news?trade_date=20260731` available=True + 50 items；`/api/reviews` news_highlights 有真实数据。
- **全量测试全绿**：319 passed / 0 failed。
- **服务重启并验证**：8788 端口运行最新代码，九个页面 + 核心 API（kline/search、kline bars 全指标、screener、news、reviews、ai/status）全部 200。
- **文档收尾**：README.md / ARCHITECTURE.md 更新为九页面 + kline/screener + 舆情快照归属设计；搜索记录补 UI 设计调研。

### 仍待办

- `ScanManager` 迁到统一 `TaskTracker`。
- 舆情来源扩展（TrendRadar 之外更多来源）。
- 页面固定 `run_id` 与数据截止时间的能力。
- 回测、策略对比与机器学习复核（不在本阶段范围）。

## 2026-08-01 第四阶段：机器学习复核层（`engine/ml/`）

### 交付物

- **`engine/ml/` 七个模块**：`labels.py`（T+N 标签 + 交易日历）、`splits.py`（净化滚动切分）、`metrics.py`（IC / IC_IR / AUC / 命中率 / 盈亏比 / 分桶）、`model.py`（纯 numpy 岭回归；LightGBM 缺失时**抛错而不降级**）、`dataset.py`（历史截面重放采样）、`registry.py`（产物落盘 + 三态可用性判定）、`train.py`（训练编排）。
- **训练 CLI**：`tools/train_ml.py`。新增 `--db` 参数——`Store` 是文件级读写连接（只是不执行 DDL），要保证线上库字节不变就先复制一份再指过去。本轮训练即用副本，训练前后 `data/market.duckdb` 的 mtime 均为 `Aug 1 17:46`，未被改动。
- **`app/services/analytics.py`**：`_ml_diagnostics()` 把产物自带的折明细、逐日 IC、分桶、数据集口径、参数、特征顺序转出给前端。**权重（`state` 段）不外传**：前端用不到，传了只是多一个泄漏面。
- **p4 因子实验室页体检面板**：`p4_factorlab.html` + `assets/js/pages/factorlab.js`（49 → 约 290 行）+ `theme.css` 的 `.gate-list` / `.gate-item` / `.section-label` / `.sparkline.tall`。门槛清查表逐项显示实际值与要求值（达标 ✓ / 不达标 ✕ / **算不出 ?** 三档），四张指标卡（样本外 IC、IC 信息比、AUC、过拟合缺口），ECharts 逐日 IC 柱状图（零轴居中、正负分色），分桶收益条 + 单调性判词，分折明细表（训练区间 / 隔离带 / 测试区间三列并排，是 purge 有没有真正生效的凭证），数据出处行。
- **`tests/test_ml.py`**：28 项合成数据单测，不碰数据库与网络。

### 设计取舍（三条，都选了"不好看但正确"的一侧）

1. **门槛不达标照样展示诊断**。只显示一句"模型不可用"等于什么都没说——用户需要看到 IC 是负的、过拟合缺口 0.25、分桶不单调，才知道下一步是换因子而不是继续攒数据。硬规则仍在：`availability != "available"` 时**一个预测值都不给**。
2. **"算不出"单列一档，不并进"不达标"**。样本不足导致 IC 为 `None`，与"算出来是 0"是两件事：前者该去补数据，后者该去改因子。
3. **采样上限扣掉最后 N 个开市日**（`dataset.label_cutoff`）。那几天的 T+N 还没发生，标签必然是 NaN；硬采下来会把"未来还没到"混进缺失统计，看起来像数据有洞，其实是采样口径错了。

### 实测结论：这套因子在 ret5 上没有正向区分度

真实库副本、`strong_mainup` 口径、60 个截面、17 个特征、15583 行样本（标签可用 15566）：

| 指标 | 值 |
|---|---|
| 训练 IC | +0.1397 |
| **样本外 IC** | **−0.1095** |
| 过拟合缺口 | 0.2493 |
| IC 信息比 | −0.5506 |
| AUC | 0.4689 |
| 命中率 | 0.4454 |
| 盈亏比 | 0.8036 |
| top 桶收益 | −0.0304 |
| 逐日 IC | 33 个截面中 24 个为负 |
| 分桶收益 | 随桶号**递增**（−0.0154 → +0.0024），即排序方向是反的 |

门槛正确拒绝了它：`availability = pending`，理由「样本外 IC -0.1095 低于门槛 0.02」。**这是有效结论，不是失败**——把它当成"没训成"去反复调参直到 IC 转正，就是在用测试集过拟合。产物留在 `data/models/factor_ml.json`，页面据此渲染出完整体检报告。

### 顺带修掉的四个缺陷

- **`registry.DEFAULT_DIR` 是 CWD 相对路径**（`Path("data/models")`）。从别的目录启动 uvicorn 会指到不存在的路径，于是"训好的模型"被静默报成"未训练"——不抛异常、不打日志，只是让页面少显示一整块内容。已改为锚定 workbench 根。
- **整个测试套件在 `__pycache__` 干净时无法收集**：`tests/test_ai.py` 与 `tests/api/test_ai.py` 同名（`test_news.py` 亦然），默认 `prepend` 导入模式按文件名建模块名，同名即冲突。此前一直被陈旧 `.pyc` 掩盖。已在 `pytest.ini` 加 `--import-mode=importlib`。
- **`.section-label` 被 `p8_ai.html` / `ai.js` 使用但全项目没有任何 CSS 规则**，渲染成裸文本。补进 `theme.css`。
- **接口测试依赖环境而非代码**：`test_factor_response_has_no_fake_ml_prediction` 断言 `not_trained`，但产物目录是仓库里的 `data/models`，训练过之后同一份测试会得到 `pending`。已加 `model_dir` autouse 夹具把产物目录指到空临时目录，并新增 `test_pending_model_still_reports_diagnostics` 覆盖"训过但反着排"这条真实形态：断言 pending + 诊断齐全 + 权重不外传 + 仍不给预测。

### 测试基线

**348 passed / 0 failed**（`python -m pytest tests -q -p no:cacheprovider --basetemp=.pytest-tmp-run`，319 → 347 → 348）。真实库 mtime 全程未变。

### 未验证项（如实记录）

浏览器渲染未做视觉验证：本机无 playwright，无法截图。已验证的是 API 侧——`/api/factors` 实际返回 33 条逐日 IC（24 负）、5 个分桶、3 折明细、`overfit_gap 0.249`、`monotonic false`、无 `state` 段；前端读的每个字段名都对着 `engine/ml/` 的 `as_dict()` 逐一核过（`decile_returns` 的 `{bucket, n, avg_return}`、`Fold.as_dict` 的区间端点 + 天数、`DatasetReport` 的 `label_cutoff` / `skipped_days` / `labels.needs_attention`），`node --check` 通过。像素级效果未经确认。

### 仍待办

- **回测与策略对比**：`picks` 历史上的滚动回测、净值曲线、回撤、胜率、换手率、多策略并排。这是"逼近实战"缺的最大一块。
- `engine/db.py` 872 行，超出项目自己定的 800 行上限，该拆。
- `ScanManager` 迁到统一 `TaskTracker`；页面固定 `run_id` 与数据截止时间；舆情来源扩展。
- `.ui-demo-7f3a9/screenshots*` 已过期（不含 p4 体检面板）。
- `requirements.txt` 里 `streamlit` / `plotly` 未被任何代码引用，该删；`lightgbm` / `scikit-learn` / `shap` 列了但未安装（当前跑纯 numpy 岭回归，`backend` 如实记为 `ridge_numpy`）。
- 四个 `.pytest-tmp*` 遗留目录删不掉（权限拒绝），无害。

## 2026-08-01 第五阶段：回测与多策略对比（上一阶段列为"缺的最大一块"）

### 交付物

- **`engine/backtest.py`**：滚动回测。`run_backtest(frame, horizon, strategy, top_k, cost_bps, mode)` 返回 `BacktestResult`，`as_dict()` 分四段——`metrics`（总收益 / 毛收益 / CAGR / 最大回撤及起止 / Sharpe / 胜率 / 平均换手 / 盈亏比 / 期数）、`assumptions`（成本、调仓模式、权重方式）、`coverage`（计划期数 / 已测期数 / 跳过期数 / `has_interior_gap`）、`periods` + `skipped`（逐期明细）。
- **`app/services/backtest.py` + `app/api/backtest.py`**：`GET /api/backtest`（单策略）与 `GET /api/backtest/compare`（多策略并排，只回摘要不回逐期）。期限清单、默认成本、策略清单都由接口下发。
- **第十个页面 `p9_backtest.html` + `assets/js/pages/backtest.js`**（约 450 行）：净值曲线（净/毛双线）、多策略对比图与对比表、逐期明细表、跳过期次表、八张指标卡。沿用既有暗色玻璃 + 直角 + `tabular-nums` 语言，未引入新字体或新配色。
- **测试**：`tests/test_backtest.py` 22 项（引擎口径）、`tests/api/test_backtest.py` 10 项（接口契约）。

### 口径取舍（这一阶段的核心，不是实现难度）

1. **重叠持仓谬误必须显式修掉**。`picks` 每个交易日都记一个篮子，`retN` 是**未来 N 个交易日**的收益。把每天的 ret5 连乘，等于同一笔钱被算了 5 次，净值曲线会按持仓天数整倍虚高。默认模式改为 `non_overlap`：按"可用截面"步进 N 个，每个截面是不同交易日，因此不重叠有保证。代价是只用掉 1/N 的截面——这个代价写进 `coverage` 明示，不藏。
2. **刻意不做"分批建仓"曲线**。每天投 1/N 资金的净值需要逐日盯市，而台账只存 T+N 那一个端点，中间的净值只能插值。插出来的回撤是画的，不是量的，宁可不给。
3. **"算不出"贯穿到曲线本身**。一期都没测出来时 `equity_curve` 返回**空列表**而不是 `[1.0]`，见下节缺陷一。

### 只有拿真实数据跑一遍才发现的两个缺陷（合成单测全绿时它们都在）

- **`drawdown.max` 在"从未测过任何一期"时报 `0.0`**。`equity_curve` 原先固定带一个 `1.0` 起点，`max_drawdown` 在只有一个点的曲线上算出 0.0，页面渲染成"最大回撤 0.00%"——读起来像"策略一路没跌"，实际是根本没测过。错误方向刚好是让结果**更好看**。已改为 `periods` 为空时 `equity_curve` / `gross_curve` 都返回 `[]`（在 property 层修，任何调用方都拿不到那个合成点）。回归测试 `test_unmeasurable_result_has_no_curve_and_no_drawdown`，docstring 里记明来自线上负载。
- **`horizons` 返回 `['ret1','ret10','ret3','ret5']`**。`sorted(HORIZON_DAYS)` 按字符串排，`ret10` 插到 `ret1` 和 `ret3` 中间。这个列表随接口下发、前端照着渲染下拉框，于是顺序就是错的。新增 `bt.horizons()` 按持仓天数排，service 与 api 两处调用点同步改。回归测试 `test_horizons_are_ordered_by_holding_days_not_by_name`。
- 顺带：`backtest.js` 里 `syncOptions()` 的注释写着"顺序由接口给出，页面不写死"，函数体里却有一份写死的 `["ret1","ret3","ret5","ret10"]` 本地重排，自相矛盾。接口修好后删掉本地重排。

### 测试基线

**382 passed / 0 failed**（`python -m pytest tests -q -p no:cacheprovider --basetemp=.pytest-tmp-run`，348 → 380 → 382）。真实库 `data/market.duckdb` mtime 全程为 `Aug 1 17:46`，未被改动；真实数据验证跑在副本 `.ui-demo-7f3a9/demo/market_bt.duckdb` 上。

### 如实记录的两件事

- **真实台账目前只有 2 个截面、5 条未回填的 picks**，所以 `/api/backtest` 现在返回的就是 `available: False` / `no_measurable_period`。这是正确答案而不是缺陷——曲线要等回填攒够期数才有。
- **像素级效果仍未验证**（本机无 playwright）。已验证的是接口负载逐字段对齐、`node --check` 通过、页面结构测试覆盖十个页面。
- **8788 端口上还挂着一个旧进程（PID 75640）跑的是加回测之前的代码**，对 `/p9_backtest.html` 和 `/api/backtest` 都 404。强杀该进程的命令被权限分类器拒绝（不是本会话起的、用户也没要求杀），因此没有绕过；真实数据验证另起了新实例。是否重启由用户决定。

### 仍待办

- `engine/db.py` 872 行超出项目自定的 800 行上限，该拆。
- `ScanManager` 迁到统一 `TaskTracker`；页面固定 `run_id` 与数据截止时间；舆情来源扩展。
- `.ui-demo-7f3a9/screenshots*` 已过期（不含 p4 体检面板、p9 回测页）；无 headless 浏览器，无法重截。
- `requirements.txt` 删 `streamlit` / `plotly`。
- 回测侧下一步：换手率口径目前按篮子重合度算，未计入个股权重变化；单边成本按调仓次数计，未区分买卖不对称。

## 2026-08-01 第六阶段：Figma 风格 UI 重构

### 交付物

- **`ui_mockups/v2/assets/css/theme.css` 整体升级为 Figma 风格**（只改主题文件，不动任何 HTML/JS/页面私有样式）：
  - 新增统一令牌：圆角 `--radius-lg 18px` / `--radius 14px` / `--radius-sm 10px` / `--radius-pill 999px`，柔和阴影 `--shadow-1/2`，缓动 `--ease: cubic-bezier(.22,1,.36,1)`，统一过渡 `--t`。
  - 组件圆角化：面板 18px（悬停轻抬 1px）、按钮 10px（按下回弹 `scale(.97)`）、输入框 10px（聚焦柔光圈）、导航胶囊 + 左侧圆润指示条、标签/进度条全胶囊、表格/图表/横幅/空态全部圆角。
  - 微交互：卡片入场错峰浮起动画（40ms 步进延迟）、统一 0.2~0.25s 缓动；`prefers-reduced-motion` 下动画与过渡全部关闭。
  - 品牌色与数据语义色零改动：核心令牌（`--bg/--surface/--navy/--text/--accent`）原值保留，A 股红涨绿跌仍由页面 `.up/.down` 负责。
- **文档同步**：`docs/PROJECT_GOAL.md`（完整项目目标文案）、`README.md`（十页面 + 回测/ML + 新主题）、`ARCHITECTURE.md`（回测与 ml 模块、十页面）。

### 验证

- 计算样式实测（Edge headless + CDP，页面 8791 副本库）：面板 18px 圆角 + 新阴影 + `panel-in` 0.5s 动画、导航 10px、标签 999px 胶囊、输入框/按钮 10px、表格/图表 14px、active 指示条 `left:9px top:11px` 胶囊，全部命中；p6 私有 `.panel` 背景覆盖不影响圆角。
- 全量测试回归：**382 passed / 0 failed**，与基线一致。
- 截图产物 `docs/theme_check/` 已删除。

### 如实记录

- 本机环境无 Playwright，像素级观感未人工确认；计算样式层已验证，动效（悬停/按压/入场）未做交互级回放。
- 8788 旧进程（PID 75640）仍未动，8791 为副本库验证实例。
- 遗留（与上阶段相同）：`requirements.txt` 删 `streamlit`/`plotly`；`engine/db.py` 872 行超上限；`.ui-demo-7f3a9/screenshots*` 过期。

### 补遗：直角元素全面清零（同日晚）

- 审计全部 10 个页面（headless Edge + CDP 遍历计算样式）：主体组件已圆角，仅剩两个直角元素——p6 的搜索下拉 `.suggest`（含高亮项 `.suggest-item`）与 p8 的黄色引导卡 `.guide-card`。
- 已补齐：`.suggest` 容器 `var(--radius)`、`.suggest-item` `var(--radius-sm)`、`.guide-card` `var(--radius)`；页面无横向溢出，所有 `.panel` 圆角统一 18px。
- 全量测试回归 **382 passed / 0 failed** 不变。
- 全页面截图存档于 `docs/theme_shots/`（10 张，1440x900，可自行查看）。


## 2026-08-02 第七阶段：舆情按行业板块分组

### 交付物

- **后端行业聚合链路**：`engine/db.py` 新增 `news_industry_summary()`（某交易日按行业聚合新闻数与情绪分布，板块内 COUNT DISTINCT 去重）与 `news_unlinked_industry_count()`（没有任何行业关联的条数）；`news_for_link()` 与仓储/服务层新增 `trade_date` 精确过滤，服务层 `NewsService.industry_overview()` 复用与 `digest()` 相同的三态缺因（no_source_registered / never_collected / no_news_on_date）。
- **接口**：`GET /api/news/industries` 行业板块总览（板块名 + 条数 + 正/负/中/未判定分布 + unlinked_count）；`GET /api/news/industries/{industry}` 新增 `trade_date` 参数支持按日下钻，不带时保持返回全部历史（向后兼容）。总览路由排在单行业路由之前，防止被通配吃掉。
- **前端**：舆情页「今日舆情」面板上方新增行业板块胶囊条（全部 / 各板块 + 条数 + 情绪计数），点击板块即下钻当日该板块关联新闻，列表条目展示「命中」原文片段作为关联依据；未匹配行业的条目数如实单列，不硬塞板块。采集完成与切换日期都会刷新板块条。
- **文档同步**：README（已完成列表）、ARCHITECTURE（行业聚合设计决定）、progress 本条目。

### 验证

- 新增 8 个 API 测试（板块聚合计数、情绪分布、unlinked 如实计数、trade_date 过滤、路由不冲突、三态缺因），全量回归 **390 passed / 0 failed**（基线 382）。
- 8791 副本库实例实测：`/api/news/industries` 返回 互联网 4 条 / 专用机械 1 条 / 银行 1 条、unlinked 238；板块下钻返回当日 1 条且带命中片段；`20990101` 返回 no_news_on_date。
- 页面交互实测（headless Edge + Playwright）：板块胶囊渲染正确，点击「互联网」列表从 50 条变为 4 条关联，每条带「命中 互联网」证据，无 JS 报错。


## 2026-08-02 第八阶段：蓝紫渐变 UI + 自选股 + 行业资金流向

### 交付物

- **UI 蓝紫渐变科技感**：`theme.css` 主体背景改为青蓝→紫双色 radial-gradient + 极淡科技网格（`body::before`），导航 active、按钮、页头标题渐变字、滚动条、面板悬停统一青紫渐变光效；p6/p2 页内样式同步换紫色调。**四个核心令牌（`--bg`/`--surface`/`--navy`/`--text`）未动**（test_ui_pages.py 断言锁定）。
- **自选股功能**：`engine/db.py` 新增 `watchlist` 表（ts_code 主键 + sort_order）与 `add/remove_watchlist`、`watchlist_quotes`；`app/services/watchlist.py`（列表支持 search/industry/sort/page，增删幂等，股票不存在抛 404）+ `app/api/watchlist.py`（`GET/POST /api/watchlist`、`DELETE /api/watchlist/{ts_code}`）。行情页新增「自选股行情」面板：添加输入框、代码/名称关键词筛选、行业下拉、移除按钮、点击行跳 K 线、个股信息栏星标一键切换。
- **行业资金流向**：`engine/db.py` 新增 `moneyflow_date_range` / `moneyflow_industry_summary`；`/api/sentiment` 新增 `industry_moneyflow` 段（最新资金流交易日按行业聚合净流入/大单净额/超大单净额/覆盖股票数，附 date_range/stock_count；无数据时 `availability="unavailable"` + reason）。情绪页新增「行业资金流向」面板，覆盖区间说明、行业筛选、涨红跌绿。

### 验收

- 全量回归：**397 passed / 0 failed**（基线 390）。
- 浏览器验收（headless Edge + Playwright，8791 演示实例）：p6 自选股添加（星标+输入框）/关键词筛选/行业筛选/点击行跳 K 线/星标移除/移除按钮删除共 14 项全部通过，无 console 报错；p2 行业资金流 53 行渲染、六列结构、覆盖区间说明（截至 2026-07-31 · 覆盖 205 只 · 数据区间 2026-07-17 ~ 2026-07-31）、行业筛选、刷新共 7 项全部通过，无 console 报错；10 个页面逐一加载均无 JS 错误。
- 验收中发现并修复两处真实缺陷：① 点击自选行跳 K 线后搜索框仍显示旧代码（`loadDetail` 补 `searchInput.value = code`）；② 情绪页行业资金流因 `sentiment.js` 漏导入 `formatDate` 渲染失败（补导入）。
- 截图产物：`docs/theme_shots/p6_chart.png`、`docs/theme_shots/p2_sentiment.png`（1440x900 全页，含自选股与行业资金流向面板）。

## 2026-08-02 第九阶段：选股台布局重构（自选股 + 行业资金流向搬上选股台）

### 交付物

- **选股台布局重构**（`ui_mockups/v2/p1_desk.html` + `assets/js/pages/desk.js`）：左主列（候选池 → 个股详情+决策依据 → 最近行情）+ 右栏（自选股行情 + 行业资金流向），蓝紫渐变风格与 p6/p2 一致。
- **候选池星标列**：每行 ☆/★ 一键加自选/移除，状态与右侧自选面板实时同步（`renderWatchStars()`）。
- **自选股行情面板**：输入代码/名称添加、移除按钮、点击行跳 K 线。
- **行业资金流向面板**：净流入 TOP12 + 覆盖区间说明 + 涨红跌绿。

### 修复

- 星标同步缺陷：`loadWatchlist()` 调用的是不存在的 `refreshWatchStars()`，ReferenceError 被 catch 吞掉导致星标永不刷新；改为实际定义的 `renderWatchStars()`。浏览器实测 13/13 通过。

### 验收

- 全量回归：**397 passed / 0 failed**。
- 浏览器验收（headless Edge + Playwright，8791 演示实例）：页面加载、候选池 200 行 8 列、星标列 200 个、星标加入自选并变 ★、输入框添加、移除按钮、点击行跳 K 线、行业资金流 12 行渲染、覆盖区间说明、净流入有值、详情联动共 13 项全部通过，无 console/pageerror。
- 截图产物：`docs/theme_shots/p1_desk.png`（1440x900 全页，含自选股与行业资金流向面板）。## 2026-08-02 第十阶段：独立自选页 + 多 agent 短线研判 + OpenAI 兼容接入

### 交付物

- **多 agent 研判引擎**（`engine/agents.py` + `app/services/agents.py` + `app/api/agents.py`）：两级混合编排——① 粗筛：方法论文本 prompt 对候选池全体打分；② 深度：前 N 只并行跑 方法论分析师（波浪/情绪/量价）、舆情分析师（个股+行业新闻按时效与来源可信度过滤）、走势分析师（MACD/KDJ/RSI/BOLL + 资金流），加权 0.4/0.3/0.3 + 决策汇总；③ 最终 M 只做多方/空方陈述 + 中性风控辩论，输出短线潜力排名。提示词全部按短线操作口径重写，不引入 LangGraph，借鉴 TradingAgents-CN 的分析师/辩论/风控协作思路。参数（候选默认 200 / 深度 8 / 最终 3）后端按上限钳制（200/30/10），同 as_of + 同参数成功批次幂等复用。
- **OpenAI 兼容接入**（`engine/ai.py`）：`NARRATOR_REGISTRY` 注册 `openai_compatible`（httpx 调 `{base_url}/chat/completions`），settings.yaml 新增 `agent:` 段（provider/base_url/model/api_key_env/temperature/max_tokens + 默认参数与上限），凭据只从 `WORKBENCH_AI_API_KEY` 环境变量读；未配置时 `/api/agents/status` 返回 disabled/unconfigured + 缺什么，`POST /api/agents/judge` 返回 503（服务暂不可用），绝不编造。
- **结果落库**：新表 `agent_runs`（run_id/as_of/参数/状态）与 `agent_judgments`（股票/阶段/分数/理由/风险/原始 JSON），接口 `GET /api/agents/status`、`GET /api/agents/candidates`、`POST /api/agents/judge`、`GET /api/agents/jobs`、`GET /api/agents/jobs/{id}`、`GET /api/agents/results`（只含成功批次，可按 as_of 过滤）。
- **AI 研判面板**（选股台 p1，`desk.js`）：候选/深度/最终三个参数面板可改并存 localStorage，强制重跑勾选，分阶段进度条，结果卡片（排名/核心逻辑/风险/数据来源引用 + 一键加入自选 + 看 K 线 + 分析师详情与多空辩论），最近研判列表；AI 未配置时按钮禁用并显示原因。
- **独立自选页**（`p10_watchlist.html` + `watchlist.js`）：自选列表 + 最新行情（收盘/涨跌幅/行业/最新交易日）+ 搜索/行业筛选 + 添加/移除 + 点击行跳 K 线 + 空态引导 + 概览统计卡；侧栏导航新增「自选」（行情节后），p6 自选面板工具条加「管理自选」链接。
- **测试**：修正 `tests/test_ai.py` 的 registry 断言（默认含 `openai_compatible`）；新建 `tests/api/test_agents.py`（status disabled、judge 503、jobs 空列表、job 404、results 按 as_of/status/limit 过滤）；新建 `tests/test_agents.py`（引擎层 JSON 容错、配置钳制、0.4/0.3/0.3 加权、落库幂等）。

### 验证

- 全量回归：**419 passed / 0 failed**（基线 397）。
- 浏览器验收（headless Edge + Playwright，8791 演示实例）：选股台 AI 研判面板渲染正确（未启用文案、`#agent-run` 禁用、默认值 200/8/3、侧栏含「自选」），无 console 报错；p6「★自选」链接点击后正常跳到 p10 自选页；`/api/agents/status` 返回 `availability=disabled` + 原因（AI 未启用，正确三态）。
- 截图产物：`workbench/.ui-check-desk.png`（AI 研判面板）。

### 如实记录

- AI 未配置，面板如实禁用、接口如实报 disabled；配置好 `agent.enabled + base_url + model + WORKBENCH_AI_API_KEY` 后才会启用。
- 8788 旧进程（PID 75640）仍未动，8791 为副本库演示实例（PID 25140，已用新代码重启）。
- 舆情分析师沿用 TrendRadar 现有数据做过滤评估，不新增采集器；等定向采集合规来源调研完成后扩展。


## 2026-08-03 第十一阶段：独立多 Agent 页面 + 双源舆情 + 设置页

### 交付物
- 独立 `p11_agents.html` + `agents.js`：个股研判（POST /api/agents/single）+ 选股流程（POST /api/agents/judge），共用进度/结果/最近记录；侧栏新增「AI Agent」。
- 独立 `p12_settings.html` + `settings.js`：`/api/settings` 读写 `config/settings.local.yaml`，UI 可配置 base_url、api_key_env、model、temperature、max_tokens、默认参数；密钥不落明文。
- 舆情双源：`_news_brief` 输出带 source/source_kind/credibility/relevance/quality_score，舆情分析师 prompt 明确「TrendRadar 已入库 + TradingAgents-CN 质量评估口径」。

### 验证
- 全量回归：**424 passed / 0 failed**。
- 浏览器验收（headless Edge，8791）：p11/p12 页面渲染正常，导航含「AI Agent」「设置」，/api/settings 返回默认值和可用性提示。

## 2026-08-03 第十二阶段：换手率口径修正 + 拆分超限的 db.py

### 换手率漏收成本（真实缺陷，不是口径偏好）

`backtest.py` 原本用 `turnover = 1.0 - kept / len(codes)`，分母是**新**篮子。篮子变大或不变时它恰好等于权重口径，**变小时会错**：5 只缩到 3 只且 3 只全留仓，算出 `1 - 3/3 = 0`，等于把清掉的 40% 仓位白送、不收任何成本。错误方向又是让净值**更好看**。原有换手测试两期篮子同样大小，所以这个方向一直没被覆盖。

改为等权**权重变化的一半**：`sum|w_new - w_old| / 2`（新增 `_turnover()` helper）。除以 2 是因为一次调仓卖出额与买入额相等（卖旧的钱买新的），只算一边才是"这次动了多大比例的仓"，`cost_bps` 按双边计价。这个口径对两个方向都成立（5→3 与 3→5 都是 0.4），还额外抓到重合度公式**结构上抓不到**的一件事：留仓的票从 20% 补到 33% 也是真实交易。首期建仓记满仓 1.0（权重公式只算出 0.5，因为只有买没有卖），偏保守。

新增两条回归测试：`test_shrinking_basket_still_charges_the_exits`（缩仓方向，就是漏掉的那条）与 `test_growing_basket_charges_the_same_as_the_mirror_shrink`（方向对称）。`as_dict()` 的 `cost_note` 同步改写，并明示**买卖不对称（印花税只在卖出端）仍未建模**——拆开买卖腿会把单个 `cost_bps` 旋钮换成一对，那是接口变更而非缺陷修复，留给用户定。`README.md:125` 原先把已实现的两项列为待办，收窄为只剩买卖不对称。

### 拆分 db.py（1182 行 → 672 行）

按**表族**拆成 mixin 放在同级模块，不动调用侧：
- `engine/schema.py`（253 行）：`_SCHEMA` / `_PICKS_SCHEMA` 两个 DDL 常量。
- `engine/db_news.py`（302 行）：`NewsAgentMixin`，只碰 news_sources / news_items / news_links / agent_runs / agent_judgments 五张表的 17 个方法。
- `engine/db.py` 672 行，`class Store(NewsAgentMixin)`。

**为什么用 mixin 而不是组合**：全项目 30 处写 `from engine.db import Store` 并直接调 `store.news_by_trade_date(...)`。组合（`store.news.by_trade_date`）要改 30 个调用点和一批测试，那是接口变更，不是拆文件。mixin 让 `Store` 的方法集合与拆分前完全一致。

验收方式是与**拆分前的 git blob** 逐项比对，而不是只比内存里的模块：两个 DDL 常量字节级相同（8683 / 493 字符），19 张表拆前拆后一致；`Store` 的方法集合 50 个对 50 个，无缺无增。

### 验证
- 全量回归：**429 passed / 0 failed**（基线 427 → +2 换手率回归测试）。
- 顺带修掉一条**先前就有**的测试缺陷：`test_calendar_lookahead_covers_longest_horizon` 把 `_HOLIDAY_BUFFER_DAYS`（14 个**自然日**）从**工作日**计数里减，单位混用且余量恰好为零，跨过午夜后就红。先按一整年逐个起始星期扫过 `_calendar_lookahead_end()` 确认实现是对的（最坏情形 24 个工作日对需要的 10 个交易日，余量 +4），才断定是测试的错——把缓冲换算成工作日并对跨度向下取整。
- 三个模块均在 800 行上限内。

### 拆分 app/services/agents.py（883 行 → 589 行）

同样的口径，但拆分依据是**职责**而不是表族——这个文件里两件事本来就不相干：任务编排（抢占 / 心跳 / 落终态 / AI 客户端 / 结果落库）和数据装配（库里的行情 → 喂给模型的紧凑快照）。

- `app/services/agents_data.py`（327 行）：`AgentDataMixin`，`_build_pool` / `_compact_row` / `_macd_state` / `_load_snapshot` / `_stock_brief` / `_daily_brief` / `_weekly_brief` / `_moneyflow_brief` / `_news_brief` 九个方法，加上只被这一族用到的 `_round`（29 处调用全在簇内）。
- `app/services/agents.py` 589 行，`class AgentJudgeManager(AgentDataMixin)`。

搬动后 `numpy` 在 `agents.py` 里再无引用（两处 `np.nan` 都在 `_daily_brief` 内），已删掉该 import；`_compact_row` 里原本硬写的 `AgentJudgeManager._macd_state(...)` 同步改成 `AgentDataMixin._macd_state(...)`——跨类硬引用是这种搬动最容易留下的哑弹，它不会在 import 期报错，只在真正跑粗筛时才炸。

外部只有 `app/api/agents.py` 与 `app/main.py` 两处 import `AgentJudgeManager`，测试没有直接碰任何私有方法，因此调用侧一行未动。验收同上：与拆分前的 git blob 比对，方法集合 25 个对 25 个，无缺无增。

### 验证（拆分后）
- 全量回归：**429 passed / 0 failed**，与拆分前基线一致。
- 至此项目内已无超过 800 行的模块。

## 2026-08-03 第十三阶段：把两个"看起来更好"的指标口径修回来

换手率那条缺陷的错误方向是让结果更好看。顺着这个方向把余下的"均值/波动"型指标逐个过了一遍，又找到两处。

### 夏普没说自己按无风险利率 = 0 算

`_sharpe` 是 `mean / std * sqrt(244 / 持仓天数)`，分子是收益均值本身而不是超额收益——真实夏普比这个低。没减不算错（回测跨期可能横跨利率变动，取哪个基准都是又一个假设），**没说**才是问题：`cost_note` 与 `mode_note` 都在 `assumptions` 里明写，夏普的口径一个字没有。项目自己的规矩是假设要写出来、不能当事实下发。

补 `sharpe_note` 到 `as_dict()["assumptions"]`（无风险利率取 0、年化假设各期独立同分布、期数 < 4 不给值），`_sharpe` 补 docstring，回归测试钉住 `"无风险利率 = 0"` 在下发内容里。前端 `renderAssumptions()` 是逐 key 显式调 `kv()` 的，不是通用遍历，所以另加了一行 `kv("夏普口径", ...)`——只改后端会让这段说明落在 payload 里没人看见。

### IC IR 两天就给值，且分母用了总体标准差

`pipeline_0803.json` 里有一条 `ic_ir: 4.8628` 配 `n_samples: 11`。真实股票因子的 IC IR 大致在 0.5 量级，4.8 不可能是真的——反推分母约 0.046，是两三天的日 IC 算出来的标准差。两个独立实现（`engine/postmortem.py` 与 `engine/ml/metrics.py`）都有同样两个毛病，方向都是把数值抬高：

1. **只要 ≥ 2 天就给值**。IR 的分母是 IC 的**跨日**标准差，两三天算出来没有统计意义，而且偏小。两处都加 `MIN_DAYS_FOR_IC_IR = 4`，与 `backtest.MIN_PERIODS_FOR_SHARPE` 同值同理——都是"均值 / 波动"型指标，几个点的波动算不出来。
2. **分母用 `ddof=0`**。这些交易日是总体的一个样本，不是总体本身；`ddof=0` 把分母算小、IR 算大。四个点上两种口径差 15%，方向恒定。两处都改 `ddof=1`。

顺带把天数下发出去：`HorizonStats` 加 `n_days` 字段（数的是**有效** IC 天数，横截面太薄算不出 IC 的那天对标准差也没贡献），`stats_as_dict()` 一并带出；前端 IC IR 卡片的说明里显示天数，`ai.js` 的标签表加 `n_days`。不给值的时候页面显示"算不出"，给值的时候旁边有天数可以自己判断——比单独甩一个 4.863 出来诚实。

### 验证
- 全量回归：**431 passed / 0 failed**（429 → +1 夏普 +1 IC IR）。
- 已有的 `test_ml.py:291`「IC 无波动时 IR 为 None」这条仍通过，但**通过的原因变了**：那个 fixture 只有两天，现在先被天数闸门拦下。断言留着，注释改成如实说明——两个原因都会给 None，别把它当成"无波动"那条的证据。
- 起过一条 `test_ic_ir_uses_sample_std_not_population_std`，写完发现 fixture 本身是错的：按天缩放 label 不改变日内相关性，四天的日 IC 全是 1.0、标准差为 0，断言会在 `None` 上炸。已删，`ddof=1` 由上面那条在真有波动的日 IC 上钉住。

## 2026-08-03 第十四阶段："没数据"被讲成了对大盘的判断

同一类缺陷继续往服务层查。`app/services/analytics.py` 的 `_market_stage` 按初筛通过率给市场定性，空明细行时 `passed_ratio` 记 `0.0`，`0.0 < 0.15`，于是页面显示**结构偏弱**——扫描一只都没扫出来，被渲染成了对大盘的结论。

`if len(rows)` 这个守卫说明作者预见到了空表，缺陷在于兜底给的是 `0.0` 而不是"算不出"。空表真实可达：`latest_scan_rows()` 只在**没有任何 run** 时抛 404，run 有行而 `scan_rows` 为空是另一回事（初筛全否、或 `record=False` 只留了表头）。

改成与同文件 `_industry_moneyflow` / `_return_summary` 一个口径：空表或缺 `passed` 列时返回 `availability="unavailable"`、`label=None`、`passed_ratio=None`、`reason`；有数据时照常给结论，并补 `sample_count`——一个结论建立在多少只股票上，是判断它有多重的必要信息。

麻烦的地方是**通过率 0% 和"一只都没扫出来"在数值上没法区分**，只能靠 `availability` 分开。所以测试里专门有一条对照：`passed=[False, False]` 要给 `available` + `结构偏弱`，证明新加的 `unavailable` 没把正常的 0% 一起误伤。

前端两个消费方都改了。`sentiment.js` 原来是 `textContent = data.market_stage.label`，`label` 变 `None` 会直接渲染成空白（比"结构偏弱"好，但仍然看不出为什么），抽出 `renderMarketStage()`：算不出显示"算不出"+原因，算得出显示结论+样本数；`p2_sentiment.html` 的 `metric-note` 加了 `id` 才能被写。`news.js` 概览卡原来 `stage.label || "—"`，改成"算不出"并在 note 里带出 `reason`。

### 验证
- 全量回归：**433 passed / 0 failed**（431 → +2）。


## 2026-08-10 防前视闸门收口：命令行入口 + 旧台账回填

接口层的可见日闸门已经生效，这一轮把两个绕过它的口子补上。两处都是"闸门算了但没用上"，不是新功能。

### 收盘任务链把可见日算了却没传给扫描

`engine/close_pipeline.run_close_pipeline` 的命令行入口先用 `require_visible_as_of` 算出可见日，传进来的 `trade_date` 只被当成"闸门目标"用于日志比对，第 2 步 `run_scan(...)` 没传 `as_of`。而 `run_scan` 不收 `as_of` 时按纯数据口径取"本地/在线最新交易日"——正好是隐藏窗口里的那天。结果整条链的截面、舆情、复盘全部落在隐藏窗口内。

改成把 `trade_date` 原样传给 `run_scan(as_of=...)`。随之两处死代码清掉：截面不再可能与闸门目标不一致，那段 `logger.info("实际批次 %s 与闸门目标 %s 不一致")` 分支不可达；扫描步 `data` 里的 `gate_target_date` 恒等于 `as_of`，无消费方，删。

### 旧台账回填用隐藏窗口的收盘价填 retN

`engine/postmortem.backfill_returns` 判定"未来到了没"只看 `store.latest_date()`（已入库行情最大日期）。库里的行情是一直摄取到最新的，所以隐藏窗口内的收盘价对它完全可见——历史选股的 `ret1/ret3/ret5/ret10` 会被当时看不到的价格填上，而这些字段正是 IC 自检、回测、因子体检的标签来源。

加 `visible_max` 参数，口径与 `engine/returns.py` 的 `_future_status` 完全一致：先判隐藏（`future_not_visible`）、再判数据缺失（`future_not_reached`），顺序不能反，否则本地已有行情的隐藏日会被直接读走。两类都是"正常等待"，一起从 `needs_attention()` 里排除（新增 `_WAITING_REASONS` 常量），避免把"还不该看"误报成"缺数据要人处理"。

四个调用方全部接上：在线扫描内部回填传截面日、收盘链条回填步传截面日并在 step data 里带出 `visible_max`、`postmortem` 命令行固定取可见日、`review` 命令行的 `--trade-date` 超限直接拒绝（复盘会把当天结论和收益回看摊开，和接口层同一条纪律）。`build_review` 的 `visible_max` 只是透传，堵住 `backfill=True` 这条公开路径绕过闸门。

`visible_max=None` 保留为"调用方不设上限"，只给纯历史离线补全用，与 `returns.py` 同构。

### 验证

- 反证测试能抓 bug：合成库上不传 `as_of` 跑 `run_scan`，截面回到最新交易日 `20250812`（可见日是 `20250715`）——新测试断言的正是这个差别。
- 新增 3 条回归：链条截面必须等于传入的可见日、回填步必须带可见上限、隐藏窗口内目标日记 `future_not_visible` 且保持 NULL（同一用例带对照组：不设上限时同一批数据能填上，证明是闸门拦的、不是缺数据）。
- 真实库命令行实跑：`review --trade-date 20991231` 被拒并报出可用最新日 `20260706`；`review --trade-date 20260706` 正常出结果；`postmortem`（跑在库副本上，未动真实库）摘要里 `visible_max` 为 `20260706`，有 1 条目标日落在隐藏窗口被记 `future_not_visible`。
- 全量回归：**676 passed / 0 failed**（673 → +3）。

## 2026-08-10 闸门补到因子训练入口

命令行四个入口收口后，把 `latest_date()` / `latest_confirmed_date()` 的调用点又过了一遍。三类分开判：

- 不受限，不动：`app/services/screener.py:80`（全市场条件筛选）、`app/services/kline.py:195`（K 线）、`app/repositories/market.py:36`（库状态展示）。结果不落台账、不进实验与评估，README 里「行情查询不受影响」说的就是这几处。
- 闸门在调用方，不动：`engine/experiments.py:360` 的 `required_entry_limit_dates` 用 `latest_date()` 判「买入日到了没」，一键流程在外面又按 `date <= visible_as_of` 过滤了一遍（`app/services/one_click.py:275-279`）。补采涨跌停数据本身不是前视，用不用由回填侧的可见上限决定。
- 真漏，已修：`engine/ml/dataset.py` 的 `build_dataset` 不传 `end` 就取库里最新交易日，而唯一的训练入口 `tools/train_ml.py` 从来没传过——因子体检的训练样本一直覆盖隐藏窗口。

### 为什么训练里的泄漏更要堵

`build_dataset` 本来就会把采样上限往前退 N 个开市日（`label_cutoff`），那是为了让标签的 T+N 已经发生，不是可见性判断：库里行情已经摄取到最新，退 N 天之后仍然落在隐藏窗口里面。结果是模型用「当时还没落地的行情」拟合，再拿它给可见日打分——泄漏发生在训练里，样本外 IC 看着还挺正常，事后从指标上分辨不出来。

修法沿用既有纪律，闸门只放在入口，engine 层保持纯数据口径：`train_from_store` 加 `end` 参数原样透传 `build_dataset`；`tools/train_ml.py` 先 `resolve_window` 再 `require_visible_as_of`，默认截止日就是可见日，新增的 `--end` 走 `ensure_visible`，超限直接退出。两处都判会出现两套口径，测试里专门钉了一条「不传 `end` 仍取库里最新日」防止有人把闸门塞进 engine。

`DatasetReport` 加 `end_day` 并写进产物 `dataset` 段：一份模型看到哪天为止，是事后判断它能不能用的必要信息，原来只有 `label_cutoff`，看不出截止日。产物 `dataset` 是自由字典（`registry.py:158` 直接 `dict()` 透传），加字段不影响读取方。

### 顺带修掉一个从没跑通的默认值

`tools/train_ml.py --strategy` 默认写的是 `"default"`，但 `config/strategies/` 下只有 `strong_mainup.yaml`——不带 `--strategy` 跑必然 `FileNotFoundError`，是实跑验证时撞出来的。改成 `settings.engine.default_strategy`，与 `run_scan.py:569` 同一口径。文件里上一条注释记着同类问题（`settings["storage"]` 写错但因为一直显式传参而没暴露），同一个入口第二次出现「默认分支从没被走过」。

### 验证

- 新增 3 条回归（`tests/test_visibility.py`）：训练截止日的三个分支（默认取可见日 / 显式合法照用 / 显式越界抛 `lookahead_blocked`）、`end` 压住采样上限、不传 `end` 保持纯数据口径。
- 真实库副本实跑（未动真实库）：默认跑法 `dataset.end_day` 为 `20260706`（可见日），库里最新是 `20260812`；`--end 20260812` 与 `--end 20991231` 都被拒并报出可用最新日 `20260706`。
- 全量回归：**679 passed / 0 failed**（676 → +3）。第一次跑漏了清空 `WORKBENCH_AI_API_KEY`，6 条 AI 用例真的打到线上模型接口拿了 404；带空凭据重跑全绿。（这条环境依赖已在同日根治，见下一节末尾，现在带凭据跑全量也全绿。）

## 2026-08-10 实验台账与收益收敛为一份口径

`experiment_decisions` 上原来挂着 16 个旧列（`entry_date` / `entry_price` / `entry_status` / `entry_reason` / `ret{1,3,5,10}` 及其 `_target_date` / `_status` / `_reason`），和独立的 `experiment_returns` 并存。两套口径同时存在，页面读哪一套都可能和另一套对不上，旧列在最后一个写入方被移除后又永远是 NULL，台账上就是一片空白列。

### 顺带撞出来的真实错误

旧回填在涨停封板、缺涨跌停价这两种「买不到」的情况下，仍然把当天开盘价写进了 `entry_price`。结果是「到底买到没买到」分不清：有价格的行看起来都像成交了。现在 `entry_price` 只在真的买到时才有值，买不到留空并由 `status` / `reason` 说明原因（`limit_up_locked` / `limit_price_missing` / `entry_bar_missing`）。

### 改动

- 库层：旧列从 DDL 删除，`Store(ensure_schema=True)` 每次开库执行 `_drop_legacy_decision_columns()`——只 DROP 还残留的旧列，一个事务、幂等，决策数据不动。
- 接口层：`GET /api/experiments` 每行挂 `entry_status` 与 `returns.{horizon}`；汇总统一走 `GET /api/returns/summary`，并支持和台账同一套筛选（`as_of` / `group_name` / `ts_code` / `entry_status`）；原 `/api/experiments/summary` 删除。
- 前端：p5 台账页只读上面两个接口，`STATUS_LABELS` / `HORIZON_LABELS` 提供全量中文映射，算不出的格子显示「—」并把原因写进 `title`，绝不退化成 0。

### 验证

- 真实库副本 + 两个合成批次冒烟：已成交、涨停封板、缺涨跌停价、从没算过四种状态在接口与页面上各自成立（AI -0.74%、混合 -5.62% 有值，规则与基准为「—」并带原因）。
- 浏览器实跑 p5（库副本，未动真实库）：筛 `filled` 与 `entry_unavailable` 时，四组卡、十期收益带、明细表和「当前筛选 N 条」同步收敛，覆盖率与状态占比一致，错误横幅为空。
- 全量回归：**679 passed / 0 failed**。

### 顺带修掉测试对开发机环境的依赖

`tests/api/conftest.py` 的 `offline_settings` 夹具现在按配置里声明的 `api_key_env` 逐个 `delenv`（`ai` 与 `agent` 两段）。原来「没配凭据要报 unconfigured」的 6 条用例只有在开发机没导出过 `WORKBENCH_AI_API_KEY` 时才绿，其中 AI 复盘用例还会真的去打线上模型接口（拿到 404）。测试断言的是代码行为，不该由开发机环境决定；需要凭据的用例自己 `setenv` 即可，夹具先跑不会被覆盖。

## 2026-08-11 流程页收尾

- 九步成功结果补齐中文 `detail`，说明直接使用该步真实日期、数量和状态；失败步骤从任务错误记录显示原因，不再出现「这一步没有留下说明」。
- 历史补齐的舆情步骤保持后端 `skipped` 语义，前端明确映射为「未执行」，不再误标绿色成功。
- 副本库浏览器验收：失败于完整性时，步骤卡显示「daily 行数未超过完整收盘阈值 1000」；历史任务、四组总数和 260 条基准分页正常，无页面重叠。
- 定向回归 76 passed / 0 failed；全量回归 682 passed / 0 failed。
- 真实模型最小请求与真实库流程当时未执行：当时未设置 `WORKBENCH_AI_API_KEY`，且没有用户对真实库写入的明确授权；OpenSpec 保持 apply 状态，不提前归档。

## 2026-08-11 真实模型联调

- 环境已出现 `WORKBENCH_AI_API_KEY`，但默认 `base_url` 缺少 `/v1`，真实请求落到网页路由并返回 `404`；已把 AI 与 Agent 地址统一修正为 `https://grok.xuan.christmas/v1`。
- TDD 红灯为 2 failed / 22 passed；修正配置后定向回归 24 passed / 0 failed。
- 修正路由后，最小请求和 `/v1/models` 都返回 `401 invalid_api_key`；当前密钥格式正常但服务判定无效，未完成真实模型验收。
- 未运行真实库一键流程，OpenSpec 保持 apply 状态。
- 最终全量回归 682 passed / 0 failed；规格复核通过，质量复核指出的测试重复断言与设置页文档漂移已修正并复审通过。
- 用户改为把有效密钥保存在 Git 忽略的 `workbench/.env`；新增统一加载器并由 `serve.py` 启动时调用，Windows 用户环境中的同名值已移除。
- `/v1/models` 确认模型 ID 为 `grok-4.5`，一次无重试最小 JSON 请求成功；真实模型验收已完成，真实库流程仍等待明确授权。
- `python-dotenv` 已恢复为正式依赖，启动顺序测试可捕获“先导入配置、后加载 `.env`”的错误；最终全量回归 685 passed / 0 failed，独立复审通过。
## 2026-08-17 真实库生产式验收

- 已按用户授权创建真实库原样备份：`workbench/data/market.duckdb.bak-20260817-184226-production-acceptance`；备份创建时源库与备份均为 59,256,832 字节，SHA-256 均为 `aa9e3a7c7642b0b2acd95499e0c089c70de417dd9acf6c853825b49665da49b5`。
- 前端真实页面验收：14 个页面均成功加载，页面 API 请求均返回 200；修复了回测成本/成交规则控件未接通、选股扫描缺少 `request` 导入、流程终态按钮不恢复三个现有功能问题。
- 修复后全量回归 **749 passed / 0 failed**，全部页面脚本 `node --check` 通过。
- 真实库在线九步流程执行两次，均在 `market_data` 失败：第一次为 Tushare `moneyflow` 连续失败，第二次为候选股票历史窗口不足；两次均未进入 scan/news/Agent/persist_experiment，未生成成功实验决策。
- 验收期间真实库确实写入了在线摄取数据与失败任务记录；复核时 `task_runs=25`、`daily=217088`、`daily_basic=38711`、`moneyflow=10992`、`trade_cal=453`、`news_items=801`、`picks=38`、`experiment_decisions=0`、`experiment_returns=0`。当前数据库 SHA-256 为 `c327c164c3f1556f45e4e583084844a114c7b2ec8b0b80e1acdbb74dc3243e08`。
- 当前生产式验收结论：前端现有功能已完成真实请求级验收；完整九步闭环**未成功**，阻塞点是在线市场数据完整性/接口条件，不是 Agent 配置或前端按钮。
- 真实页面进一步点击验收：p1「离线扫描」真实提交 `/api/scans`，任务 `c8752b8220ec48dba071e36b029f2555` 成功（260 候选、259 评分、31 通过、6 最终），页面显示 `succeeded`，两个扫描按钮恢复可点击。
- 最终数据库复核：数据库与备份均可读；备份哈希保持不变。当前库 `task_runs=26`，两次生产流程任务均为 failed，新增的扫描任务 succeeded；`experiment_decisions=0`，未伪造成功实验数据。
- Tushare 重试策略已按要求从 3 次调整为 5 次，新增红灯测试验证前 4 次失败、第 5 次成功，摄取层回归 **31 passed / 0 failed**。
- 使用五次重试重新执行真实在线九步流程，任务 `6dadb5ebc76d43809a45ceadf977182b` 仍在 `market_data` 因「候选股票历史窗口不足」失败；这次已不再是资金流接口重试问题，说明五次重试已越过第一道临时接口失败，但历史数据完整性仍不足。
- 实验决策仍为 0，未绕过历史窗口检查，也未伪造后续流程结果。

## 2026-08-17 九步软门控与真实闭环完成

- 保留每只股票 150 根历史标准；`001399.SZ` 仅有 `17/150` 根，单独排除后其余 259 只继续评分。
- Pi Agent 改为逐只严格校验；2 只非法分析被记录并排除，18 只有效结果继续完成辩论和最终筛选，不合成模型结果。
- 真实任务 `d442e819cd7a4443b1d90e060e604051` 成功：规则 3、AI 3、混合 3、基准 20 全部落库，Agent 报告可用。
- 数据库只读核对通过：`experiment_runs`、`agent_runs`、`scan_runs` 均为 `succeeded`，3 条最终判断和 29 条四组决策存在，错误字段为空。
- Review 发现 `to_legacy_output()` 的单股字段被扫描批次字段误覆盖并引用未定义 `run_date`；已用红灯测试复现后恢复旧契约。
- 终态语义复查后补强：九步仍全部尝试，但若最终没有完成实验原子提交，任务明确标记失败；行情实际入库行数少于交易日确认行数时记录完整性警告。
- 最终验证：Python 759 passed / 0 failed；Pi Agent 17 passed / 0 failed；TypeScript 类型检查通过；前端脚本 18 passed / 0 failed；离线扫描文件手动入口 4 passed / 0 failed。
- OpenSpec `one-click-experiment-tracking` 已满足真实库验收条件并归档。

## 2026-08-18 前后端逐功能验收接续

- 接手原验收任务，从自选股返回值修复处继续；没有重启原线程，也没有重复启动 Agent。
- 自选股真实接口闭环通过：首次新增 `added=true`、重复新增 `added=false`、首次删除 `removed=true`、重复删除 `removed=false`，数据库数量 `0 → 1 → 0`。
- 设置接口读写回环通过，未保存密钥明文；保存后配置仍保持原值。
- 14 个页面返回 `200`；核心只读 API 21 个全部返回 `200`；健康检查显示数据库 ready。
- 全量回归 **764 passed / 0 failed**；全部前端 JavaScript 文件 `node --check` 通过。
- 验收结束时自选股数量为 `0`，没有留下测试数据；已有 Agent 历史任务为 `5 succeeded / 16 failed`，流程历史为 `3 succeeded / 10 failed`，未新增任务。
- 本轮没有重新执行九步在线流程，避免把已经完成的真实流程结果和本次页面验收混在一起。

## 2026-08-23 辩论链路改造收尾：混合组去回声、看空不进买入名单

### 修掉的三个真缺陷

- **混合组退化成规则组副本**。`deep` 阶段的 `score` 就是候选传入的规则分（Pi 侧
  `deep.push({... score: item.score ...})`，`item` 来自 `coarse`），辩论评分只存在于 `final`。
  混合组原先取 `deep` 的 score 当 `ai_score`，于是 AI 百分位与规则百分位恒等，五五权重加了
  等于没加——线上实测 hybrid 三只与 rule 三只完全相同、`ai_score == rule_score`，三组对比里
  有两组是同一个东西。改用 `final` 的辩论评分，理由字段同步按 final 取（`thesis/verdict/action`），
  否则 `points/analysts` 一个都不存在、理由恒空。只有辩成的股票进混合组：给没辩成的补分就是
  编造 AI 判断。
- **风控判「看空」的股票被当买入决策落库**。名单语义是次日开盘买入并回填 T+N 收益，拿去和
  规则组比谁赚得多。实测一次真实运行 20 只里 19 只看空（涨停后追高），AI 组前三有 2 只是
  看空的——那组收益既不代表 AI 判断也不代表任何可执行策略。修法：Pi 侧在取前三**之前**按
  方向过滤（先截断再过滤等于让看空的把看多的挤出名单）；全部看空返回**空名单**而不是报错
  （模型认真跑完了，结论就是这批都不该追，是有效结论），「一只都没辩成」才抛错；Python 侧
  遇到非看多**拒绝**而不是静默丢弃（上游已保证只送看多，静默丢弃会让「AI 组只剩 1 只」看
  起来像模型没辩成）。
- **进度条早早跑满被钳住**。排序环节删除后 20 只全部参辩，`total_steps` 仍按 `final_n` 算，
  进度停在 100% 而后面还要跑十几分钟，看着像卡死。改为按全部候选算。

### 真实流程实跑验证

- 修复前那一跑（`41a3deff`）：20 只全部辩成、零失败，但 AI 组前三里 2 只看空，混合组
  `ai_score` 就是规则分原值——缺陷在真实数据上复现。
- 修复后重跑（`0bed5573`）：20 只全部辩成、零失败，20 只**全部**判看空 → AI 组 0、混合组 0，
  只落 `规则 3 / 基准 20`，`agents` 步记 warning 如实写明「最终 0/3」。行为正确：模型说这批
  都不该追，就不造买入名单。
- **买入路径另跑一次才算验证完**：上面两跑模型都全判看空，只走到了「空名单」这条路径，
  AI 组落库 0 行——买入路径没被验证。换信号日 `20260818` 重跑（`4b65746b`）：16 只辩成里
  1 只看多，AI 组与混合组各落 1 行，`verdict=看多`，`ai_score=62.0` 而规则分是 `0.8101`
  （不是回声）；混合组跟着 final 走只有 1 只，没拿没辩成的凑到 3 只；`agents` 步如实记
  「最终 1/3」。
- 收益行按买入语义生成：`calculate_experiment_returns` 为这 2 条决策各建 10 行（T+1 收盘 +
  T+2~T+10 开盘），买入日 `20260819`（信号日次日）。离线库缺 8/19 日线，状态如实记
  `entry_bar_missing` 并带原因，不用 0 冒充「没赚没亏」。

### 历史脏数据清理（用户确认后执行）

- 6 个批次的 ai / hybrid 组里有 17 条「看空 / 中性 / selected」却落在买入组的决策，会回填收益
  并进组间对比让 AI 组绩效失真。按用户确认删掉这 6 批的全部 ai / hybrid 行：决策 34 行、
  收益 340 行。`rule` 45 行与 `benchmark` 300 行完整保留。删前已备份
  `data/market.duckdb.bak-20260824-002814`。

### 验证

- Python **888 passed / 0 failed**；Pi Agent TypeScript **33 passed / 0 failed**，`tsc --noEmit` 通过。
- 新增红灯测试 5 项：混合组不得是规则分回声（`test_hybrid_uses_debate_score_not_rule_score_echo`）、
  看空混进 final 必须拒绝、空 final 回落规则+基准、缺 verdict 拒绝、TS 侧看空不进名单与全看空
  返回空名单。五项都先复现缺陷再修。
- 清理后复核：库里 ai / hybrid 组零残留，`/api/returns/summary` 与 14 页面接口正常。

### 前端验收又抓到一个真缺陷：辩论矩阵拼接不同股票

浏览器逐屏核对 p13 看板时发现的，不是靠读代码猜出来的。

- **现象**：一屏六格看起来是一场完整辩论，实际方法论/舆情/走势讲 `002209.SZ`、多方讲
  `000703.SZ`、空方与反驳讲 `001337.SZ`、风控又回到 `000703.SZ`——三只股票的发言被拼成
  一场根本不存在的辩论，页面毫无异常迹象。
- **根因**：20 只候选共用同一批角色名，渲染器只按角色去重（`latest.set(event.role, event)`），
  后跑完的股票覆盖先跑完的。这个缺陷此前完全没有测试覆盖。
- **修法**：p13 新增股票选择器，矩阵与风控只显示选中那只；切批次重置选中项。总览页是
  「最新动态」性质不做选择器，但每格加 `【代码】` 前缀标明归属。
- **测试**：分组逻辑抽成纯函数 `matrixStockCodes` / `latestByRoleForStock`（不碰 DOM），
  测试用 `node` 真跑它们并断言「每只股票的每个角色只能是它自己的发言」。把过滤条件改回
  旧写法，测试立刻变红并报出缺陷原貌「002209.SZ 的 methodology 格显示了 001337.SZ 的发言」，
  恢复后转绿——证明它守的是行为不是字符串。
- **浏览器实测**：选 `600547.SH` 六格全讲山东黄金、风控引用的 `RSI6=93.38 / KDJ-J=90.39`
  与方法论格数字一致；切到 `000603.SZ` 全讲盛达资源、切到 `001337.SZ` 全讲四川黄金，
  20 只都在选择器里，零 console 报错。
- 最终基线：Python **890 passed / 0 failed**（新增 2 项）；Pi Agent **33 passed / 0 failed**，
  类型检查通过；`agent-dashboard.js` 与 `overview.js` 均 `node --check` 通过。

### 台账缺批次时间，用户分不清哪次的入选（用户提出）

- **现象**：台账只按信号日分组。同一信号日可以跑多次——实测 `20260821` 一天跑了 6 次、
  `20260706` 跑了 4 次——六个批次全挤在一条「信号日」分隔线下，同一只票重复出现 6 遍
  且看不出区别。信号日只说明「基于哪天的行情」，说明不了「什么时候跑的这一次」。
- **后端**：台账查询补 `r.created_at AS run_created_at` 并加进 `ExperimentListItem`
  （pydantic 未声明的字段会被静默丢掉）；排序键加 `r.created_at DESC`，同一信号日按运行
  时间倒序。新增 `GET /api/experiments/batches` 列出全部已落库批次（只列 `succeeded`：
  没跑成的批次没有可看的入选结果）。这条路由必须排在 `/experiments/{run_id}` 之前，
  否则 `batches` 会被当成 run_id 匹配掉。
- **前端**：分组键从 `item.as_of` 改为 `${as_of}|${run_id}`，每批一条分隔线写明
  「信号日 · 运行于 2026-08-23 01:30 · 批次 0bed5573」；信号日单元格下方加一行小字标运行时刻。
  筛选栏新增「运行批次」下拉框，选项来自 batches 接口（不从分页数据里提取——一页 200 行，
  更早的批次不在这一页，下拉框会缺项）。选「全部批次」时显式 `delete params.run_id`，
  否则全局工作上下文里锁定的 run_id 会导致「选了全部却只显示一个批次」。
- **浏览器实测**：下拉框 16 个批次；不筛时 370 条、9 条分隔线，每条带不同运行时刻；
  选 `2026-08-23 00:36` 那批收窄到 23 条、只剩 1 条分隔线（规则 3 + 基准 20）；
  选 `2026-08-23 16:58` 那批 25 条，AI 1 + 混合 1 都是隆平高科、买入日 2026-08-19。
  零 console 报错。
- **测试**：新增 5 项。后端 3 项（每行必须带 `run_created_at`、批次列表最新在前且不丢项、
  未成功批次不进列表）；前端 2 项（分组键必须含 run_id + `runStamp` 精确到分钟、
  批次下拉框由 batches 接口填充）。把分组键改回只按信号日，测试立刻报「分组键没带 run_id」。
- 基线：Python **895 passed / 0 failed**；14 页面 + 17 接口全部 200。

### 台账时间不显眼 + 保存语义没写清（用户连续提出两点）

**一、时间在页面上看不见**

数据早就有了，是样式把它藏起来了：分隔线灰色小字贴在表头下面、视觉上和表头连成一片；
每行的运行时刻 10px + 75% 透明度，几乎融进背景。改法：分隔线加蓝色顶边、14px 加粗，
时刻用蓝色 12px；每行时刻改 11px 蓝色独占一行；列名「信号日」→「信号日 / 运行时刻」；
页面顶部说明补一句「同一信号日可以跑多次，每一次是独立批次」。

**二、一次运行的结果保存在哪，设计不明显**

根因不是缺功能，是**两种保存语义并存却没写在任何地方**：

- 累积（主键含 `run_id`，每次运行独立留存）：`experiment_runs`、`experiment_decisions`、
  `experiment_returns`、`agent_runs` / `agent_judgments` / `agent_events`。
- 覆盖（主键不含 `run_id`，同一 `(as_of, strategy)` 只留最后一次）：`picks`、
  `scan_runs` / `scan_rows`。

真实数据印证：8/21 跑了 6 次 → `experiment_runs` 6 行、`experiment_decisions` 163 行，
但 `picks` 只有 6 行（最后一次的名单）。看 `picks` 会以为那天只跑了一次。

**`picks` 必须覆盖，不是遗漏**：它是回测（`engine/backtest.py`）与 ML 训练
（`engine/ml/dataset.py`）的输入，两者要的是「每个交易日一份不重叠的名单」。同一天留 6 份，
同一笔钱会被算 6 次，净值直接虚高 6 倍。主键 `(as_of, strategy, ts_code)` 去重正是这个保证。
用户确认后选择**保持现状只写清语义**，不改表结构。

**写清在三处**（改一处漏两处必然漂移）：`engine/schema.py` 的 `_PICKS_SCHEMA` 头部注释、
`record_experiment()` 的 docstring 列出 5 张表各属哪种语义、`Store.all_picks()` 说明返回的是
每个信号日最新一份、ARCHITECTURE 新增「一次运行的结果保存在哪」一节带两张对照表。

**测试**：新增 2 项。① 同信号日重跑三次 → 三个批次的决策都在（各 4 行）、`picks` 只有 1 行
且不含 `run_id` 列；② 失败批次的 `run_id` 不可复用。把 `picks` 的整组删除去掉，测试立刻撞
主键约束报 `Duplicate key` 而变红——说明「覆盖」这件事由主键强制保证，不靠调用方自觉。
- 顺带记录一个真实约束：重跑同一信号日必须走 `force=true`（幂等作用域是
  `(kind, trade_date, strategy)`，已成功的任务会挡住后续运行）。测试照线上用法写。
- 基线：Python **897 passed / 0 failed**。

**三、语义只写进代码注释和文档，前端还是看不出（用户追问「前端体现在什么地方」）**

前一轮把两种语义写进了 `schema.py`、`record_experiment()`、`all_picks()` 和 ARCHITECTURE，
但那都是给读代码的人看的。页面上仍然没有任何提示，用户看到台账 395 条、回测只用一部分，
无从判断是不是漏数据。

先把消费方摸清：读 `picks`（覆盖式）的只有 `/api/backtest`（回测页 p9）；读
`experiment_decisions`（累积式）的是 `/api/experiments`（台账页 p5 与流程页 p3）。
`/api/ledger` 也走 picks 但没有任何前端页面在用。

两页各加一块带蓝色左边框的说明：
- 台账 p5：「本页是**完整历史**：每一次运行都独立留存，重跑不会覆盖旧批次。组合回测用的是
  另一张表，每个信号日只保留最新一次名单，因此**两页的条数不会一致**。」并链到 p9。
- 回测 p9：「数据来自 `picks` 表：**每个信号日只有最新一次运行的名单**。同一天重跑多次时，
  回测只用最后那一次——同一天算多份会让同一笔钱被重复计入，净值成倍虚高。」并链回 p5。

新增 `.source-note` 样式（蓝色左边框 + 浅色底 + 13px），不能沿用普通段落——用户已经反馈过
一次「淡到看不见」。

**测试**：新增 1 项，钉住两页各自的声明与互相指路的链接、以及 `.source-note` 样式存在。
这两句是纯提示文字，删掉不会有任何报错，只会让人重新踩一遍坑。
- 基线：Python **898 passed / 0 failed**；浏览器实测两页说明均渲染、零 console 报错。

**四、样式写了没生效——CSS 优先级被静默覆盖（用户第三次反馈「还是看不清」）**

前两次改分隔线都失败，根因不是没改，是**改了没生效且不报错**：

- `.date-divider td { border-top: 2px solid var(--accent) }` 写的是蓝线，浏览器渲染出来是
  灰线 `rgb(227,230,234)`。因为 `html[data-theme] th, html[data-theme] td` 的
  `border-color: var(--line-soft)` 优先级更高（`html`+属性+元素 = 0,1,2 对 `.class`+元素 = 0,1,1），
  把颜色静默覆盖了。CSS 覆盖不报错、不警告，只表现为「改了没变化」。
- 量化对比后才看清另外两点：分隔线行高 48.5px 比数据行 59.5px 还**矮**，背景
  `rgb(241,243,244)` 与数据行几乎同色——视觉分量反而比数据行更轻。

改法：选择器加 `html[data-theme]` 前缀提到同级，分隔线整块做成**实底色标题条**
（亮色深蓝底白字、暗色浅蓝底深字），批次号用胶囊标签；每行运行时刻加浅蓝底小标签，
颜色用 `!important` 压过同元素上的 `.muted`。

暗色主题单独修：`--accent` 在暗色下是浅蓝 `#8ab4f8`，白字压上去发虚，改成深色字
`#0b1220`；`.run-stamp` 写死的亮色底 `rgba(11,87,208,.1)` 在暗色下几乎不可见，换浅色半透明。

**测试**：新增 1 项 `test_batch_divider_styles_outrank_the_generic_table_border_rule`，
断言这几条规则必须带 `html[data-theme]` 前缀、低优先级旧写法不得残留、分隔线必须有实底色
（不能只靠边框——边框颜色正是被覆盖的那个属性）、run-stamp 颜色必须提权。把选择器降回
`.date-divider td` 测试立刻变红。
- 浏览器双主题实测：亮色分隔线 `rgb(11,87,208)` 底白字、暗色 `rgb(138,180,248)` 底深字，
  对比均充足；零 console 报错。
- 基线：Python **899 passed / 0 failed**。
- 顺带核实：截图里 `05b2f643` 那个 8/24 07:09 批次是本轮验证时自己触发的，不是定时任务
  （`/api/pipelines/status` 显示 `enabled=false`）。
