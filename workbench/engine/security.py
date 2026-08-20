"""持久化与日志共用的凭据脱敏。"""

from __future__ import annotations

import re


_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[^\s\"']+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b"),
    re.compile(
        r"(?i)((?:api[_-]?key|access[_-]?token|authorization)\s*[:=]\s*)"
        r"[^\s,;\"']+"
    ),
)


def redact_secrets(value: str, *, limit: int | None = None) -> str:
    """删除常见密钥形式；可选限制最终字符串长度。"""
    text = value
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(
            lambda match: (
                f"{match.group(1)}[REDACTED]"
                if match.lastindex
                else "[REDACTED]"
            ),
            text,
        )
    return text[:limit] if limit is not None else text


__all__ = ["redact_secrets"]
