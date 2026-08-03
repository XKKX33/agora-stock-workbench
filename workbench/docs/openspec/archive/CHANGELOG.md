# OpenSpec 归档日志

## 2026-08-01

- 归档阶段:收盘后自动复盘 + 舆情系统(postmarket-recap-news),状态 archived。
- 代码已实现,文档按实现事实整理,目录移入 archive。
- 覆盖内容:舆情采集链路(来源、条目、关联三表与合规闸门)、收盘后任务链(五步链、task_runs 幂等抢占、调度闸门)、复盘装配(三级标注与八分节)、API 与 AI 边界、测试隔离。

## 2026-08-01 补记(归档后收尾)

- picks 表主键已从 (run_date, strategy, ts_code) 迁移为 (as_of, strategy, ts_code):旧库自动迁移去重,保留最新 run_date,回归测试 test_picks_old_pk_migrates_to_business_key 覆盖。
- Windows + DuckDB 文件锁竞态已加固:Store 打开库短重试 3 次(20~50ms 退避),连续失败仍上抛,不吞错;API 层测试原 7 failed 已修复。
- 前端已接入真实接口三态渲染:舆情(已接入/未接入)、复盘(已生成/部分生成/待生成)、AI(已配置/未启用/未配置),六个页面共享数据链路状态条。
- 全量测试 297 passed(2026-08-01 复核)。

## 2026-08-01 补记二(工作台 UI 升级 + 舆情快照归属)

- 工作台升级为九页面:新增行情 K 线(p6_chart)、舆情(p7_news)、AI 复盘(p8_ai),全站暗色科技感主题,九项导航共享数据链路状态条;UI 设计调研沉淀在 docs/ui-design-reference.md。
- 新增 app/services/kline.py(搜索 + 日 K + MA/MACD/KDJ/RSI/BOLL 全指标后端计算)与 app/services/screener.py(全市场筛选),含 API 路由与 15 个新测试。
- 舆情快照归属修复:TrendRadar 热榜无权威发布时间,快照条目按采集日归属最近已收盘交易日(resolve_snapshot_trade_date),不再被未来数据闸门误拒;非快照来源的未来数据防线不变。实测 253 条入库、复盘 8/8 节全齐。
- 全量测试 319 passed(2026-08-01 复核)。