"""DuckDB 建表 DDL。

从 `engine/db.py` 原样搬出来的常量,没有改动任何一列的名字或类型——
搬动的目的只是让 `db.py` 回到 800 行以内,顺带让"表长什么样"能独立打开看,
不必在 900 行的 `Store` 方法里翻。

改表结构时注意两件事:
- `_SCHEMA` 是 f-string,DuckDB 里字面的花括号(注释里的 JSON 示例)必须写成
  `{{` / `}}`,否则 format 会当成占位符报 KeyError。
- 建表语句一律 `IF NOT EXISTS`,`Store(ensure_schema=True)` 每次开库都跑一遍,
  所以这里只能加表加列,不能改已有列的类型——那会在旧库上静默失配。
"""

from __future__ import annotations

_PICKS_SCHEMA = """
CREATE TABLE IF NOT EXISTS picks (
    run_date     VARCHAR,
    as_of        VARCHAR,
    strategy     VARCHAR,
    ts_code      VARCHAR,
    name         VARCHAR,
    industry     VARCHAR,
    rank         INTEGER,
    total        DOUBLE,
    money_class  VARCHAR,
    one_line     VARCHAR,
    contrib_json VARCHAR,
    feat_json    VARCHAR,
    ret1         DOUBLE,
    ret3         DOUBLE,
    ret5         DOUBLE,
    ret10        DOUBLE,
    PRIMARY KEY (as_of, strategy, ts_code)
);
"""

_SCHEMA = f"""
{_PICKS_SCHEMA}
CREATE TABLE IF NOT EXISTS stock_basic (
    ts_code   VARCHAR PRIMARY KEY,
    symbol    VARCHAR,
    name      VARCHAR,
    area      VARCHAR,
    industry  VARCHAR,
    market    VARCHAR,
    list_date VARCHAR
);
CREATE TABLE IF NOT EXISTS daily (
    ts_code    VARCHAR,
    trade_date VARCHAR,
    open       DOUBLE,
    high       DOUBLE,
    low        DOUBLE,
    close      DOUBLE,
    pre_close  DOUBLE,
    pct_chg    DOUBLE,
    vol        DOUBLE,
    amount     DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS daily_basic (
    ts_code       VARCHAR,
    trade_date    VARCHAR,
    turnover_rate DOUBLE,
    volume_ratio  DOUBLE,
    total_mv      DOUBLE,
    circ_mv       DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS moneyflow (
    ts_code         VARCHAR,
    trade_date      VARCHAR,
    net_mf_amount   DOUBLE,
    buy_lg_amount   DOUBLE,
    sell_lg_amount  DOUBLE,
    buy_elg_amount  DOUBLE,
    sell_elg_amount DOUBLE,
    PRIMARY KEY (ts_code, trade_date)
);
CREATE TABLE IF NOT EXISTS trade_cal (
    exchange   VARCHAR,
    cal_date   VARCHAR,
    is_open    INTEGER,
    PRIMARY KEY (exchange, cal_date)
);
{_PICKS_SCHEMA}
CREATE TABLE IF NOT EXISTS scan_runs (
    run_id                VARCHAR PRIMARY KEY,
    run_date              VARCHAR,
    as_of                 VARCHAR,
    strategy              VARCHAR,
    candidate_count       INTEGER,
    scored_count          INTEGER,
    passed_count          INTEGER,
    final_count           INTEGER,
    top_industries_json   VARCHAR
);
CREATE TABLE IF NOT EXISTS scan_rows (
    run_id               VARCHAR,
    ts_code              VARCHAR,
    name                 VARCHAR,
    industry             VARCHAR,
    rank                 INTEGER,
    total                DOUBLE,
    passed               BOOLEAN,
    selected             BOOLEAN,
    gate_reasons_json    VARCHAR,
    cat_scores_json      VARCHAR,
    money_class          VARCHAR,
    one_line             VARCHAR,
    contrib_json         VARCHAR,
    feat_json            VARCHAR,
    PRIMARY KEY (run_id, ts_code)
);
CREATE TABLE IF NOT EXISTS task_runs (
    task_id      VARCHAR PRIMARY KEY,
    kind         VARCHAR,   -- scan / postmortem / news / close_pipeline
    trade_date   VARCHAR,   -- 业务日期(as_of),幂等作用域的一部分
    strategy     VARCHAR,
    status       VARCHAR,   -- queued / running / succeeded / failed
    created_at   VARCHAR,   -- ISO8601(带时区)
    started_at   VARCHAR,
    finished_at  VARCHAR,
    heartbeat_at VARCHAR,   -- 运行中心跳,用于识别进程崩溃遗留的僵死任务
    result_json  VARCHAR,
    error_json   VARCHAR
);
CREATE TABLE IF NOT EXISTS news_sources (
    source_id    VARCHAR PRIMARY KEY,  -- 采集器内部标识,如 cninfo_notice
    name         VARCHAR,              -- 展示名
    kind         VARCHAR,              -- notice(公告) / news(新闻) / research(研报)
    home_url     VARCHAR,
    -- 来源基准可信度(0~1)。交易所/证监会公告高于门户转载。
    -- 它只是"来源"这一维,单条舆情的最终可信度还要结合正文完整性等因素。
    base_credibility DOUBLE,
    -- 合规备注:该来源的 robots / 使用条款结论,便于事后审计"为什么可以采"
    compliance_note  VARCHAR,
    enabled      BOOLEAN
);
CREATE TABLE IF NOT EXISTS news_items (
    news_id      VARCHAR PRIMARY KEY,  -- 规范化链接的哈希,天然幂等
    source_id    VARCHAR,
    title        VARCHAR,              -- 原文标题,不改写
    summary      VARCHAR,              -- 原文摘要/首段;来源没有就留 NULL,不自造
    url          VARCHAR,              -- 原始链接,研判可追溯的落点
    published_at VARCHAR,              -- 来源给出的发布时间(ISO8601)
    fetched_at   VARCHAR,              -- 本地抓取时间(ISO8601),与发布时间分开存
    trade_date   VARCHAR,              -- 归属交易日(按发布时间与收盘时点映射)
    -- 去重指纹:标题规范化后的哈希。同一条新闻被多家转载时 url 不同,
    -- 靠它归并;保留各自原始行,不删数据,只标 duplicate_of。
    dedup_key    VARCHAR,
    duplicate_of VARCHAR,              -- 指向首次出现的 news_id;自身为首条则 NULL
    event_type   VARCHAR,              -- 事件分类,未能判定时为 NULL(显示"未分类")
    sentiment    VARCHAR,              -- positive / negative / neutral;判不出为 NULL
    sentiment_score DOUBLE,            -- -1~1;判不出为 NULL,绝不填 0 冒充中性
    credibility  DOUBLE,               -- 0~1 综合可信度
    raw_json     VARCHAR               -- 来源原始字段,便于回溯与重算
);
CREATE TABLE IF NOT EXISTS news_links (
    news_id      VARCHAR,
    -- 关联对象:股票用 ts_code,行业用行业名。两者共用一张表,靠 link_type 区分
    link_type    VARCHAR,              -- stock / industry
    link_key     VARCHAR,
    -- 关联依据:code_in_text / name_in_text / source_field 等。
    -- 必填。没有依据的关联不写入——"这条新闻和这只股票有关"必须能说出为什么。
    match_basis  VARCHAR,
    match_text   VARCHAR,              -- 命中的原文片段,供页面展示佐证
    confidence   DOUBLE,               -- 0~1 关联置信度
    PRIMARY KEY (news_id, link_type, link_key)
);

-- 自选股:用户手动维护的观察池,与选股台账互相独立
CREATE TABLE IF NOT EXISTS watchlist (
    ts_code    VARCHAR PRIMARY KEY,
    name       VARCHAR,
    note       VARCHAR,                -- 用户备注,可空
    sort_order INTEGER NOT NULL DEFAULT 0,
    added_at   VARCHAR                 -- ISO8601
);
-- 机器学习训练记录:一次训练一个 run_id,指标与特征重要性落 JSON
CREATE TABLE IF NOT EXISTS ml_runs (
    run_id        VARCHAR PRIMARY KEY,
    model         VARCHAR,             -- 模型标识,如 gbdt / rf
    horizon_days  INTEGER,             -- 预测周期(T+N)
    trained_at    VARCHAR,             -- ISO8601
    train_start   VARCHAR,
    train_end     VARCHAR,
    val_start     VARCHAR,
    val_end       VARCHAR,
    n_train       INTEGER,
    n_val         INTEGER,
    n_features    INTEGER,
    features_json VARCHAR,             -- [{{name, importance}}]
    metrics_json  VARCHAR,             -- {{accuracy, auc, ic, ...}}
    notes         VARCHAR
);
-- 机器学习预测打分:最新截面每只股票一个 score/prob_up
CREATE TABLE IF NOT EXISTS ml_predictions (
    run_id     VARCHAR,
    ts_code    VARCHAR,
    trade_date VARCHAR,                -- 预测截面(as_of)
    score      DOUBLE,                 -- 模型输出(概率或归一化分)
    prob_up    DOUBLE,                 -- 预测上涨概率 0~1
    PRIMARY KEY (run_id, ts_code, trade_date)
);
-- 回测运行记录:策略/基准的净值曲线与统计指标
CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id       VARCHAR PRIMARY KEY,
    kind         VARCHAR,              -- strategy / benchmark
    strategy     VARCHAR,              -- 策略名,基准行为空
    as_of_start  VARCHAR,
    as_of_end    VARCHAR,
    n_points     INTEGER,              -- 净值曲线点数
    metrics_json VARCHAR,              -- {{total_return, max_drawdown, win_rate, ic, sharpe,...}}
    equity_json  VARCHAR,              -- [{{date, nav}}]
    created_at   VARCHAR               -- ISO8601
);

-- 多 agent 短线研判:每次研判一个批次(粗筛/深度学习/辩论/最终)
CREATE TABLE IF NOT EXISTS agent_runs (
    run_id        VARCHAR PRIMARY KEY,
    as_of         VARCHAR,              -- 行情截面(最新交易日)
    status        VARCHAR,              -- queued / running / succeeded / failed
    stage         VARCHAR,              -- coarse / deep / debate / done
    candidates    INTEGER,              -- 粗筛候选数量(请求值)
    depth         INTEGER,              -- 深度学习数量(请求值)
    final_count   INTEGER,              -- 最终输出数量(请求值)
    progress_json VARCHAR,              -- {{stage, step, total, message, at}}
    created_at    VARCHAR,              -- ISO8601(带时区)
    started_at    VARCHAR,
    finished_at   VARCHAR,
    heartbeat_at  VARCHAR,
    error_json    VARCHAR,
    result_json   VARCHAR               -- 批次摘要({{run_id, as_of, final: [...]}})
);
-- 多 agent 研判结果:每个 run 内每只入选股票一行
CREATE TABLE IF NOT EXISTS agent_judgments (
    run_id     VARCHAR,
    ts_code    VARCHAR,
    name       VARCHAR,
    industry   VARCHAR,
    rank       INTEGER,
    score      DOUBLE,                  -- 综合得分 0~100
    stance     VARCHAR,                 -- bullish / neutral / bearish
    thesis     VARCHAR,                 -- 核心逻辑(自然语言)
    risks      VARCHAR,                 -- JSON 数组:风险点
    stage_json VARCHAR,                 -- JSON:粗筛理由/三分析师/辩论纪要
    PRIMARY KEY (run_id, ts_code)
);
"""

__all__ = ["_SCHEMA", "_PICKS_SCHEMA"]
