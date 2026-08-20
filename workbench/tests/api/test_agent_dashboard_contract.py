from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
UI_ROOT = ROOT / "ui_mockups" / "v2"


def test_dashboard_page_is_whitelisted_and_uses_shared_shell():
    from app.main import _PAGES

    assert "p13_agent_dashboard.html" in _PAGES
    page = (UI_ROOT / "p13_agent_dashboard.html").read_text(encoding="utf-8")
    assert '/assets/css/theme.css' in page
    assert '/assets/js/app-shell.js' in page
    assert '/assets/js/pages/agent-dashboard.js' in page
    for anchor in ("批次状态", "实时通话", "收益验证", "T+1 收盘", "T+10 开盘"):
        assert anchor in page


def test_dashboard_controller_declares_api_and_replay_contract():
    controller = (UI_ROOT / "assets" / "js" / "pages" / "agent-dashboard.js").read_text(encoding="utf-8")
    for endpoint in (
        "/api/agents/jobs",
        "/api/agents/jobs/",
        "/api/returns/summary",
        "/api/returns",
    ):
        assert endpoint in controller
    assert "after_seq" in controller
    assert "EventSource" in controller
    assert "renderEvent" in controller
    assert "renderDebateMatrix" in controller
    assert "renderReturnCards" in controller
    assert "connectEventStream" in controller
    assert "暂无可测数据" in controller


def test_dashboard_nav_points_to_page():
    shell = (UI_ROOT / "assets" / "js" / "app-shell.js").read_text(encoding="utf-8")
    assert '"agent-dashboard", "p13_agent_dashboard.html"' in shell
