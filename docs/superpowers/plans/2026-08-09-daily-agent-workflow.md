# 每日选股工作流与 Agent 研判系统实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` or `superpowers:executing-plans` to implement this plan task-by-task. 每个任务完成后运行该任务列出的定向测试，再进入下一任务。

**Goal:** 固定每日一键选股工作流，建立可审计的 Agent 公开通话与多空辩论，新增 T+1 收盘至 T+10 开盘收益计算、SSE 实时事件和历史研判看板。

**Architecture:** 保留现有 `PipelineManager → OneClickRunner → engine` 主链和 `TaskTracker` 任务持久化。Agent 通过编排器把每个角色的公开结构化发言写入 `agent_events`，同一消息既作为下一个角色的输入，也作为 SSE 和历史报告的数据源；模型隐藏思维链不落库、不下发。收益采用独立 `experiment_returns` 表，不改写旧 `picks.ret1/ret3/ret5/ret10` 的历史含义。

**Tech Stack:** Python 3.13、FastAPI、DuckDB、httpx OpenAI 兼容接口、原生 JavaScript、SSE `StreamingResponse`、本地 ECharts。

## Global Constraints

- 大模型 `base_url` 使用 `https://grok.xuan.christmas/v1`，客户端请求地址固定为 `base_url.rstrip("/") + "/chat/completions"`。
- API 密钥保存在 Git 忽略的 `workbench/.env`，启动时加载到 `WORKBENCH_AI_API_KEY`；禁止写入代码、YAML、计划、日志、数据库、SSE 事件和错误信息。
- `model` 从 `config/settings.local.yaml` 或设置页读取，不在代码里猜默认模型。
- Agent 只共享公开结构化消息和数据引用，不共享或展示隐藏思维链。
- 每日流程严格串行；`collect_news` 或 `agents` 失败时整批失败，不提交成功实验结果。
- 收益口径固定为 `T+1 open` 买入、`T+1 close` 卖出，以及 `T+2 open` 到 `T+10 open` 分别卖出；v1 不计手续费、印花税和滑点。
- 单票缺行情、未来未到、未成交必须保留状态和原因，不把缺失写成真实 `0`；组合汇总中未成交资金槽位按现金收益 0 处理。
- 不引入 LangGraph、前端框架或 CDN；沿用 `Store`、`TaskTracker`、`WorkbenchError` 和原生页面结构。
- 所有测试使用临时 DuckDB；禁止测试写入 `workbench/data/market.duckdb`。

---

### Task 1: 固定每日一键工作流和失败边界

**Files:**
- Modify: `workbench/app/services/one_click.py:51-419`
- Modify: `workbench/app/services/pipelines.py:107-348`
- Modify: `workbench/app/api/pipelines.py:10-61`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/foundry.js`
- Modify: `workbench/ui_mockups/v2/p3_foundry.html`
- Test: `workbench/tests/test_one_click.py`
- Test: `workbench/tests/api/test_pipelines.py`

**Interfaces:**
- Preserve `OneClickRunner.run(...) -> dict` and `PipelineManager.start(...) -> dict`.
- Keep the ordered `STEP_NAMES` tuple as the only step order source. The final order is `preflight`, `calendar`, `market_data`, `backfill_returns`, `integrity`, `scan`, `collect_news`, `agents`, `persist_experiment`.
- Add `OneClickRunner.step_contract() -> list[dict]`, returning each step's name, display label, required flag, and output keys for the workflow page.
- Add `PipelineManager.workflow_definition() -> dict`, returning the same contract plus configured strategy, online mode, and current data cutoff.

- [ ] **Step 1: Write failing tests for the fixed contract.**

Add tests that assert:

```python
assert OneClickRunner.step_contract()[0]["name"] == "preflight"
assert [item["name"] for item in OneClickRunner.step_contract()] == [
    "preflight", "calendar", "market_data", "backfill_returns",
    "integrity", "scan", "collect_news", "agents", "persist_experiment",
]
```

Add a pipeline test that replaces the Agent operation with an exception and asserts `task_runs.status == "failed"`, `failed_step == "agents"`, and that `experiment_runs` has no `succeeded` row for that task.

- [ ] **Step 2: Run the focused tests and confirm the new assertions fail.**

Run from `workbench/`:

```powershell
$env:WORKBENCH_AI_API_KEY = ""
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_one_click.py tests/api/test_pipelines.py -q --import-mode=importlib --basetemp=.pytest-tmp-workflow-contract -p no:cacheprovider
```

Expected: the new contract or failure-boundary assertions fail before implementation.

- [ ] **Step 3: Implement the fixed workflow contract.**

Keep the current nine operations and make the order explicit in one tuple. Ensure `OneClickRunner` calls `on_step` before every next operation and that any exception immediately enters the existing failed-experiment path. Remove any UI wording that still describes the old five/six-step chain. Have `PipelineManager` expose the contract without executing the workflow.

- [ ] **Step 4: Implement workflow-page rendering.**

Update `foundry.js` to render all nine nodes from the API payload, show `queued/running/succeeded/failed`, show the failed step and completed steps, and never show a later step as successful. Keep the existing error envelope handling.

- [ ] **Step 5: Run the focused tests.**

```powershell
$env:WORKBENCH_AI_API_KEY = ""
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_one_click.py tests/api/test_pipelines.py -q --import-mode=importlib --basetemp=.pytest-tmp-workflow-contract2 -p no:cacheprovider
```

Expected: all focused tests pass.

---

### Task 2: Add OpenAI-compatible streaming and secure provider configuration

**Files:**
- Modify: `workbench/engine/ai.py:92-183`
- Modify: `workbench/engine/agents.py:252-390`
- Modify: `workbench/engine/config.py:40-64`
- Modify: `workbench/config/settings.yaml`
- Modify: `workbench/app/services/settings_store.py`
- Modify: `workbench/ui_mockups/v2/p12_settings.html`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/settings.js`
- Test: `workbench/tests/test_ai.py`
- Test: `workbench/tests/test_agents.py`
- Test: `workbench/tests/api/test_settings.py`

**Interfaces:**
- Add `OpenAICompatibleClient.chat_stream(messages, *, json_mode=False, temperature=None, max_tokens=None, retries=2) -> Iterator[str]`. It must parse OpenAI-compatible SSE `data:` lines, ignore `[DONE]`, yield only textual deltas, and raise `AIRequestError` for malformed non-terminal chunks or HTTP/TLS failures.
- Add `OpenAICompatibleClient.chat_text(...) -> str` only if needed to share non-streaming and streaming accumulation; do not duplicate request construction.
- Add `load_ai_config(...)` support for the configured base URL without changing the existing `AIConfig` public fields.
- Add `stream: bool` to the internal Agent call options, defaulting to `False` for analyst calls and `True` for debate calls.

- [ ] **Step 1: Write failing transport tests.**

Use `httpx.MockTransport` and fake responses; never call the real endpoint. Cover:

```python
def test_chat_stream_yields_text_deltas(): ...
def test_chat_stream_ignores_done_marker(): ...
def test_chat_stream_rejects_malformed_delta(): ...
def test_endpoint_appends_chat_completions_to_configured_base_url(): ...
def test_api_key_never_appears_in_error_text_or_payload_log(): ...
```

Use a fake `AIConfig(base_url="https://grok.xuan.christmas/v1", model="test-model", api_key_env="WORKBENCH_AI_API_KEY", enabled=True)` and set the test key through `monkeypatch` only.

- [ ] **Step 2: Run the tests and confirm they fail.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_ai.py tests/test_agents.py -q --import-mode=importlib --basetemp=.pytest-tmp-ai-stream-red -p no:cacheprovider
```

Expected: streaming method and new endpoint/config assertions fail.

- [ ] **Step 3: Implement the streaming client.**

Refactor the HTTP request setup so both `chat()` and `chat_stream()` use the same endpoint, headers, timeout, retry and redaction rules. Parse each `data:` line as JSON, read `choices[0].delta.content`, and close the client on every failed attempt. Do not include request headers, prompt text, or response body in persisted errors.

- [ ] **Step 4: Connect configuration to the supplied provider.**

Set the checked-in default `ai.base_url` and `agent.base_url` to `https://grok.xuan.christmas/v1` only if the project convention requires a non-empty default; otherwise store it in the local settings path used by the settings page. Keep model editable. Update the settings page copy to say that the endpoint is OpenAI compatible and keys are environment-only. Never put the user-provided key in any file.

- [ ] **Step 5: Run the focused tests.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_ai.py tests/test_agents.py tests/api/test_settings.py -q --import-mode=importlib --basetemp=.pytest-tmp-ai-stream-green -p no:cacheprovider
```

Expected: all focused tests pass without network access.

---

### Task 3: Persist Agent public conversation events

**Files:**
- Modify: `workbench/engine/schema.py:239-326`
- Modify: `workbench/engine/db.py:55-160`
- Modify: `workbench/engine/db_news.py`
- Create: `workbench/engine/db_agents.py`
- Modify: `workbench/engine/agents.py:252-613`
- Modify: `workbench/app/services/agents.py:57-588`
- Test: `workbench/tests/test_experiment_store.py`
- Create: `workbench/tests/test_agent_events.py`

**Interfaces:**
- Add table `agent_events` with primary key `(run_id, seq)` and columns: `run_id`, `seq`, `event_id`, `event_type`, `ts_code`, `stage`, `role`, `round_no`, `content_json`, `citations_json`, `status`, `created_at`.
- Add `Store.append_agent_event(event: dict) -> dict`, `Store.agent_events(run_id: str, after_seq: int = 0, limit: int = 500) -> list[dict]`, and `Store.agent_event_last_seq(run_id: str) -> int`.
- Add `AgentEventBus.publish(event: dict) -> dict`, `AgentEventBus.subscribe(run_id: str, after_seq: int) -> Iterator[dict]`, and `AgentEventBus.close(run_id: str) -> None`. The bus is an in-process wake-up layer only; DuckDB remains the source of truth.
- Add `AgentJudgeManager.events(run_id: str, after_seq: int = 0, limit: int = 500) -> dict`.

- [ ] **Step 1: Write failing persistence and ordering tests.**

Cover:

```python
def test_agent_events_have_monotonic_sequence(tmp_path): ...
def test_agent_events_resume_after_sequence(tmp_path): ...
def test_agent_event_payload_does_not_contain_api_key(tmp_path, monkeypatch): ...
def test_schema_migration_adds_agent_events_without_deleting_agent_runs(tmp_path): ...
```

Assert that event payload contains only the role's public JSON and citation fields.

- [ ] **Step 2: Run the tests and confirm they fail.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_agent_events.py tests/test_experiment_store.py -q --import-mode=importlib --basetemp=.pytest-tmp-agent-events-red -p no:cacheprovider
```

- [ ] **Step 3: Add schema migration and storage methods.**

Keep event DDL in `schema.py`; keep Agent-specific methods in `db_agents.py` mixed into `Store` using the existing mixin convention. Sequence allocation must be transactional per run. `content_json` and `citations_json` must be strict JSON with secrets redacted before insertion.

- [ ] **Step 4: Add the event bus and manager integration.**

The manager publishes `run.started`, `stage.started`, `message.completed`, `stage.completed`, `run.completed`, and `run.failed` after persistence. A listener always reads from DuckDB after wake-up so reconnect/resume works even when the process restarted.

- [ ] **Step 5: Run the focused tests.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_agent_events.py tests/test_experiment_store.py -q --import-mode=importlib --basetemp=.pytest-tmp-agent-events-green -p no:cacheprovider
```

Expected: all focused tests pass.

---

### Task 4: Rebuild multi-Agent public debate with message handoff

**Files:**
- Modify: `workbench/engine/agents.py:262-519`
- Modify: `workbench/app/services/agents.py:284-585`
- Modify: `workbench/engine/schema.py:295-325`
- Test: `workbench/tests/test_agents.py`
- Test: `workbench/tests/test_agent_events.py`

**Interfaces:**
- Add `DebateMessage` dataclass with `role`, `stage`, `round_no`, `content`, and `citations`.
- Add `run_public_debate(client, config, *, snapshot: dict, deep: dict, emit: ProgressFn | None, publish: Callable[[dict], dict] | None) -> dict`.
- Keep `run_judge(...) -> dict` and `run_single(...) -> dict` return shapes compatible; add `public_debate` and `event_seq` data rather than removing existing `final` fields.

- [ ] **Step 1: Write failing handoff tests with a fake client.**

The fake client records each call's messages. Assert:

```python
roles = [event["role"] for event in events if event["event_type"] == "message.completed"]
assert roles == ["methodology", "sentiment", "trend", "bull", "bear", "bull_counter", "risk_chair"]
assert "bull" in json.dumps(call_for("bear")["messages"])
assert "bear" in json.dumps(call_for("bull_counter")["messages"])
assert "bull_counter" in json.dumps(call_for("risk_chair")["messages"])
```

Also assert malformed JSON from any public role raises `AgentOutputError` and marks the task failed.

- [ ] **Step 2: Run the tests and confirm they fail.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_agents.py tests/test_agent_events.py -q --import-mode=importlib --basetemp=.pytest-tmp-debate-red -p no:cacheprovider
```

- [ ] **Step 3: Implement deterministic public message handoff.**

Run the three analysts independently. Build a public transcript containing only validated JSON. Inject that transcript into the next prompt. Run `bull`, `bear`, `bull_counter`, and `risk_chair` in the fixed order. Use `chat_stream()` for the four debate roles and `chat()` for the three independent analyst roles unless the provider reports streaming unsupported; unsupported streaming is a clear provider error, not a silent fallback in the debate path.

- [ ] **Step 4: Publish events and preserve final persistence.**

Emit one `message.completed` event after each validated public message and `message.delta` events while streaming. Store the final risk output in the existing `agent_judgments.stage_json` with the complete public transcript and event sequence references. Do not store hidden reasoning.

- [ ] **Step 5: Run the focused tests.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_agents.py tests/test_agent_events.py -q --import-mode=importlib --basetemp=.pytest-tmp-debate-green -p no:cacheprovider
```

Expected: all focused tests pass.

---

### Task 5: Add SSE event and historical report APIs

**Files:**
- Modify: `workbench/app/api/agents.py:34-104`
- Modify: `workbench/app/services/agents.py`
- Modify: `workbench/app/main.py:121-136`
- Create: `workbench/app/schemas/agent_events.py`
- Test: `workbench/tests/api/test_agents.py`
- Create: `workbench/tests/api/test_agent_stream.py`

**Interfaces:**
- Add `GET /api/agents/jobs/{job_id}/events?after_seq=0&limit=500`, returning `{run_id, items, next_seq, has_more}`.
- Add `GET /api/agents/jobs/{job_id}/stream?after_seq=0`, returning `text/event-stream` with `id`, `event`, and JSON `data` fields.
- Keep `/api/agents/jobs/{job_id}` and `/api/agents/results` unchanged except for additive `event_count` and `report_available` fields.
- Route `/events` and `/stream` before `/agents/jobs/{job_id}` so they are not captured as job IDs.

- [ ] **Step 1: Write failing API tests.**

Cover:

```python
def test_agent_events_endpoint_supports_after_seq(client, seeded_agent_run): ...
def test_agent_stream_returns_sse_headers(client, seeded_agent_run): ...
def test_agent_stream_replays_persisted_events(client, seeded_agent_run): ...
def test_agent_stream_returns_structured_failure_event(client, failed_agent_run): ...
```

Assert `Content-Type` starts with `text/event-stream`, each event has an integer `id`, and a replay request excludes events at or below `after_seq`.

- [ ] **Step 2: Run the tests and confirm they fail.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/api/test_agents.py tests/api/test_agent_stream.py -q --import-mode=importlib --basetemp=.pytest-tmp-agent-api-red -p no:cacheprovider
```

- [ ] **Step 3: Implement replay and live SSE.**

Use a synchronous generator with `StreamingResponse`. First yield persisted events after `after_seq`; then wait on the in-process bus and poll DuckDB. Send `heartbeat` at least every 15 seconds. Stop after `run.completed` or `run.failed`, and close the subscription in `finally`.

- [ ] **Step 4: Add additive API schemas and error mapping.**

Use `WorkbenchError` for unknown jobs, invalid sequence/limit, AI configuration failure and stream failure. Do not expose raw provider responses or environment values.

- [ ] **Step 5: Run the focused tests.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/api/test_agents.py tests/api/test_agent_stream.py -q --import-mode=importlib --basetemp=.pytest-tmp-agent-api-green -p no:cacheprovider
```

Expected: all focused API tests pass.

---

### Task 6: Implement T+1 close through T+10 open returns

**Files:**
- Modify: `workbench/engine/schema.py:259-293`
- Create: `workbench/engine/returns.py`
- Modify: `workbench/engine/db_experiments.py`
- Modify: `workbench/app/services/one_click.py:213-234`
- Modify: `workbench/app/services/pipelines.py:275-318`
- Create: `workbench/app/services/returns.py`
- Create: `workbench/app/api/returns.py`
- Modify: `workbench/app/main.py:121-136`
- Test: `workbench/tests/test_experiments.py`
- Create: `workbench/tests/test_returns.py`
- Create: `workbench/tests/api/test_returns.py`

**Interfaces:**
- Add table `experiment_returns` with primary key `(run_id, group_name, ts_code, horizon)` and columns `run_id`, `group_name`, `ts_code`, `horizon`, `entry_date`, `entry_price`, `sell_date`, `sell_session`, `sell_price`, `status`, `reason`, `gross_return`, `created_at`, `updated_at`.
- Add `calculate_experiment_returns(store: Store, *, run_id: str | None = None, exchange: str = "SSE") -> ReturnsSummary`.
- Add `returns_summary(store: Store, *, run_id: str | None = None) -> dict`.
- Add `POST /api/returns/calculate` returning a task/job response and `GET /api/returns` for detail plus `GET /api/returns/summary` for grouped cards.
- Use horizons exactly: `t1_close`, `t2_open`, `t3_open`, `t4_open`, `t5_open`, `t6_open`, `t7_open`, `t8_open`, `t9_open`, `t10_open`.

- [ ] **Step 1: Write failing domain tests.**

Seed an isolated database with trade calendar and daily bars, then assert:

```python
assert result["t1_close"].gross_return == close_t1 / open_t1 - 1
assert result["t2_open"].gross_return == open_t2 / open_t1 - 1
assert result["t10_open"].sell_session == "open"
```

Also cover `future_not_reached`, `entry_bar_missing`, `limit_up_locked`, `target_bar_missing`, idempotent reruns, and a portfolio summary that distinguishes a missing single-stock return from a true zero return.

- [ ] **Step 2: Run the tests and confirm they fail.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_returns.py tests/test_experiments.py -q --import-mode=importlib --basetemp=.pytest-tmp-returns-red -p no:cacheprovider
```

- [ ] **Step 3: Add schema and pure calculation code.**

Keep legacy `picks.ret1/ret3/ret5/ret10` untouched. Resolve market sessions from `trade_cal`. Use the first open session after `as_of` for entry, its `open` for purchase and `close` for `t1_close`; use sessions 2–10 `open` values for the remaining horizons. Keep each horizon row independently retryable. Existing entry-limit validation remains the only v1 no-buy rule; sell simulation uses the recorded target price when available.

- [ ] **Step 4: Integrate returns into the daily workflow and standalone action.**

Run prior-batch recalculation in `backfill_returns`. Add a separate manual calculation manager for the one-click returns button so users can recalculate without rerunning Agent. Both paths must call the same domain function and use the same `TaskTracker` idempotency contract.

- [ ] **Step 5: Implement API summaries.**

Return per horizon and group: planned count, filled count, unavailable count, measurable count, average, median, equal-slot portfolio gross return, coverage, status distribution, and item details. Return `available=false` when no measurable target data exists.

- [ ] **Step 6: Run focused tests.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_returns.py tests/test_experiments.py tests/api/test_returns.py -q --import-mode=importlib --basetemp=.pytest-tmp-returns-green -p no:cacheprovider
```

Expected: all focused tests pass.

---

### Task 7: Build the independent Agent report dashboard and SSE client

**Files:**
- Create: `workbench/ui_mockups/v2/p13_agent_dashboard.html`
- Create: `workbench/ui_mockups/v2/assets/js/pages/agent-dashboard.js`
- Modify: `workbench/ui_mockups/v2/assets/js/app-shell.js:25-40`
- Modify: `workbench/app/main.py:44-58`
- Modify: `workbench/ui_mockups/v2/assets/css/theme.css`
- Test: `workbench/tests/test_ui_pages.py`
- Create: `workbench/tests/api/test_agent_dashboard_contract.py`

**Interfaces:**
- Dashboard reads `/api/agents/jobs`, `/api/agents/jobs/{id}`, `/api/agents/jobs/{id}/events`, `/api/agents/jobs/{id}/stream`, `/api/returns/summary`, and `/api/returns`.
- Add navigation key `agent-dashboard` pointing to `p13_agent_dashboard.html`.
- `agent-dashboard.js` exports `renderEvent`, `renderDebateMatrix`, `renderReturnCards`, and `connectEventStream` for deterministic DOM tests.

- [ ] **Step 1: Write failing page contract tests.**

Assert the page is in the whitelist, navigation contains the page, scripts reference `agent-dashboard.js`, and the page contains anchors for:

```text
批次状态、实时通话、方法论、舆情、走势、多方、空方、风控、收益验证、T+1 收盘、T+10 开盘
```

- [ ] **Step 2: Run the tests and confirm they fail.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_ui_pages.py tests/api/test_agent_dashboard_contract.py -q --import-mode=importlib --basetemp=.pytest-tmp-agent-dashboard-red -p no:cacheprovider
```

- [ ] **Step 3: Implement the dashboard layout and event timeline.**

Use existing page header, panels, status tags, CSS tokens, responsive grid and light/dark theme. Render event role and stage labels in Chinese. Show `message.delta` as an updating bubble, replace it with validated `message.completed`, and show failure details without raw provider payloads.

- [ ] **Step 4: Implement SSE connection and history replay.**

On batch selection, load persisted events first, then open `EventSource` with `after_seq=lastSeq`. On reconnect, reuse the last sequence. Stop on completed/failed. Keep a manual refresh fallback for browsers that block streaming.

- [ ] **Step 5: Implement conclusion and return panels.**

Show analyst scores, public bull/bear/bull-counter content, risk-chair verdict and citations. Return cards must show each horizon's availability, portfolio return, coverage and missing reasons; never render missing as `0.00%`.

- [ ] **Step 6: Run page and JavaScript checks.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_ui_pages.py tests/api/test_agent_dashboard_contract.py -q --import-mode=importlib --basetemp=.pytest-tmp-agent-dashboard-green -p no:cacheprovider
node --check ui_mockups/v2/assets/js/pages/agent-dashboard.js
```

Expected: tests pass and JavaScript syntax check exits successfully.

---

### Task 8: Align documentation, configuration and existing pages

**Files:**
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `docs/PROJECT_GOAL.md`
- Modify: `workbench/config/settings.yaml`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/agents.js`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/ledger.js`
- Modify: `workbench/ui_mockups/v2/assets/js/pages/backtest.js`
- Modify: `workbench/ui_mockups/v2/p11_agents.html`
- Modify: `workbench/ui_mockups/v2/p5_ledger.html`
- Test: `workbench/tests/test_ui_pages.py`

**Interfaces:**
- Documentation must describe the actual nine-step workflow, `agent_events`, `experiment_returns`, SSE endpoints, and p13 dashboard.
- Existing p11 Agent page remains the launch/configuration page; p13 is the live/history report page.
- Existing ledger and backtest pages must label legacy returns separately from new `t1_close`/`t2_open`…`t10_open` returns.

- [ ] **Step 1: Update configuration and copy.**

Set the provider base URL to `https://grok.xuan.christmas/v1` and the model to `grok-4.5`. Keep the key only in the Git-ignored `workbench/.env`; the settings page must not receive or persist it.

- [ ] **Step 2: Update architecture and user-facing workflow documentation.**

Replace stale six-step descriptions with the nine-step `OneClickRunner` chain. Document the public-message handoff, SSE resume sequence, return formulas, no-cost assumption, table responsibilities, and explicit unavailable states. Remove stale historical claims such as “registry empty” and old test counts from current-status sections; retain them only in dated historical notes.

- [ ] **Step 3: Align existing pages.**

Add links from p11 to p13. Make p5 use new return endpoints for the experiment report and mark legacy fields. Make p9 distinguish old backtest inputs from new experiment return horizons.

- [ ] **Step 4: Run documentation/UI contract checks.**

```powershell
C:/Users/xuan/anaconda3/python.exe -m pytest tests/test_ui_pages.py -q --import-mode=importlib --basetemp=.pytest-tmp-docs-green -p no:cacheprovider
```

Expected: all page contract tests pass.

---

### Task 9: Full verification and one real provider smoke request

**Files:**
- No new production files.
- Test outputs only under ignored `.pytest-tmp-final/`.

- [ ] **Step 1: Run the complete isolated test suite with no external AI key.**

```powershell
$env:WORKBENCH_AI_API_KEY = ""
C:/Users/xuan/anaconda3/python.exe -m pytest tests -q --import-mode=importlib --basetemp=.pytest-tmp-final -p no:cacheprovider
```

Expected: zero failures; report the observed pass/fail counts.

- [ ] **Step 2: Verify the real database is unchanged by tests.**

Record the `data/market.duckdb` file size and modification time before the test run, then compare after it. The values must be identical.

- [ ] **Step 3: Run a minimal provider protocol smoke request only after the environment key is set externally.**

Use the configured base URL and model with a single short request. Do not print the key, authorization header, full prompt, full response, or response body. Record only HTTP success/failure class, model name, and whether the response matched the OpenAI-compatible schema. If the provider fails TLS, auth, or schema validation, leave the production system in explicit `unconfigured`/`provider_error` state and report the exact class without retrying indefinitely.

- [ ] **Step 4: Run the local end-to-end flow on a temporary database.**

Seed a temporary database, run the one-click workflow with a fake AI transport that emits deterministic analyst and debate JSON, consume the SSE stream, calculate returns against seeded bars, and assert that the dashboard endpoints expose the same event order and return values.

- [ ] **Step 5: Final cleanup and review.**

Delete only ignored test output directories created by this plan. Review the diff for secret leakage, stale six-step text, legacy return-field ambiguity, missing SSE route ordering, and any path that turns missing data into zero. Run the full test command once more after cleanup.
