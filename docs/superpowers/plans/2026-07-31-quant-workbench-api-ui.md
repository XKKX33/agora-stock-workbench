# Hermes 股票量化工作台 API 与五页动态化实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立 FastAPI 服务，将扫描引擎、DuckDB 和五个精简后的黑白深蓝页面连接成可运行的本地股票量化工作台。

**Architecture:** Tushare 继续负责在线数据更新，DuckDB 保存行情、扫描批次、全部候选和选股台账。FastAPI 通过 repository/service 分层读取 DuckDB，并提供扫描任务接口；现有 HTML 使用公共原生 JavaScript 模块动态渲染，不引入前端框架。

**Tech Stack:** Python 3.11、FastAPI、Uvicorn、Pydantic、DuckDB、Pandas、原生 JavaScript、HTML、CSS、Pytest。

**Execution note:** 项目当前不是 Git 仓库，且用户禁止擅自提交，因此计划不包含 commit 步骤。

---

## 文件结构

新增后端文件：

- `workbench/app/__init__.py`：应用包入口。
- `workbench/app/main.py`：FastAPI 应用、静态页面和路由注册。
- `workbench/app/config.py`：路径和应用配置。
- `workbench/app/errors.py`：业务异常和统一错误响应。
- `workbench/app/dependencies.py`：服务依赖。
- `workbench/app/schemas/common.py`：公共响应结构。
- `workbench/app/schemas/scans.py`：扫描任务结构。
- `workbench/app/schemas/stocks.py`：股票列表和详情结构。
- `workbench/app/schemas/analytics.py`：总览、情绪、因子和台账结构。
- `workbench/app/repositories/market.py`：DuckDB 市场和扫描查询。
- `workbench/app/services/overview.py`：总览服务。
- `workbench/app/services/scans.py`：扫描任务服务。
- `workbench/app/services/stocks.py`：股票服务。
- `workbench/app/services/analytics.py`：情绪、因子和台账服务。
- `workbench/app/api/health.py`：健康检查。
- `workbench/app/api/overview.py`：总览接口。
- `workbench/app/api/scans.py`：扫描接口。
- `workbench/app/api/stocks.py`：股票接口。
- `workbench/app/api/analytics.py`：情绪、因子和台账接口。

新增前端文件：

- `workbench/ui_mockups/v2/assets/css/theme.css`：统一黑白深蓝主题。
- `workbench/ui_mockups/v2/assets/js/api.js`：API 请求和错误处理。
- `workbench/ui_mockups/v2/assets/js/app-shell.js`：导航、更新时间和扫描状态。
- `workbench/ui_mockups/v2/assets/js/format.js`：格式化工具。
- `workbench/ui_mockups/v2/assets/js/pages/overview.js`：总览页。
- `workbench/ui_mockups/v2/assets/js/pages/desk.js`：选股台。
- `workbench/ui_mockups/v2/assets/js/pages/sentiment.js`：情绪页。
- `workbench/ui_mockups/v2/assets/js/pages/foundry.js`：选股流程页。
- `workbench/ui_mockups/v2/assets/js/pages/factorlab.js`：因子页。
- `workbench/ui_mockups/v2/assets/js/pages/ledger.js`：台账页。

新增测试文件：

- `workbench/tests/api/conftest.py`：临时 DuckDB 和 FastAPI 测试客户端。
- `workbench/tests/api/test_health.py`：健康与静态页面。
- `workbench/tests/api/test_overview.py`：总览。
- `workbench/tests/api/test_scans.py`：扫描任务。
- `workbench/tests/api/test_stocks.py`：股票列表与详情。
- `workbench/tests/api/test_analytics.py`：情绪、因子与台账。

修改文件：

- `workbench/engine/db.py`：增加扫描批次和扫描明细表。
- `workbench/engine/run_scan.py`：持久化全部候选及漏斗摘要。
- `workbench/requirements.txt`：增加 FastAPI 与 Uvicorn。
- `workbench/ui_mockups/v2/index.html`：精简总览并动态化。
- `workbench/ui_mockups/v2/p1_desk.html`：精简选股台并动态化。
- `workbench/ui_mockups/v2/p2_sentiment.html`：精简情绪页并动态化。
- `workbench/ui_mockups/v2/p3_foundry.html`：精简流程页并动态化。
- `workbench/ui_mockups/v2/p4_factorlab.html`：精简因子页并动态化。
- `workbench/ui_mockups/v2/p5_ledger.html`：精简台账页并动态化。
- `README.md`：更新安装、启动和使用方法。
- `ARCHITECTURE.md`：更新服务端和动态页面架构。

---

### Task 1: 固化依赖与应用配置

**Files:**
- Modify: `workbench/requirements.txt`
- Create: `workbench/app/__init__.py`
- Create: `workbench/app/config.py`
- Test: `workbench/tests/api/test_health.py`

- [ ] **Step 1: 写配置失败测试**

```python
def test_app_settings_resolve_database_from_workbench(tmp_path):
    settings = AppSettings(workbench_root=tmp_path)
    assert settings.db_path == tmp_path / "data" / "market.duckdb"
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `C:\Users\xuan\anaconda3\python.exe -m pytest -q tests/api/test_health.py`

Expected: FAIL，原因是 `app.config` 尚不存在。

- [ ] **Step 3: 增加 Web 依赖**

在 `requirements.txt` 增加：

```text
fastapi>=0.115
uvicorn[standard]>=0.30
httpx>=0.27
```

- [ ] **Step 4: 实现应用配置**

```python
@dataclass(frozen=True)
class AppSettings:
    workbench_root: Path = WORKBENCH_ROOT
    host: str = "127.0.0.1"
    port: int = 8765

    @property
    def db_path(self) -> Path:
        return self.workbench_root / "data" / "market.duckdb"

    @property
    def ui_root(self) -> Path:
        return self.workbench_root / "ui_mockups" / "v2"
```

- [ ] **Step 5: 运行配置测试**

Expected: PASS。

### Task 2: 将完整扫描结果写入 DuckDB

**Files:**
- Modify: `workbench/engine/db.py`
- Modify: `workbench/engine/run_scan.py`
- Test: `workbench/tests/test_run_scan_offline.py`

- [ ] **Step 1: 写扫描持久化失败测试**

```python
result = run_scan(online=False, db_path=dbp, record=True)
with Store(dbp) as store:
    runs = store.scan_runs()
    rows = store.scan_rows(runs.iloc[0]["run_id"])
assert len(runs) == 1
assert len(rows) == result.scored_count
assert set(rows["passed"].unique()).issubset({True, False})
```

- [ ] **Step 2: 新增表结构**

`scan_runs` 保存：`run_id`、`run_date`、`as_of`、`strategy`、`candidate_count`、`scored_count`、`passed_count`、`final_count`、`top_industries_json`。

`scan_rows` 保存：`run_id`、`ts_code`、`name`、`industry`、`rank`、`total`、`passed`、`selected`、`gate_reasons_json`、`cat_scores_json`、`money_class`、`one_line`、`contrib_json`、`feat_json`。

- [ ] **Step 3: 增加 Store 方法**

```python
def record_scan(self, run_row: dict, rows: pd.DataFrame) -> None: ...
def latest_scan_run(self, strategy: str | None = None) -> pd.DataFrame: ...
def scan_runs(self, strategy: str | None = None) -> pd.DataFrame: ...
def scan_rows(self, run_id: str) -> pd.DataFrame: ...
```

- [ ] **Step 4: 修改扫描结果结构**

`ScanResult` 增加 `run_id` 和 `scored` 字段；`run_scan()` 生成 UUID，并在 `record=True` 时一次性写入 `scan_runs`、`scan_rows` 和 `picks`。

- [ ] **Step 5: 删除自动复盘的静默跳过**

不再使用 `except Exception: print(...)` 掩盖错误。复盘失败必须抛出异常并由扫描任务记录为失败。

- [ ] **Step 6: 运行离线扫描测试**

Expected: 原测试及新增持久化断言全部通过。

### Task 3: 建立 FastAPI 骨架和统一错误格式

**Files:**
- Create: `workbench/app/main.py`
- Create: `workbench/app/errors.py`
- Create: `workbench/app/dependencies.py`
- Create: `workbench/app/schemas/common.py`
- Create: `workbench/app/api/health.py`
- Test: `workbench/tests/api/conftest.py`
- Test: `workbench/tests/api/test_health.py`

- [ ] **Step 1: 写健康检查和静态页面失败测试**

```python
def test_health_reports_database_ready(client):
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["database"] == "ready"

def test_root_serves_overview_page(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "量化工作台" in response.text
```

- [ ] **Step 2: 实现统一错误结构**

```json
{
  "error": {
    "code": "database_unavailable",
    "message": "DuckDB 数据库不存在",
    "details": {}
  }
}
```

- [ ] **Step 3: 创建应用工厂**

```python
def create_app(settings: AppSettings | None = None) -> FastAPI:
    app = FastAPI(title="Hermes Quant Workbench")
    app.state.settings = settings or AppSettings()
    app.include_router(health.router, prefix="/api")
    app.mount("/assets", StaticFiles(directory=app.state.settings.ui_root / "assets"), name="assets")
    return app
```

根页面和五个 HTML 页面通过白名单路由使用 `FileResponse` 返回，禁止任意文件路径访问。

- [ ] **Step 4: 运行测试**

Expected: 健康检查和静态页面测试通过。

### Task 4: 实现总览和数据新鲜度接口

**Files:**
- Create: `workbench/app/repositories/market.py`
- Create: `workbench/app/services/overview.py`
- Create: `workbench/app/schemas/analytics.py`
- Create: `workbench/app/api/overview.py`
- Test: `workbench/tests/api/test_overview.py`

- [ ] **Step 1: 写总览失败测试**

```python
def test_overview_uses_latest_scan_and_table_dates(client):
    payload = client.get("/api/overview").json()
    assert payload["latest_trade_date"] == "20260730"
    assert payload["latest_scan"]["scored_count"] == 7
    assert payload["tables"]["daily"]["row_count"] > 0
```

- [ ] **Step 2: 实现只读 repository**

每个请求打开独立 `Store`，查询完成后关闭，避免跨线程复用 DuckDB 连接。

- [ ] **Step 3: 实现 overview service**

组合数据表日期、最新扫描批次、最新入选股票和扫描任务状态。数据库不存在或没有扫描记录时返回明确错误或空状态，不读取 JSON 文件。

- [ ] **Step 4: 注册 `GET /api/overview` 并运行测试**

Expected: PASS。

### Task 5: 实现后台扫描任务接口

**Files:**
- Create: `workbench/app/schemas/scans.py`
- Create: `workbench/app/services/scans.py`
- Create: `workbench/app/api/scans.py`
- Test: `workbench/tests/api/test_scans.py`

- [ ] **Step 1: 写任务状态失败测试**

```python
def test_scan_job_runs_offline(client):
    created = client.post("/api/scans", json={"online": False, "record": True})
    assert created.status_code == 202
    job_id = created.json()["job_id"]
    assert wait_for_job(client, job_id)["status"] == "succeeded"

def test_second_running_scan_returns_conflict(client, blocking_scan):
    assert client.post("/api/scans", json={"online": False}).status_code == 409
```

- [ ] **Step 2: 实现单任务执行器**

使用 `ThreadPoolExecutor(max_workers=1)`、线程锁和内存任务字典。任务状态只包含 `queued`、`running`、`succeeded`、`failed`。

- [ ] **Step 3: 扫描失败时保留原始错误**

任务记录异常类型和简洁消息；API 返回该任务失败，不自动切换离线模式或旧结果。

- [ ] **Step 4: 注册扫描接口并运行测试**

Expected: 成功、失败和冲突测试通过。

### Task 6: 实现股票列表和详情接口

**Files:**
- Create: `workbench/app/schemas/stocks.py`
- Create: `workbench/app/services/stocks.py`
- Create: `workbench/app/api/stocks.py`
- Test: `workbench/tests/api/test_stocks.py`

- [ ] **Step 1: 写筛选和详情失败测试**

```python
def test_stocks_filter_by_passed_and_industry(client):
    payload = client.get("/api/stocks", params={"passed": True, "industry": "半导体"}).json()
    assert all(item["passed"] for item in payload["items"])
    assert all(item["industry"] == "半导体" for item in payload["items"])

def test_stock_detail_contains_factor_trace(client):
    payload = client.get("/api/stocks/600001.SH").json()
    assert payload["ts_code"] == "600001.SH"
    assert payload["factors"]
    assert "gate_reasons" in payload
```

- [ ] **Step 2: 实现分页和排序白名单**

只允许按 `rank`、`total`、`industry` 和 `money_class` 排序，拒绝把用户输入直接拼入 SQL。

- [ ] **Step 3: 解析 JSON 字段**

`gate_reasons_json`、`cat_scores_json`、`contrib_json` 和 `feat_json` 在 service 层解析为结构化响应。

- [ ] **Step 4: 补充行情和资金序列**

详情接口使用 `Store.history()` 和 `Store.moneyflow_tail()`，所有查询限制在扫描 `as_of` 及以前。

- [ ] **Step 5: 运行测试**

Expected: 筛选、排序、分页、详情和不存在股票测试通过。

### Task 7: 实现情绪、因子和台账接口

**Files:**
- Create: `workbench/app/services/analytics.py`
- Create: `workbench/app/api/analytics.py`
- Test: `workbench/tests/api/test_analytics.py`

- [ ] **Step 1: 写分析接口失败测试**

```python
def test_sentiment_marks_unavailable_fields(client):
    payload = client.get("/api/sentiment").json()
    assert payload["community_sentiment"]["availability"] == "pending"

def test_factor_response_has_no_fake_ml_prediction(client):
    payload = client.get("/api/factors/600001.SH").json()
    assert payload["machine_learning"]["availability"] == "pending"
    assert "probability" not in payload["machine_learning"]

def test_ledger_summary_uses_only_filled_returns(client):
    payload = client.get("/api/ledger/summary").json()
    assert payload["ret5"]["sample_count"] >= 0
```

- [ ] **Step 2: 实现情绪接口**

行业热度来自最新扫描批次；资金共振来自扫描明细的 `money_class`；尚未接入的社区舆情只返回缺失原因。

- [ ] **Step 3: 实现因子接口**

从扫描明细计算权重、覆盖率、缺失率和候选池分布。没有模型文件时只返回 `availability: pending`。

- [ ] **Step 4: 实现台账与统计**

统计只使用非空收益，响应同时返回样本数。样本为零时平均值和命中率为 `null`。

- [ ] **Step 5: 运行测试**

Expected: 情绪、因子、台账空状态和统计测试通过。

### Task 8: 建立公共前端层和主题

**Files:**
- Create: `workbench/ui_mockups/v2/assets/css/theme.css`
- Create: `workbench/ui_mockups/v2/assets/js/api.js`
- Create: `workbench/ui_mockups/v2/assets/js/app-shell.js`
- Create: `workbench/ui_mockups/v2/assets/js/format.js`
- Modify: `workbench/ui_mockups/v2/index.html`
- Modify: `workbench/ui_mockups/v2/p1_desk.html`
- Modify: `workbench/ui_mockups/v2/p2_sentiment.html`
- Modify: `workbench/ui_mockups/v2/p3_foundry.html`
- Modify: `workbench/ui_mockups/v2/p4_factorlab.html`
- Modify: `workbench/ui_mockups/v2/p5_ledger.html`

- [ ] **Step 1: 定义主题变量**

```css
:root {
  --bg: #070b12;
  --surface: #0d1420;
  --surface-raised: #121c2b;
  --navy: #16243a;
  --line: #263449;
  --text: #f4f7fb;
  --text-muted: #8d9aab;
  --accent: #5f8fc9;
  --positive: #3da678;
  --negative: #c85d67;
  --warning: #c49a4a;
}
```

- [ ] **Step 2: 实现统一 API 请求**

```javascript
export async function request(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json", ...(options.headers || {}) } });
  const payload = await response.json().catch(() => null);
  if (!response.ok) throw new ApiError(response.status, payload?.error?.message || "请求失败");
  return payload;
}
```

- [ ] **Step 3: 实现公共页面壳**

统一导航、当前页面状态、数据日期、扫描状态、错误条和刷新按钮。页面控制器只负责自己的业务区域。

- [ ] **Step 4: 删除旧主题依赖**

六个 HTML 页面只引用公共主题和页面控制器，不保留大面积渐变、发光、紫色和青色背景。

### Task 9: 动态化总览与选股台

**Files:**
- Create: `workbench/ui_mockups/v2/assets/js/pages/overview.js`
- Create: `workbench/ui_mockups/v2/assets/js/pages/desk.js`
- Modify: `workbench/ui_mockups/v2/index.html`
- Modify: `workbench/ui_mockups/v2/p1_desk.html`

- [ ] **Step 1: 重写总览页面结构**

保留数据状态、最近扫描、入选股票和五页入口；删除教学文字、重复真实性说明和装饰指标。

- [ ] **Step 2: 总览读取 `/api/overview`**

加载成功后渲染真实交易日、表状态和入选股票；空扫描显示“尚未扫描”；错误时不显示旧值。

- [ ] **Step 3: 重写选股台结构**

保留扫描按钮、筛选栏、候选表和详情面板。列表读取 `/api/stocks`，详情读取 `/api/stocks/{code}`。

- [ ] **Step 4: 接入扫描任务**

按钮调用 `POST /api/scans`，轮询任务状态；成功后重新加载总览和股票列表，失败时显示任务错误。

### Task 10: 动态化情绪、流程、因子和台账页

**Files:**
- Create: `workbench/ui_mockups/v2/assets/js/pages/sentiment.js`
- Create: `workbench/ui_mockups/v2/assets/js/pages/foundry.js`
- Create: `workbench/ui_mockups/v2/assets/js/pages/factorlab.js`
- Create: `workbench/ui_mockups/v2/assets/js/pages/ledger.js`
- Modify: `workbench/ui_mockups/v2/p2_sentiment.html`
- Modify: `workbench/ui_mockups/v2/p3_foundry.html`
- Modify: `workbench/ui_mockups/v2/p4_factorlab.html`
- Modify: `workbench/ui_mockups/v2/p5_ledger.html`

- [ ] **Step 1: 情绪页读取 `/api/sentiment`**

只展示市场阶段、行业热度、资金共振和待接入舆情状态。

- [ ] **Step 2: 流程页读取 `/api/overview` 与 `/api/stocks`**

用紧凑漏斗展示候选数、打分数、通过数和入选数；候选表展示淘汰原因。

- [ ] **Step 3: 因子页读取 `/api/factors` 与 `/api/factors/{code}`**

展示策略权重、覆盖率、缺失率和单股贡献；机器学习区域只显示训练状态。

- [ ] **Step 4: 台账页读取 `/api/ledger` 与 `/api/ledger/summary`**

支持筛选和分页；无收益样本时显示明确空状态，不绘制误导图表。

### Task 11: 完整验证与文档更新

**Files:**
- Modify: `README.md`
- Modify: `ARCHITECTURE.md`
- Modify: `workbench/.env.example`

- [ ] **Step 1: 安装到现有 Anaconda 环境**

不创建新环境。使用现有解释器安装缺失依赖：

```powershell
C:\Users\xuan\anaconda3\python.exe -m pip install -r workbench\requirements.txt
```

- [ ] **Step 2: 运行后端测试**

```powershell
C:\Users\xuan\anaconda3\python.exe -m pytest -q workbench\tests
```

Expected: 全部通过。

- [ ] **Step 3: 启动服务**

```powershell
C:\Users\xuan\anaconda3\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8765
```

- [ ] **Step 4: 浏览器验收六个入口**

检查 `/`、`p1_desk.html`、`p2_sentiment.html`、`p3_foundry.html`、`p4_factorlab.html`、`p5_ledger.html`。

每页验证加载、成功、空数据和 API 错误状态，确认无横向溢出、旧静态示例和大面积高饱和色。

- [ ] **Step 5: 更新项目文档**

README 写清安装、启动、在线扫描、离线扫描和测试；架构文档写清 Tushare → DuckDB → FastAPI → 页面数据流。

---

## 自检结果

- 设计文档中的五页动态化、信息删减、黑白深蓝主题、Tushare 更新和 DuckDB 查询均有对应任务。
- 候选池与淘汰原因缺少持久化的问题由 Task 2 解决。
- 所有接口字段在扫描表、选股台账或明确的待接入状态中有来源。
- 计划没有使用静态 JSON 兜底、模拟数据或自动离线降级。
- 计划没有创建新虚拟环境、Git 分支、worktree 或提交。
