# 三步量化工作台实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. 本次按用户要求由主会话执行，不派生子代理。

**Goal:** 将工作台收敛为六个主入口，并让选股、舆情和 Agent 三步形成可观察、可交接、可验收的真实闭环。

**Architecture:** 继续使用 FastAPI + DuckDB + 原生 JavaScript。扫描和舆情复用 `task_runs`，由后端阶段回调更新 `result.progress` 和 `result.steps`；前端新增共享任务面板，轮询任务详情并恢复刷新前的状态。Agent 复用已有 SSE 公开事件。

**Tech Stack:** Python 3、FastAPI、DuckDB、原生 JavaScript ESM、pytest、Playwright 浏览器验收。

**Spec:** `workbench/docs/superpowers/specs/2026-08-22-three-step-workbench-design.md`

## Global Constraints

- 不新增行情或舆情数据源。
- 不修改选股方法论和 Agent 角色职责。
- 不伪造进度、日志、数量或结论。
- 读路径不写库；任务进度只由后台真实阶段更新。
- 缺数据、未配置和失败必须显式显示。
- 不创建 worktree，不提交用户未要求的提交。
- 生产代码改动先有失败测试，再实现。

---

### Task 1: 任务进度领域契约

**Files:**
- Modify: `workbench/app/services/tasks.py`
- Modify: `workbench/engine/run_scan.py`
- Modify: `workbench/app/services/scans.py`
- Modify: `workbench/engine/news.py`
- Modify: `workbench/app/services/news_collect.py`
- Test: `workbench/tests/test_task_tracker.py`
- Test: `workbench/tests/test_run_scan_offline.py`
- Test: `workbench/tests/test_news.py`

**Interfaces:**
- `TaskTracker.progress(task_id, result)` 保持现有接口，增加统一的 progress/steps 数据约束。
- 扫描内部阶段回调接收 `(stage, step, total, message, detail)`，由 `ScanManager` 映射到 `TaskTracker.progress`。
- 舆情采集内部阶段回调接收 `(stage, step, total, message)`，由 `NewsCollectManager` 映射到 `TaskTracker.progress`。

- [ ] 先增加测试：任务进度持久化后 `GET` 形态含 progress、steps 和 logs；扫描/舆情阶段回调能留下运行中结果。
- [ ] 运行测试确认新断言失败。
- [ ] 实现统一进度追加和阶段收尾，不改变终态 result/error 语义。
- [ ] 运行任务、扫描和舆情针对性测试。

### Task 2: 共享前端任务面板

**Files:**
- Create: `workbench/ui_mockups/v2/assets/js/task-panel.js`
- Modify: `workbench/ui_mockups/v2/assets/css/theme.css`
- Test: `workbench/tests/test_ui_pages.py`

**Interfaces:**
- `createTaskPanel(root, options)` 渲染任务编号、状态、进度条、阶段和日志。
- `panel.update(task)` 从后端任务详情渲染，不自行计算任务进度。
- `panel.reset()` 清空本次任务。

- [ ] 先增加页面契约测试，要求共享面板导出函数和进度/日志结构。
- [ ] 运行测试确认失败。
- [ ] 实现可访问的进度条、状态标签、阶段列表、日志滚动区和终态摘要。
- [ ] 运行 UI 契约测试。

### Task 3: 三步页面接线

**Files:**
- Modify: `workbench/ui_mockups/v2/p1_desk.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/desk.js`
- Modify: `workbench/ui_mockups/v2/p7_news.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/news.js`
- Modify: `workbench/ui_mockups/v2/p11_agents.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/agents.js`
- Modify: `workbench/ui_mockups/v2/p13_agent_dashboard.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/agent-dashboard.js`
- Test: `workbench/tests/test_ui_pages.py`

**Interfaces:**
- 选股提交后轮询 `/api/scans/{job_id}`，面板显示真实任务并把 `run_id/as_of/strategy/candidate_codes` 写入工作上下文。
- 舆情提交后轮询 `/api/news/collect/{job_id}`，面板显示真实任务；从上下文读取候选行业并提供 Agent 交接。
- Agent 提交使用工作上下文和候选池，实时公开事件仍走 `/stream`。

- [ ] 先增加页面契约测试，覆盖任务面板挂载、交接链接和上下文参数。
- [ ] 运行测试确认失败。
- [ ] 接入统一面板，保留现有 API 和错误处理。
- [ ] 实现完成摘要不清空、失败可重试、刷新恢复。
- [ ] 运行 UI 测试和 JavaScript 语法检查。

### Task 4: 主导航和自选行情收敛

**Files:**
- Modify: `workbench/ui_mockups/v2/assets/js/app-shell.js`
- Modify: `workbench/ui_mockups/v2/p10_watchlist.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/watchlist.js`
- Modify: `workbench/ui_mockups/v2/p6_chart.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/chart.js`
- Modify: `workbench/ui_mockups/v2/p12_settings.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/settings.js`
- Test: `workbench/tests/test_ui_pages.py`

**Interfaces:**
- 主导航只显示总览、方法论选股、板块舆情、多 Agent 辩论、自选与行情、设置六项。
- 自选和行情页面继续调用现有 watchlist/kline API，不复制后端。
- 设置保存反馈继续显示实际 API 返回结果。

- [ ] 先增加导航契约测试。
- [ ] 运行测试确认失败。
- [ ] 调整导航与页面入口，不删除旧页面文件和接口。
- [ ] 运行 UI 测试并检查六页可达。

### Task 5: 最终验证

**Files:**
- No new production files.

- [ ] 运行针对性 pytest，报告通过/失败数量。
- [ ] 启动服务并用浏览器点击选股、舆情、Agent、自选、行情和设置。
- [ ] 发现失败时写回归测试并修复。
- [ ] 再次运行测试和浏览器验收，逐项对照 spec。
