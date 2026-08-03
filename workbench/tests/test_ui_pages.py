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


def test_theme_uses_tech_dark_palette():
    content = (UI_ROOT / "assets" / "css" / "theme.css").read_text(
        encoding="utf-8"
    )
    for token in (
        "--bg: #070b12",
        "--surface: #0d1420",
        "--navy: #16243a",
        "--text: #f4f7fb",
    ):
        assert token in content


def test_page_controllers_use_expected_apis():
    expected = {
        "overview.js": "/api/overview",
        "desk.js": "/api/stocks",
        "sentiment.js": "/api/sentiment",
        "foundry.js": "/api/overview",
        "factorlab.js": "/api/factors",
        "ledger.js": "/api/ledger",
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


def test_every_page_is_reachable_from_the_shell_nav():
    """新页面必须挂进侧边栏。只加文件不加导航等于藏起来了。"""
    shell = (UI_ROOT / "assets" / "js" / "app-shell.js").read_text(encoding="utf-8")
    for page in PAGES:
        assert page in shell, f"{page} 未出现在侧边栏导航里"