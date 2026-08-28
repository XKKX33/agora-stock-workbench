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


def _js_function(source: str, name: str, *, exported: bool = True) -> str:
    """从 JS 源码里切出一个具名函数的完整定义（含函数体）。

    按大括号配平找结尾，这样前端改了函数体测试也跟着走，不会测一份过期副本。
    `exported=False` 用于模块内部函数（没有 export 前缀）。
    """
    prefix = "export function" if exported else "function"
    start = source.index(f"{prefix} {name}(")
    body_start = source.index("{", start)
    depth = 0
    for index in range(body_start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                # 去掉 export：切片要在 node 里当普通脚本跑。
                return source[start:index + 1].replace("export function", "function", 1)
    raise AssertionError(f"{name} 的函数体大括号不配平")


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
def test_agent_uses_minimax_m3_and_global_font_is_simsun():
    settings = (Path(__file__).resolve().parents[1] / "config" / "settings.local.yaml").read_text(encoding="utf-8")
    theme = (UI_ROOT / "assets" / "css" / "theme.css").read_text(encoding="utf-8")
    assert "model: minimax-m3" in settings
    expected_stack = '"SimSun", "宋体", serif'
    assert f'--font-display: {expected_stack};' in theme
    assert f'--font-body: {expected_stack};' in theme
    assert f'--font-mono: {expected_stack};' in theme


def test_common_button_rows_center_controls_cleanly():
    theme = (UI_ROOT / "assets" / "css" / "theme.css").read_text(encoding="utf-8")
    assert ".button-row, .filters" in theme
    assert "justify-content: center" in theme
    assert ".page-header .button-row" in theme
    assert "align-items: center" in theme


def test_primary_pages_are_reachable_from_shell_nav():
    """有独立结论要看的页面都必须能从侧栏点到。

    台账与回测原先不在侧栏，只能靠 p3 里的文字链接或手敲地址进——用户因此找不到台账页，
    连续三轮误以为改动没生效。诊断页（情绪 / 因子 / AI 复盘 / Agent 看板）仍不进侧栏：
    它们是从主流程页面下钻进去的，不是独立入口。
    """
    shell = (UI_ROOT / "assets" / "js" / "app-shell.js").read_text(encoding="utf-8")
    for page in (
        "index.html", "p1_desk.html", "p7_news.html", "p11_agents.html",
        "p5_ledger.html", "p9_backtest.html", "p10_watchlist.html", "p12_settings.html",
    ):
        assert page in shell, f"{page} 不在侧栏，用户点不到"
    for page in ("p2_sentiment.html", "p3_foundry.html", "p4_factorlab.html", "p8_ai.html", "p13_agent_dashboard.html"):
        assert page not in shell, f"{page} 是下钻页，不该占侧栏入口"


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



def test_shell_module_loads_with_six_navigation_entries():
    """导航由同一 ESM 模块渲染，主流程只保留六个入口。"""
    shell = (UI_ROOT / "assets" / "js" / "app-shell.js").read_text(encoding="utf-8")
    assert shell.count("export function initShell(page)") == 1
    for label in ("总览", "方法论选股", "板块舆情", "多 Agent 辩论", "自选与行情", "设置"):
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


def test_ledger_groups_rows_by_batch_not_only_by_signal_date(tmp_path):
    """同一信号日跑多次时，台账必须按批次分段，不能只按信号日。

    线上实测 20260821 一天跑了 6 次。只按 as_of 分组的话六个批次全挤在一条
    「信号日」分隔线下，同一只票重复出现 6 遍且看不出区别——用户无法判断自己在看
    哪一次的入选结果。信号日说明「基于哪天的行情」，说明不了「什么时候跑的这一次」。
    """
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        raise AssertionError("本机没有 node，无法跑前端行为测试")

    js = (UI_ROOT / "assets" / "js" / "pages" / "ledger.js").read_text(encoding="utf-8")
    # 分组键必须同时含信号日与批次；缺 run_id 就是旧缺陷。
    assert "const key = `${item.as_of}|${item.run_id}`;" in js, "分组键没带 run_id"

    harness = tmp_path / "stamp.mjs"
    harness.write_text(
        _js_function(js, "runStamp", exported=False)
        + "\nconsole.log(JSON.stringify(process.argv.slice(2).map((v) => runStamp(v === '__null__' ? null : v))));\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [node, str(harness), "2026-08-23T01:30:50.006488+00:00", "2026-08-23 16:58:13", "__null__"],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    stamps = json.loads(result.stdout)
    # 精确到分钟：同一天跑 6 次，只到小时的话 6 次里可能有两次撞在同一格。
    assert stamps[0] == "2026-08-23 01:30"
    assert stamps[1] == "2026-08-23 16:58"
    # 时间缺失时返回空串，由调用方显示「时间未记录」，不能伪造一个时间。
    assert stamps[2] == ""


def test_ledger_exposes_batch_filter_backed_by_the_batches_endpoint():
    """批次下拉框必须存在且由 /api/experiments/batches 填充。

    从分页后的台账数据里提取批次是不行的：一页 200 行，更早的批次不在这一页，
    下拉框会缺项，用户选不到自己要看的那一次。
    """
    html = (UI_ROOT / "p5_ledger.html").read_text(encoding="utf-8")
    js = (UI_ROOT / "assets" / "js" / "pages" / "ledger.js").read_text(encoding="utf-8")
    assert 'id="run-id"' in html
    assert '"/api/experiments/batches"' in js
    # 选「全部批次」必须清掉工作上下文里锁定的 run_id，否则选了全部却只显示一个批次。
    assert "delete params.run_id;" in js
    # 换批次等于换一份名单，页码要回到第一页。
    assert 'querySelector("#run-id")?.addEventListener("change"' in js


def test_ledger_and_backtest_declare_their_opposite_persistence_semantics():
    """两页必须各自写明数据来源的保存语义，且互相指路。

    台账读 `experiment_decisions`（累积：每次运行都留），回测读 `picks`（覆盖：每个信号日
    只留最新一次）。两种语义相反，不写在页面上用户会以为两页条数应该一致——实测同一信号日
    跑 6 次时台账有 6 份、回测只用 1 份。

    这两句是纯提示文字，删掉不会有任何报错，只会让人重新踩一遍坑，所以用测试钉住。
    """
    ledger = (UI_ROOT / "p5_ledger.html").read_text(encoding="utf-8")
    backtest = (UI_ROOT / "p9_backtest.html").read_text(encoding="utf-8")

    # 台账：声明自己是完整历史，并指向回测说明差异。
    assert "完整历史" in ledger
    assert "p9_backtest.html" in ledger
    # 回测：声明只用每个信号日最新一次，并说明为什么（重复计入会虚高），指回台账。
    assert "只有最新一次运行的名单" in backtest
    assert "重复计入" in backtest
    assert "p5_ledger.html" in backtest
    # 说明块要有独立样式，否则会混进正文被忽略——用户已反馈过「没看到」。
    css = (UI_ROOT / "assets" / "css" / "theme.css").read_text(encoding="utf-8")
    assert ".source-note {" in css
    assert 'class="source-note"' in ledger and 'class="source-note"' in backtest


def test_batch_divider_styles_outrank_the_generic_table_border_rule():
    """批次分隔线的样式必须能压过通用表格边框规则。

    这里踩过两次：`.date-divider td { border-top: 2px solid var(--accent) }` 写的是蓝线，
    渲染出来是灰线——因为 `html[data-theme] th, html[data-theme] td` 的
    `border-color: var(--line-soft)` 优先级更高（html + 属性 + 元素 = 0,1,2 对 0,1,1），
    静默把颜色覆盖了。CSS 覆盖不报错、不警告，只是「改了没效果」，所以必须用测试钉住。

    同理 `.run-stamp` 会被同元素上的 `.muted` 染成浅灰。
    """
    css = (UI_ROOT / "assets" / "css" / "theme.css").read_text(encoding="utf-8")
    # 分隔线与运行时刻都必须带 html[data-theme] 前缀，才与通用规则同级并靠顺序取胜。
    for selector in (
        "html[data-theme] .date-divider td",
        "html[data-theme] .date-divider span",
        "html[data-theme] .date-divider small",
        "html[data-theme] .run-stamp",
    ):
        assert selector in css, f"{selector} 缺失：样式会被通用表格规则覆盖"
    # 低优先级的旧写法不能残留，否则读代码的人以为它在生效。
    assert "\n.date-divider td {" not in css
    assert "\n.run-stamp {" not in css
    # 分隔线靠底色区分，不靠一条边框线——边框颜色正是被覆盖的那个属性。
    divider = css[css.index("html[data-theme] .date-divider td"):]
    divider = divider[: divider.index("}")]
    assert "background: var(--accent)" in divider, "分隔线必须有实底色才和数据行分得开"
    # .muted 会赢，所以 run-stamp 的颜色必须显式提权。
    stamp = css[css.index("html[data-theme] .run-stamp"):]
    stamp = stamp[: stamp.index("}")]
    assert "color: var(--accent) !important" in stamp


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


def test_theme_uses_simsun_for_all_text_roles():
    css = (UI_ROOT / "assets" / "css" / "theme.css").read_text(
        encoding="utf-8"
    )
    expected_stack = '"SimSun", "宋体", serif'
    assert f"--font-display: {expected_stack};" in css
    assert f"--font-body: {expected_stack};" in css
    assert f"--font-mono: {expected_stack};" in css

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
    for export in ("renderEvent", "renderDebateMatrix", "renderFinalDebate", "renderReturnCards", "connectEventStream"):
        assert f"export function {export}" in js
    assert "escapeHtml" in js
    assert "暂无可测数据" in js


def test_agent_dashboard_renders_four_round_final_debate_without_faking_missing_text():
    """终稿四段(多方/空方/多方反驳/风控)必须渲染，缺失要标缺失而不是补占位文本。"""
    html = (UI_ROOT / "p13_agent_dashboard.html").read_text(encoding="utf-8")
    js = (UI_ROOT / "assets" / "js" / "pages" / "agent-dashboard.js").read_text(encoding="utf-8")
    assert 'id="final-debate"' in html
    assert "终稿多空辩论" in html
    for key in ("bull_case", "bear_case", "rebuttal", "risk_control"):
        assert key in js
    assert "多方反驳" in js
    assert "缺失：该轮辩论未产出" in js
    # 实时事件必须累积后整体重渲染，否则六格面板会被单条事件冲掉。
    assert "liveEvents = [...liveEvents, event]" in js
    assert "renderFinalDebate(job?.judgments)" in js


def test_debate_matrix_groups_events_by_stock_instead_of_letting_them_overwrite(tmp_path):
    """辩论矩阵必须按股票分组，不能让不同股票的同名角色互相覆盖。

    20 只候选共用七个角色名（methodology/sentiment/trend/bull/bear/bull_counter/risk_chair）。
    原实现只按角色去重，后跑完的股票覆盖先跑完的——线上实测一屏里方法论/舆情/走势讲
    002209.SZ、多方讲 000703.SZ、空方与反驳讲 001337.SZ、风控又回到 000703.SZ，
    拼出一场根本不存在的辩论。

    这里真跑 JS 而不是断言源码文本：文本断言守不住行为。把两个纯函数从模块里切出来
    （它们不碰 DOM），用 node 执行并断言分组结果。
    """
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        raise AssertionError("本机没有 node，无法跑前端行为测试")

    source = (UI_ROOT / "assets" / "js" / "pages" / "agent-dashboard.js").read_text(encoding="utf-8")
    # 只取纯函数与它们依赖的两个小工具，避开顶层 initShell() 与绝对路径 import。
    needed = ("parseJson", "eventContent", "eventStockCode", "matrixStockCodes", "latestByRoleForStock")
    for name in needed:
        assert name in source, f"{name} 不在模块里，纯函数被改名或删除"

    harness = tmp_path / "matrix.mjs"
    harness.write_text(
        "const parseJson = (value, fallback = {}) => { if (value && typeof value === 'object') return value;"
        " try { return JSON.parse(value || ''); } catch { return fallback; } };\n"
        "const eventContent = (event) => parseJson(event?.content_json ?? event?.content, {});\n"
        "const eventStockCode = (event) => String(eventContent(event)?.ts_code ?? event?.ts_code ?? '');\n"
        + _js_function(source, "matrixStockCodes")
        + "\n"
        + _js_function(source, "latestByRoleForStock")
        + "\n"
        "const events = JSON.parse(process.argv[2]);\n"
        "const codes = matrixStockCodes(events);\n"
        "const perStock = {};\n"
        "for (const code of codes) {\n"
        "  const latest = latestByRoleForStock(events, code);\n"
        "  perStock[code] = Object.fromEntries(Object.entries(latest).map(([role, e]) => [role, eventStockCode(e)]));\n"
        "}\n"
        "console.log(JSON.stringify({ codes, perStock }));\n",
        encoding="utf-8",
    )

    def event(code: str, role: str) -> dict:
        return {"role": role, "content_json": json.dumps({"role": role, "ts_code": code})}

    # 三只股票交错发言，复刻线上事件顺序：后到的不得覆盖先到的。
    events = [
        event("002209.SZ", "methodology"),
        event("000703.SZ", "methodology"),
        event("002209.SZ", "bull"),
        event("001337.SZ", "methodology"),
        event("000703.SZ", "bull"),
        event("001337.SZ", "bear"),
        event("000703.SZ", "risk_chair"),
        event("002209.SZ", "risk_chair"),
    ]
    result = subprocess.run(
        [node, str(harness), json.dumps(events, ensure_ascii=False)],
        capture_output=True, text=True, encoding="utf-8", timeout=60,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)

    # 三只股票都出现在选择器里，按首次发言顺序。
    assert payload["codes"] == ["002209.SZ", "000703.SZ", "001337.SZ"]
    # 每只股票的每个角色都只能是它自己的发言——这是缺陷的要害。
    for code, roles in payload["perStock"].items():
        assert roles, f"{code} 分组后没有任何角色"
        for role, owner in roles.items():
            assert owner == code, f"{code} 的 {role} 格显示了 {owner} 的发言"
    # 002209.SZ 只发了 methodology 与 bull、risk_chair：没发的角色不得凭空出现。
    assert set(payload["perStock"]["002209.SZ"]) == {"methodology", "bull", "risk_chair"}
    assert set(payload["perStock"]["001337.SZ"]) == {"methodology", "bear"}


def test_debate_matrix_returns_nothing_when_no_stock_selected():
    """没选股票时不得回落到"最后一条事件"——那正是旧缺陷的形态。"""
    js = (UI_ROOT / "assets" / "js" / "pages" / "agent-dashboard.js").read_text(encoding="utf-8")
    assert "if (!code) return latest;" in js
    # 切批次必须重置选中股票，否则上一批的代码会把新批次面板判成空。
    assert "resetMatrixStock()" in js
    assert 'id="matrix-stock-select"' in (UI_ROOT / "p13_agent_dashboard.html").read_text(encoding="utf-8")


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
def test_shared_task_panel_contract_is_declared():
    panel = (UI_ROOT / "assets" / "js" / "task-panel.js").read_text(encoding="utf-8")
    assert "export function createTaskPanel" in panel
    assert "data-task-bar" in panel
    assert "data-task-logs" in panel
    assert "aria-valuenow" in panel
def test_three_step_pages_mount_task_panel_and_context_handoffs():
    desk_html = (UI_ROOT / "p1_desk.html").read_text(encoding="utf-8")
    desk_js = (UI_ROOT / "assets" / "js" / "pages" / "desk.js").read_text(encoding="utf-8")
    news_html = (UI_ROOT / "p7_news.html").read_text(encoding="utf-8")
    news_js = (UI_ROOT / "assets" / "js" / "pages" / "news.js").read_text(encoding="utf-8")
    assert 'id="scan-task-panel"' in desk_html
    assert 'from "/assets/js/task-panel.js"' in desk_js
    assert '/api/scans/${jobId}' in desk_js
    assert 'p7_news.html' in desk_html
    assert 'candidate_codes' in desk_js
    assert 'id="news-task-panel"' in news_html
    assert 'from "/assets/js/task-panel.js"' in news_js
    assert '/api/news/collect/${jobId}' in news_js
    assert 'p11_agents.html' in news_html


def test_shell_exposes_the_primary_workbench_entries():
    shell = (UI_ROOT / "assets" / "js" / "app-shell.js").read_text(encoding="utf-8")
    for label in ("总览", "方法论选股", "板块舆情", "多 Agent 辩论", "实验台账", "组合回测", "自选与行情", "设置"):
        assert label in shell
    # 下钻页不占侧栏入口：它们从主流程页面进，独立列出反而让侧栏失焦。
    for label in ("因子", "AI复盘", "研判看板"):
        assert label not in shell
def test_three_step_controllers_resume_jobs_and_forward_work_context():
    desk = (UI_ROOT / "assets" / "js" / "pages" / "desk.js").read_text(encoding="utf-8")
    news = (UI_ROOT / "assets" / "js" / "pages" / "news.js").read_text(encoding="utf-8")
    agents = (UI_ROOT / "assets" / "js" / "pages" / "agents.js").read_text(encoding="utf-8")
    assert 'document.querySelector("#scan-progress").textContent = done.status' in desk
    assert "resumeScan" in desk
    assert "getWorkContext()" in news
    assert "setWorkContext" in news
    assert 'query("/api/news/collect/jobs"' in news
    assert "context.candidate_codes" in agents
def test_overview_is_the_unified_workbench_surface():
    html = (UI_ROOT / "index.html").read_text(encoding="utf-8")
    js = (UI_ROOT / "assets" / "js" / "pages" / "overview.js").read_text(encoding="utf-8")
    for control in ("overview-scan", "overview-news", "overview-agents", "overview-refresh", "overview-pipeline", "overview-blackboard", "overview-progress", "overview-agent-panels"):
        assert f'id="{control}"' in html
    for endpoint in ("/api/scans", "/api/news/collect", "/api/agents/judge", "/api/pipelines"):
        assert endpoint in js
    assert "online: true" in js
    assert js.count("force: true") >= 4
    assert "const jobId =" in js
    assert "methodology" in html and "sentiment" in html and "trend" in html
    assert "bull_counter" in js and "risk_chair" in html


def test_task_tracker_log_entries_keep_level_and_detail():
    tasks = (UI_ROOT / "assets" / "js" / "task-panel.js").read_text(encoding="utf-8")
    assert "log.level" in tasks
    assert "log.detail" in tasks
    assert "scrollTop" in tasks
