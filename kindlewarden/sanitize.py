"""敏感信息兜底扫描。

副屏是**常亮摆在桌面上的**，相当于半公开显示。简报是 agent 生成的自由文本，
比结构化事件流更容易夹带密钥、token、客户数据。

第一道防线是 skill 和 MCP tool description 的约束（agent 不该发这些东西）。
这里是**第三道防线**：服务端正则兜底。

策略是**打码而不是拒绝** —— 拒绝等于这次汇报彻底丢失，而 agent 通常不会重发。
打码能保住简报的其余价值，同时在响应里回报 redactions 让 agent 知道出了问题。
"""

from __future__ import annotations

import re

REDACTED = "[已脱敏]"

# 顺序有意义：越具体的模式越先跑，避免被泛化模式部分吃掉
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("anthropic_key", re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{16,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("aws_key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{12,}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("google_key", re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{4,}")),
    ("bearer", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("url_credentials", re.compile(r"(?i)\b[a-z][a-z0-9+.\-]*://[^\s/:@]+:[^\s/@]+@")),
    # 放在最后：最容易误伤，所以让更具体的模式先命中
    ("key_value", re.compile(
        r"(?i)\b(?:password|passwd|pwd|secret|api[_-]?key|access[_-]?key|"
        r"client[_-]?secret|token)\s*[:=]\s*[\"']?([^\s\"',;]{8,})"
    )),
]


def scan(text: str) -> list[str]:
    """返回命中的敏感模式名（去重）。"""
    if not text:
        return []
    return [name for name, pat in PATTERNS if pat.search(text)]


def redact(text: str) -> tuple[str, list[str]]:
    """打码并返回命中的模式名。

    ``key_value`` 只替换值、保留键名，这样「token: [已脱敏]」仍然可读，
    不会让整句话变得莫名其妙。
    """
    if not text:
        return text, []

    hits: list[str] = []
    out = text
    for name, pat in PATTERNS:
        if not pat.search(out):
            continue
        hits.append(name)
        if name == "key_value":
            out = pat.sub(lambda m: m.group(0).replace(m.group(1), REDACTED), out)
        elif name == "url_credentials":
            out = pat.sub(lambda m: m.group(0).rstrip("@").split("://")[0] + "://" + REDACTED + "@", out)
        else:
            out = pat.sub(REDACTED, out)
    return out, hits


def redact_deep(value, hits: list[str] | None = None, _depth: int = 0):
    """递归打码嵌套结构（dict/list/str）。

    ``hits`` 会被就地累加，调用方传入一个空列表即可收齐所有命中。
    """
    if hits is None:
        hits = []
    if _depth > 6:
        return value
    if isinstance(value, str):
        new, found = redact(value)
        hits.extend(found)
        return new
    if isinstance(value, dict):
        return {k: redact_deep(v, hits, _depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_deep(v, hits, _depth + 1) for v in value]
    return value
