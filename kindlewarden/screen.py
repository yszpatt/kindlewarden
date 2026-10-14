"""点击区域（tap map）与开屏参数的计算。

**这是 KOReader 插件方案的关键协议。**

看板是服务端渲染的 PNG，插件只负责显示和把点击坐标转发回来。但插件怎么知道
用户点到了哪一条简报？两个办法：

1. 插件用固定坐标猜 —— 版面一改就失效
2. **服务端在渲染时把每张卡片的矩形一起返回** —— 插件完全不需要懂排版

选 2。这跟整个项目"服务端决定排版"的原则一致：插件是哑终端。

坐标用**图像坐标系**（1072×1448），插件按自己的显示尺寸等比换算。
"""

from __future__ import annotations

from . import layout as _layout
from .config import Config
from .layout import ALERT_H, BRIEF_ITEM_H, MARGIN, plan
from .models import Board

# 详情页屏幕的返回按钮矩形（与 render.render_detail 里的一致）
DETAIL_BACK_RECT = [48, 30, 248, 96]


def _canvas_w() -> int:
    """当前排版画布的宽度。

    ⚠️ **必须动态读**，不能用 ``from .layout import CANVAS_W`` 拷一份。

    横屏时 layout 的 CANVAS_W 会变成 1448（宽>高排版），
    而这里如果还拿着导入时那份 1072，算出来的卡片矩形就会**比实际窄**，
    点击区域和画面错开 —— 典型的"点了没反应"。

    （服务端还会把这些矩形按旋转做一次变换，见 BoardService._transform_rect。）
    """
    return _layout.CANVAS_W


def board_taps(board: Board, cfg: Config) -> tuple[list[dict], list[dict]]:
    """算出看板上每条简报 / 待办的点击矩形。

    必须用**和渲染同一套布局计算**（``layout.plan``）—— 否则屏幕上画的位置
    和可点区域会错开，这是这类"服务端出图 + 客户端收点击"方案最经典的 bug。

    返回 ``(briefs, todos)``，每项形如
    ``{"id": ..., "rect": [x0, y0, x1, y1], "title": ...}``。
    """
    lay = plan(
        panels=cfg.panels,
        has_alert=board.alert is not None,
        n_briefs=len(board.briefs),
        n_todos=len(board.todos),
        # ⚠️ 和 render.render_board 必须用**同一个**上限计算
        #    （横屏另有上限）。两边不一致就会出现
        #    "画面画了 5 条、点击区域只认 4 条"，而且不报错。
        max_briefs=_layout.effective_max_briefs(
            cfg.board.max_briefs, cfg.display.landscape_max_briefs),
        max_todos=cfg.board.max_todos,
    )

    briefs: list[dict] = []
    if lay.briefs is not None:
        z = lay.briefs
        for i, brief in enumerate(board.briefs[:z.shown]):
            top = z.y + i * z.item_h
            briefs.append({
                "id": brief.id,
                "rect": [MARGIN, top, MARGIN + (_canvas_w() - 2 * MARGIN),
                         top + z.item_h],
                "title": brief.title,
            })

    todos: list[dict] = []
    if lay.todos is not None:
        z = lay.todos
        for i, todo in enumerate(board.todos[:z.shown]):
            top = z.y + i * z.item_h
            todos.append({
                "id": todo.id,
                "rect": [MARGIN, top, MARGIN + (_canvas_w() - 2 * MARGIN),
                         top + z.item_h],
                "title": todo.text,
                "done": todo.done,
            })

    return briefs, todos


def board_screen_payload(board: Board, cfg: Config, *, rev: str,
                         generated_at: float, health: str = "ok") -> dict:
    """开屏 JSON：插件拿它建点击映射 + 显示状态信息。"""
    briefs, todos = board_taps(board, cfg)
    return {
        "screen": "board",
        "rev": rev,
        "generated_at": generated_at,
        "source_health": health,
        "size": [_canvas_w(), _layout.CANVAS_H],
        "alert": board.alert.to_dict() if board.alert else None,
        "briefs": briefs,
        "todos": todos,
    }


def detail_screen_payload(brief_id: str, taps: list[dict], *, page: int,
                          total: int, rev: str, title: str) -> dict:
    """详情页 JSON。``taps`` 由 :func:`render.render_detail` 直接给出。"""
    return {
        "screen": "detail",
        "rev": rev,
        "brief_id": brief_id,
        "title": title,
        "page": page,
        "pages": total,
        "size": [_canvas_w(), _layout.CANVAS_H],
        "taps": taps,
    }


def hit_test(taps: list[dict], x: float, y: float) -> dict | None:
    """给出图像坐标，返回命中的区域（没有就 None）。

    插件在设备上用，但这条逻辑是纯函数 —— 放在服务端可以单测，
    避免"坐标换算错了"这种只能上机才发现的问题。
    """
    for item in taps:
        x0, y0, x1, y1 = item["rect"]
        if x0 <= x <= x1 and y0 <= y <= y1:
            return item
    return None
