# Learnings

## [LRN-20260804-001] correction

**Logged**: 2026-08-04T13:00:00+08:00
**Priority**: high
**Status**: resolved
**Area**: backend

### Summary

“自动运行”指一键完成业务流程，不代表 Windows 开机自启。

### Details

把用户要求的“流程全自动”误解为操作系统级无人值守运行，导致方案扩展到计划任务、系统服务和进程看门。正确边界是用户正常打开工作台后只点击一次，后续步骤自动串行执行。

### Suggested Action

设计自动化能力前先区分业务流程自动化与操作系统运行自动化；没有明确授权时不得引入开机自启。

### Metadata

- Source: user_feedback
- Related Files: workbench/engine/close_pipeline.py, workbench/app/services/pipelines.py
- Tags: automation, scope, correction

### Resolution

- **Resolved**: 2026-08-04T13:00:00+08:00
- **Notes**: 已将当前设计收窄为工作台内的一键完整流程。

---

## [LRN-20260811-002] correction

**Logged**: 2026-08-11T21:20:00+08:00
**Priority**: medium
**Status**: resolved
**Area**: infra

### Summary

更新 Windows 用户级环境变量后，不需要把重启 Codex 说成必要条件。

### Details

已运行进程不会自动刷新环境变量快照，但后续 PowerShell 命令可以直接从 User 级环境读取新值，并只注入即将启动的 Python 子进程。因此重启只是可选做法，不是模型接口或 Codex 的要求。

### Suggested Action

需要读取刚更新的用户环境变量时，在执行命令内显式调用 `[Environment]::GetEnvironmentVariable(..., 'User')` 并设置子进程环境，不要求用户重启应用。

### Metadata

- Source: user_feedback
- Related Files: lessons.md
- Tags: windows, environment-variable, process-inheritance, correction

### Resolution

- **Resolved**: 2026-08-11T21:20:00+08:00
- **Notes**: 已纠正后续操作说明，不再要求重启 Codex。

---
