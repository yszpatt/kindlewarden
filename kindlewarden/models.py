"""领域模型与输入规范化。

长度约束全部来自 ``docs/layout-spec.md`` 的**实测值**，不是拍的：
标题区实际只有 854px 宽，44px 字号下纯中文约 20 字就满；摘要是单行，约 28 字。

规范化原则（架构文档 §2.3）：**字段尽量宽容**。必填只有标题，其他都有默认值，
超长自动截断，绝不因为格式问题让 agent 的调用失败 ——
宁可展示一份不完美的简报，也不要让 agent 卡在格式校验上。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import sanitize
from .textutil import cap_chars, display_width, multi_line, one_line

STATUSES = ("ok", "partial", "failed", "info")

# ---------------------------------------------------------------- 显示约束
# 这些是**渲染时**的显示宽度参考（以「一个汉字」为 1.0 单位），
# 由 docs/layout-spec.md 的实测值反推而来：标题区 854px 宽、44px 字号。
# 代码里不用它们做截断 —— 真正的显示截断是渲染器按**像素宽度**做的。
# 保留下来是为了让管理页能提示「这条在板上会被截断」。
TITLE_DISPLAY_W = 20.0
SUMMARY_DISPLAY_W = 28.0
TODO_DISPLAY_W = 24.0
ALERT_TITLE_DISPLAY_W = 15.0
ALERT_DETAIL_DISPLAY_W = 30.0

# ---------------------------------------------------------------- 存储上限
# 只用来防止往事件日志里灌长文，**不是**显示约束。
# 存全量才能让管理页展示完整内容；板上显示多少由版面决定。
TITLE_STORE_MAX = 200
SUMMARY_STORE_MAX = 1000
TODO_STORE_MAX = 200
ALERT_TITLE_STORE_MAX = 120
ALERT_DETAIL_STORE_MAX = 300

MAX_ARTIFACTS = 5
MAX_TAGS = 6


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _as_status(value: Any) -> str:
    s = str(value or "").strip().lower()
    return s if s in STATUSES else "info"


def normalize_artifacts(raw: Any) -> list[dict]:
    """把产出物规范化为 ``{label, kind, ref}`` 列表。

    **公开函数**（不加下划线），因为 MCP 层也要用它 ——
    工具的 ``artifacts`` 参数必须接受和服务端一样的形式，否则
    agent 传对象会撞上 pydantic 校验错误（踩过）。

    接受两种写法：
      * 字符串 —— ``"PR #482"``，label 和 ref 都用它
      * 对象   —— ``{"label": ..., "kind": ..., "ref": ...}``

    为什么要宽容：管理页和文档里产出物都显示为对象，
    agent 自然会照着传。工具签名匹配实际数据模型，通用性才成立。
    """
    out: list[dict] = []
    for item in list(raw or [])[:MAX_ARTIFACTS]:
        if isinstance(item, str):
            item = {"label": item, "ref": item}
        if not isinstance(item, dict):
            continue
        label = one_line(str(item.get("label") or item.get("ref") or ""))
        if not label:
            continue
        out.append({
            "label": label[:60],
            "kind": one_line(str(item.get("kind") or ""))[:16],
            "ref": one_line(str(item.get("ref") or ""))[:400],
        })
    return out


# 向后兼容的别名：老代码里用的是下划线版本
_normalize_artifacts = normalize_artifacts


def _normalize_source(raw: Any) -> dict:
    if not isinstance(raw, dict):
        return {}
    keep = ("harness", "machine", "session", "cwd")
    return {k: one_line(str(raw[k]))[:120] for k in keep if raw.get(k)}


@dataclass
class Brief:
    """一条任务完成简报。"""

    id: str
    ts: float
    title: str
    status: str = "info"
    summary: str = ""
    artifacts: list[dict] = field(default_factory=list)
    project: str = ""
    tags: list[str] = field(default_factory=list)
    duration_s: int = 0
    source: dict = field(default_factory=dict)
    pinned: bool = False
    superseded_by: str = ""
    retracted: bool = False

    @property
    def visible(self) -> bool:
        """被取代或被撤回的简报不上屏。"""
        return not self.retracted and not self.superseded_by

    @property
    def sort_key(self) -> tuple:
        """钉住的排最前，其余按时间倒序。"""
        return (not self.pinned, -self.ts)

    def display_warnings(self) -> list[str]:
        """这条简报在**板上**会不会被截断。

        入库不再截断之后，这是唯一能告诉用户「你写的在屏幕上放不下」的渠道 ——
        管理页把它显示在详情里。板上一行字的容量是硬限制，改不了，
        但至少要让用户知道，而不是默默地被切掉。

        ⚠️ **只看第一句，不能量整段。**

        summary 现在支持多行（详情页会分段显示），但**看板只显示第一句**。
        拿整段的宽度去比阈值是错的：一段写得很长但首句简洁的简报，
        在板上根本不会被截断，却会报"会被截断" —— 那就是误报。

        而且实测这么做会**让 /board 直接 500**：

            ValueError: can't measure length of multiline text

        原因是下游有库对含换行的文本做宽度度量时会抛异常。
        所以这里必须先取单行，再量宽度 —— 两件事都要做。
        """
        out: list[str] = []

        if display_width(one_line(self.title)) > TITLE_DISPLAY_W:
            out.append(f"标题在板上会被截断（约 {TITLE_DISPLAY_W:.0f} 字以内才放得下）")

        # 看板上 summary 只显示第一句，所以只判断第一句。
        first_line = (self.summary or "").split("\n", 1)[0]
        if display_width(one_line(first_line)) > SUMMARY_DISPLAY_W:
            out.append(
                f"摘要首句在板上会被截断（约 {SUMMARY_DISPLAY_W:.0f} 字以内才放得下）")
        return out

    def to_dict(self) -> dict:
        return {
            "id": self.id, "ts": self.ts, "title": self.title, "status": self.status,
            "summary": self.summary, "artifacts": self.artifacts,
            "project": self.project, "tags": self.tags, "duration_s": self.duration_s,
            "source": self.source, "pinned": self.pinned,
            "superseded_by": self.superseded_by, "retracted": self.retracted,
        }

    @classmethod
    def from_event(cls, data: dict) -> "Brief":
        """从事件日志中恢复。对缺失/多余字段都宽容。"""
        return cls(
            id=str(data.get("id") or ""),
            ts=float(data.get("ts") or 0.0),
            title=str(data.get("title") or ""),
            status=_as_status(data.get("status")),
            summary=str(data.get("summary") or ""),
            artifacts=list(data.get("artifacts") or []),
            project=str(data.get("project") or ""),
            tags=list(data.get("tags") or []),
            duration_s=int(data.get("duration_s") or 0),
            source=dict(data.get("source") or {}),
            pinned=bool(data.get("pinned")),
            superseded_by=str(data.get("superseded_by") or ""),
            retracted=bool(data.get("retracted")),
        )

    @classmethod
    def from_request(cls, data: dict) -> tuple["Brief", list[str], bool]:
        """从 MCP / HTTP 请求构造。

        返回 ``(brief, 打码命中列表, 是否触发存储上限)``。

        **不做显示截断。** 之前这里会把标题截到 20 字、摘要截到 28 字，
        结果是完整文本**在入库时就丢了** —— 管理页的详情功能因此无从展示。
        现在只做存储封顶，显示截断交给渲染器按像素宽度做。

        ⚠️ **summary 保留换行，不能压平。**

        踩过一次：summary 和 title 一样走 ``one_line()``，把 ``\\n`` 全换成空格。
        于是 agent 精心分的段落在**入库那一刻就没了** ——
        详情页渲染得再对也显示不出层次，长一点的描述糊成一整坨。

        只看看板的话这个 bug 是**看不见的**：看板本来就是单行截断，
        压平与否没差别。只有点进详情页才会发现"怎么不分段"。

        所以现在：``one_line`` 只对**单行字段**（title/project/tags）用；
        summary 走 ``multi_line`` —— 它同样清理控制字符、压掉多余空行，
        但**保留段落结构**。
        """
        fields = {
            "title": one_line(str(data.get("title") or "")),
            "summary": multi_line(str(data.get("summary") or "")),
            "project": one_line(str(data.get("project") or "")),
            "tags": [one_line(str(t)) for t in list(data.get("tags") or [])[:MAX_TAGS]],
            "artifacts": _normalize_artifacts(data.get("artifacts")),
            "source": _normalize_source(data.get("source")),
        }

        hits: list[str] = []
        fields = sanitize.redact_deep(fields, hits)

        title, cut_t = cap_chars(fields["title"], TITLE_STORE_MAX)
        summary, cut_s = cap_chars(fields["summary"], SUMMARY_STORE_MAX)

        return cls(
            id=new_id("brief"),
            ts=time.time(),
            title=title or "(无标题)",
            status=_as_status(data.get("status")),
            summary=summary,
            artifacts=fields["artifacts"],
            project=fields["project"],
            tags=[t for t in fields["tags"] if t],
            duration_s=max(0, int(data.get("duration_s") or 0)),
            source=fields["source"],
        ), hits, (cut_t or cut_s)


@dataclass
class Todo:
    """副屏待办板的一条。给人用的 —— 不是 agent 的内部计划。"""

    id: str
    ts: float
    text: str
    done: bool = False
    done_ts: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "text": self.text,
                "done": self.done, "done_ts": self.done_ts}

    @classmethod
    def from_event(cls, data: dict) -> "Todo":
        return cls(
            id=str(data.get("id") or ""),
            ts=float(data.get("ts") or 0.0),
            text=str(data.get("text") or ""),
            done=bool(data.get("done")),
            done_ts=float(data.get("done_ts") or 0.0),
        )

    @classmethod
    def from_request(cls, data: dict) -> tuple["Todo", list[str], bool]:
        text, hits = sanitize.redact(one_line(str(data.get("text") or "")))
        text, cut = cap_chars(text, TODO_STORE_MAX)
        return cls(id=new_id("todo"), ts=time.time(), text=text or "(空)"), \
            ([hits] if hits else []), cut


@dataclass
class Alert:
    """需要人介入的告警。副屏上优先级最高的东西。"""

    id: str
    ts: float
    title: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {"id": self.id, "ts": self.ts, "title": self.title, "detail": self.detail}

    def display_warnings(self) -> list[str]:
        """告警在板上会不会被截断（告警条是最强的注意力信号，挤掉字很致命）。"""
        out: list[str] = []
        if display_width(self.title) > ALERT_TITLE_DISPLAY_W:
            out.append(f"告警标题会被截断（约 {ALERT_TITLE_DISPLAY_W:.0f} 字以内才放得下）")
        if display_width(self.detail) > ALERT_DETAIL_DISPLAY_W:
            out.append(f"告警详情会被截断（约 {ALERT_DETAIL_DISPLAY_W:.0f} 字以内才放得下）")
        return out

    @classmethod
    def from_event(cls, data: dict) -> "Alert":
        return cls(
            id=str(data.get("id") or ""),
            ts=float(data.get("ts") or 0.0),
            title=str(data.get("title") or ""),
            detail=str(data.get("detail") or ""),
        )

    @classmethod
    def from_request(cls, data: dict) -> tuple["Alert", list[str], bool]:
        title, hits_t = sanitize.redact(one_line(str(data.get("title") or "")))
        detail, hits_d = sanitize.redact(one_line(str(data.get("detail") or "")))
        title, cut_t = cap_chars(title, ALERT_TITLE_STORE_MAX)
        detail, cut_d = cap_chars(detail, ALERT_DETAIL_STORE_MAX)
        return cls(id=new_id("alert"), ts=time.time(),
                   title=title or "(无标题)", detail=detail), \
            (hits_t + hits_d), (cut_t or cut_d)


@dataclass
class Board:
    """投影出来的副屏视图。渲染器的唯一输入。"""

    briefs: list[Brief] = field(default_factory=list)
    todos: list[Todo] = field(default_factory=list)
    alert: Alert | None = None
    total_briefs: int = 0
    total_todos: int = 0

    @property
    def is_empty(self) -> bool:
        return not self.briefs and not self.todos and self.alert is None
