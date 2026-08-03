# 进度记录

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
