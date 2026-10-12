"""事件溯源存储。

架构决策（docs/architecture.md §6）：**不写「当前状态」，而是追加领域事件。**

好处：
- 可回放：改了版面后能重现历史画面
- 崩溃恢复 = 从日志重建，不需要额外机制
- 调试审计天然具备
- 与 DSH 自身的设计哲学一致（它的会话日志也是事件流）

成本：就是一个 JSONL 文件，零依赖。

事件类型
--------
``brief.published``   ``{brief, supersedes}``
``brief.superseded``  ``{id, by}``
``brief.retracted``   ``{id}``
``brief.deleted``     ``{id}``
``brief.pinned``      ``{id, pinned}``
``todo.added``        ``{todo}``
``todo.toggled``      ``{id, done, done_ts}``
``todo.edited``       ``{id, text}``
``todo.removed``      ``{id}``
``alert.raised``      ``{alert}``
``alert.cleared``     ``{id}``

撤回 vs 删除
------------
``brief.retracted`` —— 从副屏拿下，但**归档里还在**（`include_hidden` 能看到），
用于"这条不该在板上"。
``brief.deleted`` —— **彻底移出投影**，连归档也不再有，用于"我压根不想留这条"。

两者都是追加事件，日志本身永远不被改写（事件溯源的前提）。

投影（:func:`project`）是**纯函数**，没有 IO、没有时间依赖 ——
因此可以用固定的事件流做快照测试，「看板显示错了」是可复现的 bug。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Iterable, Iterator

from .models import Alert, Board, Brief, Todo


class EventStore:
    """单文件 append-only 事件日志。

    写入端只有一个（受件服务），所以进程内锁就足够了。
    ``warden`` CLI 与 MCP 瘦客户端都走 HTTP，不直接碰这个文件。
    """

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._seq: int | None = None

    # ---------------------------------------------------------------- 写入

    def append(self, etype: str, data: dict) -> dict:
        with self._lock:
            seq = self._last_seq() + 1
            event = {"seq": seq, "ts": time.time(), "type": etype, "data": data}
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())   # 断电也不丢事件
            self._seq = seq
            return event

    def _last_seq(self) -> int:
        if self._seq is None:
            self._seq = 0
            for ev in self.read_events():
                seq = ev.get("seq")
                if isinstance(seq, int) and seq > self._seq:
                    self._seq = seq
        return self._seq

    # ---------------------------------------------------------------- 读取

    def read_events(self) -> Iterator[dict]:
        """逐行读取。

        容忍**残行** —— 进程崩在写一半时会留下不完整的最后一行，
        这时应该跳过它继续，而不是让整个看板失效。
        """
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(ev, dict) and ev.get("type"):
                    yield ev

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._last_seq()

    # ---------------------------------------------------------------- 便捷写入口

    def publish_brief(self, brief: Brief, supersedes: str = "") -> dict:
        return self.append("brief.published",
                           {"brief": brief.to_dict(), "supersedes": supersedes})

    def add_todo(self, todo: Todo) -> dict:
        return self.append("todo.added", {"todo": todo.to_dict()})

    def toggle_todo(self, todo_id: str, done: bool) -> dict:
        return self.append("todo.toggled",
                           {"id": todo_id, "done": done,
                            "done_ts": time.time() if done else 0.0})

    def edit_todo(self, todo_id: str, text: str) -> dict:
        return self.append("todo.edited", {"id": todo_id, "text": text})

    def remove_todo(self, todo_id: str) -> dict:
        return self.append("todo.removed", {"id": todo_id})

    def raise_alert(self, alert: Alert) -> dict:
        return self.append("alert.raised", {"alert": alert.to_dict()})

    def clear_alert(self, alert_id: str = "") -> dict:
        return self.append("alert.cleared", {"id": alert_id})

    def pin_brief(self, brief_id: str, pinned: bool = True) -> dict:
        return self.append("brief.pinned", {"id": brief_id, "pinned": pinned})

    def retract_brief(self, brief_id: str) -> dict:
        return self.append("brief.retracted", {"id": brief_id})

    def delete_brief(self, brief_id: str) -> dict:
        """彻底删除：连归档里也不再有这条。

        仍然是**追加**一个事件，不改写日志 —— 投影时把它丢掉而已。
        """
        return self.append("brief.deleted", {"id": brief_id})

    def supersede_brief(self, brief_id: str, by: str) -> dict:
        return self.append("brief.superseded", {"id": brief_id, "by": by})


def project(events: Iterable[dict], *, include_hidden: bool = False) -> Board:
    """纯函数：事件流 → 副屏视图。

    这里没有 IO、没有 ``time.time()`` —— 所有时间都来自事件本身。
    这让整个投影可以脱离真实时间被测试和重放。

    ``include_hidden=True`` 时把已撤回/已被取代的简报也返回（仍排在后面），
    供管理页查看归档。``total_briefs`` 始终是**可见**数量。
    """
    briefs: dict[str, Brief] = {}
    todo_items: dict[str, Todo] = {}
    alert: Alert | None = None
    # 被彻底删除的简报 id。**单独用一个集合**记录，而不是从 briefs 里 pop ——
    # 因为删除事件可能出现在该简报的发布事件**之前**（日志被手工编辑、
    # 或将来做并发导入时会遇到），先 pop 再 publish 会让它"复活"。
    deleted_briefs: set[str] = set()

    for ev in events:
        etype = ev.get("type")
        data = ev.get("data") or {}

        if etype == "brief.published":
            brief = Brief.from_event(data.get("brief") or {})
            if not brief.id:
                continue
            if brief.id in deleted_briefs:
                continue                # 已彻底删除 → 不再复活
            briefs[brief.id] = brief
            target = str(data.get("supersedes") or "")
            if target and target in briefs and target != brief.id:
                briefs[target].superseded_by = brief.id

        elif etype == "brief.superseded":
            found = briefs.get(str(data.get("id") or ""))
            if found:
                found.superseded_by = str(data.get("by") or "")

        elif etype == "brief.retracted":
            found = briefs.get(str(data.get("id") or ""))
            if found:
                found.retracted = True

        elif etype == "brief.deleted":
            target = str(data.get("id") or "")
            deleted_briefs.add(target)
            briefs.pop(target, None)

        elif etype == "brief.pinned":
            found = briefs.get(str(data.get("id") or ""))
            if found:
                found.pinned = bool(data.get("pinned"))

        elif etype == "todo.added":
            todo = Todo.from_event(data.get("todo") or {})
            if todo.id:
                todo_items[todo.id] = todo

        elif etype == "todo.toggled":
            todo = todo_items.get(str(data.get("id") or ""))
            if todo:
                todo.done = bool(data.get("done"))
                todo.done_ts = float(data.get("done_ts") or 0.0)

        elif etype == "todo.edited":
            todo = todo_items.get(str(data.get("id") or ""))
            if todo:
                todo.text = str(data.get("text") or todo.text)

        elif etype == "todo.removed":
            todo_items.pop(str(data.get("id") or ""), None)

        elif etype == "alert.raised":
            raised = Alert.from_event(data.get("alert") or {})
            if raised.id:
                alert = raised

        elif etype == "alert.cleared":
            target = str(data.get("id") or "")
            if alert and (not target or alert.id == target):
                alert = None

    visible = sorted((b for b in briefs.values() if b.visible), key=lambda b: b.sort_key)
    todos = sorted(todo_items.values(), key=lambda t: (t.done, t.ts))

    if include_hidden:
        hidden = sorted((b for b in briefs.values() if not b.visible),
                        key=lambda b: -b.ts)
        listed = visible + hidden
    else:
        listed = visible

    return Board(briefs=listed, todos=todos, alert=alert,
                 total_briefs=len(visible), total_todos=len(todos))


def apply_retention(board: Board, *, now: float,
                    brief_retention_h: float = 24.0,
                    failed_retention_h: float = 72.0,
                    show_info: bool = True) -> Board:
    """按老化规则过滤。

    单独成一个函数而不是塞进 :func:`project`：投影保持纯粹，
    老化策略是**配置**，应该能独立调整和测试。
    """
    kept: list[Brief] = []
    for brief in board.briefs:
        if brief.pinned:
            kept.append(brief)          # 钉住的永不过期
            continue
        age_h = (now - brief.ts) / 3600.0
        limit = failed_retention_h if brief.status == "failed" else brief_retention_h
        if age_h > limit:
            continue
        if brief.status == "info" and not show_info:
            continue
        kept.append(brief)

    return Board(briefs=kept, todos=board.todos, alert=board.alert,
                 total_briefs=board.total_briefs, total_todos=board.total_todos)
