"""KOReader 插件方案的服务端契约测试。

插件（Lua）在设备上跑，没法在这里测。但**它依赖的服务端契约可以测**，
而且这正是最容易出错的地方：

  - 点击区域必须和渲染出来的画面**严格对齐**（错开几个像素就是"点不中"）
  - 详情页的分页与返回/翻页区域必须自洽
  - 坐标必须是图像坐标系（1072×1448），插件按缩放比换算

把契约冻在这里，插件那边就只需要关心"拿到矩形、换算坐标、派发"。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from kindlewarden.config import Config
from kindlewarden.layout import CANVAS_H, CANVAS_W, plan
from kindlewarden.models import Alert, Board, Brief, Todo
from kindlewarden.render import render_board, render_detail
from kindlewarden.screen import board_taps, hit_test
from kindlewarden.server import build_server

TS = 1791609120.0


@pytest.fixture
def live(tmp_path):
    cfg = Config()
    cfg.data_dir = tmp_path
    cfg.source_path = tmp_path / "panels.toml"
    cfg.server.host = "127.0.0.1"
    cfg.server.port = 0
    httpd = build_server(cfg)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", cfg
    httpd.shutdown()
    httpd.server_close()


def _get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as resp:
        raw = resp.read()
        if "json" in resp.headers.get("Content-Type", ""):
            return json.loads(raw.decode("utf-8"))
        return raw


def _post(base, path, payload):
    req = urllib.request.Request(
        base + path, method="POST",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"})
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _board(n_briefs=3, n_todos=2, alert=False) -> Board:
    briefs = [Brief(id=f"b{i}", ts=TS - i * 60, title=f"第 {i} 条简报",
                    status="ok", summary="摘要", project="proj")
              for i in range(n_briefs)]
    todos = [Todo(id=f"t{i}", ts=TS - i * 60, text=f"第 {i} 条待办")
             for i in range(n_todos)]
    a = Alert(id="a1", ts=TS, title="需要确认", detail="") if alert else None
    return Board(briefs=briefs, todos=todos, alert=a,
                 total_briefs=len(briefs), total_todos=len(todos))


# ---------------------------------------------------------------- 坐标契约


def test_tap_rects_match_the_rendered_layout():
    """点击矩形必须和渲染用的布局**完全同源**。

    这是这类「服务端出图 + 客户端收点击」方案最经典的 bug：画面和可点区域
    各算一遍，结果错开几像素到几十像素，表现为"点了没反应"或"点错条目"。
    两边都走 ``layout.plan`` 就不会。
    """
    cfg = Config()
    board = _board(n_briefs=4, n_todos=3, alert=True)

    _img, lay = render_board(board, cfg, now=TS)
    briefs, todos = board_taps(board, cfg)

    assert len(briefs) == lay.briefs.shown
    assert len(todos) == lay.todos.shown

    for i, item in enumerate(briefs):
        expected_top = lay.briefs.y + i * lay.briefs.item_h
        assert item["rect"][1] == expected_top
        assert item["rect"][3] == expected_top + lay.briefs.item_h

    for i, item in enumerate(todos):
        expected_top = lay.todos.y + i * lay.todos.item_h
        assert item["rect"][1] == expected_top


def test_tap_coordinates_are_within_the_canvas():
    cfg = Config()
    briefs, todos = board_taps(_board(n_briefs=5, n_todos=4, alert=True), cfg)
    for item in briefs + todos:
        x0, y0, x1, y1 = item["rect"]
        assert 0 <= x0 < x1 <= CANVAS_W
        assert 0 <= y0 < y1 <= CANVAS_H


def test_only_visible_items_get_tap_rects():
    """超出屏幕容量的简报不该有点击区域 —— 点了会打不开。

    render_board 会在放不下时截断，board_taps 必须同样截断。
    """
    cfg = Config()
    board = _board(n_briefs=30, n_todos=30)
    briefs, todos = board_taps(board, cfg)
    _img, lay = render_board(board, cfg, now=TS)
    assert len(briefs) == lay.briefs.shown < 30
    assert len(todos) == lay.todos.shown < 30


def test_hit_test_picks_the_right_item():
    cfg = Config()
    board = _board(n_briefs=3, n_todos=2)
    briefs, _todos = board_taps(board, cfg)

    for item in briefs:
        x0, y0, x1, y1 = item["rect"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        hit = hit_test(briefs, cx, cy)
        assert hit is not None and hit["id"] == item["id"]


def test_hit_test_returns_none_on_empty_space():
    cfg = Config()
    briefs, _ = board_taps(_board(n_briefs=1, n_todos=0), cfg)
    # 画布最右下角不该命中任何卡片（内容都在上半部分）
    assert hit_test(briefs, CANVAS_W - 2, CANVAS_H - 2) is None


# ---------------------------------------------------------------- 详情页契约


def test_detail_has_back_region_on_every_page():
    cfg = Config()
    brief = Brief(id="b1", ts=TS, title="标题", summary="很长的摘要" * 60)
    for page in (0, 1, 2):
        _img, taps, total, _ez = render_detail(brief, cfg, now=TS, page=page)
        assert any(t["kind"] == "back" for t in taps), f"第 {page} 页没有返回区域"
        assert total >= 1


def test_detail_paginates_long_content_and_exposes_page_regions():
    """内容长到一页放不下时要分页，并且给出翻页区域。

    分页而不是滚动：e-ink 上翻页比拖动自然，而且插件只需要把点击坐标
    映射到服务端给的矩形上 —— 服务端说"这块是下一页"。
    """
    cfg = Config()
    brief = Brief(id="b1", ts=TS, title="长内容", summary="这是一段很长的摘要。" * 80)
    _img, taps, total, _ez = render_detail(brief, cfg, now=TS, page=0)
    assert total > 1, "这么长的内容应该分页"
    assert any(t["kind"] == "page" for t in taps), "第一页应该有下一页区域"

    # 最后一页只有"上一页"，没有"下一页"
    _img, last_taps, last_total, _ez = render_detail(brief, cfg, now=TS, page=total - 1)
    assert last_total == total
    kinds = [t["kind"] for t in last_taps]
    assert "back" in kinds
    assert "page" in kinds
    pages = [t.get("page") for t in last_taps if t["kind"] == "page"]
    # ⚠️ taps 里的 page 是 **1 基准**（和 URL 参数同一个基准）
    assert pages == [total - 1], (
        f"最后一页（第 {total} 页）只能往回翻到第 {total - 1} 页，实际给了 {pages}"
    )


def test_page_numbers_use_one_based_index_matching_the_url():
    """``taps[].page`` 必须是 **1 基准**，和 URL 的 ``?page=N`` 一致。

    ⚠️ 这是一个让"翻页完全没反应"的 bug，实测踩到：

    服务端原来给的是 **0 基准**（``page - 1`` / ``page + 1``），
    而插件的 ``self.page`` 是 **1 基准**（URL 里 ``page=1`` 表示第一页）。
    于是插件把 0 基准的值当 1 基准发回去：

        服务端返回 {"page": 1}   ← 0 基准，意思是"第 2 页"
        插件 self.page = 1，请求 ?page=1
        服务端按 1 基准解析      → 又是第 1 页

    **点「下一页」永远停在第一页。** 用户报的就是"翻页不成功"。

    这条测试盯住两边基准一致：第 1 页给出的"下一页"必须是 2。
    """
    cfg = Config()
    brief = Brief(id="b1", ts=TS, title="长内容",
                  summary="这是一段很长的摘要。" * 80)
    _img, taps, total, _ez = render_detail(brief, cfg, now=TS, page=0)
    assert total > 1, "内容不够长，测不出翻页"

    page_taps = {t["page"] for t in taps if t["kind"] == "page"}
    # 第 1 页（内部 page=0）的"下一页"应该是 2（1 基准）
    assert 2 in page_taps, (
        f"第 1 页给出的翻页目标是 {page_taps} —— 应该是 2（1 基准）。"
        f"如果这里是 1，说明还是 0 基准，插件发回去会原地不动"
    )
    assert 0 not in page_taps, "出现了 0 页码 —— URL 的 page 从 1 开始"


def test_last_page_prev_targets_the_second_to_last():
    """最后一页的「上一页」要指向倒数第二页（1 基准）。

    注：页数取决于字体和行距，所以**不硬编码**要几页 ——
    用足够长的内容保证至少 2 页，然后按实际 total 来断言。
    （第一版写死"至少 3 页"，结果那段文字只渲染出 2 页，测试自己挂了。）
    """
    cfg = Config()
    brief = Brief(id="b1", ts=TS, title="长内容",
                  summary="这是一段很长的摘要内容，用来撑出多页。" * 200)
    _img, _taps, total, _ez = render_detail(brief, cfg, now=TS, page=0)
    assert total >= 2, f"内容不够长（只渲染出 {total} 页），测不出翻页"

    # 内部 page=total-1 就是最后一页（0 基准的入参）
    _img, taps, total2, _ez = render_detail(brief, cfg, now=TS, page=total - 1)
    assert total2 == total

    page_taps = [t["page"] for t in taps if t["kind"] == "page"]
    # 最后一页（1 基准的第 total 页）的「上一页」= total - 1
    assert page_taps == [total - 1], (
        f"最后一页（第 {total} 页）的上一页应该是 {total - 1}，实际 {page_taps}"
    )
    # 最后一页不该有「下一页」
    assert total not in page_taps and (total + 1) not in page_taps, (
        f"最后一页出现了「下一页」目标：{page_taps}"
    )


def test_detail_page_index_is_clamped():
    """页码越界不该崩 —— 插件可能拿到过期的页数。"""
    cfg = Config()
    brief = Brief(id="b1", ts=TS, title="短", summary="短")
    _img, _taps, total, _ez = render_detail(brief, cfg, now=TS, page=999)
    assert total == 1


def test_detail_renders_full_text_not_truncated():
    """详情页存在的意义就是显示**全文** —— 看板是单行截断的。

    所以详情页用的字体必须比看板小、并且折行；如果它也走单行截断，
    这个功能就没有价值了。

    注：详情页一页装得下 20 行左右，所以验证"没被截断"要用足够长的文本 ——
    180 字是能一页放下的，那样测不出问题（第一版就写短了）。
    """
    cfg = Config()
    long_summary = "这是一段很长的摘要在看板上会被截断。" * 120
    brief = Brief(id="b1", ts=TS, title="标题", summary=long_summary)
    _img, _taps, total, _ez = render_detail(brief, cfg, now=TS, page=0)
    assert total >= 2, "长摘要应该占不止一页，说明没有被截断成一行"


def test_detail_preserves_paragraph_breaks():
    """详情页必须**保留换行分段**，不能把整段揉成一行。

    这是踩过的坑：``wrap_px`` 会把 ``\\n`` 拉平成空格（它设计给单行字段用），
    于是简报里精心分的段落在详情页上糊成一整坨 —— 描述一复杂就没法读。

    现在按段渲染：段与段之间留白。验证方式：分段版本应该比"拉平版本"
    **占更多纵向空间**（因为多了段间距）。
    """
    cfg = Config()
    # 同样的文字，一个有分段、一个是连续文本
    parts = [f"第 {i} 条原因：说明这个原因是什么。" for i in range(1, 7)]
    brief_seg = Brief(id="seg", ts=TS, title="分段", summary="\n".join(parts))
    brief_flat = Brief(id="flat", ts=TS, title="拉平", summary="".join(parts))

    img_seg, _t, total_seg, _ez = render_detail(brief_seg, cfg, now=TS, page=0)
    img_flat, _t2, total_flat, _ez = render_detail(brief_flat, cfg, now=TS, page=0)

    def content_bottom(img) -> int:
        """内容最低点（最后一个非白像素的行号）。"""
        px = img.load()
        w, h = img.size
        for y in range(h - 1, -1, -1):
            for x in range(0, w, 4):
                if px[x, y] < 240:
                    return y
        return 0

    bottom_seg = content_bottom(img_seg)
    bottom_flat = content_bottom(img_flat)

    assert bottom_seg > bottom_flat, (
        f"分段版本(底部 {bottom_seg}) 不比拉平版本(底部 {bottom_flat}) 占更多空间 —— "
        "段间留白没生效，简报的分段结构会丢失"
    )


def test_detail_has_no_truncation_warning():
    """详情页**不该**出现"板上会被截断"那类提示。

    那是给**看板**的提示（看板单行截断，所以要知道内容会被切）。
    详情页显示的是全文，根本没有截断这回事 ——
    摆在这儿只会让人以为"这条简报有问题"，纯属噪音。

    ⚠️ 检查前必须**剥掉注释**：详情页那段代码的注释里正好在解释
    "为什么不再画 display_warnings"，不剥的话测的是注释而不是代码。
    （这个坑踩了第四次，所以固定成肌肉记忆：检查"有没有某种写法"，
      先剥注释。）
    """
    import re as _re
    from pathlib import Path as _Path

    src = (_Path(__file__).resolve().parent.parent
           / "kindlewarden" / "render.py").read_text(encoding="utf-8")
    src = _re.sub(r"#[^\n]*", "", src)          # 剥掉 Python 注释

    detail_part = src.split("def render_detail", 1)[1]
    assert "display_warnings" not in detail_part, (
        "详情页渲染里还在用 display_warnings —— 那个提示只对看板有意义"
    )


def test_display_warnings_still_exists_for_other_callers():
    """``display_warnings()`` 本身要留着 —— 管理页还在用它。

    详情页不用了，不代表这个能力该删：管理控制台要据此提醒
    "这条简报到看板上会被截断"，那是**发布时**最有用的反馈。
    """
    from kindlewarden.models import Brief as _Brief

    brief = _Brief(id="x", ts=0.0, status="ok",
                   title="很长" * 40, summary="也很长" * 200)
    assert brief.display_warnings(), (
        "display_warnings 不再报告超长内容了 —— 管理页就失去了"
        "「这条发上去会被截断」的提醒"
    )


# ---------------------------------------------------------------- 按钮不许重叠


def test_exit_button_never_overlaps_page_buttons():
    """「退出」按钮和详情页的翻页按钮**绝不能重叠**。

    这是实测踩到的 UI 缺陷：两个按钮叠在一起，都点不准。

    根因是"两边各算各的位置"：
      * 插件自己算退出按钮（贴屏幕底边、按控件尺寸向上长）
      * 服务端渲染详情页时在右下角画「下一页 ›」

        翻页按钮  y 1366 ~ 1432
        退出按钮  y 1387 ~ 1442     ← 重叠 45px

    修法：**服务端定义一次**（``exit_zone_rect()``），
    通过 ``exit_zone`` 字段发给插件照画。
    底部边距（y ≥ CANVAS_H - MARGIN）成为退出按钮的专属区，
    服务端画任何东西都不许进去。
    """
    from kindlewarden.render import (CANVAS_H, CANVAS_W, detail_footer_rect,
                                     exit_zone_is_clear, exit_zone_rect)

    ez = exit_zone_rect()
    ft = detail_footer_rect()

    for name, r in (("退出区", ez), ("翻页横条", ft)):
        x0, y0, x1, y1 = r
        assert 0 <= x0 < x1 <= CANVAS_W, f"{name} 横向越界: {r}"
        assert 0 <= y0 < y1 <= CANVAS_H, f"{name} 纵向越界: {r}"

    assert exit_zone_is_clear(), (
        f"退出按钮保留区 {ez} 和翻页按钮横条 {ft} 重叠了 —— "
        f"用户会看到两个按钮压在一起，都点不准"
    )


def test_exit_zone_reaches_the_bottom_edge():
    """退出按钮要在**底部边距内**，别悬在中间。

    它是"退出"入口，放在手指最容易够到的右下角才合理；
    而且那里是画布上唯一一块服务端保证不画东西的区域。
    """
    from kindlewarden.render import (CANVAS_H, CANVAS_W, EXIT_ZONE_MARGIN,
                                     exit_zone_rect)

    x0, y0, x1, y1 = exit_zone_rect()
    assert x1 == CANVAS_W - EXIT_ZONE_MARGIN, "没贴右边距"
    assert CANVAS_H - y1 <= 8, f"离底边太远（{CANVAS_H - y1}px）"
    assert y0 >= CANVAS_H - EXIT_ZONE_MARGIN, (
        f"按钮顶部 {y0} 侵入了内容区（底部边距从 {CANVAS_H - EXIT_ZONE_MARGIN} 开始）"
    )


def test_detail_png_does_not_paint_in_the_exit_zone():
    """服务端**不许**在退出按钮保留区里画东西。

    否则插件画的按钮会和它叠在一起 —— 这正是原来的 bug。
    验证：把那块像素全查一遍，应该基本是白的
    （允许极少量抗锯齿，放宽到 1%）。
    """
    from kindlewarden.render import exit_zone_rect

    cfg = Config()
    # 长标题 + 长摘要，让内容尽量占满，能暴露"画到保留区"的问题
    brief = Brief(id="b1", ts=TS, status="ok",
                  title="很长的标题" * 8,
                  summary="很长很长的摘要内容。" * 130,
                  project="proj")
    img, _taps, total, _ez = render_detail(brief, cfg, now=TS, page=0)
    assert total > 1, "内容不够长，测不出问题"

    x0, y0, x1, y1 = exit_zone_rect()
    px = img.load()
    ink = 0
    total_px = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            total_px += 1
            if px[x, y] < 240:
                ink += 1
    ratio = ink / total_px if total_px else 0
    assert ratio < 0.01, (
        f"退出按钮保留区里有 {ink} 个非白像素（{ratio * 100:.1f}%）—— "
        f"服务端在那里画了东西，插件画按钮时会重叠"
    )


def test_exit_zone_fully_inside_the_bottom_margin():
    """退出区必须**完整落在底部边距内**（y ∈ [CANVAS_H-MARGIN, CANVAS_H]）。

    这是踩过两次的坑：

    1. 第一版把退出区定成 36px 高、贴到画布底边 ——
       而插件那个带边框的按钮实际 42~49px，
       于是**按钮底部画出画布外**（用户报"下边缘跑到页面外"）
    2. 改成 48px 后装得下按钮了，但**顶边侵入了翻页按钮的横条** ——
       40px 的底部边距根本装不下 48px 的东西

    所以这条测试盯住：退出区既不能超出画布，也不能越出底部边距。
    """
    from kindlewarden.render import (CANVAS_H, CANVAS_W, MARGIN,
                                     exit_zone_rect)

    x0, y0, x1, y1 = exit_zone_rect()

    assert 0 <= x0 < x1 <= CANVAS_W, f"横向越界: {[x0, x1]}"
    assert 0 <= y0 < y1 <= CANVAS_H, f"纵向越界: {[y0, y1]}"

    # 关键：完全在底部边距里
    assert y0 >= CANVAS_H - MARGIN, (
        f"退出区顶边 {y0} 侵入了内容区（底部边距从 {CANVAS_H - MARGIN} 开始）—— "
        f"会挤压正文，也会和翻页按钮重叠"
    )
    # 别贴着物理底边（边框会被看起来像切掉）
    assert CANVAS_H - y1 >= 2, f"离底边太近（{CANVAS_H - y1}px）"


def test_exit_zone_is_tall_enough_for_the_widget():
    """退出区要**装得下插件的按钮控件**。

    ⚠️ 这是一个**跨语言**的约束，本地只能估算：
    插件用 ``FrameContainer`` 包一个 ``TextWidget``，高度 =
        TextWidget（≈ 字号×1.5 + 内边距×2）
      + (bordersize + margin)×2
      + padding×2

    实测踩过：原来用 18 号字 + padding 6 + 边框 2，算出来约 49px，
    **比退出区高 15px** —— 按钮底部直接画出画布外。

    这条测试把两边的参数对上：Lua 里的字号/内边距和这里的估算必须自洽。
    """
    import re
    from pathlib import Path as _Path

    from kindlewarden.render import exit_zone_rect

    lua = (_Path(__file__).resolve().parent.parent / "koplugin"
           / "kindlewarden.koplugin" / "main.lua").read_text(encoding="utf-8")

    # 从 Lua 里抽出退出按钮的实际参数
    m_font = re.search(r'text\s*=\s*_\("退出[^"]*"\),\s*\n\s*face\s*=\s*'
                       r'Font:getFace\("cfont",\s*(\d+)\)', lua)
    assert m_font, "找不到退出按钮的字号"
    font_size = int(m_font.group(1))

    m_frame = re.search(r"FrameContainer:new\{(.*?)\}", lua, re.S)
    assert m_frame, "找不到 FrameContainer 参数"
    frame_src = m_frame.group(1)

    def num(name: str, default: int = 0) -> int:
        mm = re.search(rf"{name}\s*=\s*(\d+)", frame_src)
        return int(mm.group(1)) if mm else default

    border = num("bordersize")
    padding = num("padding")

    # TextWidget 的内边距
    m_tw = re.search(r"TextWidget:new\{(.*?)\}", lua, re.S)
    text_pad = num("padding", 3) if m_tw else 3
    if m_tw:
        mm = re.search(r"padding\s*=\s*(\d+)", m_tw.group(1))
        text_pad = int(mm.group(1)) if mm else 3

    # 估算高度（和 _clamp_rect / 服务端保持一致的口径）
    text_h = round(font_size * 1.5) + 2 * text_pad
    widget_h = text_h + border * 2 + padding * 2

    x0, y0, x1, y1 = exit_zone_rect()
    zone_h = y1 - y0

    assert widget_h <= zone_h, (
        f"退出按钮控件约 {widget_h}px（{font_size} 号字 / padding {padding} / "
        f"边框 {border}），而退出区只有 {zone_h}px —— "
        "按钮会画出画布外（这正是「下边缘跑到页面外」的原因）"
    )


def test_lua_has_the_matching_fallback_geometry():
    """Lua 的兜底矩形要和服务端的 ``exit_zone_rect()`` 一致。

    服务端没给 ``exit_zone`` 时（老版本、请求失败）插件用自己的兜底值。
    两者不一致的话，同一块地方会出现两种位置 —— 又变成"各算各的"。
    """
    import re
    from pathlib import Path as _Path

    from kindlewarden.render import CANVAS_H, CANVAS_W, exit_zone_rect

    lua = (_Path(__file__).resolve().parent.parent / "koplugin"
           / "kindlewarden.koplugin" / "main.lua").read_text(encoding="utf-8")
    m = re.search(r"local EXIT_ZONE_FALLBACK = \{([^}]*)\}", lua)
    assert m, "找不到 EXIT_ZONE_FALLBACK"

    parts = [p.strip() for p in m.group(1).split(",")]

    # 允许写成 IMG_W - 192 这种表达式，这里只核对数值关系
    def ev(expr: str, **kw) -> int:
        expr = expr.replace("IMG_W", str(kw["w"])).replace("IMG_H", str(kw["h"]))
        return int(eval(expr, {"__builtins__": {}}, {}))  # noqa: S307

    fb = [ev(p, w=CANVAS_W, h=CANVAS_H) for p in parts]
    ez = exit_zone_rect()

    # 允许小幅偏差（服务端可能微调），但位置必须大体一致
    for i, (a, b) in enumerate(zip(fb, ez)):
        assert abs(a - b) <= 8, (
            f"兜底矩形第 {i} 项 {a} 和服务端的 {b} 差太多 —— "
            f"两条路径画出来的位置会不一样"
        )


def test_detail_json_honours_the_page_parameter(live):
    """``/detail.json`` 必须**按传入的 page 返回对应页的 taps**。

    ⚠️ 这是一个真 bug，而且很隐蔽：那个端点曾经写死 ``svc.detail(brief_id, 0)``。

    后果：
      * PNG 拿到的确实是请求的那一页（``/detail.png`` 的参数是对的）
      * 但 **taps 永远是第 1 页的**

    于是第 2 页上显示的按钮位置来自第 1 页，更糟的是：
    第 1 页只有「下一页」，所以**第 2 页的「上一页」根本点不出来**。

    我修「下一页」时只测了下一页，没顺手测上一页，所以漏了它。
    **按钮类的 bug 要一次测全部按钮。**
    """
    base, _cfg = live
    brief_id = _post(base, "/brief", {
        "title": "分页参数测试",
        "summary": "很长的内容。" * 300})["brief_id"]

    first = _get(base, f"/detail.json?id={brief_id}&page=1&c=kindle")
    total = first["pages"]
    assert total >= 3, f"内容不够长（只 {total} 页），测不出翻页差异"

    # 逐页取 json，确认报告的页码和请求一致
    for page in range(1, total + 1):
        info = _get(base, f"/detail.json?id={brief_id}&page={page}&c=kindle")
        assert info["page"] == page, (
            f"请求第 {page} 页，json 却自称第 {info['page']} 页 —— "
            f"说明 page 参数被忽略了"
        )

    # 更实质的检查：第 2 页必须能往回翻
    second = _get(base, f"/detail.json?id={brief_id}&page=2&c=kindle")
    targets = [t["page"] for t in second["taps"] if t["kind"] == "page"]
    assert 1 in targets, (
        f"第 2 页的按钮目标是 {targets} —— 缺「上一页 → 1」。"
        f"这正是 page 参数被忽略时的症状"
    )
    assert 3 in targets, f"第 2 页缺「下一页 → 3」，实际 {targets}"


def test_every_button_on_every_page_has_a_valid_target(live):
    """**每一页的每一个按钮**，目标都要合法。

    这是"修一个漏一个"的教训：修「下一页」时只测了下一页，
    结果漏掉「上一页」（``/detail.json`` 忽略 page 参数）。
    所以这里**一次遍历所有页、所有按钮**。

    合法性规则：
      * 第 1 页：不能有「上一页」，必须有「下一页 → 2」
      * 中间页：上一页 → N-1，下一页 → N+1
      * 最后一页：上一页 → N-1，不能有「下一页」
      * 每页都要有「back」
    """
    base, _cfg = live
    brief_id = _post(base, "/brief", {
        "title": "全按钮遍历",
        "summary": "很长的内容。" * 300})["brief_id"]

    info1 = _get(base, f"/detail.json?id={brief_id}&page=1&c=kindle")
    total = info1["pages"]
    assert total >= 3, f"内容不够长（只 {total} 页）"

    for page in range(1, total + 1):
        info = _get(base, f"/detail.json?id={brief_id}&page={page}&c=kindle")
        kinds = [t["kind"] for t in info["taps"]]
        targets = [t["page"] for t in info["taps"] if t["kind"] == "page"]

        assert "back" in kinds, f"第 {page} 页没有返回按钮"

        if page == 1:
            assert not any(t < page for t in targets), \
                f"第 1 页出现了「上一页」：{targets}"
            assert (page + 1) in targets, \
                f"第 1 页缺「下一页 → {page + 1}」，实际 {targets}"
        elif page < total:
            assert (page - 1) in targets, \
                f"第 {page} 页缺「上一页 → {page - 1}」，实际 {targets}"
            assert (page + 1) in targets, \
                f"第 {page} 页缺「下一页 → {page + 1}」，实际 {targets}"
        else:
            assert (page - 1) in targets, \
                f"最后一页缺「上一页 → {page - 1}」，实际 {targets}"
            assert not any(t > page for t in targets), \
                f"最后一页出现了「下一页」：{targets}"


def test_button_labels_are_vertically_centred(live):
    """按钮里的文字必须**垂直居中**。

    踩过一次：三个按钮（上一页/下一页/返回）都用写死的 ``rect_top + 16``
    画文字，而框高 64~66px —— 上方留白 27px、下方只有 8px，
    **文字明显偏下**（用户报的"文字靠下了一点"）。

    这里用像素分析验证：把按钮矩形那块抠出来，
    比较文字墨迹的**上方留白**和**下方留白**，差值不该超过几像素。
    """
    import io

    from PIL import Image

    base, _cfg = live
    brief_id = _post(base, "/brief", {
        "title": "居中测试",
        "summary": "很长的内容。" * 300})["brief_id"]

    info = _get(base, f"/detail.json?id={brief_id}&page=2&c=kindle")
    png = _get(base, f"/detail.png?id={brief_id}&page=2&c=kindle")
    img = Image.open(io.BytesIO(png)).convert("L")
    px = img.load()

    checked = 0
    for tap in info["taps"]:
        if tap["kind"] not in ("page", "back"):
            continue
        x0, y0, x1, y1 = tap["rect"]

        # ⚠️ 必须**避开按钮的边框**，否则量到的是边框而不是文字。
        #
        # 踩过一次：直接扫描整个矩形，结果 y0 和 y0+1 那两行是**顶边框**
        # （横跨整条矩形，175+ 个像素），被当成文字墨迹，
        # 于是报"上方留白 4px、下方 16px，没居中" —— **测量方式错了**，
        # 代码其实是居中的。
        #
        # 判据：边框行横跨整条矩形（像素数 > 矩形宽度的 1/3），
        #       文字行只在左边一段（按钮文字靠左）。
        width = x1 - x0
        border_threshold = width // 3

        def row_ink(yy: int) -> int:
            return sum(1 for xx in range(x0 + 4, x1 - 4) if px[xx, yy] < 240)

        text_rows = [
            yy for yy in range(y0, y1 + 1)
            if 0 < row_ink(yy) < border_threshold
        ]
        if not text_rows:
            continue          # 这块没文字（空按钮），跳过

        top_ink, bottom_ink = min(text_rows), max(text_rows)
        above = top_ink - y0
        below = y1 - bottom_ink
        diff = abs(above - below)
        assert diff <= 8, (
            f"{tap['kind']} 按钮的文字没居中：上方留白 {above}px、"
            f"下方 {below}px（差 {diff}px）。"
            f"矩形 {tap['rect']} —— 多半是又用写死的偏移画文字了"
        )
        checked += 1

    assert checked >= 2, f"只检查到 {checked} 个按钮，样本太少"


# ---------------------------------------------------------------- HTTP 契约


def test_screen_json_endpoint_returns_taps(live):
    base, _cfg = live
    for i in range(3):
        _post(base, "/brief", {"title": f"简报 {i}", "status": "ok"})
    info = _get(base, "/screen.json?c=kindle")

    assert info["screen"] == "board"
    assert info["size"] == [CANVAS_W, CANVAS_H]
    assert len(info["briefs"]) == 3
    for item in info["briefs"]:
        assert item["id"] and item["rect"] and item["title"]


def test_detail_endpoints_agree_with_each_other(live):
    """``/detail.json`` 给的页数必须和 ``/detail.png`` 实际渲染的一致。

    不一致的话插件会给出一个点了没反应的"下一页"。
    """
    base, _cfg = live
    brief_id = _post(base, "/brief", {
        "title": "有长摘要的简报",
        "summary": "很长的内容。" * 100})["brief_id"]

    info = _get(base, f"/detail.json?id={brief_id}&c=kindle")
    assert info["screen"] == "detail"
    assert info["brief_id"] == brief_id
    assert info["pages"] >= 1
    assert any(t["kind"] == "back" for t in info["taps"])

    # 每一页都要能真的下到图
    for page in range(1, info["pages"] + 1):
        png = _get(base, f"/detail.png?id={brief_id}&page={page}&c=kindle")
        assert png[:8] == b"\x89PNG\r\n\x1a\n", f"第 {page} 页不是 PNG"


def test_detail_png_rejects_unknown_id(live):
    base, _cfg = live
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base, "/detail.png?id=brief_nope&c=kindle")
    assert exc.value.code == 404


def test_detail_png_page_is_one_based_in_the_url(live):
    """URL 里的 page 从 1 开始（人看地址栏友好），内部是 0 开始。"""
    base, _cfg = live
    brief_id = _post(base, "/brief", {"title": "x", "summary": "y" * 500})["brief_id"]
    info = _get(base, f"/detail.json?id={brief_id}&c=kindle")
    if info["pages"] < 2:
        pytest.skip("内容不够长，没分页")

    first = _get(base, f"/detail.png?id={brief_id}&page=1&c=kindle")
    second = _get(base, f"/detail.png?id={brief_id}&page=2&c=kindle")
    assert first != second, "page=1 和 page=2 应该是不同的画面"


def test_board_payload_also_carries_rects(live):
    """管理页的 /board 也带上 rect —— 以后要在网页上做点击预览就有现成数据。"""
    base, _cfg = live
    _post(base, "/brief", {"title": "带矩形的简报"})
    board = _get(base, "/board")
    assert board["screen"] == "board"
    assert board["briefs"][0]["rect"], "板上可见的简报要有 rect"
    assert board["size"] == [CANVAS_W, CANVAS_H]
