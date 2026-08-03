# 任务清单:收盘后自动复盘 + 舆情系统

日期: 2026-08-01
状态: 全部完成,阶段已归档

## 阶段一:任务链与调度

- [x] 定义收盘后任务链五步(PIPELINE_STEPS)与 StepResult 状态(ok、skipped、unavailable),失败直接抛异常中止。
- [x] 实现 task_runs 表与 claim_task 抢占、心跳超时判定、finish_task 回写真实交易日。
- [x] 实现调度闸门 decide_due_run(calendar_missing、calendar_stale、before_run_after)。
- [x] 实现调度线程:enabled 与 running 独立上报,单次失败写 last_error 不退出线程。
- [x] 实现手动触发接口 POST /api/pipelines 与 /api/scans 的 202、200、409、400、503 语义。

## 阶段二:舆情存储与采集

- [x] 建 news_sources、news_items、news_links 三张表并实现 upsert(DELETE+INSERT 幂等)。
- [x] 实现链接规范化、news_id 哈希主键与 dedup_key 去重指纹,转载只标 duplicate_of 不删。
- [x] 实现 FETCHER_REGISTRY 白名单与 compliance_note 强制校验,未注册采集器启动即报错。
- [x] 实现 TrendRadar 采集器:GPL 源码隔离在 vendor,importlib 单文件加载,HTTPS 与域名校验。
- [x] 实现采集窗口(上一开市日收盘至目标日收盘)与 close_cutoff 归属交易日切分。
- [x] 实现时间衰减 time_decay,发布时间晚于基准直接抛错。
- [x] 实现个股与行业关联,按 match_basis 分级置信度,无依据不写入。
- [x] 实现热榜无权威时间的标注(time_basis=first_seen_at_collect)与无可追溯链接条目丢弃。
- [x] 实现 POST /api/news/collect 一键采集与 GET /api/news 系列查询接口。

## 阶段三:复盘装配

- [x] 实现复盘八个分节与 fact、derived、unverified 三级标注。
- [x] 实现舆情缺失三态(no_source_registered、never_collected、no_news_on_date)。
- [x] 实现行业热度公式与 NEAR_LIMIT_UP_PCT 阈值。
- [x] 实现 news_alignment 以 as_of 等于 trade_date 截断,关联带 match_basis。
- [x] 实现 GET /api/reviews 只读接口,无行情 404 no_trade_date。

## 阶段四:T+N 回填与自检

- [x] 实现 ret1、ret3、ret5、ret10 收益回填与四种 pending 原因。
- [x] 实现 IC、RankIC、IC_IR、胜率、盈亏比、分层评估,NaN 与 inf 转 None。

## 阶段五:AI 边界与测试隔离

- [x] 实现 AI availability 三态(disabled、unconfigured、available)与 NARRATOR_REGISTRY 空闸门。
- [x] 实现未配置调用抛 AIUnavailableError,接口 503 ai_unavailable 附 details。
- [x] 实现 offline_settings autouse fixture,测试全用临时库隔离。
- [x] README 更新验收口径:一条命令启动、离线闭环演示、舆情可追溯、幂等重跑、AI 未配置标注。
