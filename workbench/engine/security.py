"""持久化与日志共用的凭据脱敏。"""

from __future__ import annotations

import re


# 键名集合：凡是这些名字承载的值,一律当凭据处理。
# 覆盖三种真实书写形态,少一种就漏一种:
#   1. `api_key=xxx` / `api_key: xxx`   —— 环境变量、日志行、命令行
#   2. `"api_key":"xxx"` / `'api_key': 'xxx'` —— HTTP 错误体里回显的 JSON/dict
#   3. `Authorization: Bearer xxx`      —— 请求头
_SECRET_KEY_NAMES = (
    r"api[_-]?key"
    r"|access[_-]?token"
    r"|refresh[_-]?token"
    r"|internal[_-]?token"
    r"|token"
    r"|authorization"
    r"|secret"
    r"|password"
    r"|passwd"
    r"|credential"
    r"|private[_-]?key"
)

_SECRET_PATTERNS = (
    # Bearer 头。值可能带引号,连引号一起吃掉才不会留下半截。
    re.compile(r"(?i)\bBearer\s+[^\s\"',}\]]+"),
    # 供应商前缀的裸密钥。OpenAI `sk-`、xAI `xai-`、Anthropic `sk-ant-`(被 sk- 覆盖)。
    re.compile(r"\b(?:sk|xai)-[A-Za-z0-9_-]{8,}"),
    # JSON / dict 形态:键名带引号,值带引号。保留键名与引号结构,只换掉值。
    re.compile(
        rf"(?i)([\"'](?:{_SECRET_KEY_NAMES})[\"']\s*:\s*[\"'])[^\"']*([\"'])"
    ),
    # key=value / key: value 形态。值到空白或分隔符为止。
    # 键名允许带前缀:真实环境变量叫 TUSHARE_TOKEN、WORKBENCH_AI_API_KEY,
    # 用 \b 锚定会因为前面是下划线(词字符)而永不匹配。前缀并入分组 1 原样保留。
    # (?!\[REDACTED\]) 防止和上面的 Bearer 规则重复替换:
    # `authorization: Bearer xxx` 已被替换成 `authorization: [REDACTED]`,
    # 不挡住的话这条会把 `[REDACTED` 再包一层,输出多出一个右括号。
    re.compile(
        rf"(?i)([A-Za-z0-9_.-]*(?:{_SECRET_KEY_NAMES})\s*[:=]\s*)"
        rf"(?!\[REDACTED\])[^\s,;\"'}}\]]+"
    ),
)

# Windows 与 POSIX 绝对路径。服务器磁盘布局不该出现在给浏览器的错误文案里。
_PATH_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:[\\/][^\s,;\"']*"),
    re.compile(r"(?<![\w.])/(?:home|Users|root|var|opt|srv|etc)/[^\s,;\"']*"),
)

_REDACTED = "[REDACTED]"


def redact_secrets(value: str, *, limit: int | None = None) -> str:
    """删除常见密钥形式；可选限制最终字符串长度。

    只脱敏凭据,不脱敏路径——路径脱敏会破坏本地排障时看日志定位文件的能力。
    要把路径也去掉,用 redact_for_client()。
    """
    text = value
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(_replace_secret, text)
    return text[:limit] if limit is not None else text


def _replace_secret(match: re.Match[str]) -> str:
    """保留匹配里的结构性分组(键名、引号),只把值替换掉。"""
    if not match.lastindex:
        return _REDACTED
    head = match.group(1)
    tail = match.group(2) if match.lastindex >= 2 else ""
    return f"{head}{_REDACTED}{tail}"


def redact_for_client(value: str, *, limit: int | None = None) -> str:
    """给浏览器看的文案:凭据 + 服务器绝对路径都去掉。

    路径必须去:错误响应会直接渲染到页面上,把 C:\\Users\\<用户名>\\... 这类
    磁盘布局暴露给任何能打开页面的人,对排障没有帮助。
    """
    text = redact_secrets(value)
    for pattern in _PATH_PATTERNS:
        text = pattern.sub("[PATH]", text)
    return text[:limit] if limit is not None else text


__all__ = ["redact_secrets", "redact_for_client"]
