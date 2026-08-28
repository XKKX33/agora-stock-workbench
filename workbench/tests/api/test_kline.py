"""K线接口隔离测试:搜索 / 详情 / 404,使用 conftest 的隔离库。"""

from __future__ import annotations

from tests.test_run_scan_offline import AS_OF, _TRADE_DATES


def _codes(items: list[dict]) -> list[str]:
    return [item["ts_code"] for item in items]


def test_search_finds_seeded_stock(client):
    resp = client.get("/api/kline/search", params={"q": "600001"})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["items"]
    first = payload["items"][0]
    assert first["ts_code"] == "600001.SH"
    assert first["symbol"] == "600001"
    assert first["name"] == "强主升A"
    assert first["industry"] == "半导体"
    assert first["last_date"] == AS_OF
    assert first["close"] is not None
    assert first["pct_chg"] is not None


def test_search_empty_q_returns_all_sorted_by_amount(client):
    resp = client.get("/api/kline/search")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 7
    # 爆量涨停日的成交额最大,应排第一
    assert items[0]["ts_code"] == "600030.SH"


def test_search_by_name_and_symbol_prefix_priority(client):
    resp = client.get("/api/kline/search", params={"q": "强主升"})
    assert resp.status_code == 200
    assert _codes(resp.json()["items"]) == ["600001.SH", "600002.SH", "600003.SH"]

    resp = client.get("/api/kline/search", params={"q": "60000", "limit": 5})
    assert resp.status_code == 200
    codes = _codes(resp.json()["items"])
    # 600001/600002/600003 的 symbol 以 60000 开头,应排在前面
    assert codes[:3] == ["600001.SH", "600002.SH", "600003.SH"]


def test_kline_detail_shape_and_ascending_bars(client):
    resp = client.get("/api/kline/600001.SH", params={"days": 250})
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ts_code"] == "600001.SH"
    assert payload["symbol"] == "600001"
    assert payload["name"] == "强主升A"
    assert payload["industry"] == "半导体"
    assert payload["market"] == "主板"
    assert payload["list_date"] == "20100101"

    quote = payload["quote"]
    for key in (
        "trade_date", "open", "high", "low", "close", "pre_close",
        "pct_chg", "vol", "amount", "turnover_rate", "volume_ratio",
        "total_mv", "circ_mv",
    ):
        assert quote[key] is not None, key

    assert len(payload["recent5"]) == 5
    assert len(payload["moneyflow"]) == 6  # 种子库只灌了最后 6 个交易日的资金流
    for mf in payload["moneyflow"]:
        assert set(mf) >= {
            "trade_date", "net_mf_amount", "buy_lg_amount",
            "sell_lg_amount", "buy_elg_amount", "sell_elg_amount",
        }

    bars = payload["bars"]
    assert len(bars) == len(_TRADE_DATES)
    dates = [bar["trade_date"] for bar in bars]
    assert dates == sorted(dates)
    assert dates[0] == _TRADE_DATES[0]
    assert dates[-1] == AS_OF

    indicator_keys = [
        "ma5", "ma10", "ma20", "ma60",
        "ema12", "ema26", "dif", "dea", "macd",
        "k", "d", "j", "rsi6", "rsi12", "rsi24",
        "boll_mid", "boll_upper", "boll_lower",
    ]
    for bar in bars:
        for key in ("trade_date", "open", "high", "low", "close", "vol", "amount"):
            assert key in bar
    last = bars[-1]
    for key in indicator_keys:
        assert last[key] is not None, key
    first = bars[0]
    assert first["ma5"] is None
    assert first["rsi6"] is None


def test_kline_days_limit(client):
    resp = client.get("/api/kline/600001.SH", params={"days": 10})
    assert resp.status_code == 200
    assert len(resp.json()["bars"]) == 10


def test_kline_unknown_stock_404(client):
    resp = client.get("/api/kline/999999.SZ")
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "stock_not_found"


def test_kline_search_limit_boundary(client):
    # limit 超出白名单由 FastAPI 返回 422
    assert client.get("/api/kline/search", params={"limit": 101}).status_code == 422
    assert client.get("/api/kline/search", params={"limit": 0}).status_code == 422


def test_indicators_stay_empty_until_they_have_enough_bars(client):
    """预热期不许给数。

    ewm 和 rolling 不一样:rolling 样本不足会给 NaN,ewm 从第一根就吐数。
    首根 ema12==ema26 会让 dif/macd 恰好等于 0.0,画在图上就是一条贴着零轴的线,
    看起来像"多空平衡"的真实信号,其实只是还没算出来。
    """
    bars = client.get("/api/kline/600001.SH", params={"days": 250}).json()["bars"]
    assert len(bars) >= 40, "种子库太短,测不出预热边界"

    # 各指标第一个非空位置(0 基下标)
    def first_filled(key: str) -> int:
        for i, bar in enumerate(bars):
            if bar[key] is not None:
                return i
        raise AssertionError(f"{key} 整列都是空的")

    # 口径统一成 rolling(n):第 n 根(下标 n-1)起才有值
    # dif 要 26 根;dea 是 dif 的 9 日 ema,要第 9 个 dif,即第 34 根
    assert first_filled("dif") == 25
    assert first_filled("dea") == 33
    assert first_filled("macd") == 33
    # KDJ 的 rsv 用 rolling(9)
    assert first_filled("k") == 8
    assert first_filled("d") == 8
    # rolling 类保持原有语义
    assert first_filled("ma5") == 4
    assert first_filled("ma20") == 19
    assert first_filled("boll_mid") == 19
    # RSI 用 ewm,同样要掩掉预热
    assert first_filled("rsi6") == 5
    assert first_filled("rsi24") == 23


def test_first_bar_never_reports_a_zero_macd(client):
    """首根 macd 必须是 None,不能是 0.0——那是最容易被误读成信号的假值。"""
    bars = client.get("/api/kline/600001.SH", params={"days": 250}).json()["bars"]

    assert bars[0]["macd"] is None
    assert bars[0]["dif"] is None
    assert bars[0]["ema12"] is None


def test_short_history_stock_reports_no_long_period_indicators(client):
    """只取 10 根时,26/35 日指标必须整列为空。"""
    bars = client.get("/api/kline/600001.SH", params={"days": 10}).json()["bars"]

    assert len(bars) == 10
    for key in ("dif", "dea", "macd", "ma20", "ma60", "boll_mid"):
        assert all(bar[key] is None for bar in bars), f"{key} 在 10 根数据里给出了值"
    # 9 日以内的指标在够样本后可以有值
    assert bars[-1]["k"] is not None
    assert bars[-1]["ma5"] is not None
