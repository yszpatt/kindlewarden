"""受件与分发服务。

职责（架构文档 §1）：
- **收**：``POST /brief`` 等受件端点，把不同终端推来的简报写进事件日志
- **投**：``GET /rev`` / ``GET /screen.png``，供 Kindle 侧拉取
- **管**：``GET /`` 控制台页面，以及配套的 ``/config`` ``/clear`` ``/device``

设计要点
--------
- ``rev`` = 渲染结果的哈希，**只有画面真的变了它才变** ——
  这是「不变就不刷」省电模型的基础。
- 缓存键 = ``(事件序号, 心跳桶)``。心跳桶让画面在无事件时也会
  每 ``heartbeat_min`` 分钟重渲染一次，更新时间戳、证明系统还活着。
- 无第三方依赖（stdlib ``http.server``）：这个服务要 7×24 常驻，
  少一个依赖就少一种半夜挂掉的方式。
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import threading
import time

from PIL import Image
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from . import layout
from . import render
from .admin_ui import ADMIN_HTML, KINDLE_HTML, VIEWER_HTML
from .config import (EDITABLE, RESTART_REQUIRED, Config, apply_patch,
                     load as load_config, save as save_config)
from .layout import CANVAS_H, CANVAS_W, Layout, fit_box
from .models import Alert, Board, Brief, Todo
from .render import (FontBook, encode_png, exit_zone_rect,
                     exit_zone_rect_at, render_board, render_detail,
                     render_offline)
from .screen import board_taps, hit_test
from .store import EventStore, apply_retention, project

MAX_BODY = 256 * 1024


def _int_or(raw: str, fallback: int) -> int:
    """把查询参数转成正整数，失败就用兜底值。"""
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback

# Kindle 自带浏览器的刷新间隔上限（秒）。
#
# 越狱客户端每次轮询都会重新问 /rev，所以能自适应 30s/300s。
# 但浏览器页面不行 —— <meta refresh> 是页面加载时**定死**的：
# 空闲时按 300s 渲染，之后即使立刻来了新简报，也要等满 5 分钟才会出现。
#
# 收紧到 60s 的代价其实几乎为零：图片 URL 带 rev 且长缓存，
# 画面没变时每次刷新只重取几百字节的 HTML，图片直接走浏览器缓存。
BROWSER_REFRESH_MAX = 60


@dataclass
class Snapshot:
    png: bytes
    rev: str
    generated_at: float
    board: Board
    layout: Layout | None
    health: str = "ok"


class BoardService:
    """把事件日志、投影、渲染、缓存串起来。线程安全。"""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = EventStore(cfg.state_path)
        self.fonts = FontBook()
        self._lock = threading.RLock()
        self._cache_key: tuple | None = None
        self._cache: Snapshot | None = None
        self._last_good: Snapshot | None = None
        self._offline_png: bytes | None = None
        self._offline_key: float | None = None
        self.device_path = cfg.data_dir / "device.json"
        self._device: dict = self._load_device()

    def _load_device(self) -> dict:
        try:
            return json.loads(self.device_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    # ---------------------------------------------------------------- 读

    def board(self, now: float | None = None,
              *, include_hidden: bool = False) -> Board:
        now = now if now is not None else time.time()
        raw = project(self.store.read_events(), include_hidden=include_hidden)
        return apply_retention(
            raw, now=now,
            brief_retention_h=self.cfg.board.brief_retention_h,
            failed_retention_h=self.cfg.board.failed_retention_h,
            show_info=self.cfg.board.show_info,
        )

    def snapshot(self, now: float | None = None, force: bool = False) -> Snapshot:
        """取当前画面。命中缓存时不做任何渲染。"""
        now = now if now is not None else time.time()
        heartbeat = max(1.0, self.cfg.server.heartbeat_min) * 60.0
        key = (self.store.last_seq, int(now // heartbeat))

        with self._lock:
            if not force and self._cache is not None and self._cache_key == key:
                return self._cache
            try:
                snap = self._build(now)
            except Exception:
                # 渲染失败不该让已经上屏的画面消失：退回上一张好图，并把健康度标脏
                if self._last_good is not None:
                    return Snapshot(
                        png=self._last_good.png, rev=self._last_good.rev,
                        generated_at=self._last_good.generated_at,
                        board=self._last_good.board, layout=self._last_good.layout,
                        health="stale",
                    )
                raise
            self._cache, self._cache_key = snap, key
            self._last_good = snap
            return snap

    def offline_png(self, now: float | None = None) -> bytes:
        """连接中断时显示的画面。设备在首次成功同步时把它缓存到本地。"""
        now = now if now is not None else time.time()
        bucket = int(now // 86400)          # 每天重算一次即可
        with self._lock:
            if self._offline_png is None or self._offline_key != bucket:
                self._offline_png = encode_png(render_offline(self.cfg, now=now,
                                                              fonts=self.fonts))
                self._offline_key = bucket
            return self._offline_png

    def _build(self, now: float) -> Snapshot:
        board = self.board(now)
        disp = self.cfg.display

        # ---- 横屏：先按**宽>高**排版，再旋转 90° 输出
        #
        # 尺寸正好对上：1448×1072 转 90° = 1072×1448，
        # 而插件硬编码的就是 1072×1448 —— 所以它毫无察觉。
        #
        # 为什么不干脆渲染成 1072×1448 的"宽版面"：
        #   因为那等于把 1072 当宽度、1448 当高度，排版算法会按"竖长条"分配
        #   空间（比如简报区能放 6 条）。转 90° 看的时候，那个"竖长条"其实
        #   横过来了 —— 得到的是"横向很宽但每栏很矮"的怪版面。
        #   先按真实方向（宽 1448、高 1072）排版，转过来才是对的。
        if disp.landscape:
            render.set_canvas(layout.DEVICE_H, layout.DEVICE_W)
        else:
            render.reset_canvas()

        try:
            img, lay = render_board(board, self.cfg, now=now, fonts=self.fonts)
        finally:
            # ⚠️ 必须恢复：这些是**模块级全局变量**，
            #    不恢复的话下一次渲染（可能是竖屏）会沿用横屏尺寸。
            render.reset_canvas()

        img = self._apply_display(img)
        png = encode_png(img)
        return Snapshot(
            png=png,
            rev=hashlib.sha1(png).hexdigest()[:12],
            generated_at=now,
            board=board,
            layout=lay,
        )

    def _apply_display(self, img):
        """把显示模式（横屏旋转 / 夜间反色）作用到**已经渲染好的图**上。

        这个顺序是刻意的 —— **先转再反色**，而不是先反色再转。
        两者结果相同（旋转和逐像素映射可交换），但先转能保证
        "反色一定作用在最终交付的那张图上"，不会因为旋转引入的边缘
        像素而漏掉某些区域。

        最关键的是：**这里输出的尺寸永远是设备物理尺寸（1072×1448）**。
        插件只认这个尺寸，任何模式都不能改变它。
        """
        disp = self.cfg.display

        if disp.landscape:
            # ⚠️ PIL 的 transpose 常量名有历史包袱，**别按名字猜方向**。
            #
            # 实测（用左上角标记块验证）：
            #   ROTATE_90  —— 左上角跑到**左下** → 实际是**逆时针**
            #   ROTATE_270 —— 左上角跑到**右上** → 实际是**顺时针**
            #
            # 而 _transform_rect 里的公式："cw" 用 (x,y)→(H-y, x)。
            # 所以这里必须**反过来对应**：
            #   rotate == "cw"  →  ROTATE_270（那个才是顺时针）
            #   rotate == "ccw" →  ROTATE_90
            #
            # 踩过一次：按名字对上（cw↔ROTATE_90），结果画面转到了下方
            # 而点击区域按右侧算 —— 画和坐标**反着转**，
            # 现象是"画面能看但点哪儿都不对"。
            img = img.transpose(Image.ROTATE_270 if disp.rotate == "cw"
                                else Image.ROTATE_90)

        if disp.night:
            # 反色：255 - v。
            # 我们的调色板本来就落在 17 的网格上（255/15），
            # 所以 255 - (17n) = 17(15-n) 仍在网格上 —— 量化结果不变，
            # 不需要重新对齐。这一点让夜间模式可以做成纯后处理。
            img = img.point(lambda v: 255 - v)

        return img

    def poll_interval(self, snap: Snapshot, now: float | None = None) -> int:
        """服务端告诉客户端下次何时来。

        服务端最清楚事件流的活跃度，比客户端猜更准。
        """
        now = now if now is not None else time.time()
        if snap.board.alert is not None:
            return self.cfg.server.poll_active_s
        newest = max((b.ts for b in snap.board.briefs), default=0.0)
        if now - newest < 600:
            return self.cfg.server.poll_active_s
        return self.cfg.server.poll_idle_s

    def blocks(self, snap: Snapshot) -> list[dict]:
        """分区哈希。

        当前客户端**不使用**（决策：不做脏区域更新），但协议字段现在就是真的 ——
        将来做局部刷新不需要改协议。
        """
        if snap.layout is None:
            return []
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(snap.png))
        out: list[dict] = []
        for z in snap.layout.zones():
            top = max(0, z.label_y)
            bottom = min(img.height, z.y + z.shown * z.item_h)
            if bottom <= top:
                continue
            crop = img.crop((0, top, img.width, bottom))
            out.append({
                "id": z.key,
                "bbox": [0, top, img.width, bottom],
                "hash": hashlib.sha1(crop.tobytes()).hexdigest()[:8],
            })
        return out

    # ---------------------------------------------------------------- 写

    def publish_brief(self, payload: dict) -> dict:
        brief, hits, truncated = Brief.from_request(payload)
        supersedes = str(payload.get("supersedes") or "")
        self.store.publish_brief(brief, supersedes=supersedes)
        self._invalidate()
        return {
            "accepted": True,
            "brief_id": brief.id,
            "redactions": sorted(set(hits)),
            "truncated": truncated,
            "title": brief.title,
            "warnings": brief.display_warnings(),
        }

    def add_todo(self, payload: dict) -> dict:
        todo, hits, truncated = Todo.from_request(payload)
        self.store.add_todo(todo)
        self._invalidate()
        return {"accepted": True, "todo_id": todo.id,
                "redactions": sorted(set(hits)), "truncated": truncated}

    def toggle_todo(self, todo_id: str, done: bool) -> dict:
        self.store.toggle_todo(todo_id, done)
        self._invalidate()
        return {"accepted": True, "todo_id": todo_id, "done": done}

    def update_todo(self, todo_id: str, text: str) -> dict:
        todo, hits, truncated = Todo.from_request({"text": text})
        self.store.edit_todo(todo_id, todo.text)
        self._invalidate()
        return {"accepted": True, "todo_id": todo_id, "text": todo.text,
                "redactions": sorted(set(hits)), "truncated": truncated}

    def remove_todo(self, todo_id: str) -> dict:
        self.store.remove_todo(todo_id)
        self._invalidate()
        return {"accepted": True, "todo_id": todo_id}

    def raise_alert(self, payload: dict) -> dict:
        alert, hits, truncated = Alert.from_request(payload)
        self.store.raise_alert(alert)
        self._invalidate()
        return {"accepted": True, "alert_id": alert.id,
                "redactions": sorted(set(hits)), "truncated": truncated,
                "warnings": alert.display_warnings()}

    def clear_alert(self, alert_id: str = "") -> dict:
        self.store.clear_alert(alert_id)
        self._invalidate()
        return {"accepted": True}

    def pin_brief(self, brief_id: str, pinned: bool = True) -> dict:
        self.store.pin_brief(brief_id, pinned)
        self._invalidate()
        return {"accepted": True, "brief_id": brief_id, "pinned": pinned}

    def retract_brief(self, brief_id: str) -> dict:
        self.store.retract_brief(brief_id)
        self._invalidate()
        return {"accepted": True, "brief_id": brief_id}

    def delete_brief(self, brief_id: str) -> dict:
        """彻底删除一条简报（连归档也不再有）。

        与 ``retract_brief`` 的区别：撤回只是从板上拿下、归档还在；
        删除是移出投影。两者都追加事件，日志不被改写。
        """
        self.store.delete_brief(brief_id)
        self._invalidate()
        return {"accepted": True, "brief_id": brief_id, "deleted": True}

    def _invalidate(self) -> None:
        with self._lock:
            self._cache_key = None

    # ---------------------------------------------------------------- 设备在线状态

    def note_device(self, ip: str, user_agent: str = "") -> None:
        """记录 Kindle 最后一次拉取。

        客户端会带 ``?c=kindle``，所以管理页自己的轮询不会被算成设备活动 ——
        否则「Kindle 最后在线」永远显示「刚刚」，这个指标就废了。
        """
        entry = {"ip": ip, "ua": (user_agent or "")[:160], "last_seen": time.time()}
        with self._lock:
            self._device = entry
        try:
            self.device_path.write_text(
                json.dumps(entry, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def device_last_seen(self) -> dict:
        """最近一次设备活动（给 /check 页面用）。"""
        with self._lock:
            dev = dict(self._device)
        if dev.get("last_seen"):
            dev["age_s"] = max(0, int(time.time() - dev["last_seen"]))
        return dev

    def device_payload(self) -> dict:
        with self._lock:
            dev = dict(self._device)
        if dev.get("last_seen"):
            dev["age_s"] = max(0, int(time.time() - dev["last_seen"]))
        return {
            "device": dev or None,
            "poll_idle_s": self.cfg.server.poll_idle_s,
            "poll_active_s": self.cfg.server.poll_active_s,
            "heartbeat_min": self.cfg.server.heartbeat_min,
            # 管理页拿它来显示「Kindle 上打开哪个地址」——
            # 这是最常被问的问题，不该让人去命令行里查。
            "lan": lan_addresses(),
            "port": self.cfg.server.port,
        }

    # ---------------------------------------------------------------- 配置

    def config_payload(self) -> dict:
        # ⚠️ 这里是**逐节列出**的，新增配置节必须加进来 ——
        #    否则管理页看不到、也改不了它（现象是"设置项凭空消失"）。
        #    踩过一次：加了 display 节但忘了这里，管理页里那两个开关根本不出现。
        return {
            "panels": asdict(self.cfg.panels),
            "board": asdict(self.cfg.board),
            "ambient": asdict(self.cfg.ambient),
            "browser": asdict(self.cfg.browser),
            "display": asdict(self.cfg.display),
            "server": asdict(self.cfg.server),
            "editable": {k: list(v) for k, v in EDITABLE.items()},
            "restart_required": list(RESTART_REQUIRED),
            "path": str(self.cfg.source_path or self.cfg.config_path),
        }

    def update_config(self, patch: dict) -> dict:
        changed = apply_patch(self.cfg, patch)
        path = save_config(self.cfg)
        self._invalidate()          # cfg 是同一个对象，清缓存即生效
        return {"accepted": True, "changed": changed, "path": str(path)}

    # ---------------------------------------------------------------- 清空

    def clear_all(self) -> dict:
        """清空看板。

        用**事件**（撤回简报 + 删除待办 + 清除告警）实现，而不是删文件 ——
        事件溯源的意义就是可回放，「清空」也应该是日志里一次可追溯的操作。
        """
        board = project(self.store.read_events())
        retracted = 0
        for brief in board.briefs:
            self.store.retract_brief(brief.id)
            retracted += 1
        for todo in board.todos:
            self.store.remove_todo(todo.id)
        if board.alert:
            self.store.clear_alert(board.alert.id)
        self._invalidate()
        return {"accepted": True, "retracted": retracted,
                "todos_removed": len(board.todos),
                "alert_cleared": board.alert is not None}

    # ---------------------------------------------------------------- 序列化

    def rev_payload(self, snap: Snapshot, now: float | None = None) -> dict:
        return {
            "global_rev": snap.rev,
            "generated_at": snap.generated_at,
            "source_health": snap.health,
            "next_poll_after": self.poll_interval(snap, now),
            "blocks": self.blocks(snap),
        }

    def board_taps_for_delivery(self, snap: Snapshot) -> tuple[list[dict], list[dict]]:
        """算出看板的点击区域，**已经换算到交付坐标系**。

        ⚠️ 这是唯一的实现 —— ``/board`` 和 ``/screen.json`` 都必须用它。

        踩过两次同一个坑：
          1. 只在 ``/board`` 里做了旋转变换，``/screen.json`` 忘了 ——
             而**插件用的是后者**，所以横屏下点哪儿都不对
          2. 两处各自调 ``board_taps``，但画布尺寸没切到横屏 ——
             矩形按竖屏算，画面是横屏画的

        把"切画布 → 算矩形 → 旋转 → 恢复画布"收进一个方法，
        两个端点就不可能不一致了。
        """
        if self.cfg.display.landscape:
            render.set_canvas(layout.DEVICE_H, layout.DEVICE_W)
        else:
            render.reset_canvas()
        try:
            briefs, todos = board_taps(snap.board, self.cfg)
        finally:
            render.reset_canvas()

        return (
            [{**b, "rect": self._transform_rect(b["rect"])} for b in briefs],
            [{**t, "rect": self._transform_rect(t["rect"])} for t in todos],
        )

    def board_payload(self, snap: Snapshot, *, include_hidden: bool = False) -> dict:
        board = snap.board
        if include_hidden:
            board = self.board(include_hidden=True)

        # 点击区域：插件靠它把触摸坐标映射成"点了哪一条"。
        # 必须和渲染用同一套布局计算，否则画面和可点区域会错开。
        brief_rects, todo_rects = self.board_taps_for_delivery(snap)
        rect_by_id = {b["id"]: b["rect"] for b in brief_rects}
        todo_rect_by_id = {t["id"]: t["rect"] for t in todo_rects}

        def brief_json(b: Brief) -> dict:
            data = b.to_dict()
            # 入库不再截断，所以「板上会不会被切掉」这条信息只能由服务端算给管理页
            data["warnings"] = b.display_warnings()
            # 板上可见的条目才有点击区域
            if b.id in rect_by_id:
                data["rect"] = rect_by_id[b.id]
            return data

        def todo_json(t: Todo) -> dict:
            data = t.to_dict()
            if t.id in todo_rect_by_id:
                data["rect"] = todo_rect_by_id[t.id]
            return data

        return {
            "rev": snap.rev,
            "generated_at": snap.generated_at,
            "source_health": snap.health,
            "screen": "board",
            "size": [CANVAS_W, CANVAS_H],
            "alert": board.alert.to_dict() if board.alert else None,
            "briefs": [brief_json(b) for b in board.briefs],
            "todos": [todo_json(t) for t in board.todos],
            "total_briefs": board.total_briefs,
            "total_todos": board.total_todos,
        }

    # ---------------------------------------------------------------- 详情页

    def _find_brief(self, brief_id: str) -> Brief | None:
        for brief in self.board(include_hidden=True).briefs:
            if brief.id == brief_id:
                return brief
        return None

    def _transform_rect(self, rect: list[int]) -> list[int]:
        """把**排版坐标系**里的矩形换算到**最终交付图**的坐标系。

        横屏时会转 90°，所以点击区域必须跟着转 —— 否则插件拿到的矩形
        和它看到的画面**对不上**，点哪儿都是错的。

        这是整个横屏功能最容易出错的地方：画转了、坐标没转，
        现象是"按钮看得见但点不中"或者"点到旁边的条目"。

        ⚠️ **矩形是半开区间**：``[x0, y0, x1, y1]`` 里 ``x1``/``y1`` 是**不含**的，
        所以宽度是 ``x1 - x0``（box_taps 里 x1 = MARGIN + CONTENT_W 就是这么来的）。

        按半开区间推导（源尺寸 W×H，输出尺寸 H×W）::

            顺时针：(x, y) → (H - 1 - y, x)
              像素 x 范围 [x0, x1)
              → x' = H-1-y 落在 y∈[y0,y1) → x' ∈ [H-y1, H-y0)
              → y' = x                    → y' ∈ [x0, x1)

            逆时针：(x, y) → (y, W - 1 - x)
              → [y0, W-x1, y1, W-x0)

        这样写**没有 ±1**，而且面积严格不变。

        第一版把矩形当闭区间、又加了 +1，结果 10×10 的框变换后变成 11×11 ——
        面积变了，说明不是纯旋转。测试当场抓到了。
        """
        if not self.cfg.display.landscape:
            return list(rect)

        x0, y0, x1, y1 = rect
        # 排版时的尺寸（横屏是 DEVICE_H × DEVICE_W）
        sw, sh = layout.DEVICE_H, layout.DEVICE_W

        if self.cfg.display.rotate == "cw":
            return [sh - y1, x0, sh - y0, x1]
        return [y0, sw - x1, y1, sw - x0]

    def detail(self, brief_id: str, page: int = 0, now: float | None = None):
        """渲染详情页。

        返回 ``(brief, png, taps, page, total, exit_zone)``；找不到返回 ``None``。
        """
        brief = self._find_brief(brief_id)
        if brief is None:
            return None
        now = now if now is not None else time.time()
        disp = self.cfg.display

        if disp.landscape:
            render.set_canvas(layout.DEVICE_H, layout.DEVICE_W)
        else:
            render.reset_canvas()
        try:
            img, taps, total, exit_zone = render_detail(
                brief, self.cfg, now=now, fonts=self.fonts, page=page)
        finally:
            render.reset_canvas()

        page = max(0, min(page, total - 1))
        img = self._apply_display(img)

        # ⚠️ 坐标要旋转，但 **exit_zone 用交付坐标系、不旋转** —— 两者不一样。
        #
        # 为什么 exit_zone 特殊：
        #   * 它是**插件画的**（不依赖服务端，见"退不出去"那次事故）。
        #     插件拿到矩形后画一个固定大小（152×34）的**横**按钮。
        #     如果把矩形转成 34×152 的竖条，按钮会**溢出 118px**，
        #     而且点击区域和实际画出来的位置对不上。
        #   * 不旋转并不会盖住内容：MARGIN 四边都是 40px，
        #     所以「交付图的底部边带」旋转后正好落在「排版图的左/右边距带」——
        #     两边都是布局引擎留白的区域，本来就是空的。
        #
        # （实测：竖屏 [880,1410,1032,1444] 的底部带，顺时针旋转后
        #   对应排版图的右边距 [1410,40,1444,192]，确实在 40px 边距带里。）
        #
        # ⚠️ 另外，render_detail 返回的 exit_zone 用的是**当前画布**
        #    （横屏时是 1448×1072 的排版坐标系），不能直接交付 ——
        #    交付坐标系是 1072×1448，那个矩形右边界 1408 会超出 1072。
        #    所以这里**重新按交付尺寸算一次**。
        taps = [{**t, "rect": self._transform_rect(t["rect"])} for t in taps]
        exit_zone = self._delivery_exit_zone()

        return brief, encode_png(img), taps, page, total, exit_zone

    def _delivery_exit_zone(self) -> list[int]:
        """退出按钮保留区，按**交付坐标系**（1072×1448）算。

        和 ``render.exit_zone_rect()`` 是同一个定义，但那个函数读的是
        "当前画布"（横屏渲染时被设成了 1448×1072），所以不能直接用。
        """
        return exit_zone_rect_at(layout.DEVICE_W, layout.DEVICE_H)


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    service: BoardService
    protocol_version = "HTTP/1.1"
    server_version = "kindlewarden"

    # -------------------------------------------------- 连接异常兜底

    def handle_one_request(self) -> None:
        """把「客户端中途断连」当成正常现象，而不是错误。

        ⚠️ 这是踩过的坑，而且后果很严重：**一个浏览器就能把 7×24 的服务搞死。**

        实测：服务的日志以

            ConnectionResetError: [WinError 10054] 远程主机强迫关闭了一个现有的连接。

        结尾，然后进程 exit 1。起因只是浏览器请求 ``/favicon.ico`` 之后
        关掉了连接 —— 而 ``socketserver`` 会在请求处理里抛未捕获异常。

        对一个要长期常驻、给设备提供画面的服务来说，这完全不可接受：
        用户随时在开关浏览器页面，每次都可能触发。

        这里把连接类异常咽掉（客户端走了就是走了，没什么可做的），
        其余异常仍然照常抛出，免得掩盖真正的 bug。
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError, TimeoutError):
            # 客户端提前断开：正常现象，静默收工。
            # 关掉连接，避免 HTTP/1.1 keep-alive 在坏连接上继续转。
            self.close_connection = True

    def handle(self) -> None:
        """同上：整个连接处理过程都要能容忍断连。"""
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError,
                BrokenPipeError, TimeoutError):
            self.close_connection = True

    # -------------------------------------------------- 工具

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict | None = None, cache: str = "no-store") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            # 写响应也可能撞上对端已经断开 —— 同样不该让服务挂掉
            try:
                self.wfile.write(body)
            except (ConnectionResetError, ConnectionAbortedError,
                    BrokenPipeError, TimeoutError):
                self.close_connection = True

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def _error(self, code: int, message: str) -> None:
        self._json(code, {"error": message})

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        if not raw:
            return {}
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def log_message(self, fmt: str, *args) -> None:
        # 默认日志把每个轮询都打出来太吵；只保留错误
        if args and str(args[1] if len(args) > 1 else "").startswith(("4", "5")):
            super().log_message(fmt, *args)

    def _query(self) -> dict[str, list[str]]:
        return parse_qs(urlsplit(self.path).query)

    def _is_device_request(self) -> bool:
        """区分「Kindle 在拉」和「管理页在拉」。

        管理页自己也轮询 /rev，如果不区分，「Kindle 最后在线」会永远显示「刚刚」，
        这个指标就完全没有意义了。
        """
        if self.headers.get("X-Warden-Client", "").lower() == "kindle":
            return True
        return self._query().get("c", [""])[0].lower() == "kindle"

    def _note_device(self) -> None:
        self.service.note_device(self.client_address[0],
                                 self.headers.get("User-Agent", ""))

    # -------------------------------------------------- GET

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        svc = self.service
        query = self._query()

        if path in ("/", "/index.html", "/admin"):
            return self._send(200, ADMIN_HTML.encode("utf-8"),
                              "text/html; charset=utf-8")

        if path in ("/check", "/api/check"):
            # **设备自检页** —— 让 Kindle 自己报告"我能不能连上"。
            #
            # 为什么需要它：主机侧能查的都查了（服务在跑、IP 没变、地址对），
            # 但设备报「服务不可达」。剩下的可能都在设备侧
            # （连的哪个 WiFi、能不能路由到主机），而那些从 USB 分区读不出来。
            #
            # 让用户用 Kindle 浏览器打开这个页面，一眼就能分清：
            #   * 打得开 → 网络通，问题在插件
            #   * 打不开 → 网络不通，查 WiFi / 防火墙
            #
            # 页面必须**零 JS**：Kindle 浏览器的老 WebKit 不支持 let/const，
            # 脚本一出现整段都不执行。
            ua = self.headers.get("User-Agent", "")
            client_ip = self.client_address[0]
            html = _check_page(client_ip=client_ip, ua=ua,
                               port=cfg_port(self.server),
                               device_last=svc.device_last_seen())
            return self._send(200, html.encode("utf-8"),
                              "text/html; charset=utf-8")

        if path in ("/rev", "/api/rev"):
            if self._is_device_request():
                self._note_device()
            snap = svc.snapshot()
            return self._json(200, svc.rev_payload(snap))

        if path in ("/screen.png", "/screen", "/api/screen.png"):
            if self._is_device_request():
                self._note_device()
            snap = svc.snapshot()
            etag = f'"{snap.rev}"'
            if self.headers.get("If-None-Match") == etag:
                return self._send(304, b"", "image/png", {"ETag": etag})

            # 带 ?rev= 的 URL 是**内容寻址**的：内容一变 rev 就变、URL 就变。
            # 所以可以放心长缓存 —— 这样 Kindle 浏览器只有在画面真的变化时
            # 才会重新下载，省流量也省电量。没有 rev 的请求保持 no-store。
            rev_param = query.get("rev", [""])[0]
            cache = ("public, max-age=31536000, immutable"
                     if rev_param else "no-store")
            return self._send(200, snap.png, "image/png",
                              {"ETag": etag}, cache=cache)

        if path in ("/offline.png", "/api/offline.png"):
            return self._send(200, svc.offline_png(), "image/png")

        if path in ("/board", "/api/board"):
            flag = query.get("include_hidden", ["0"])[0]
            include_hidden = flag not in ("0", "", "false", "False")
            snap = svc.snapshot()
            return self._json(200, svc.board_payload(snap, include_hidden=include_hidden))

        if path in ("/detail.png", "/api/detail.png"):
            # KOReader 插件用它显示详情页。参数：id=<简报id>&page=<页码，从 1 开始>
            if self._is_device_request():
                self._note_device()
            brief_id = query.get("id", [""])[0]
            # 对外用 1 开始（人看地址栏友好），内部 0 开始
            page = max(0, _int_or(query.get("page", ["1"])[0], 1) - 1)
            got = svc.detail(brief_id, page)
            if got is None:
                return self._error(404, f"没有这条简报：{brief_id}")
            _brief, png, _taps, _page, _total, _ez = got
            return self._send(200, png, "image/png")

        if path in ("/screen.json", "/api/screen.json"):
            # 插件开屏时拉这个：一条 JSON 里同时拿到 rev、点击区域、页数。
            # 单独一个端点而不是复用 /board，是因为插件在 Kindle 上解析，
            # 给它的东西越少越好（/board 是给管理页用的，字段多得多）。
            if self._is_device_request():
                self._note_device()
            snap = svc.snapshot()
            # ⚠️ 必须走 board_taps_for_delivery（它含横屏的坐标旋转）——
            #    直接调 board_taps 会漏掉旋转，而**插件用的就是这个端点**。
            briefs, todos = svc.board_taps_for_delivery(snap)
            return self._json(200, {
                "screen": "board",
                "rev": snap.rev,
                "generated_at": snap.generated_at,
                "source_health": snap.health,
                "size": [CANVAS_W, CANVAS_H],
                "briefs": briefs,
                "todos": todos,
                # ⚠️ 「退出」按钮的保留区，由服务端定义、插件照画。
                #    踩过一次：插件自己算位置、服务端也在右下角画翻页按钮，
                #    两边各算各的，结果完全重叠。
                #    现在只有这一处定义，从根上不可能再撞。
                #
                #    用 _delivery_exit_zone()：横屏时"当前画布"是 1448×1072，
                #    直接调 exit_zone_rect() 会拿到排版坐标系的矩形
                #    （右边界 1408），而交付坐标系只有 1072 宽 —— 越界。
                "exit_zone": svc._delivery_exit_zone(),
            })

        if path in ("/detail.json", "/api/detail.json"):
            brief_id = query.get("id", [""])[0]
            # ⚠️ **必须带上 page 参数** —— 这个端点曾经写死 page=0。
            #
            # 后果很隐蔽：插件请求第 2 页时，PNG 拿到的确实是第 2 页，
            # 但**taps 永远是第 1 页的**。
            # 于是：
            #   * 第 2 页上显示的是第 1 页的按钮位置（可能对不上）
            #   * 第 2 页的「‹ 上一页」目标来自第 1 页的 taps —— 而第 1 页
            #     只有「下一页」，所以**「上一页」点不动**
            #
            # 修「下一页」时我只测了下一页，没顺手测上一页，所以漏了它。
            page = max(0, _int_or(query.get("page", ["1"])[0], 1) - 1)
            got = svc.detail(brief_id, page)
            if got is None:
                return self._error(404, f"没有这条简报：{brief_id}")
            brief, _png, taps, cur_page, total, ez = got
            return self._json(200, {
                "screen": "detail",
                "brief_id": brief.id,
                "title": brief.title,
                # 1 基准，和 URL 参数一致（插件拿它就能直接用）
                "page": cur_page + 1,
                "pages": total,
                "size": [CANVAS_W, CANVAS_H],
                "taps": taps,
                # 退出按钮保留区，和 /screen.json 用同一个定义
                "exit_zone": ez,
            })

        if path in ("/config", "/api/config"):
            return self._json(200, svc.config_payload())

        if path in ("/device", "/api/device"):
            return self._json(200, svc.device_payload())

        if path in ("/health", "/api/health"):
            return self._json(200, {"status": "ok", "seq": svc.store.last_seq})

        if path in ("/view", "/viewer"):
            return self._send(200, VIEWER_HTML.encode("utf-8"),
                              "text/html; charset=utf-8")

        if path in ("/k", "/kindle"):
            # 给 Kindle 自带浏览器用的零 JS 页面。
            # 刷新间隔由服务端按当前活跃度注入 —— 老浏览器不需要为此跑任何脚本。
            if self._is_device_request():
                self._note_device()
            snap = svc.snapshot()
            every = max(15, min(svc.poll_interval(snap), BROWSER_REFRESH_MAX))

            # 图片尺寸在这里算好并写成 width/height 属性，不交给 CSS。
            # 视口默认值可以在管理控制台里调，也支持用 ?w= / ?h= 临时试。
            box_w = _int_or(query.get("w", [""])[0], self.service.cfg.browser.viewport_w)
            box_h = _int_or(query.get("h", [""])[0], self.service.cfg.browser.viewport_h)
            disp_w, disp_h = fit_box(CANVAS_W, CANVAS_H, box_w, box_h)

            page = (KINDLE_HTML
                    .replace("__EVERY__", str(every))
                    .replace("__REV__", snap.rev)
                    .replace("__W__", str(disp_w))
                    .replace("__H__", str(disp_h)))
            return self._send(200, page.encode("utf-8"),
                              "text/html; charset=utf-8")

        self._error(404, "未找到：" + path)

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    # -------------------------------------------------- POST

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        svc = self.service
        try:
            payload = self._read_json()
        except (ValueError, json.JSONDecodeError) as exc:
            return self._error(400, f"请求体解析失败：{exc}")

        try:
            if path in ("/brief", "/api/brief"):
                return self._json(200, svc.publish_brief(payload))

            if path in ("/todo", "/api/todo"):
                return self._json(200, svc.add_todo(payload))

            if path in ("/todo/update", "/api/todo/update"):
                return self._json(200, svc.update_todo(
                    str(payload.get("id") or ""), str(payload.get("text") or "")))

            if path in ("/todo/toggle", "/api/todo/toggle"):
                return self._json(200, svc.toggle_todo(
                    str(payload.get("id") or ""), bool(payload.get("done", True))))

            if path in ("/todo/remove", "/api/todo/remove"):
                return self._json(200, svc.remove_todo(str(payload.get("id") or "")))

            if path in ("/alert", "/api/alert"):
                return self._json(200, svc.raise_alert(payload))

            if path in ("/alert/clear", "/api/alert/clear"):
                return self._json(200, svc.clear_alert(str(payload.get("id") or "")))

            if path in ("/pin", "/api/pin"):
                return self._json(200, svc.pin_brief(
                    str(payload.get("id") or ""), bool(payload.get("pinned", True))))

            if path in ("/retract", "/api/retract"):
                return self._json(200, svc.retract_brief(str(payload.get("id") or "")))

            if path in ("/brief/delete", "/api/brief/delete"):
                return self._json(200, svc.delete_brief(str(payload.get("id") or "")))

            if path in ("/config", "/api/config"):
                return self._json(200, svc.update_config(payload))

            if path in ("/clear", "/api/clear"):
                return self._json(200, svc.clear_all())

            if path in ("/refresh", "/api/refresh"):
                return self._json(200, {"accepted": True,
                                        "rev": svc.snapshot(force=True).rev})

            if path in ("/register", "/api/register"):
                self._note_device()
                return self._json(200, {"accepted": True})
        except Exception as exc:                      # noqa: BLE001
            return self._error(500, f"处理失败：{exc}")

        self._error(404, "未找到：" + path)


# ---------------------------------------------------------------- 启动与自检


def is_likely_virtual(addr: str) -> bool:
    """判断是否像虚拟网卡地址。

    这个判断很重要：WSL / Hyper-V / Docker / VirtualBox 的虚拟网卡也是
    「局域网 IPv4」，但它们**从 Kindle 根本连不上**。
    如果不区分，启动提示会把 172.31.x.x 这样的地址推荐给用户，
    而现象依然是「页面无法显示」—— 和监听地址错误一样难以定位。
    """
    try:
        a, b, c, _d = (int(part) for part in addr.split("."))
    except ValueError:
        return False
    if a == 172 and 16 <= b <= 31:        # WSL / Docker / Hyper-V 默认段
        return True
    if (a, b, c) == (192, 168, 56):       # VirtualBox host-only
        return True
    if (a, b) == (169, 254):              # link-local
        return True
    return False


def cfg_port(server) -> int:
    """从 server 实例取端口（/check 页面要显示它）。"""
    try:
        return int(server.server_address[1])
    except (AttributeError, IndexError, TypeError, ValueError):
        return 8577


def _check_page(*, client_ip: str, ua: str, port: int,
                device_last: dict) -> str:
    """设备自检页 —— 让 Kindle 自己回答"我能不能连上服务"。

    **零 JavaScript。** Kindle 浏览器的老 WebKit 不支持 ``let``/``const``
    （脚本里一出现，整段都不执行），所以这个页面必须纯静态 HTML。

    它要回答一个二分问题：
      * 打得开 → 网络通，问题在插件
      * 打不开 → 网络不通，查 WiFi / 防火墙

    打不开时用户看不到这个页面（那是必然的），所以页面本身要在
    **打开后**尽可能给出信息：请求来自哪个 IP、上次设备活动是什么时候。
    """
    import html as _html

    last = device_last or {}
    age = last.get("age_s")
    if age is None:
        last_txt = "从未有过设备请求"
    elif age < 60:
        last_txt = f"{age} 秒前"
    elif age < 3600:
        last_txt = f"{age // 60} 分钟前"
    else:
        last_txt = f"{age // 3600} 小时前"

    ip = _html.escape(str(client_ip))
    ua_txt = _html.escape((ua or "(空)")[:120])
    last_ip = _html.escape(str(last.get("ip") or "—"))

    return f"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>kindlewarden 自检</title>
<style>
  body {{ font-family: sans-serif; margin: 24px; color: #000; background: #fff; }}
  h1 {{ font-size: 26px; margin: 0 0 18px; }}
  .ok {{ color: #060; font-weight: bold; font-size: 22px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 14px; }}
  td {{ border-bottom: 1px solid #ccc; padding: 10px 6px; font-size: 17px;
        vertical-align: top; }}
  td.k {{ color: #666; width: 40%; }}
  .foot {{ margin-top: 22px; color: #666; font-size: 15px; line-height: 1.5; }}
</style>
</head>
<body>
  <h1>kindlewarden 自检</h1>
  <p class="ok">网络是通的 —— 这个页面就是从 Kindle 上打开的。</p>
  <table>
    <tr><td class="k">你的请求来自</td><td>{ip}</td></tr>
    <tr><td class="k">服务端口</td><td>{port}</td></tr>
    <tr><td class="k">上次设备活动</td><td>{last_txt}（来自 {last_ip}）</td></tr>
    <tr><td class="k">User-Agent</td><td>{ua_txt}</td></tr>
  </table>
  <p class="foot">
    既然能打开这个页面，说明 <b>Kindle 到主机的网络没问题</b>。<br>
    如果副屏仍然显示空白，那就是插件侧的问题，不是网络。<br><br>
    副屏画面在 <a href="/screen.png">/screen.png</a>，
    可以点开看看服务端到底有没有画东西。
  </p>
</body>
</html>
"""


#: 用这个环境变量**覆盖**「Kindle 该访问哪个地址」。
#:
#: 为什么需要：容器里自动探测拿到的是**容器自己的**地址（172.17.x.x），
#: 那个地址在局域网里根本不存在 —— Kindle 照着连必然失败，
#: 而现象只是「页面无法显示」，很难联想到是地址猜错了。
#: 所以容器部署时要显式告诉它宿主机的地址：
#:
#:     docker run -e KINDLEWARDEN_PUBLIC_HOST=192.168.1.50 ...
PUBLIC_HOST_ENV = "KINDLEWARDEN_PUBLIC_HOST"


def lan_addresses() -> list[str]:
    """枚举本机局域网 IPv4，**真实出站网卡排在第一位**。

    用途是告诉用户「Kindle 上到底该打开哪个地址」。
    打印 ``<本机 LAN IP>`` 这种占位符是没用的，会掩盖两类真实故障：
    服务只监听了 127.0.0.1，以及把虚拟网卡地址推荐给了用户。

    若设了 ``KINDLEWARDEN_PUBLIC_HOST``，直接用它（容器部署必需，
    见 ``PUBLIC_HOST_ENV`` 的说明）。此时**只返回那一个地址** ——
    自动探测出来的容器地址是错的，列在旁边只会让人挑错。
    """
    override = (os.environ.get(PUBLIC_HOST_ENV) or "").strip()
    if override:
        # 允许写成 "http://1.2.3.4:8577" 或 "1.2.3.4"，都取主机名部分
        override = override.split("//")[-1].split("/")[0].split(":")[0]
        if override:
            return [override]

    ordered: list[str] = []

    # 出站路由选择的源地址 = 内核认定的真实网卡。这是最可靠的判据。
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("192.0.2.1", 1))     # TEST-NET-1，不会真的发包
            addr = sock.getsockname()[0]
            if addr and not addr.startswith("127."):
                ordered.append(addr)
    except OSError:
        pass

    found: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.add(info[4][0])
    except OSError:
        pass

    rest = [a for a in found if not a.startswith("127.") and a not in ordered]
    # 真实网卡在前，已知虚拟网卡垫底
    rest.sort(key=lambda a: (is_likely_virtual(a), a))
    return ordered + rest


def startup_banner(*, host: str, port: int, lan: list[str], cfg: Config) -> list[str]:
    """启动提示。纯函数，便于测试 —— 这段文案里藏着一类真实故障的警告。"""
    lines = [
        "kindlewarden 副屏服务已启动",
        f"  控制台      http://127.0.0.1:{port}/",
    ]

    if host in ("0.0.0.0", "::", ""):
        if lan:
            lines.append(f"  Kindle 端   http://{lan[0]}:{port}/rev")
            for extra in lan[1:]:
                tag = "虚拟网卡，Kindle 连不上" if is_likely_virtual(extra) else "其它网卡"
                lines.append(f"              http://{extra}:{port}/rev   （{tag}）")
            lines.append("  提示：先用 Kindle 自带浏览器打开上面的地址做联网预检，再越狱。")
            lines.append("        若 Kindle 连不上，跑 `warden doctor` 定位。")
            lines.append('        多半是防火墙拦了入站： New-NetFirewallRule '
                         '-DisplayName "kindlewarden" -Direction Inbound '
                         f"-Protocol TCP -LocalPort {port} "
                         "-Action Allow -Profile Private")
        else:
            lines.append("  ⚠ 没找到局域网 IPv4 —— 这台机器可能没连上网络，Kindle 将无法访问")
    else:
        # 这个分支值得显式警告：只绑回环时本机测试一切正常，但设备永远连不上，
        # 而且报错现象是「页面无法显示」，很难联想到是监听地址的问题。
        lines.append(f"  ⚠ 只监听 {host} —— 局域网上的其它设备（包括 Kindle）连不上")
        lines.append("     要用 0.0.0.0 启动： warden serve --host 0.0.0.0")

    lines.append(f"  事件日志    {cfg.state_path}")
    lines.append(f"  配置        {cfg.config_path}")
    return lines


class RobustHTTPServer(ThreadingHTTPServer):
    """一个连接出问题**绝不影响**整个服务的 HTTP 服务器。

    为什么要自定义：``socketserver`` 默认的 ``handle_error`` 会把 traceback
    打到 stderr。对一个常驻服务来说，一次客户端断连就能刷满日志，
    而且实测那次直接导致进程 exit 1 —— 服务彻底停摆，Kindle 再也拉不到画面。

    这个类是"7×24 常驻"这个定位的一部分：
    **单个客户端的任何行为都不该让服务停下。**
    """

    daemon_threads = True

    # ⚠️ **不要设 ``allow_reuse_address = True``。**
    #
    # 踩过一次，而且后果很隐蔽：在 Windows 上 ``SO_REUSEADDR`` 的语义是
    # **允许两个 socket 绑同一个端口**（Linux 只允许绑 TIME_WAIT 状态的），
    # 于是"第二个实例"照样能启动成功，两个服务同时"监听" 8577，
    # 连接被随机分给其中一个。
    #
    # 现象会变成：有时响应正常、有时数据是旧的、有时状态不一致 ——
    # 极难排查。而且它让"端口占用"这个本该立刻暴露的错误**变成静默**。
    #
    # 保持默认的 False：第二个实例会拿到"端口已被占用"，
    # 由 ``serve()`` 给出明确的提示（见那里的处理）。
    allow_reuse_address = False

    def handle_error(self, request, client_address) -> None:
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError,
                            BrokenPipeError, TimeoutError)):
            # 客户端断开：安静地忽略。它只是关掉了页面/拔了网线。
            return
        # 其余是真的 bug，值得记下来 —— 但**不退出**。
        sys.stderr.write(
            f"[kindlewarden] 处理 {client_address} 的请求时出错："
            f"{type(exc).__name__}: {exc}\n"
        )
        sys.stderr.flush()


def build_server(cfg: Config) -> RobustHTTPServer:
    service = BoardService(cfg)
    handler = type("BoundHandler", (Handler,), {"service": service})
    httpd = RobustHTTPServer((cfg.server.host, cfg.server.port), handler)
    return httpd


def serve(cfg: Config | None = None) -> None:
    cfg = cfg or load_config()

    # ⚠️ 端口占用要**明确报错**，不能静默失败。
    #
    # 踩过一次：计划任务启动的实例绑不上端口就退出了，
    # 而屏幕上什么都没有 —— 于是我（和用户）都以为"服务在跑"，
    # 实际跑的是另一个手动启动的实例。
    #
    # 对常驻服务来说，"我起不来"必须说得清清楚楚。
    try:
        httpd = build_server(cfg)
    except OSError as exc:
        if getattr(exc, "errno", None) in (48, 98, 10048) or "10048" in str(exc):
            port = cfg.server.port
            print(f"✗ 端口 {port} 已经被占用 —— 服务没有启动。")
            print()
            print("  很可能已经有一个实例在跑（那就不需要再起一个）。")
            print(f"  先看看它是否正常： http://127.0.0.1:{port}/health")
            print()
            print("  想确认是谁占着：")
            print(f"    netstat -ano | findstr :{port}")
            print("  换一个端口启动：")
            print(f"    warden serve --port {port + 1}")
            raise SystemExit(1) from exc
        raise

    host, port = httpd.server_address[0], httpd.server_address[1]

    for line in startup_banner(host=host, port=port,
                               lan=lan_addresses(), cfg=cfg):
        print(line)
    # 立刻刷出来 —— 常驻服务被重定向到日志时，缓冲会让日志看起来是空的
    sys.stdout.flush()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    except Exception as exc:                       # noqa: BLE001
        # 服务循环自己出错也不能静默退出 —— 那会让副屏悄悄失联。
        print(f"\n✗ 服务循环异常退出：{type(exc).__name__}: {exc}")
        sys.stdout.flush()
        raise
    finally:
        httpd.server_close()
