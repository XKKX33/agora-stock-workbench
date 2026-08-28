"""喂给模型的技术指标不许是预热噪音。

`ewm` 从第一根样本就吐数,不像 `rolling` 会给 NaN。9 根日线算出的
"零轴上红柱"、`ma60`、"周线多头" 看着都像结论,实际是还没算出来的中间态。
模型拿到这种字段没法分辨真假,只会当事实用。

运行:
    cd workbench
    python -m pytest tests/test_indicator_warmup.py -q
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.services.agents_data import AgentDataMixin


def _history(bars: int, *, start_close: float = 10.0) -> pd.DataFrame:
    """造 bars 根连续日线。

    收盘价必须有涨有跌:单调上行会让 RSI 的 avg_down 恒为 0,算出的 100 是
    除零产物而不是超买信号,那样就测不到真实分支了。
    """
    dates = pd.bdate_range("2026-01-01", periods=bars).strftime("%Y%m%d")
    wave = np.sin(np.arange(bars) / 3.0) * 0.4
    closes = start_close + np.arange(bars) * 0.05 + wave
    return pd.DataFrame(
        {
            "trade_date": dates,
            "close": closes,
            "pct_chg": np.r_[0.0, np.diff(closes) / closes[:-1] * 100],
            "high": closes * 1.01,
            "low": closes * 0.99,
            "vol": np.full(bars, 1000.0),
            "amount": np.full(bars, 10000.0),
        }
    )


class TestDailyBrief:
    """深度研判喂的日线摘要。"""

    def test_nine_bars_report_no_macd_and_no_long_moving_averages(self):
        brief = AgentDataMixin._daily_brief(_history(9))

        assert brief["macd"] == {"dif": None, "dea": None, "hist": None}
        assert brief["ma"]["ma5"] is not None, "5 日均线有 9 根就够了"
        assert brief["ma"]["ma20"] is None
        assert brief["ma"]["ma60"] is None
        assert brief["range_20"]["high"] is None
        # KDJ 的 rsv 用 rolling(9),第 9 根正好有值,所以这里该给数
        assert brief["kdj"]["k"] is not None

    def test_kdj_stays_empty_until_the_ninth_bar(self):
        assert AgentDataMixin._daily_brief(_history(8))["kdj"]["k"] is None
        assert AgentDataMixin._daily_brief(_history(9))["kdj"]["k"] is not None

    def test_ma60_is_not_a_nine_bar_average_wearing_a_sixty_bar_label(self):
        """tail(60).mean() 在 9 根时会算出 9 根的均值——数值正常,含义是错的。"""
        short = AgentDataMixin._daily_brief(_history(9))
        long = AgentDataMixin._daily_brief(_history(80))

        assert short["ma"]["ma60"] is None
        assert long["ma"]["ma60"] is not None

    def test_thirty_four_bars_unlock_the_full_macd(self):
        """dif 要 26 根;dea 是 dif 的 9 日 ema,要第 9 个 dif,即第 34 根。"""
        just_short = AgentDataMixin._daily_brief(_history(33))
        just_enough = AgentDataMixin._daily_brief(_history(34))

        assert just_short["macd"]["hist"] is None
        assert just_short["macd"]["dif"] is not None, "26 根就够算 dif"
        assert just_enough["macd"]["hist"] is not None
        assert just_enough["macd"]["dea"] is not None

    def test_full_history_reports_every_indicator(self):
        brief = AgentDataMixin._daily_brief(_history(120))

        assert brief["macd"]["dif"] is not None
        assert brief["kdj"]["k"] is not None
        assert brief["rsi6"] is not None
        assert brief["ma"]["ma60"] is not None
        assert brief["range_20"]["low"] is not None


class TestMacdState:
    """粗筛喂的一句话 MACD 状态。"""

    def test_short_history_gets_no_state_string(self):
        row = pd.Series({"ts_code": "600001.SH", "name": "测试", "industry": "半导体",
                         "money_class": None})

        compact = AgentDataMixin._compact_row(row, _history(9))

        assert compact["macd_state"] == ""
        assert compact["pct_5d"] is not None, "5 日涨幅有 9 根就够"
        assert compact["pct_20d"] is None

    def test_state_string_appears_exactly_at_thirty_four_bars(self):
        row = pd.Series({"ts_code": "600001.SH", "name": "测试", "industry": "半导体",
                         "money_class": None})

        assert AgentDataMixin._compact_row(row, _history(33))["macd_state"] == ""
        assert AgentDataMixin._compact_row(row, _history(34))["macd_state"] != ""

    def test_long_history_gets_a_real_state_string(self):
        row = pd.Series({"ts_code": "600001.SH", "name": "测试", "industry": "半导体",
                         "money_class": None})

        compact = AgentDataMixin._compact_row(row, _history(60))

        assert compact["macd_state"], "样本充足却没给 MACD 状态"
        assert "零轴" in compact["macd_state"]


class TestWeeklyBrief:
    """周线摘要。"""

    def test_two_weeks_of_data_do_not_produce_a_weekly_trend(self):
        brief = AgentDataMixin._weekly_brief(_history(10))

        assert brief["trend"] is None
        assert brief["macd_dif"] is None
        assert brief["last_6"], "周涨跌本身样本够就该给"

    def test_thirty_four_weeks_produce_a_weekly_trend(self):
        # 34 周 × 5 个交易日,略多给几根保证 resample 后满 34 周
        brief = AgentDataMixin._weekly_brief(_history(36 * 5))

        assert brief["trend"] in {"周线多头", "周线空头", "周线修复中"}
        assert brief["macd_dif"] is not None
