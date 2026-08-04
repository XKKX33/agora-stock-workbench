# Errors

## [ERR-20260731-001] powershell-foreach-pipeline

**Logged**: 2026-07-31T16:20:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

PowerShell 中直接把 `foreach` 语句接到管道导致解析失败。

### Error

```text
不允许使用空管道元素。
```

### Context

- 统计工作台页面和数据文件时触发。
- 未修改项目文件或数据。

### Suggested Fix

先把循环结果写入数组，再单独通过管道格式化输出。

### Metadata

- Reproducible: yes
- Related Files: none

### Resolution

- **Resolved**: 2026-07-31T16:22:00+08:00
- **Notes**: 改为数组收集后命令正常完成。

---

## [ERR-20260804-010] powershell-nested-regex-quoting

**Logged**: 2026-08-04T12:30:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

在一个并行命令中内嵌带单双引号的敏感信息正则，PowerShell 解析失败。

### Error

```text
The string is missing the terminator: ".
```

### Context

- 全量测试与敏感信息检查被放在同一次并行调用中。
- 敏感信息正则同时包含单双引号，经过 JSON、JavaScript 和 PowerShell 三层解析后失去结束引号。

### Suggested Fix

复杂正则先存入 PowerShell 单引号变量，测试命令与扫描命令分开运行。

### Metadata

- Reproducible: yes
- Related Files: none

### Resolution

- **Resolved**: 2026-08-04T12:31:00+08:00
- **Notes**: 后续验证拆成独立命令，不再嵌套多层引号。

---

## [ERR-20260731-002] missing-duckdb-dependency

**Logged**: 2026-07-31T16:25:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: tests

### Summary

现有 Conda 环境均未安装 `duckdb`，离线测试无法启动。

### Error

```text
ModuleNotFoundError: No module named 'duckdb'
```

### Context

- 已检查 `base`、`onetrans` 和 `tencent` 三个现有 Conda 环境。
- 按用户要求没有新建环境，也没有安装依赖。

### Suggested Fix

确定复用哪个现有环境后，再安装 `requirements.txt` 中缺少的依赖并运行测试。

### Metadata

- Reproducible: no
- Related Files: workbench/requirements.txt

### Resolution

- **Resolved**: 2026-07-31T21:15:00+08:00
- **Notes**: 复核 Conda `base` 实测已有 `duckdb 1.5.5`，本条前提不再成立。同环境还有 fastapi 0.136.1、uvicorn 0.46.0、pandas 2.3.3、tushare 1.4.29、pytest 8.4.2、httpx 0.28.1。舆情与调度所需的 akshare、feedparser、apscheduler、jieba、snownlp 仍缺，但当前实现没有依赖它们（采集器注册表刻意留空），不构成阻塞。

---

## [ERR-20260731-003] powershell-recursive-cache-scan

**Logged**: 2026-07-31T17:40:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary

PowerShell 递归查找规则文件时进入无权限的 Pytest 缓存目录，命令以非零状态结束。

### Error

```text
Get-ChildItem : 对路径“workbench\.pytest-tmp”和“workbench\.pytest_cache”的访问被拒绝。
```

### Context

- 使用 `Get-ChildItem -Recurse -Force` 查找 `AGENTS.md` 和 `lessons.md` 时触发。
- 项目正文已正常读取，未影响业务文件和数据。

### Suggested Fix

优先使用 `rg --files`，并排除 `.pytest_cache`、`.pytest-tmp` 等缓存目录。

### Metadata

- Reproducible: yes
- Related Files: none
- Recurrence-Count: 2
- Last-Seen: 2026-07-31

### Resolution

- **Resolved**: 2026-07-31T17:42:00+08:00
- **Notes**: 改用 `rg --files` 后文件清单读取成功；17:50 再次误用递归扫描，后续禁止对项目根目录执行 `Get-ChildItem -Recurse`。

---

## [ERR-20260731-004] powershell-empty-regex-match

**Logged**: 2026-07-31T17:45:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary

页面引用统计同时使用了错误的正则转义和未检查空匹配的分组访问，产生解析错误。

### Error

```text
rg: regex parse error: unclosed group
Cannot index into a null array.
```

### Context

- 只读统计 HTML 页面引用和脚本链接时触发。
- 未修改页面或业务数据。

### Suggested Fix

复杂引号改用 PowerShell 单引号字面量；访问 `Matches.Groups` 前先检查匹配数量。

### Metadata

- Reproducible: yes
- Related Files: none
- Recurrence-Count: 2
- Last-Seen: 2026-07-31

### Resolution

- **Resolved**: 2026-07-31T17:46:00+08:00
- **Notes**: 后续改用直接文件清单和固定字符串检索；18:05 再次因 PowerShell 引号与 glob 组合导致正则解析失败。

---

## [ERR-20260731-005] apply-patch-ambiguous-context

**Logged**: 2026-07-31T17:53:00+08:00
**Priority**: low
**Status**: resolved
**Area**: docs

### Summary

补丁使用了不唯一的上下文，导致优先级和复发字段写入了错误条目。

### Error

```text
补丁成功应用，但命中了 ERR-20260731-001，而不是预期的 ERR-20260731-003。
```

### Context

- 更新 `.learnings/ERRORS.md` 的复发记录时触发。
- 只影响错误日志，未影响业务代码。

### Suggested Fix

修改重复结构的 Markdown 时，补丁上下文必须包含唯一标题或完整区块。

### Metadata

- Reproducible: yes
- Related Files: .learnings/ERRORS.md

### Resolution

- **Resolved**: 2026-07-31T17:54:00+08:00
- **Notes**: 已恢复 ERR-001，并把复发字段移到 ERR-003。

---

## [ERR-20260731-006] wrong-agent-wait-tool

**Logged**: 2026-07-31T18:00:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary

等待子代理时误用了命令会话等待工具。

### Error

```text
exec cell dummy not found
```

### Context

- 子代理并行审计期间触发。
- 未影响项目文件和代理任务。

### Suggested Fix

命令会话使用 `functions.wait`，子代理使用 `collaboration.wait_agent`。

### Metadata

- Reproducible: yes
- Related Files: none
- Recurrence-Count: 6
- Last-Seen: 2026-07-31

### Resolution

- **Resolved**: 2026-07-31T18:01:00+08:00
- **Notes**: 多次误选命令等待接口；停止主动轮询，改为依赖代理完成消息和 `collaboration.list_agents` 状态查询。

---

## [ERR-20260731-007] missing-windows-py-launcher

**Logged**: 2026-07-31T18:08:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

系统没有安装或暴露 Windows `py` 启动器。

### Error

```text
py : The term 'py' is not recognized.
```

### Context

- 只读枚举 Python 解释器时触发。
- 已知 Conda 环境路径可直接使用。

### Suggested Fix

仅使用 `C:\Users\xuan\anaconda3` 下的明确解释器路径。

### Metadata

- Reproducible: yes
- Related Files: none

### Resolution

- **Resolved**: 2026-07-31T18:09:00+08:00
- **Notes**: 后续不再调用 `py`。

---

## [ERR-20260731-008] api-quota-exhausted

**Logged**: 2026-07-31T21:15:00+08:00
**Priority**: critical
**Status**: pending
**Area**: infra

### Summary

API 账户额度耗尽，所有需要预扣费的子代理操作失败。

### Error

```text
403 预扣费额度失败, 用户剩余额度: ＄0.061716, 需要预扣费额度: ＄0.100000
```

### Context

- 三个并行调研/审计子代理（代码审计、舆情数据源调研、codex 线程挖掘）全部在第一条消息后返回 403 失败。
- 剩余额度 $0.0617 不足以预扣费 $0.10。
- 5 分钟循环任务 `18cbfe76` 已停止，避免继续消耗额度。

### Suggested Fix

用户充值 API 账户后才能按规则使用子代理完成复杂调研与 Review；在此之前由主会话直接执行。

### Metadata

- Reproducible: yes
- Related Files: none

---

## [ERR-20260801-009] shell-classifier-unavailable

**Logged**: 2026-08-01T00:00:00+08:00
**Priority**: critical
**Status**: pending
**Area**: infra

### Summary

安全分类器持续不可用，`Bash` 与 `PowerShell` 两条命令通道全部被拒，测试一次都跑不了。

### Error

```text
claude-opus-5 is temporarily unavailable, so auto mode cannot determine the
safety of Bash right now.
```

### Context

- 自 2026-07-31 23:xx 起连续被拒 **21 次**，跨多个上下文窗口，`Bash` 与 `PowerShell` 表现一致。
- 直接后果：交付要求第 8 项「报告测试通过/失败数量」**无法给出真实数字**。本阶段新增的约 20 个文件（`engine/news*.py`、`review.py`、`ai.py`、`close_pipeline.py`、`app/services/*`、`app/api/*` 及全部新测试）**从未被导入或执行过**。
- 读文件、搜索代码等只读操作不受影响，因此实现与静态审查得以继续。
- 静态审查已借此发现一处静默缺陷（`news_for_link` 漏选 `source_home_url`），说明纯静态审查有效但不能替代执行。

### Suggested Fix

分类器恢复后按此顺序验证，先窄后宽：

```bash
cd workbench
python -m pytest tests/test_ai.py -q          # 纯单元,最自足
python -m pytest tests/test_review.py -q      # 含新增回归断言
python -m pytest tests/api -q                 # 接口层
python -m pytest tests -q                     # 全量,取通过/失败数
```

然后把 `progress.md`「未验证风险点」5 条逐条确认或修正，再把该节结论从「已改未验证」改为「已验证」。

### Metadata

- Reproducible: yes
- Related Files: workbench/tests/, progress.md

---
