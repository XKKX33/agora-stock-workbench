# Hermes 一键全流程与按日期收益追踪 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户点击一次“一键全流程”后，系统用真实数据完成扫描、舆情和 `grok-4.5` 多 Agent 研判，保存规则、AI、混合、基准四组结果，并在后续运行时按信号日期回填次日开盘买入后的真实收益。

**Architecture:** 保留 `PipelineManager` 作为唯一后台任务入口；把数据准备、实验分组、收益回填拆成纯业务模块，实验元数据独立存入两张新表。前端只提交和轮询任务，实验查询走独立只读 API，台账页按信号日期展示。

**Tech Stack:** Python 3.12、FastAPI、DuckDB、Pandas、httpx、原生 JavaScript、Pytest、Playwright。

---

## 文件结构

- `workbench/engine/ai.py`：OpenAI 兼容客户端，严格拼接 `/chat/completions` 并清洗错误。
- `workbench/engine/ingest_tushare.py`：交易日历、行情、指标、资金流和涨跌停价摄取。
- `workbench/engine/experiments.py`：四组构造、候选池哈希和收益回填纯业务规则。
- `workbench/engine/db_experiments.py`：实验表的写入、查询与事务边界。
- `workbench/engine/schema.py`：新增 `daily_limit`、`experiment_runs`、`experiment_decisions`。
- `workbench/app/services/one_click.py`：同步执行一键流程各业务步骤，不管理线程。
- `workbench/app/services/pipelines.py`：后台任务、幂等、进度和失败落库。
- `workbench/app/services/experiments.py`：按日期查询明细与统计。
- `workbench/app/api/experiments.py`、`workbench/app/schemas/experiments.py`：实验只读 API。
- `workbench/ui_mockups/v2/p3_foundry.html`、`assets/js/pages/foundry.js`：一键按钮、步骤状态、四组摘要。
- `workbench/ui_mockups/v2/p5_ledger.html`、`assets/js/pages/ledger.js`：按日期实验台账。

### Task 1: 锁定 `grok-4.5` 配置和请求契约

**Files:**
- Modify: `workbench/config/settings.yaml`
- Modify: `workbench/engine/ai.py`
- Modify: `workbench/app/services/ai.py`
- Test: `workbench/tests/test_ai.py`
- Test: `workbench/tests/api/test_settings.py`

- [x] **Step 1: 写失败测试**

```python
def test_openai_client_uses_v1_chat_completions(monkeypatch):
    seen = {}
    transport = httpx.MockTransport(lambda request: capture_ok(request, seen))
    client = OpenAICompatibleClient(
        AIConfig(enabled=True, provider="openai_compatible",
                 model="grok-4.5", base_url="https://grok.xuan.christmas/v1",
                 api_key_env="WORKBENCH_AI_TEST_KEY"),
        transport=transport,
    )
    assert client.generate("system", "user") == "ok"
    assert seen["url"] == "https://grok.xuan.christmas/v1/chat/completions"
    assert seen["model"] == "grok-4.5"
```

- [x] **Step 2: 运行并确认失败**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/test_ai.py tests/api/test_settings.py -q`

Expected: 新增请求注入或配置断言失败。

- [x] **Step 3: 最小实现**

`settings.yaml` 只保存非敏感配置：

```yaml
ai:
  enabled: true
  provider: openai_compatible
  base_url: https://grok.xuan.christmas/v1
  api_key_env: WORKBENCH_AI_API_KEY
  model: grok-4.5
agent:
  enabled: true
```

客户端允许测试注入 `httpx.BaseTransport`，生产仍从环境变量取密钥；`app/services/ai.py` 改用 `load_settings_with_local()`，与 Agent 和设置页使用同一份合并配置。

- [x] **Step 4: 运行并确认通过**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/test_ai.py tests/api/test_settings.py -q`

Expected: 全部通过。

### Task 2: 建立实验表与严格存储接口

**Files:**
- Modify: `workbench/engine/schema.py`
- Create: `workbench/engine/db_experiments.py`
- Modify: `workbench/engine/db.py`
- Test: `workbench/tests/test_experiment_store.py`

- [x] **Step 1: 写失败测试**

```python
def test_four_groups_commit_in_one_transaction(tmp_path):
    with Store(tmp_path / "x.duckdb", ensure_schema=True) as store:
        store.record_experiment(run_row("r1"), four_group_rows("r1"))
        assert store.experiment_run("r1")["status"] == "succeeded"
        assert set(store.experiment_decisions("r1")["group_name"]) == {
            "rule", "ai", "hybrid", "benchmark"
        }

def test_incomplete_groups_are_rejected(tmp_path):
    with Store(tmp_path / "x.duckdb", ensure_schema=True) as store:
        with pytest.raises(ValueError, match="四组"):
            store.record_experiment(run_row("r1"), three_group_rows("r1"))
```

- [x] **Step 2: 运行并确认失败**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/test_experiment_store.py -q`

Expected: 表或方法不存在。

- [x] **Step 3: 最小实现**

新增 `ExperimentMixin`，只公开 `create_experiment_run`、`fail_experiment_run`、`record_experiment`、`experiment_run`、`experiment_decisions`、`pending_experiment_decisions`、`update_experiment_return`。`record_experiment` 在一个 DuckDB 事务中验证四组齐全后写明细并把批次标成 `succeeded`。

- [x] **Step 4: 运行并确认通过**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/test_experiment_store.py -q`

Expected: 全部通过。

### Task 3: 四组构造与真实收益回填

**Files:**
- Create: `workbench/engine/experiments.py`
- Modify: `workbench/engine/ingest_tushare.py`
- Test: `workbench/tests/test_experiments.py`
- Test: `workbench/tests/test_ingest.py`

- [x] **Step 1: 写失败测试**

```python
def test_build_groups_share_frozen_pool_and_use_percentiles():
    groups = build_experiment_groups(scan_rows, agent_result, final_count=3)
    assert set(groups) == {"rule", "ai", "hybrid", "benchmark"}
    assert len(groups["rule"]) == len(groups["ai"]) == len(groups["hybrid"]) == 3
    assert len(groups["benchmark"]) == len(scan_rows)

def test_next_open_entry_and_horizons_are_market_sessions(store):
    backfill_experiment_returns(store, exchange="SSE")
    row = store.experiment_decisions("r1").iloc[0]
    assert row.entry_date == "20260805"
    assert row.entry_price == 10.0
    assert row.ret1 == pytest.approx(0.02)
    assert row.ret3 == pytest.approx(0.08)

def test_locked_limit_up_is_not_filled(store):
    backfill_experiment_returns(store, exchange="SSE")
    row = store.experiment_decisions("r2").iloc[0]
    assert row.entry_status == "entry_unavailable"
    assert row.entry_reason == "limit_up_locked"
```

- [x] **Step 2: 运行并确认失败**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/test_experiments.py tests/test_ingest.py -q`

Expected: 新模块和 `daily_limit` 不存在。

- [x] **Step 3: 最小实现**

`candidate_hash` 使用按 `ts_code` 排序后的规范 JSON 做 SHA-256。规则组按规则分，AI 组按风控最终分，混合组按当日百分位各 50%，基准组保留冻结候选池。收益只使用市场交易日历、下一交易日开盘价和指定目标日收盘价；停牌、无开盘、一字涨停分别记录原因，绝不换邻近价格或补零。

- [x] **Step 4: 运行并确认通过**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/test_experiments.py tests/test_ingest.py -q`

Expected: 全部通过。

### Task 4: 编排一键全流程

**Files:**
- Create: `workbench/app/services/one_click.py`
- Modify: `workbench/engine/run_scan.py`
- Modify: `workbench/app/services/pipelines.py`
- Modify: `workbench/app/schemas/pipelines.py`
- Test: `workbench/tests/test_one_click.py`
- Test: `workbench/tests/api/test_pipelines.py`

- [ ] **Step 1: 写失败测试**

```python
def test_one_click_runs_steps_in_fixed_order(fake_dependencies):
    result = run_one_click(...)
    assert [step["name"] for step in result["steps"]] == [
        "preflight", "calendar", "market_data", "backfill_returns",
        "integrity", "scan", "collect_news", "agents", "persist_experiment"
    ]

def test_ai_failure_marks_batch_failed_without_partial_four_groups(...):
    with pytest.raises(AIRequestError):
        run_one_click(...)
    assert store.experiment_decisions("r1").empty
    assert store.experiment_run("r1")["status"] == "failed"
```

- [ ] **Step 2: 运行并确认失败**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/test_one_click.py tests/api/test_pipelines.py -q`

Expected: 一键服务或新步骤不存在。

- [ ] **Step 3: 最小实现**

把 `run_scan` 的数据准备和评分拆成两个显式函数，使完整性闸门位于评分前。`PipelineManager` 仍负责线程和 `task_runs`；`OneClickRunner` 负责同步业务步骤，并直接复用 `run_judge`，不启动第二个后台任务。任务结果保存当前步骤、全部步骤、四组数量和数据截止时间。

- [ ] **Step 4: 运行并确认通过**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/test_one_click.py tests/api/test_pipelines.py -q`

Expected: 全部通过。

### Task 5: 增加按日期实验查询 API

**Files:**
- Create: `workbench/app/schemas/experiments.py`
- Create: `workbench/app/services/experiments.py`
- Create: `workbench/app/api/experiments.py`
- Modify: `workbench/app/dependencies.py`
- Modify: `workbench/app/main.py`
- Test: `workbench/tests/api/test_experiments.py`

- [x] **Step 1: 写失败测试**

```python
def test_experiments_filter_by_signal_date_group_stock_and_entry_status(client):
    response = client.get("/api/experiments", params={
        "as_of": "20260804", "group": "ai", "ts_code": "000001.SZ",
        "entry_status": "filled"
    })
    assert response.status_code == 200
    assert all(item["as_of"] == "20260804" for item in response.json()["items"])

def test_summary_never_invents_statistics_without_samples(client):
    payload = client.get("/api/experiments/summary").json()
    assert payload["groups"]["ai"]["ret5"]["average"] is None
    assert payload["groups"]["ai"]["ret5"]["sample_count"] == 0
```

- [x] **Step 2: 运行并确认失败**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/api/test_experiments.py -q`

Expected: 路由返回 404。

- [x] **Step 3: 最小实现**

实现列表、批次明细和汇总三个只读端点。列表按 `as_of DESC, group_name, rank` 排序；查询参数严格校验，`NULL` 与真实 0 保持不同。

- [x] **Step 4: 运行并确认通过**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/api/test_experiments.py -q`

Expected: 全部通过。

### Task 6: 完成一键按钮和按日期台账

**Files:**
- Modify: `workbench/ui_mockups/v2/p1_desk.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/desk.js`
- Modify: `workbench/ui_mockups/v2/p2_sentiment.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/sentiment.js`
- Modify: `workbench/ui_mockups/v2/p3_foundry.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/foundry.js`
- Modify: `workbench/ui_mockups/v2/p5_ledger.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/ledger.js`
- Modify: `workbench/ui_mockups/v2/p11_agents.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/agents.js`
- Modify: `workbench/ui_mockups/v2/assets/css/theme.css`
- Modify: `workbench/ui_mockups/v2/assets/js/app-shell.js`
- Test: `workbench/tests/test_ui_pages.py`

- [ ] **Step 1: 写失败测试**

```python
def test_foundry_has_one_click_pipeline_contract(ui_root):
    html = (ui_root / "p3_foundry.html").read_text(encoding="utf-8")
    js = (ui_root / "assets/js/pages/foundry.js").read_text(encoding="utf-8")
    assert 'id="one-click"' in html
    assert "/api/pipelines" in js
    assert "persist_experiment" in js

def test_ledger_reads_experiments_by_date(ui_root):
    js = (ui_root / "assets/js/pages/ledger.js").read_text(encoding="utf-8")
    assert "/api/experiments" in js
    assert "entry_status" in js

def test_selection_actions_have_unambiguous_labels(ui_root):
    foundry = (ui_root / "p3_foundry.html").read_text(encoding="utf-8")
    agents = (ui_root / "p11_agents.html").read_text(encoding="utf-8")
    assert "一键全流程" in foundry
    assert "Agent 选股研判" in agents
    assert "单股深度研判" in agents

def test_shell_has_persistent_light_dark_theme_control(ui_root):
    shell = (ui_root / "assets/js/app-shell.js").read_text(encoding="utf-8")
    css = (ui_root / "assets/css/theme.css").read_text(encoding="utf-8")
    assert "prefers-color-scheme" in shell
    assert "localStorage" in shell
    assert '[data-theme="light"]' in css
    assert '[data-theme="dark"]' in css
```

- [ ] **Step 2: 运行并确认失败**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/test_ui_pages.py -q`

Expected: 新页面契约不存在。

- [ ] **Step 3: 最小实现**

先用真实浏览器和真实接口响应复现情绪页问题，记录控制台错误、失败请求或错误 DOM 状态，补回归测试后修根因。系统选股页、选股流程页和 Agent 页使用互不混淆的动作名称：系统规则扫描、完整一键流程、Agent 选股研判、单股深度研判；按钮旁只显示必要状态，不用说明文字替代操作。全站重构为共用 DOM 的明亮/暗夜双主题：首次跟随系统，手动切换后由 localStorage 记忆；明亮版采用近白中性底、深灰文字、低阴影与淡蓝主操作，暗夜版采用中性炭黑，移除蓝紫渐变。流程页固定尺寸步骤列表显示等待、运行、成功和失败，刷新后从最近任务恢复。台账页提供日期、组别、股票、成交状态筛选，按日期分组显示买入价、四期收益和缺失原因。

- [ ] **Step 4: 静态测试与浏览器验收**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests/test_ui_pages.py -q`

Run: `node --check ui_mockups/v2/assets/js/pages/foundry.js`

Run: `node --check ui_mockups/v2/assets/js/pages/ledger.js`

Expected: 全部通过；Playwright 桌面与手机截图无重叠，按钮可触发并恢复进度。

### Task 7: 全量验证、真实最小请求和归档

**Files:**
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `task_plan.md`
- Modify: `progress.md`
- Modify: `findings.md`
- Archive: `workbench/docs/openspec/one-click-experiment-tracking/`

- [ ] **Step 1: 全量测试**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest tests -q --import-mode=importlib --basetemp=.pytest-tmp-one-click -p no:cacheprovider`

Expected: 0 failed。

- [ ] **Step 2: 配置 Git 忽略的 `.env` 并做一次最小真实请求**

使用项目 `.env` 中的 `WORKBENCH_AI_API_KEY`，请求只验证 `grok-4.5` 可返回结构化内容；输出只记录 HTTP 状态、模型名和是否成功，不打印密钥或完整响应。

- [ ] **Step 3: 真实流程验收**

在用户确认真实数据库写入后运行一次一键流程；四组必须共用 `run_id`、`as_of`、`candidate_hash` 和 `data_cutoff_at`。收益回填验收使用隔离数据库副本，不修改历史真实结果。

- [ ] **Step 4: Review 与第一性原理自检**

检查同类配置读取、失败状态、事务边界、未来数据、`NULL` 语义、密钥日志和重复代码；删除能被现有模块直接替代的新抽象。

- [ ] **Step 5: 更新文档并归档 OpenSpec**

把运行命令、环境变量名、API、模块职责、测试数量和真实验收结果写入 README/ARCHITECTURE；OpenSpec 只在代码与验收一致后移入 archive。

## 自检结果

- 规格覆盖：一键步骤、四组口径、按日期台账、次日开盘买入、四期收益、不可成交、配置统一、真实验收均有对应任务。
- 占位扫描：没有 `TBD`、`TODO`、`implement later` 或“补适当错误处理”类空步骤。
- 类型一致：统一使用 `run_id`、`as_of`、`group_name`、`entry_status`、`ret1/ret3/ret5/ret10`，与设计文档一致。
