"""交易日参数的校验契约。

所有接受 `trade_date` / `as_of` 的读接口必须共用同一套校验:格式不是 YYYYMMDD,
或者日期本身不存在(比如 20260231),一律 422 并点名字段。

这条契约值得单独一个文件,因为它横跨 6 个路由模块。校验器散在各处抄一遍必然漂移,
漂移的表现是:用户把日期写错一个字符,却收到「那天没有数据」而不是「日期写错了」,
于是去查数据,查不到问题。

运行:
    cd workbench
    python -m pytest tests/api/test_date_validation.py -q
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.api

# 每一条都是真实用户可能敲出来的错:随手打的字、带横线的习惯写法、
# 不存在的 2 月 31 日、复制粘贴粘多了的数字。
MALFORMED_DATES = ["notadate", "2026-08-21", "20260231", "999999999999", "2026082"]

# (路径, 参数名)。带路径参数的端点用真实存在的代码/行业名占位。
DATE_ENDPOINTS = [
    ("/api/reviews", "trade_date"),
    ("/api/news", "trade_date"),
    ("/api/news/industries", "trade_date"),
    ("/api/screener", "as_of"),
    ("/api/agents/results", "as_of"),
    ("/api/experiments", "as_of"),
]


@pytest.mark.parametrize("path,field", DATE_ENDPOINTS)
@pytest.mark.parametrize("bad", MALFORMED_DATES)
def test_malformed_dates_are_rejected_with_422(client, path, field, bad):
    """非法日期必须 422，绝不能当成「那天没数据」放行。"""
    response = client.get(path, params={field: bad})

    assert response.status_code == 422, (
        f"{path} 的 {field}={bad} 被放行了，用户会以为那天真的没数据"
    )
    error = response.json()["error"]
    assert error["code"] == "request_validation_failed"


@pytest.mark.parametrize("path,field", DATE_ENDPOINTS)
def test_nonexistent_calendar_date_names_the_offending_field(client, path, field):
    """20260231 格式合法但日期不存在，报错必须点名是哪个字段。

    只做正则校验会放过它：8 位数字全是数字，正则过得去，业务层随后当成
    「那天没有条目」。用户看到的是空结果，不是「你写的日期不存在」。
    """
    response = client.get(path, params={field: "20260231"})

    assert response.status_code == 422
    error = response.json()["error"]
    assert field in error["message"], f"报错没点名字段 {field}: {error['message']}"
    assert error["details"]["field"] == field


@pytest.mark.parametrize("path,field", DATE_ENDPOINTS)
def test_wellformed_date_is_not_rejected_by_the_validator(client, path, field):
    """合法日期不能被校验器误伤。

    只断言「不是 422」：各端点对该日期有没有数据、要不要配套参数，
    是它们各自的业务规则，不属于日期校验的职责。
    """
    from tests.test_run_scan_offline import _TRADE_DATES

    visible = _TRADE_DATES[-21]
    response = client.get(path, params={field: visible})

    assert response.status_code != 422, (
        f"{path} 把合法日期 {visible} 当成非法参数拒了"
    )


def test_stock_and_industry_scoped_dates_are_validated_too(client):
    """带路径参数的舆情端点也走同一套校验。"""
    for path, field in [
        ("/api/news/stocks/000001.SZ", "as_of"),
        ("/api/news/industries/专用机械", "trade_date"),
        ("/api/news/industries/专用机械", "as_of"),
    ]:
        response = client.get(path, params={field: "20260231"})
        assert response.status_code == 422, f"{path} 的 {field} 漏了校验"
        assert response.json()["error"]["details"]["field"] == field
