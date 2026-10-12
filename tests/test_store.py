"""事件溯源投影的测试。

投影是纯函数，所以可以用固定的、不依赖真实时间的事件流做断言 ——
「看板显示错了」因此是可复现的 bug，而不是玄学。
"""

from __future__ import annotations

import time

from kindlewarden.models import Brief, Todo
from kindlewarden.store import EventStore, apply_retention, project

DAY = 86400.0


def _brief_event(seq, brief_id, title, ts, *, supersedes="", status="ok"):
    return {
        "seq": seq, "ts": ts, "type": "brief.published",
        "data": {
            "supersedes": supersedes,
            "brief": {
                "id": brief_id, "ts": ts, "title": title, "status": status,
                "summary": "", "artifacts": [], "project": "", "tags": [],
                "duration_s": 0, "source": {}, "pinned": False,
                "superseded_by": "", "retracted": False,
            },
        },
    }


def test_empty_stream_gives_empty_board():
    board = project([])
    assert board.is_empty
    assert board.briefs == []
    assert board.todos == []
    assert board.alert is None


def test_briefs_sorted_newest_first():
    events = [
        _brief_event(1, "b1", "旧", 1000.0),
        _brief_event(2, "b2", "新", 2000.0),
        _brief_event(3, "b3", "中", 1500.0),
    ]
    board = project(events)
    assert [b.title for b in board.briefs] == ["新", "中", "旧"]


def test_pinned_brief_floats_to_top():
    events = [
        _brief_event(1, "b1", "新", 2000.0),
        _brief_event(2, "b2", "旧但钉住", 1000.0),
        {"seq": 3, "ts": 2001.0, "type": "brief.pinned",
         "data": {"id": "b2", "pinned": True}},
    ]
    board = project(events)
    assert [b.title for b in board.briefs] == ["旧但钉住", "新"]


def test_supersede_hides_previous_and_is_idempotent():
    events = [
        _brief_event(1, "b1", "初版", 1000.0),
        _brief_event(2, "b2", "修订版", 2000.0, supersedes="b1"),
    ]
    board = project(events)
    assert [b.title for b in board.briefs] == ["修订版"]
    # total 反映的是可见数量
    assert board.total_briefs == 1


def test_supersede_unknown_id_is_harmless():
    events = [
        _brief_event(1, "b1", "初版", 1000.0),
        _brief_event(2, "b2", "修订版", 2000.0, supersedes="不存在"),
    ]
    board = project(events)
    assert len(board.briefs) == 2


def test_retracted_brief_disappears():
    events = [
        _brief_event(1, "b1", "误报", 1000.0),
        {"seq": 2, "ts": 1001.0, "type": "brief.retracted", "data": {"id": "b1"}},
    ]
    assert project(events).briefs == []


def test_retracted_brief_stays_in_the_archive():
    """撤回只是"从板上拿下"，归档里还要能查到 —— 这是它和删除的分界。"""
    events = [
        _brief_event(1, "b1", "误报", 1000.0),
        {"seq": 2, "ts": 1001.0, "type": "brief.retracted", "data": {"id": "b1"}},
    ]
    archived = project(events, include_hidden=True).briefs
    assert [b.id for b in archived] == ["b1"]
    assert archived[0].retracted is True


def test_deleted_brief_is_gone_even_from_the_archive():
    """``brief.deleted`` 是彻底删除：连 ``include_hidden`` 都不再返回。

    与 ``retract`` 的区别就在这里 —— 两者都是追加事件，
    但删除是把这个 id 移出投影，而不是只打个标记。
    """
    events = [
        _brief_event(1, "b1", "要删掉的", 1000.0),
        _brief_event(2, "b2", "留下的", 2000.0),
        {"seq": 3, "ts": 2001.0, "type": "brief.deleted", "data": {"id": "b1"}},
    ]
    assert [b.id for b in project(events).briefs] == ["b2"]
    assert [b.id for b in project(events, include_hidden=True).briefs] == ["b2"]
    assert project(events).total_briefs == 1


def test_deleted_brief_does_not_resurrect_from_an_earlier_event():
    """删除事件排在发布事件**之前**时也不能"复活"。

    日志被手工编辑、或将来做并发导入时会遇到这种顺序。
    如果实现是"先 pop 掉 briefs 里的条目"，后面的 publish 会把它又加回来。
    """
    events = [
        {"seq": 1, "ts": 1.0, "type": "brief.deleted", "data": {"id": "b1"}},
        _brief_event(2, "b1", "删了又发", 1000.0),
    ]
    assert project(events).briefs == []
    assert project(events, include_hidden=True).briefs == []


def test_deleting_an_unknown_brief_is_harmless():
    events = [
        _brief_event(1, "b1", "正常", 1000.0),
        {"seq": 2, "ts": 1001.0, "type": "brief.deleted", "data": {"id": "不存在"}},
    ]
    assert [b.id for b in project(events).briefs] == ["b1"]


def test_delete_brief_is_append_only():
    """彻底删除也不改写日志 —— 事件仍然是**追加**的。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "board.jsonl"
        store = EventStore(path)
        store.publish_brief(Brief(id="b1", ts=1000.0, title="要删的"))
        store.publish_brief(Brief(id="b2", ts=2000.0, title="留着的"))
        before = path.read_text(encoding="utf-8")
        store.delete_brief("b1")

        after = path.read_text(encoding="utf-8")
        assert after.startswith(before), "删除必须是追加，不能改动已有事件"
        assert "brief.deleted" in after
        board = project(store.read_events())
        assert [b.id for b in board.briefs] == ["b2"]


def test_alert_raised_and_cleared():
    events = [
        {"seq": 1, "ts": 1.0, "type": "alert.raised",
         "data": {"alert": {"id": "a1", "ts": 1.0, "title": "需要确认", "detail": ""}}},
    ]
    assert project(events).alert.title == "需要确认"

    events.append({"seq": 2, "ts": 2.0, "type": "alert.cleared", "data": {"id": "a1"}})
    assert project(events).alert is None


def test_todo_toggle_and_remove():
    events = [
        {"seq": 1, "ts": 1.0, "type": "todo.added",
         "data": {"todo": {"id": "t1", "ts": 1.0, "text": "写文档", "done": False}}},
        {"seq": 2, "ts": 2.0, "type": "todo.toggled",
         "data": {"id": "t1", "done": True, "done_ts": 2.0}},
    ]
    board = project(events)
    assert board.todos[0].done is True
    assert board.todos[0].done_ts == 2.0

    events.append({"seq": 3, "ts": 3.0, "type": "todo.removed", "data": {"id": "t1"}})
    assert project(events).todos == []


def test_retention_keeps_failed_longer():
    now = time.time()
    board = project([
        _brief_event(1, "b_ok", "旧的正常简报", now - 30 * 3600, status="ok"),
        _brief_event(2, "b_fail", "旧的失败简报", now - 30 * 3600, status="failed"),
    ])
    kept = apply_retention(board, now=now)
    assert [b.title for b in kept.briefs] == ["旧的失败简报"]


def test_retention_never_expires_pinned():
    now = time.time()
    board = project([_brief_event(1, "b1", "很久以前但钉住", now - 100 * 3600)])
    board.briefs[0].pinned = True
    assert len(apply_retention(board, now=now).briefs) == 1


def test_store_roundtrip_and_torn_line_tolerance(tmp_path):
    """崩在写一半留下的残行必须被跳过，而不是让整个看板失效。"""
    path = tmp_path / "board.jsonl"
    store = EventStore(path)
    store.publish_brief(Brief(id="b1", ts=1.0, title="第一条"))
    store.add_todo(Todo(id="t1", ts=2.0, text="一条待办"))

    with path.open("a", encoding="utf-8") as fh:
        fh.write('{"seq":3,"ts":3.0,"type":"brief.pub')   # 残行

    board = project(store.read_events())
    assert len(board.briefs) == 1
    assert len(board.todos) == 1
    assert store.last_seq == 2


def test_store_seq_continues_after_reopen(tmp_path):
    path = tmp_path / "board.jsonl"
    EventStore(path).publish_brief(Brief(id="b1", ts=1.0, title="一"))
    reopened = EventStore(path)
    # 走 append 路径而不是直接读 _seq
    reopened.publish_brief(Brief(id="b2", ts=2.0, title="二"))
    seqs = [ev["seq"] for ev in reopened.read_events()]
    assert seqs == [1, 2]
