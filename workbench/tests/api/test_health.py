def test_health_reports_database_ready(client):
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.json()["database"] == "ready"


def test_root_serves_overview_page(client):
    response = client.get("/")

    assert response.status_code == 200
    assert "量化工作台" in response.text


def test_unknown_page_is_not_exposed(client):
    response = client.get("/../../.env")

    assert response.status_code == 404


def test_whitelist_covers_every_shipped_page(client):
    """白名单漏一页,那一页就 404。加页面必须同步加白名单。"""
    from app.main import _PAGES

    ui_root = client.app.state.settings.ui_root
    on_disk = {path.name for path in ui_root.glob("p*.html")} | {"index.html"}
    assert on_disk == _PAGES

    for page in sorted(_PAGES):
        response = client.get(f"/{page}")
        assert response.status_code == 200, page
