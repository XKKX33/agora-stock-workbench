import re
from pathlib import Path


UI_ROOT = Path(__file__).resolve().parents[1] / "ui_mockups" / "v2"

PAGES = {
    "index.html": "overview.js",
    "p1_desk.html": "desk.js",
    "p2_sentiment.html": "sentiment.js",
    "p3_foundry.html": "foundry.js",
    "p4_factorlab.html": "factorlab.js",
    "p5_ledger.html": "ledger.js",
    "p6_chart.html": "chart.js",
    "p7_news.html": "news.js",
    "p8_ai.html": "ai.js",
    "p9_backtest.html": "backtest.js",
    "p10_watchlist.html": "watchlist.js",
    "p11_agents.html": "agents.js",
    "p13_agent_dashboard.html": "agent-dashboard.js",
}


def test_pages_use_shared_theme_and_dynamic_controller():
    for page, controller in PAGES.items():
        content = (UI_ROOT / page).read_text(encoding="utf-8")
        assert '/assets/css/theme.css' in content
        assert '/assets/js/app-shell.js' in content
        assert f'/assets/js/pages/{controller}' in content
        assert 'id="app-shell"' in content
        assert "结构示例" not in content

    shell = (UI_ROOT / "assets" / "js" / "app-shell.js").read_text(
        encoding="utf-8"
    )
    assert 'id="app-status"' in shell


def test_theme_has_persistent_light_dark_tokens():
    content = (UI_ROOT / "assets" / "css" / "theme.css").read_text(
        encoding="utf-8"
    )
    for token in (
        '[data-theme="light"]',
        '[data-theme="dark"]',
        "--bg: #f7f8fa",
        "--surface: #ffffff",
        "--text: #202124",
        "--accent: #0b57d0",
        "--bg: #171717",
        "--surface: #222222",
    ):
        assert token in content.lower()


def test_page_controllers_use_expected_apis():
    expected = {
        "overview.js": "/api/overview",
        "desk.js": "/api/stocks",
        "sentiment.js": "/api/sentiment",
        "foundry.js": "/api/pipelines",
        "factorlab.js": "/api/factors",
        "ledger.js": "/api/experiments",
        "chart.js": "/api/kline",
        "news.js": "/api/news",
        "ai.js": "/api/reviews",
        "backtest.js": "/api/backtest",
        "watchlist.js": "/api/watchlist",
    }
    for filename, endpoint in expected.items():
        content = (UI_ROOT / "assets" / "js" / "pages" / filename).read_text(
            encoding="utf-8"
        )
        assert endpoint in content
    desk = (UI_ROOT / "assets" / "js" / "pages" / "desk.js").read_text(encoding="utf-8")
    assert 'import { query, request } from "/assets/js/api.js";' in desk
    assert 'import { escapeHtml, formatDate, formatNumber, formatPercent, statusTag } from "/assets/js/format.js";' in desk


def test_page_controllers_import_every_shared_helper_they_use():
    shared_helpers = {
        "/assets/js/api.js": {"query", "request"},
        "/assets/js/app-shell.js": {
            "clearError",
            "getWorkContext",
            "initShell",
            "setLoading",
            "setStatus",
            "setWorkContext",
            "showError",
            "workContextParams",
        },
        "/assets/js/format.js": {
            "escapeHtml",
            "formatDate",
            "formatNumber",
            "formatPercent",
            "statusTag",
        },
    }
    import_pattern = re.compile(
        r'import\s*\{(?P<names>[^}]*)\}\s*from\s*["\'](?P<module>/assets/js/(?:api|app-shell|format)\.js)["\'];'
    )
    failures = []

    for controller in (UI_ROOT / "assets" / "js" / "pages").glob("*.js"):
        content = controller.read_text(encoding="utf-8")
        imported = {module: set() for module in shared_helpers}
        for match in import_pattern.finditer(content):
            imported[match.group("module")].update(
                name.strip() for name in match.group("names").split(",")
            )
        code = import_pattern.sub("", content)
        for module, helpers in shared_helpers.items():
            if module.endswith("format.js"):
                used = {name for name in helpers if re.search(rf"\b{name}\b", code)}
            else:
                used = {name for name in helpers if re.search(rf"\b{name}\s*\(", code)}
            missing = used - imported[module]
            if missing:
                failures.append(f"{controller.name} -> {module}: {sorted(missing)}")

    assert not failures, "缺少共享模块导入: " + "; ".join(failures)


def test_every_page_is_reachable_from_the_shell_nav():
    """新页面必须挂进侧边栏。只加文件不加导航等于藏起来了。"""
    shell = (UI_ROOT / "assets" / "js" / "app-shell.js").read_text(encoding="utf-8")
    for page in PAGES:
        assert page in shell, f"{page} 未出现在侧边栏导航里"


def test_foundry_has_one_click_pipeline_contract():
    html = (UI_ROOT / "p3_foundry.html").read_text(encoding="utf-8")
    js = (UI_ROOT / "assets" / "js" / "pages" / "foundry.js").read_text(
        encoding="utf-8"
    )
    assert 'id="one-click"' in html
    assert "一键全流程" in html
    assert 'request("/api/pipelines"' in js
    assert 'query("/api/pipelines"' in js
    assert "persist_experiment" in js
    for step in (
        "preflight",
        "calendar",
        "market_data",
        "backfill_returns",
        "integrity",
        "scan",
        "collect_news",
        "agents",
        "persist_experiment",
    ):
        assert step in js
    for state in ("等待", "运行", "成功", "失败"):
        assert state in js
    assert 'id="pipeline-error"' in html
    assert "task.error.message" in js
    assert "失败于" in js
    assert "completed_steps" in js
    assert "currentTask?.error?.message" in js
    assert "这一步没有留下说明。" not in js
    assert 'status === "skipped"' in js



def test_shell_module_loads_with_all_navigation_entries():
    """导航由同一 ESM 模块渲染；语法错误会令所有入口消失。"""
    shell = (UI_ROOT / "assets" / "js" / "app-shell.js").read_text(encoding="utf-8")
    assert shell.count("export function initShell(page)") == 1
    for label in ("自选", "舆情", "研判看板"):
        assert label in shell


def test_foundry_hands_same_pipeline_run_to_agent_dashboard_popup():
    js = (UI_ROOT / "assets" / "js" / "pages" / "foundry.js").read_text(encoding="utf-8")
    assert 'window.open("about:blank", "hermes-agent-dashboard")' in js
    assert "p13_agent_dashboard.html?run_id=" in js
    assert "hermes.work-context" in js
    assert "popup 被浏览器拦截" in js


def test_agent_dashboard_prefers_explicit_url_run_id_before_saved_context():
    js = (UI_ROOT / "assets" / "js" / "pages" / "agent-dashboard.js").read_text(encoding="utf-8")
    assert "new URLSearchParams(window.location.search).get(\"run_id\")" in js
    assert "const contextRunId = getWorkContext().run_id;" in js
    assert "jobs.some((job) => (job.job_id || job.task_id || job.run_id) === contextRunId)" in js
    assert "urlRunId || selectedJobId || contextJob" in js
def test_foundry_exposes_schedule_gate_and_manual_controls():
    """调度状态、闸门结论、强制重跑与按日期补齐都必须在页面上能看见、能点。"""
    html = (UI_ROOT / "p3_foundry.html").read_text(encoding="utf-8")
    js = (UI_ROOT / "assets" / "js" / "pages" / "foundry.js").read_text(
        encoding="utf-8"
    )
    for control in (
        'id="schedule-grid"',
        'id="gate-note"',
        'id="trigger-form"',
        'id="trigger-date"',
        'id="trigger-strategy"',
        'id="trigger-online"',
        'id="trigger-force"',
        'id="trigger-ignore-gate"',
        'id="trigger-result"',
        'id="backfill-form"',
        'id="backfill-count"',
        'id="backfill-force"',
        'id="backfill-run"',
        'id="backfill-result"',
        'id="step-detail"',
        'id="group-tabs"',
        'id="group-rows"',
        'id="history-tabs"',
        'id="history-rows"',
    ):
        assert control in html
    assert 'request("/api/pipelines/status")' in js
    assert 'request("/api/pipelines/workflow")' in js
    assert 'request("/api/pipelines/backfill"' in js
    assert 'query("/api/experiments"' in js
    assert "ignore_gate" in js
    assert "force:" in js
    assert "job.reused" in js
    assert "data_cutoff_at" in js
    assert "output_keys" in js
    # 闸门四种结论与补齐进度字段必须有中文落点,不能只丢原始英文
    for reason in ("calendar_missing", "calendar_stale", "before_run_after", "ready"):
        assert reason in js
    for field in ("current_date", "completed", "reused", "failed_date", "dates"):
        assert field in js
    for kind in ("one_click_pipeline", "one_click_backfill"):
        assert kind in html or kind in js


def test_foundry_group_table_is_paginated():
    """基准组是整个候选池,一页装不下:必须有翻页控件并写明总条数,不能悄悄只显示前 200 条。"""
    html = (UI_ROOT / "p3_foundry.html").read_text(encoding="utf-8")
    js = (UI_ROOT / "assets" / "js" / "pages" / "foundry.js").read_text(
        encoding="utf-8"
    )
    css = (UI_ROOT / "assets" / "css" / "theme.css").read_text(encoding="utf-8")
    for control in ('id="group-prev"', 'id="group-page-label"', 'id="group-next"'):
        assert control in html
    assert "pagination-bar" in html
    assert ".pagination-bar" in css
    # 请求必须带页码,页数必须由接口返回的总条数算出
    assert "page: groupPage" in js
    assert "per_page: GROUP_PAGE_SIZE" in js
    assert "renderGroupPagination(data.total)" in js
    assert "当前组 ${formatNumber(data.total, 0)} 条" in js


def test_foundry_step_states_match_theme_styles():
    """步骤状态类名必须在 theme.css 里真有样式,否则页面上看不出成功和失败。"""
    css = (UI_ROOT / "assets" / "css" / "theme.css").read_text(encoding="utf-8")
    js = (UI_ROOT / "assets" / "js" / "pages" / "foundry.js").read_text(
        encoding="utf-8"
    )
    styled = set(re.findall(r"\.pipeline-step\.([a-z-]+)", css))
    block = re.search(r"const STATE_LABELS = \{([^}]*)\}", js)
    assert block is not None
    states = set(re.findall(r"(\w+):", block.group(1)))
    # waiting 是默认态,不需要额外样式;其余状态必须有对应 CSS 规则
    assert states - {"waiting"} <= styled
    assert ".mode-tabs .tab-btn" in css


def test_ledger_reads_experiments_by_signal_date():
    html = (UI_ROOT / "p5_ledger.html").read_text(encoding="utf-8")
    js = (UI_ROOT / "assets" / "js" / "pages" / "ledger.js").read_text(
        encoding="utf-8"
    )
    for control in (
        'id="as-of"',
        'id="group"',
        'id="ts-code"',
        'id="entry-status"',
        'id="prev-page"',
        'id="next-page"',
        'id="page-label"',
    ):
        assert control in html
    for field in (
        "as_of",
        "group_name",
        "ts_code",
        "entry_status",
        "entry_date",
        "entry_price",
        "returns",
        "t1_close",
        "t3_open",
        "t5_open",
        "t10_open",
    ):
        assert field in js
    for legacy in ("/api/experiments/summary", "ret1", "ret3", "ret5", "ret10", "entry_reason", "legacy"):
        assert legacy not in js and legacy not in html
    assert 'query("/api/experiments"' in js
    assert 'import { escapeHtml, formatDate, formatNumber, formatPercent, statusTag } from "/assets/js/format.js";' in js
    assert "summaryFilters()" in js
    assert "currentPage" in js
    assert "escapeHtml(formatDate(item.as_of))" in js
    assert "escapeHtml(formatNumber(item.rank, 0))" in js


def test_agent_dashboard_does_not_treat_pipeline_run_as_agent_job():
    js = (UI_ROOT / "assets" / "js" / "pages" / "agent-dashboard.js").read_text(encoding="utf-8")
    assert "const contextRunId = getWorkContext().run_id;" in js
    assert "jobs.some((job) => (job.job_id || job.task_id || job.run_id) === contextRunId)" in js
    assert "urlRunId || selectedJobId || getWorkContext().run_id" not in js


def test_foundry_escapes_rule_rank_before_rendering():
    js = (UI_ROOT / "assets" / "js" / "pages" / "foundry.js").read_text(
        encoding="utf-8"
    )
    assert "escapeHtml(formatNumber(item.rank, 0))" in js


def test_selection_actions_have_unambiguous_labels():
    desk = (UI_ROOT / "p1_desk.html").read_text(encoding="utf-8")
    foundry = (UI_ROOT / "p3_foundry.html").read_text(encoding="utf-8")
    agents = (UI_ROOT / "p11_agents.html").read_text(encoding="utf-8")
    assert "系统规则扫描" in desk
    assert "一键全流程" in foundry
    assert "Agent 选股研判" in agents
    assert "单股深度研判" in agents


def test_shell_has_accessible_persistent_theme_control():
    shell = (UI_ROOT / "assets" / "js" / "app-shell.js").read_text(
        encoding="utf-8"
    )
    assert "prefers-color-scheme" in shell
    assert "localStorage" in shell
    assert 'id="theme-toggle"' in shell
    assert 'aria-label="切换明亮与暗夜主题"' in shell
    assert 'title="切换明亮与暗夜主题"' in shell


def test_theme_uses_times_for_latin_and_simsun_for_chinese():
    css = (UI_ROOT / "assets" / "css" / "theme.css").read_text(
        encoding="utf-8"
    )
    expected_stack = '"Times New Roman", "SimSun", "宋体", serif'
    assert f"--font-display: {expected_stack};" in css
    assert f"--font-body: {expected_stack};" in css


def test_sentiment_uses_news_contract_and_defines_market_stage_once():
    html = (UI_ROOT / "p2_sentiment.html").read_text(encoding="utf-8")
    js = (UI_ROOT / "assets" / "js" / "pages" / "sentiment.js").read_text(
        encoding="utf-8"
    )
    assert "news_sentiment" in js
    assert "community_sentiment" not in js
    assert js.count("function renderMarketStage(") == 1
    assert "news-sentiment" in html

def test_agent_dashboard_has_report_anchors_and_safe_client_contract():
    html = (UI_ROOT / "p13_agent_dashboard.html").read_text(encoding="utf-8")
    js = (UI_ROOT / "assets" / "js" / "pages" / "agent-dashboard.js").read_text(encoding="utf-8")
    for anchor in ("批次状态", "实时通话", "方法论", "舆情", "走势", "多方", "空方", "风控", "收益验证", "T+1 收盘", "T+10 开盘"):
        assert anchor in html or anchor in js
    for endpoint in ("/api/agents/jobs", "/api/agents/jobs/", "/events", "/stream", "/api/returns/summary", "/api/returns"):
        assert endpoint in js
    for export in ("renderEvent", "renderDebateMatrix", "renderReturnCards", "connectEventStream"):
        assert f"export function {export}" in js
    assert "escapeHtml" in js
    assert "暂无可测数据" in js
def test_shell_exposes_persistent_work_context_contract():
    shell = (UI_ROOT / "assets" / "js" / "app-shell.js").read_text(encoding="utf-8")
    for marker in (
        'hermes.work-context',
        'export function getWorkContext()',
        'export function setWorkContext(',
        'export function workContextParams(',
        'id="work-context"',
        "run_id",
        "data_cutoff",
        "missing_reason",
    ):
        assert marker in shell


def test_batch_pages_forward_work_context_without_gating_independent_paths():
    pages = {
        "desk.js": ("workContextParams", "/api/stocks"),
        "ledger.js": ("workContextParams", "/api/experiments"),
        "backtest.js": ("workContextParams", "/api/backtest"),
        "agents.js": ("workContextParams", "/api/agents/judge"),
        "agent-dashboard.js": ("workContextParams", "/api/agents/jobs"),
    }
    for filename, markers in pages.items():
        content = (UI_ROOT / "assets" / "js" / "pages" / filename).read_text(encoding="utf-8")
        for marker in markers:
            assert marker in content


def test_work_context_clears_an_explicitly_empty_stale_field():
    shell = (UI_ROOT / "assets" / "js" / "app-shell.js").read_text(encoding="utf-8")
    assert "Object.prototype.hasOwnProperty.call(next, field)" in shell
    assert "delete context[field]" in shell
    # Watchlist and K-line search remain independent latest-market paths.
    desk = (UI_ROOT / "assets" / "js" / "pages" / "desk.js").read_text(encoding="utf-8")
    assert 'query("/api/watchlist"' in desk
    assert 'query("/api/kline/search"' in desk
def test_backtest_cost_controls_are_wired_to_existing_fields():
    """回测页的买卖成本与成交规则控件必须真正触发重算。"""
    html = (UI_ROOT / "p9_backtest.html").read_text(encoding="utf-8")
    js = (UI_ROOT / "assets" / "js" / "pages" / "backtest.js").read_text(encoding="utf-8")
    for control in ("buy-cost-bps", "sell-cost-bps", "rebalance-mode", "limit-up-fill-policy"):
        assert f'id="{control}"' in html
        assert f'"#{control}"' in js
    assert '["#top-k", "#buy-cost-bps", "#sell-cost-bps", "#rebalance-mode", "#limit-up-fill-policy"]' in js
def test_desk_scan_button_imports_request_for_real_submission():
    """选股台扫描按钮不能因缺少 request 导入而只在点击时崩溃。"""
    js = (UI_ROOT / "assets" / "js" / "pages" / "desk.js").read_text(encoding="utf-8")
    assert 'import { query, request } from "/assets/js/api.js";' in js


def test_foundry_reenables_one_click_after_terminal_task():
    """流程任务结束后，一键按钮必须恢复可点击，才能重跑或验收失败分支。"""
    js = (UI_ROOT / "assets" / "js" / "pages" / "foundry.js").read_text(encoding="utf-8")
    terminal_block = js[js.index('if (["succeeded", "failed"].includes(task.status))'):js.index('  } catch (error) {', js.index('if (["succeeded", "failed"].includes(task.status))'))]
    assert 'document.querySelector("#one-click").disabled = false;' in terminal_block
