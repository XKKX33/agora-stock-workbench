# 规格:收盘后自动复盘 + 舆情系统

status: archived
日期: 2026-08-01
范围: 舆情采集链路、收盘后任务链、复盘装配、API 与 AI 边界。以下均为已实现事实,接口与字段以代码为准。

## 一、舆情采集链路

### 数据表与关键字段(engine/db.py)

news_sources(来源登记):
- source_id 主键,采集器内部标识;name 展示名;kind 分 notice(公告)、news(新闻)、research(研报)。
- home_url 来源首页;base_credibility 来源基准可信度(0~1),未逐条评估时留 NULL 显示未评估,不给默认值。
- compliance_note 合规备注必填:记录 robots 与使用条款核验结论,缺失时拒绝登记(NewsSource 构造直接抛错)。
- enabled 是否启用。

news_items(舆情条目):
- news_id 主键:规范化链接(剔除跟踪参数、保留并排序其余查询参数)做 SHA-256 取前 32 位,同链接天然幂等。
- title 原文标题不改写;summary 原文摘要,来源没有就留 NULL,不自造。
- url 原始链接(研判可追溯落点);published_at 来源发布时间与 fetched_at 本地抓取时间分开存;trade_date 归属交易日。
- dedup_key 去重指纹 = SHA-256(trade_date 与规范化标题拼接);转载条目 url 不同但指纹相同,靠它归并。
- duplicate_of 指向转载首条的 news_id,只标不删;自身为首条时 NULL。
- event_type、sentiment、sentiment_score、credibility 判不出时一律 NULL,绝不填 0 冒充中性。
- raw_json 保留来源原始字段,便于回溯与重算。

news_links(舆情与股票、行业的关联):
- 主键 (news_id, link_type, link_key);link_type 分 stock(股票,key 为 ts_code)、industry(行业,key 为行业名)。
- match_basis 匹配依据必填,没有依据的关联不写入。取值与置信度:
  - source_field 来源字段直接给出,置信度 1.0
  - code_in_text 正文命中股票代码,置信度 0.95
  - name_in_text 正文命中股票名(名称长度不小于 3),置信度 0.75
  - industry_name_in_text 正文命中行业名,置信度 0.7
  - via_linked_stock 经已关联股票间接带出,置信度 0.6 乘原关联置信度
- match_text 命中的原文片段,供页面展示佐证;confidence 关联置信度。

### 采集窗口与归属(engine/news.py、engine/news_text.py)

- 采集窗口 = 上一开市日收盘时点至目标日收盘时点,正是归属到目标日的那段区间。
- 归属交易日按 close_cutoff(默认 15:00)切分:开市日收盘时点及之前发布归当日,之后或非开市日发布归后一开市日。
- 日历向前取 20 个开市日(CALENDAR_LOOKBACK)用于解析归属;关联用的股票档案来自目标日行情截面,截面为空直接抛 NewsCollectError,不静默。
- 时间衰减 time_decay 按 half_life_days(默认 3 天)计算;发布时间晚于基准时直接抛错,这是未来数据泄漏,不允许四舍五入。

### 前视纪律

- 所有行情、资金、舆情查询一律按 <= as_of 过滤,杜绝前视。
- 归属交易日由 close_cutoff 切分,复盘关联与查询用 as_of 等于 trade_date 截断。
- time_decay 对晚于基准的发布时间直接抛错,未来数据不得进入计算。

### 去重与缺失语义

- 单条不合格(缺链接、标题空、来源无发布时间、解析失败)拒收,逐条记原因;采集器整体抛错则整批上抛,不降级成没有舆情。
- 无启用来源返回空结果,由调用方标 unavailable;页面必须区分未配置与采到 0 条,两者含义完全不同。
- 热榜无权威发布时间:published_at 记采集时刻,并在 raw_json 的 time_basis 显式标注 first_seen_at_collect,不冒充发布时间;summary 留 NULL;无可追溯链接的条目丢弃;全部平台失败显式抛错。

### 合规闸门与 GPL 隔离(engine/news_config.py、engine/news_trendradar.py)

- FETCHER_REGISTRY 是采集器白名单,配置引用未注册采集器时启动即抛 NewsConfigError 并列出当前已注册项;当前只注册 trendradar。
- 每个来源强制 compliance_note,合规核验结论(robots、使用条款、数据源)落到该字段,核验过的来源才进白名单。
- GPL-3.0 的 TrendRadar 源码原样保留在 vendor/TrendRadar,本包不拷一行;engine/news_trendradar.py 用 importlib.util.spec_from_file_location 按单文件路径加载 vendor 下 fetcher.py 的 DataFetcher,刻意不 import trendradar 包;vendor 缺失抛 TrendRadarConfigError。
- expected_domain 交给 vendor 的 DataFetcher 做 HTTPS 与域名校验,防链接劫持与数据篡改。

## 二、收盘后任务链

### 五步执行链(engine/close_pipeline.py)

闸门确认目标交易日后执行 PIPELINE_STEPS 固定五步:ingest_market(更新行情;离线模式 skipped)、scan(扫描,以实际 as_of 为准,与闸门目标不一致时回写校正)、backfill_returns(回填 T+N 收益)、collect_news(采集舆情)、postmortem(生成复盘)。

- 每步结果独立记录(StepResult),任务失败时能看出卡在哪一步、之前哪几步已写库。
- 失败即中止并上抛:摄取失败还继续扫描,会拿旧数据产出看似正常的复盘,不做静默降级。
- StepResult 的 status 只有 ok、skipped、unavailable;失败不用状态表示,直接抛异常。
- on_step 回调(心跳、落进度)异常也会中止链条。

### 幂等与并发(engine/db.py、app/services/pipelines.py)

- task_runs 表:task_id 主键;kind 实际取值 scan、close_pipeline、news_collect;trade_date 与 strategy 组成业务幂等键;status 分 queued、running、succeeded、failed;heartbeat_at 用于识别僵死任务;result_json、error_json 存结果与错误。
- claim_task 在单事务里检查加插入,抢占业务键;DuckDB 单写者,跨进程由文件写锁串行。
- 心跳超时才允许抢占:scan 默认 3600 秒、close_pipeline 7200 秒、news_collect 1800 秒;时间戳无法解析时保守判存活,不抢占——误判僵死会让两个进程同时写同一批数据,远比多等一轮严重。
- 重复触发命中同批次已完成任务时返回 reused=true(HTTP 200),不重复执行。
- 所有 upsert 一律 DELETE+INSERT,重复回补不产生脏数据。
- DuckDB 打开偶发 WinError 32(文件被占用)时重试 3 次,退避约 20 至 50 毫秒;读路径 ensure_schema=False 不执行 DDL,app/main.py 启动迁移时补表,库文件不存在不建库只警告。

### 调度闸门(app/services/scheduler.py、engine/schedule.py)

- decide_due_run 是纯函数闸门:日历为空返回 calendar_missing;未覆盖今天返回 calendar_stale;今天开市但未到 run_after(默认 15:30)返回 before_run_after;否则放行,目标交易日取自交易日历。
- 调度线程每 tick(默认 60 秒)问闸门;不判重,幂等靠 task_runs。
- enabled 与 running 是两个独立字段:enabled=false 且 running=false 是正常关闭;enabled=true 且 running=false 是故障。
- 单次失败不退出线程,原因写进 last_error;pipeline_in_progress、pipeline_not_due 属正常状态。
- 手动触发:ignore_gate 只跳过运行时间判定,不跳过交易日判定;手动指定 trade_date 不跑闸门,但必须校验是开市日。

### 舆情采集步三态

- news 段未启用 → unavailable(news_disabled)。
- 一个启用来源都没有 → unavailable(no_enabled_source)。
- 采集器正常跑完但窗口内 0 条 → ok 且 fetched=0,这是真实的今天没消息。
- 采集器抛错 → 整条链中止,绝不降级成上面两种。

### T+N 回填(engine/postmortem.py)

- 期限 ret1、ret3、ret5、ret10;基准 = as_of 当日收盘价,目标 = 第 N 个交易日收盘价。
- 只有未来已发生且已入库才回填;未到期的样本记 pending,原因分 future_not_reached(日历还没到)、calendar_missing(日历未覆盖)、target_bar_missing(目标日无行情,停牌退市或未回补)、base_missing(as_of 无基准价)。
- evaluate 按已回填样本计算 IC、RankIC、IC_IR、胜率、盈亏比与分层收益(rank1、rank2_3、rank4plus);NaN 与 inf 一律转 None,样本不足算不出就如实缺失。

## 三、复盘装配(engine/review.py)

### 三级标注

- fact(事实):直接来自已入库的行情、公告或新闻原文,不含判断。
- derived(规则计算结果):由固定公式或阈值从事实推出,换公式结论就会变。
- unverified(待验证判断):尚未被后续行情或人工确认,不能当结论用。

### 八个分节

market_structure(市场结构,fact)、industry_heat(行业热度,derived)、selection(入选与淘汰,derived)、factor_contribution(因子贡献,derived)、money_confirmation(资金确认,derived)、news_alignment(舆情与价格资金对应,unverified)、news_highlights(舆情要点,fact)、prediction_review(预测回顾,derived)。

- 每节 available=true 时带 data,false 时带 missing_reason 与 detail,不写占位数据。
- 行业热度 heat 公式 = 0.25 乘平均涨幅 + 0.25 乘中位涨幅 + 5 乘上涨占比 + 10 乘强势占比 + log1p(成交额) 除以 5;近涨停阈值 NEAR_LIMIT_UP_PCT = 9.5。
- 舆情缺失三态:no_source_registered(没有登记任何来源)、never_collected(登记了但从未采集)、no_news_on_date(采过但该日无条目)。
- news_alignment 以 as_of 等于 trade_date 截断,关联必须带 match_basis,避免把后一天的新闻算进前一天。
- 复盘接口默认只读:GET /api/reviews 固定 backfill=False 不触发回填;无行情时 404 且带 no_trade_date。

## 四、API 与 AI 边界

### 盘后任务链与扫描

- POST /api/pipelines:新建返回 202;命中同批次已完成返回 200 且 reused=true;409 分 pipeline_in_progress(已有在跑)与 pipeline_not_due(未到运行时间);400 分 not_trading_day 与 invalid_trade_date;503 calendar_unusable(日历不可用)。
- GET /api/pipelines/status 必须排在 GET /api/pipelines/{job_id} 之前,否则 status 会被当成 job_id;另有 GET /api/pipelines 列表接口。
- POST /api/scans 与盘后任务链同语义(新建 202、复用 200、在跑 409)。

### 舆情接口

- POST /api/news/collect:后台线程执行,立即返回 job_id;新建 202、命中同交易日已采成功 200(reused=true);409 分 news_disabled 与 no_enabled_source。
- GET /api/news/collect/jobs 必须排在 /news/collect/{job_id} 之前,否则 jobs 会被当成 job_id。
- GET /api/news:trade_date 省略时取舆情库最新一天(不是行情最新日);默认 limit 50、上限 200;include_duplicates 默认 false;available=false 时 missing_reason 说明缺在哪一环。
- GET /api/news/sources 返回来源清单,含合规备注。
- GET /api/news/stocks/{ts_code}、GET /api/news/industries/{industry} 支持 as_of 参数,按 <= as_of 过滤,杜绝前视。

### AI 边界(engine/ai.py、app/services/ai.py、app/api/ai.py)

- NARRATOR_REGISTRY 是叙述器白名单,当前为空 dict,是有意的闸门:没有注册实现就不可用,不替用户默认。
- availability 三态:disabled(配置明确关闭)、unconfigured(开着但缺 provider、模型、凭据或注册实现)、available(三样齐全)。
- unconfigured 时 missing 一次列全:provider、已注册实现、model、api_key_env。
- 凭据只从环境变量 WORKBENCH_AI_API_KEY 读,不写进配置文件。
- 未配置时调用生成直接抛 AIUnavailableError,接口返回 503 ai_unavailable 并附 details=status(),绝不回退规则模板。
- POST /api/ai/reviews 的输入是 build_review 的返回值(已带三级标注),叙述器不得新增事实;响应带 grounded_in 标明依据。

## 测试隔离(tests/api/conftest.py)

- offline_settings 为 autouse fixture:深拷贝 load_settings,把 news 段改成 enabled=false、sources=[];给 close_pipeline、run_scan、news_collect、pipelines、scans、ai 六个模块的 load_settings 打补丁。
- 测试库一律 tmp_path 加 _seed_db 种子,扫描用离线 run_scan,不碰 data/market.duckdb。
