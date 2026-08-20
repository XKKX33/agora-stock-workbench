# Errors

## [ERR-20260812-001] powershell-background-process-policy

**Logged**: 2026-08-12T13:10:39+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary

正式验收服务的 PowerShell `Start-Process` 后台启动命令被执行策略拒绝。

### Error

```text
command rejected: blocked by policy
```

### Context

- 目标是在独立端口启动真实验收服务。
- 命令尚未执行，未修改数据库，也未输出任何凭据。

### Suggested Fix

需要长期运行的服务改用受控持续终端会话，避免后台进程封装。

### Metadata

- Reproducible: unknown
- Related Files: workbench/serve.py

### Resolution

- **Resolved**: 2026-08-12T13:10:39+08:00
- **Notes**: 改用工具提供的持续会话运行服务。

---

## [ERR-20260817-002] codex-thread-tool-page-limits

**Logged**: 2026-08-17T20:15:00+08:00
**Priority**: low
**Status**: resolved
**Area**: config

### Summary
Codex 任务读取工具对单次返回数量有固定上限，超出后参数校验直接失败。

### Error
```text
read_thread: turnLimit 必须小于等于 10
list_threads / list_archived_threads: limit 必须小于等于 50
```

### Context
- 读取其他任务时分别传入了 `turnLimit=20` 和 `limit=100`。
- 请求在参数校验阶段失败，没有修改任何任务或文件。

### Suggested Fix
首次调用直接使用允许的最大值；需要更多历史时使用返回的分页游标继续读取。

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-17T20:15:00+08:00
- **Notes**: 改用 `turnLimit=10` 和 `limit=50` 后继续读取。

---

## [ERR-20260817-003] stale-workbench-conda-path

**Logged**: 2026-08-17T20:32:00+08:00
**Priority**: low
**Status**: resolved
**Area**: config

### Summary
历史记录中的 `workbench` Conda 环境已不存在，直接调用该路径无法启动测试。

### Error
```text
C:\Users\xuan\anaconda3\envs\workbench\python.exe 无法识别为可执行程序
```

### Context
- 目标是运行历史窗口隔离行为的定向测试。
- 错误发生在命令解析阶段，测试和数据库均未启动。

### Suggested Fix
运行前先核实现有环境；当前项目继续复用已安装依赖的 Anaconda 基础环境。

### Metadata
- Reproducible: yes
- Related Files: README.md

### Resolution
- **Resolved**: 2026-08-17T20:32:00+08:00
- **Notes**: 改用 `C:\Users\xuan\anaconda3\python.exe` 后测试正常运行。

---

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
- Recurrence-Count: 2
- Last-Seen: 2026-08-18

### Resolution

- **Resolved**: 2026-07-31T16:22:00+08:00
- **Notes**: 改为数组收集后命令正常完成。

---

## [ERR-20260805-012] powershell-start-process-python-inline-argument-splitting

**Logged**: 2026-08-05T16:30:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

PowerShell `Start-Process -ArgumentList` 拆散了 Python `-c` 的内联代码，隔离验收服务未启动。

### Error

```text
File "<string>", line 1
  from
      ^
SyntaxError: invalid syntax
```

### Context

- 目标是后台启动使用隔离 DuckDB 的 `uvicorn` 服务。
- 失败只发生在参数解析阶段，未打开或修改数据库。

### Suggested Fix

给传入 `-c` 的完整代码显式加引号，确保它作为单个进程参数传递。

### Metadata

- Reproducible: yes
- Related Files: workbench/output/playwright/

### Resolution

- **Resolved**: 2026-08-05T16:30:00+08:00
- **Notes**: 改为显式引用完整 `-c` 参数后重试。

---

## [ERR-20260805-013] powershell-python-inline-quote-loss

**Logged**: 2026-08-05T16:35:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

PowerShell 命令字符串中的嵌套引号破坏了 Python `-c` 查询语句。

### Error

```text
SyntaxError: unterminated string literal
```

### Context

- 查询隔离 DuckDB 的测试任务错误字段时发生。
- Python 在语法解析阶段退出，数据库未被打开。

### Suggested Fix

通过标准输入传脚本；脚本内优先使用相对路径，避免 PowerShell 管道改写中文路径编码。

### Metadata

- Reproducible: yes
- Related Files: workbench/output/playwright/
- See Also: ERR-20260805-012

### Resolution

- **Resolved**: 2026-08-05T16:40:00+08:00
- **Notes**: 通过标准输入传脚本，并在脚本内使用相对路径后，Python 已成功打开隔离数据库。

---

## [ERR-20260805-014] task-runs-error-column-name

**Logged**: 2026-08-05T16:40:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

验收查询误用 `task_runs.error`，实际列名是 `error_json`。

### Error

```text
Binder Error: Referenced column "error" not found in FROM clause
```

### Context

- 只读查询隔离验收数据库中的最近失败任务。
- DuckDB 成功打开后在绑定 SQL 列名时中止。

### Suggested Fix

查询前以 `schema.py` 或 `DESCRIBE task_runs` 为准，使用 `error_json`。

### Metadata

- Reproducible: yes
- Related Files: workbench/engine/schema.py

### Resolution

- **Resolved**: 2026-08-05T16:40:00+08:00
- **Notes**: 后续查询改用 `error_json`。

---

## [ERR-20260805-015] playwright-network-command-renamed

**Logged**: 2026-08-05T16:45:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

Playwright 技能参考中的 `network` 命令在当前 CLI 版本中不存在，实际命令是 `requests`。

### Error

```text
Unknown command: network
```

### Context

- 页面与前四项验收已正常完成，只中断了网络请求列表检查。
- 当前 CLI 的帮助信息明确列出 `requests`。

### Suggested Fix

以当前 CLI `--help` 为准，网络请求列表使用 `requests`。

### Metadata

- Reproducible: yes
- Related Files: none

### Resolution

- **Resolved**: 2026-08-05T16:45:00+08:00
- **Notes**: 后续命令改用 `requests`。

---

## [ERR-20260805-016] playwright-theme-button-outside-viewport

**Logged**: 2026-08-05T16:47:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

长表翻页后页面位于底部，Playwright 无法点击视口外的侧栏主题按钮。

### Error

```text
TimeoutError: element is outside of the viewport
```

### Context

- 台账 209 条分页与筛选已正常完成。
- 失败只发生在后续主题切换操作。

### Suggested Fix

长页面切换全局控件前先按 `Home` 回到页首并重新获取快照。

### Metadata

- Reproducible: yes
- Related Files: workbench/ui_mockups/v2/assets/js/app-shell.js

### Resolution

- **Resolved**: 2026-08-05T16:47:00+08:00
- **Notes**: 后续先回页首再点击主题按钮。

---

## [ERR-20260805-017] ripgrep-windows-path-glob

**Logged**: 2026-08-05T16:52:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

在 Windows 上把 `*.yaml` 通配符直接写入 `rg` 路径参数，路径被判定为非法。

### Error

```text
文件名、目录名或卷标语法不正确。
```

### Context

- 目标文件前的代码内容已正常读取。
- 非零状态只来自最后一个含通配符的路径参数。

### Suggested Fix

搜索目录并使用 `rg -g '*.yaml'` 过滤，不把通配符写入 Windows 路径参数。

### Metadata

- Reproducible: yes
- Related Files: workbench/config/
- Recurrence-Count: 4
- Last-Seen: 2026-08-17

### Resolution

- **Resolved**: 2026-08-05T16:52:00+08:00
- **Notes**: 后续统一使用 `-g` 过滤文件名；2026-08-17 再次误用后，搜索测试文件先用 `rg --files`，内容过滤只传目录并使用 `-g`。

---

## [ERR-20260805-011] deepseekv4flash-channel-unavailable

**Logged**: 2026-08-05T11:59:36+08:00
**Priority**: high
**Status**: resolved
**Area**: config

### Summary

真实最小请求已到达 `api.pie-xian.com`，但账户分组下的 `deepseekv4flash` 没有可用渠道。

### Error

```text
HTTP 503 model_not_found: 分组 user 下模型 deepseekv4flash 无可用渠道（distributor）
```

### Context

- 请求地址为配置中的 OpenAI 兼容 `/v1/chat/completions`。
- 密钥认证已通过到模型路由阶段；完整响应和密钥均未写入项目文件。
- 未更换模型，也未生成替代结果。

### Suggested Fix

查询 `/v1/models` 使用服务商登记的正式模型 ID，禁止猜测模型别名。

### Metadata

- Reproducible: yes
- Related Files: workbench/config/settings.yaml, workbench/engine/ai.py

### Resolution

- **Resolved**: 2026-08-05T12:03:00+08:00
- **Notes**: 模型清单确认正式 ID 为 `deepseek-v4-flash`；更新配置后真实最小请求成功并返回合法 JSON。

---

## [ERR-20260804-011] pytest-workdir-path-mismatch

**Logged**: 2026-08-04T16:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

从仓库根目录运行以 `tests/` 开头的 Pytest 路径，导致测试文件找不到。

### Error

```text
ERROR: file or directory not found: tests/api/test_experiments.py
no tests ran
```

### Context

- 测试实际位于 `workbench/tests/`。
- 命令使用了适合 `workbench` 工作目录的相对路径，但执行目录误设为仓库根目录。

### Suggested Fix

运行前同时核对命令的相对路径与 `workdir`；本项目使用 `tests/...` 时必须把工作目录设为 `workbench`。

### Metadata

- Reproducible: yes
- Related Files: workbench/tests/api/test_experiments.py

### Resolution

- **Resolved**: 2026-08-04T16:01:00+08:00
- **Notes**: 改在 `workbench` 目录重新运行同一组测试。

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
- Recurrence-Count: 3
- Last-Seen: 2026-08-17

### Resolution

- **Resolved**: 2026-08-04T12:31:00+08:00
- **Notes**: 后续验证拆成独立命令，不再嵌套多层引号；2026-08-05 复发后，复杂匹配改成多个固定字符串查询。2026-08-17 在 JavaScript 编排层嵌入 PowerShell 与 Python 查询时再次复发，改为拆分简单命令。

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
- Recurrence-Count: 3
- Last-Seen: 2026-08-05

### Resolution

- **Resolved**: 2026-07-31T17:54:00+08:00
- **Notes**: 已恢复 ERR-001，并把复发字段移到 ERR-003；2026-08-05 两次复发后，所有补丁都先读取目标区块并使用唯一标题上下文。

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

## [ERR-20260805-010] playwright-skill-root-mismatch

**Logged**: 2026-08-05T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

读取 Playwright 技能时误把技能根目录 `r0` 展开到了 `.agents`，导致文件不存在。

### Error

```text
Cannot find path 'C:\Users\xuan\.agents\skills\playwright\SKILL.md'
```

### Context

- 当前技能目录映射中 `r0` 是 `C:\Users\xuan\.codex\skills`。
- 失败发生在只读规则加载阶段，没有改动业务数据。

### Suggested Fix

每次按技能清单中的根目录映射展开短路径，不凭同名目录猜测。

### Metadata

- Reproducible: yes
- Related Files: none

### Resolution

- **Resolved**: 2026-08-05T00:00:00+08:00
- **Notes**: 已改用 `C:\Users\xuan\.codex\skills\playwright\SKILL.md` 并读取成功。

---

## [ERR-20260811-011] powershell-python-c-quote-loss

**Logged**: 2026-08-11T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

PowerShell 把多行脚本作为 `python -c` 参数传递时丢失了 Python 字符串引号，脚本在本地语法解析阶段失败。

### Error

```text
SyntaxError: '{' was never closed
```

### Context

- 失败命令用于真实模型最小连通性检查。
- 请求没有发出，没有产生接口费用，也没有接触数据库。

### Suggested Fix

Windows 下把多行 Python 脚本通过标准输入交给现有 Conda 解释器，不再用 `python -c` 传递复杂引号。

### Metadata

- Reproducible: yes
- Related Files: workbench/engine/ai.py

### Resolution

- **Resolved**: 2026-08-11T00:00:00+08:00
- **Notes**: 后续命令改用标准输入传递脚本。

---

## [ERR-20260811-012] ai-base-url-missing-v1

**Logged**: 2026-08-11T20:44:56+08:00
**Priority**: high
**Status**: resolved
**Area**: config

### Summary

默认模型地址缺少 `/v1`，真实请求落到网页路由并返回 `HTTP 404`。

### Error

```text
engine.ai.AIRequestError: 模型接口返回 HTTP 404
```

### Context

- `/v1/models` 和 `/v1/chat/completions` 均能命中 API 鉴权层。
- 根路径下的 `/chat/completions` 返回 `404`，证明问题来自默认 `base_url`。

### Suggested Fix

默认 `ai.base_url` 和 `agent.base_url` 必须包含服务声明的 `/v1` API 前缀，并由契约测试锁定。

### Metadata

- Reproducible: yes
- Related Files: workbench/config/settings.yaml, workbench/tests/test_ai.py

### Resolution

- **Resolved**: 2026-08-11T20:44:56+08:00
- **Notes**: 地址改为 `https://grok.xuan.christmas/v1`，定向测试通过。

---

## [ERR-20260811-013] ai-api-key-rejected

**Logged**: 2026-08-11T20:44:56+08:00
**Priority**: high
**Status**: resolved
**Area**: config

### Summary

模型请求进入正确 API 后，当前 `WORKBENCH_AI_API_KEY` 被服务判定为无效。

### Error

```text
HTTP 401: invalid_api_key（客户端 API Key 无效）
```

### Context

- 环境变量已设置，长度为 51，具有 `sk-` 前缀且没有首尾空白。
- 同一凭据访问 `/v1/models` 也稳定返回 `401`，排除请求正文和模型名问题。
- 全程没有打印或写入密钥值。

### Suggested Fix

替换为该服务当前有效的 API Key 后，重新执行一次 `max_tokens=8` 的最小请求。

### Metadata

- Reproducible: yes
- Related Files: workbench/config/settings.yaml, workbench/engine/ai.py

### Resolution

- **Resolved**: 2026-08-11T22:00:00+08:00
- **Notes**: 用户提供服务认可的新凭据并保存在 Git 忽略的 `workbench/.env`；模型清单确认 `grok-4.5`，一次无重试最小 JSON 请求成功。

---

## [ERR-20260811-014] pytest-command-timeout-too-short

**Logged**: 2026-08-11T21:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

全量回归的命令超时先后设为 1 秒和 120 秒，均在用例完成前被工具中止。

### Error

```text
Exit code 124: command timed out
```

### Context

- 中止不是测试失败，不能用于报告通过或失败数量。
- 本项目当前全量测试实际耗时约 156 秒。

### Suggested Fix

全量回归直接预留至少 6 分钟，等待完整退出码和汇总计数。

### Metadata

- Reproducible: yes
- Related Files: workbench/tests/

### Resolution

- **Resolved**: 2026-08-11T21:00:00+08:00
- **Notes**: 改用 6 分钟上限后，完整回归 682 项通过、0 项失败。

---

## [ERR-20260811-015] powershell-matches-automatic-variable

**Logged**: 2026-08-11T21:10:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

密钥落盘扫描误用 PowerShell 自动变量 `$Matches` 保存路径，正则过滤覆盖了扫描结果。

### Error

```text
SECRET_MATCHES=1
0 = \vendor\
```

### Context

- 该输出来自路径排除正则，不代表文件包含密钥。
- 脚本没有打印密钥内容。

### Suggested Fix

PowerShell 脚本不得把 `$Matches` 当普通变量；使用任务专用名称和显式列表类型。

### Metadata

- Reproducible: yes
- Related Files: none

### Resolution

- **Resolved**: 2026-08-11T21:10:00+08:00
- **Notes**: 改用 `$secretHitPaths` 后重新扫描，确认 0 个文件包含当前密钥。

---

## [ERR-20260811-016] cleanup-command-blocked-by-policy

**Logged**: 2026-08-11T21:15:00+08:00
**Priority**: low
**Status**: pending
**Area**: infra

### Summary

删除已核对的 pytest、Playwright 临时目录和日志时，命令被安全策略拦截。

### Error

```text
rejected: blocked by policy
```

### Context

- 删除前已单独列出并确认 23 个目标均位于项目目录。
- 命令未执行，任何文件都没有被删除。
- 目标均为 `.gitignore` 中的测试缓存、截图或日志，不含数据库和业务输出。

### Suggested Fix

安全策略允许后，再使用已核对的绝对路径逐项清理；不得绕过策略或扩大删除范围。

### Metadata

- Reproducible: yes
- Related Files: workbench/.pytest-tmp-*, workbench/.playwright-cli, workbench/output/playwright

---

## [ERR-20260811-017] ambiguous-yaml-patch-context

**Logged**: 2026-08-11T21:30:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: config

### Summary

Hermes 配置中存在多组相同的 `base_url` / `api_key` 行，缺少父级字段的补丁命中了错误供应商。

### Error

```text
AssertionError: providers.otokapi 被误改，auxiliary.compression 未更新
```

### Context

- 目标是更新 `model`、`providers.custom` 与 `auxiliary.compression` 三处 CPA 配置。
- 第三个补丁块只有重复字段，没有包含 `compression:` 父级上下文，因此先匹配到 `providers.otokapi`。

### Suggested Fix

修改包含重复键值的 YAML 时，每个补丁块必须带唯一父级路径，并在写入后逐路径做结构化断言。

### Metadata

- Reproducible: yes
- Related Files: C:\Users\xuan\.hermes\config.yaml

### Resolution

- **Resolved**: 2026-08-11T21:35:00+08:00
- **Notes**: 使用 `otokapi:` 与 `compression:` 唯一父级上下文修正配置；结构化校验 7/7 通过。

---

## [ERR-20260811-018] cli-config-validation-command-mismatch

**Logged**: 2026-08-11T21:35:00+08:00
**Priority**: low
**Status**: resolved
**Area**: config

### Summary

配置验证时使用了 Hermes 不存在的复数命令 `models`，且尝试读取 OpenCode 已禁用的 `cpa` 供应商。

### Error

```text
hermes: invalid choice: 'models'
OpenCode: Provider not found: cpa
```

### Context

- Hermes 的正确帮助命令是 `hermes model --help`。
- OpenCode 配置中的 `cpa` 原本位于 `disabled_providers`，程序不会加载它。

### Suggested Fix

先读取各工具帮助；禁用供应商使用结构化配置解析验证，不把“未加载”误判成配置损坏。

### Metadata

- Reproducible: yes
- Related Files: C:\Users\xuan\.config\opencode\opencode.json

### Resolution

- **Resolved**: 2026-08-11T21:35:00+08:00
- **Notes**: 改用受支持命令验证入口，OpenCode CPA 用 JSON 断言验证，全部通过。

---

## [ERR-20260811-019] dotenv-patch-context-and-size

**Logged**: 2026-08-11T22:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: config

### Summary

给 `.env` 与多份文档打大补丁时，先因脱敏上下文不匹配失败，随后因补丁过大导致工具输出关闭。

### Error

```text
apply_patch verification failed
code-mode host closed its stdout
```

### Context

- `.env` 的原值被安全读取时已脱敏，不能作为补丁的精确上下文。
- 两次失败都没有部分覆盖密钥或业务配置。

### Suggested Fix

敏感文件只做无上下文新增，不读取或匹配现有密钥；跨多份文档的补丁按文件拆小。

### Metadata

- Reproducible: yes
- Related Files: workbench/.env, README.md, ARCHITECTURE.md

### Resolution

- **Resolved**: 2026-08-11T22:00:00+08:00
- **Notes**: `.env` 单独新增密钥行，代码与文档拆分补丁后全部成功。

---

## [ERR-20260811-020] dotenv-secret-scan-validator

**Logged**: 2026-08-11T22:10:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary

密钥位置扫描先漏掉无后缀的 `.env`，修正后又因字符串与 `Path` 类型比较导致错误退出。

### Error

```text
SECRET_FILES=
SECRET_FILES=workbench/.env（但退出码为 1）
```

### Context

- `Path(".env").suffix` 是空字符串，不能只按扩展名筛选。
- 扫描结果保存的是字符串，退出断言却与 `Path` 对象比较。

### Suggested Fix

显式按 `path.name == ".env"` 纳入扫描，并统一用 POSIX 字符串比较相对路径。

### Metadata

- Reproducible: yes
- Related Files: workbench/.env

### Resolution

- **Resolved**: 2026-08-11T22:10:00+08:00
- **Notes**: 修正后扫描退出码为 0，真实密钥只命中 `workbench/.env`。

---

## [ERR-20260816-001] bash-windows-absolute-python-path

**Logged**: 2026-08-16T00:00:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary

当前 Bash 工具不会直接执行 `C:\Users\...\python.exe` 或 `/c/Users/.../python.exe`，Windows 绝对路径被当成不存在的命令。

### Error

```text
command not found: C:Usersxuananaconda3python.exe
command not found: /c/Users/xuan/anaconda3/python.exe
```

### Context

- 目标是在 `workbench` 内运行项目规定的 Conda Python 全量测试。
- 两次失败均发生在命令解析阶段，测试和数据库尚未启动。

### Suggested Fix

在当前 Bash 工具中通过 `cmd.exe /c "C:\Users\xuan\anaconda3\python.exe ..."` 调用 Windows 可执行文件。

### Metadata

- Reproducible: yes
- Related Files: workbench/README.md

### Resolution

- **Resolved**: 2026-08-16T00:00:00+08:00
- **Notes**: 改用 `cmd.exe /c` 后全量测试成功运行，707 项通过。

---
## [ERR-20260816-002] real-pi-top20-technical-invalid-json

**Logged**: 2026-08-16T22:45:00+08:00
**Priority**: high
**Status**: pending
**Area**: backend

### Summary
真实 Top20 Agent 批次在 technical 阶段返回非法 JSON，严格协议校验使整批失败。

### Error
```text
Pi Agent HTTP 409: technical returned invalid JSON
```

### Context
- 正式 API 请求参数为 `candidates=20, depth=20, final=3`。
- 任务 `d614c85912f646a7958569927d3089f6` 产生 56 条消息事件和失败终止事件。
- 失败批次 `result_json` 为空，未写入成功结果；这是预期的失败边界。

### Suggested Fix
+下次真实验收前重新发起受控批次；不得把非法 JSON 转成模板结果或继续落库。

### Metadata
- Reproducible: unknown
- Related Files: workbench/pi_agent/src/workflow.ts, workbench/app/services/pi_agent.py

---

## [ERR-20260816-003] duplicate-pi-agent-port

**Logged**: 2026-08-16T22:25:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
重复启动真实 Pi Agent 监听 `127.0.0.1:43123` 时端口已被工作台生命周期占用。

### Error
```text
Error: listen EADDRINUSE: address already in use 127.0.0.1:43123
```

### Context
- 工作台现有服务已由 `serve.py` 启动并持有 Pi Agent。
- 未创建第二个有效服务，未产生数据库修改。

### Suggested Fix
+复用工作台已启动的 Pi Agent，或先通过受控服务生命周期停止旧实例。

### Metadata
- Reproducible: yes
- Related Files: workbench/app/main.py, workbench/app/services/pi_agent.py

### Resolution
- **Resolved**: 2026-08-16T22:26:00+08:00
- **Notes**: 改用工作台正式 API 复用已启动实例。

---
## [ERR-20260817-001] production-pipeline-market-data

**Logged**: 2026-08-17T19:03:30+08:00
**Priority**: high
**Status**: pending
**Area**: infra

### Summary
真实库在线九步流程两次均在 `market_data` 步骤失败，未进入扫描、舆情、Agent 和实验提交。

### Error
```text
第一次：Tushare moneyflow 请求连续失败(重试 3 次)
第二次：候选股票历史窗口不足
```

### Context
- 使用当前 `settings.yaml`，在线模式，策略 `strong_mainup`，未修改 AI 配置。
- 任务 `4207fe454cae4a92a1d360dcc7f11814` 的信号日为 `20260720`。
- 任务 `9b183420f9be489db3212872dbb644f0` 的信号日为 `20260720`。
- 两次均严格记录为 failed；未生成成功实验决策。

### Suggested Fix
先独立确认 Tushare 资金流接口与候选股票历史窗口覆盖，再重新执行；不得绕过数据完整性校验或伪造流程成功。

### Metadata
- Reproducible: yes
- Related Files: workbench/engine/ingest_tushare.py, workbench/app/services/one_click.py

---

## [ERR-20260817-004] rg-relative-path-duplicated-workbench

**Logged**: 2026-08-17T21:40:00+08:00
**Priority**: low
**Status**: resolved
**Area**: infra

### Summary
已在 `workbench` 目录执行命令时，又给 `rg` 传入 `workbench/app/...`，导致路径重复而搜索失败。

### Error
```text
rg: workbench\app\api\agents.py: 系统找不到指定的路径。 (os error 3)
```

### Context
- 工作目录已经是 `C:\Users\xuan\Desktop\桌面\股票\workbench`。
- 目标文件的正确相对路径应从 `app/` 开始。

### Suggested Fix
运行相对路径命令前先核对 `workdir`；位于 `workbench` 时不要再次添加 `workbench/` 前缀。

### Metadata
- Reproducible: yes
- Related Files: workbench/app/api/agents.py, workbench/app/api/pipelines.py

### Resolution
- **Resolved**: 2026-08-17T21:40:00+08:00
- **Notes**: 后续改用相对于当前 `workbench` 工作目录的路径。

---

## [ERR-20260818-001] playwright-stale-ref-after-refresh

**Logged**: 2026-08-18T00:03:00+08:00
**Priority**: low
**Status**: resolved
**Area**: tests

### Summary
页面刷新后继续使用刷新前的元素引用，Playwright 拒绝点击。

### Error
```text
Error: Ref e109 not found in the current page snapshot. Try capturing new snapshot.
```

### Context
- 总览页刷新后表格行引用已从 `e109` 变为 `e218`。
- 页面功能正常，失败来自验收操作仍引用旧快照。

### Suggested Fix
任何刷新、导航或大幅 DOM 更新后重新获取快照，只使用最新快照里的引用。

### Metadata
- Reproducible: yes
- Related Files: none

### Resolution
- **Resolved**: 2026-08-18T00:03:00+08:00
- **Notes**: 后续验收已改为刷新后重新快照再操作。

---
