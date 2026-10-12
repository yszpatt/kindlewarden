"""渲染器的结构性测试。

**不做整图快照比对** —— 那会因为 Pillow/字体版本差异而脆弱。
只断言真正重要的不变量：尺寸、确定性、调色板网格、编码格式。
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from kindlewarden.config import Config, PanelsConfig
from kindlewarden.models import Alert, Board, Brief, Todo
from kindlewarden.render import (BLACK, GRAY, LGRAY, RULE, WHITE, FontBook,
                                 encode_png, quantize16, render_board,
                                 render_offline)

# 固定时间戳，让渲染完全确定（2026-10-10 14:32 左右）
TS = 1791609120.0


@pytest.fixture(scope="module")
def fonts() -> FontBook:
    return FontBook()


def _board(*, alert=False, n_briefs=5, n_todos=4) -> Board:
    briefs = [
        Brief(id=f"b{i}", ts=TS - i * 600, title=f"第 {i} 条简报", status="ok",
              summary="摘要内容", project="backend")
        for i in range(n_briefs)
    ]
    todos = [Todo(id=f"t{i}", ts=TS - i * 600, text=f"第 {i} 条待办", done=(i == 1))
             for i in range(n_todos)]
    a = Alert(id="a1", ts=TS, title="允许执行 rm -rf build", detail="已等待 3 分钟") if alert else None
    return Board(briefs=briefs, todos=todos, alert=a,
                 total_briefs=n_briefs, total_todos=n_todos)


def test_canvas_matches_device(fonts):
    img, _ = render_board(_board(), Config(), now=TS, fonts=fonts)
    assert img.size == (1072, 1448)
    assert img.mode == "L"


def test_render_is_deterministic(fonts):
    """同样的输入必须产出同样的像素 —— 否则 rev 机制会失效，屏幕会莫名刷新。"""
    cfg = Config()
    a, _ = render_board(_board(), cfg, now=TS, fonts=fonts)
    b, _ = render_board(_board(), cfg, now=TS, fonts=fonts)
    assert a.tobytes() == b.tobytes()


def test_rendered_palette_lands_on_device_grid():
    """我们选的灰阶必须落在 16 灰阶网格（17 的倍数）上。

    这条是 mockup 验证出来的：三个灰值全在网格上，所以量化版与原始版
    肉眼无差别。（字形边缘的抗锯齿像素本来就会被设备抖动，那是有意为之。）
    """
    for value in (BLACK, GRAY, LGRAY, RULE, WHITE):
        assert value % 17 == 0, f"{value} 不在设备网格上"


def test_quantize_is_idempotent(fonts):
    img, _ = render_board(_board(), Config(), now=TS, fonts=fonts)
    once = quantize16(img)
    twice = quantize16(once)
    assert once.tobytes() == twice.tobytes()


def test_quantize_shrinks_png(fonts):
    img, _ = render_board(_board(), Config(), now=TS, fonts=fonts)
    import io
    raw, quant = io.BytesIO(), io.BytesIO()
    img.save(raw, format="PNG")
    encode_png(img)[:0]  # 确保编码路径可用
    quant.write(encode_png(img))
    assert quant.tell() < raw.tell()


def test_encode_png_signature(fonts):
    img, _ = render_board(_board(), Config(), now=TS, fonts=fonts)
    data = encode_png(img)
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


def test_empty_board_renders_without_error(fonts):
    """空态必须是独立模式 —— 常规布局 + 空列表会看起来像崩溃。"""
    empty = Board()
    img, lay = render_board(empty, Config(), now=TS, fonts=fonts)
    assert img.size == (1072, 1448)
    assert lay.empty
    # 空态不该出现板块标题的空壳：中部区域应当是干净的
    assert img.getpixel((536, 700)) in (WHITE, BLACK, GRAY, LGRAY)


def test_alert_state_changes_output(fonts):
    cfg = Config()
    with_alert, _ = render_board(_board(alert=True), cfg, now=TS, fonts=fonts)
    without, _ = render_board(_board(alert=False), cfg, now=TS, fonts=fonts)
    assert with_alert.tobytes() != without.tobytes()


def test_offline_screen_renders(fonts):
    img = render_offline(Config(), now=TS, fonts=fonts)
    assert img.size == (1072, 1448)
    assert img.mode == "L"


def test_all_panels_disabled_falls_back_to_empty(fonts):
    cfg = Config(panels=PanelsConfig(alerts=False, briefs=False, todos=False))
    img, lay = render_board(_board(alert=True), cfg, now=TS, fonts=fonts)
    assert lay.empty
    assert img.size == (1072, 1448)


def test_long_chinese_text_does_not_crash(fonts):
    """超长中文必须被像素级截断，而不是溢出或抛异常。"""
    board = _board(n_briefs=1, n_todos=1)
    board.briefs[0].title = "修复" * 200
    board.briefs[0].summary = "摘要" * 200
    board.todos[0].text = "待办" * 200
    board.total_briefs, board.total_todos = 1, 1
    img, _ = render_board(board, Config(), now=TS, fonts=fonts)
    assert img.size == (1072, 1448)


def test_board_renders_multiline_summary(fonts):
    """看板必须能渲染**多行 summary**。

    ⚠️ 这是一个让整个 ``/board`` 端点 500 的 bug，实测踩到：

        ValueError: can't measure length of multiline text

    Pillow 的 ``textlength()`` 对含换行的文本直接抛异常。
    而看板渲染简报时要量宽度做截断 —— 一个多行 summary 就能把
    整条链路打挂：**管理页和 Kindle 都取不到数据**。

    修复放在 ``_tw()`` 内部：它在渲染器里被到处调用（截断、右对齐、居中），
    要求每个调用点都记得"先压平"是不现实的，漏一个就炸。

    这个 bug 也说明**"数据层对了"不等于"整条链路对了"** ——
    事件流里存得好好的，渲染时才炸。
    """
    board = _board(n_briefs=1, n_todos=1)
    board.briefs[0].summary = "第一句结论。\n\n- 原因一\n- 原因二\n\n最后一段。"
    board.total_briefs, board.total_todos = 1, 1

    img, _ = render_board(board, Config(), now=TS, fonts=fonts)
    assert img.size == (1072, 1448)

    png = encode_png(img)                     # 量化 + 编码也要能过
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_display_warnings_only_checks_the_first_line():
    """看板的截断提示**只看第一句**，不能量整段。

    summary 支持多行之后，拿整段宽度去比阈值是错的：
    一段写得很长但**首句简洁**的简报，在板上根本不会被截断，
    却会报"会被截断" —— 那是误报，会让用户白改本来没问题的地方。

    看板上 summary 只显示第一句，判断依据就该是那一句。
    （而且量整段还会撞上上面那个 ``ValueError``。）
    """
    # 首句很短，后面很长 —— 板上不会被截断
    b = Brief(id="b1", ts=TS, status="ok", title="短标题",
              summary="短句。\n\n" + "这一段很长很长。" * 50)
    warns = b.display_warnings()
    assert not any("摘要" in w for w in warns), (
        f"误报摘要会被截断：{warns} —— 首句明明很短，板上放得下"
    )

    # 首句本身就超长 —— 应该报
    b2 = Brief(id="b2", ts=TS, status="ok", title="短标题",
               summary="这一句特别特别长" * 20)
    warns2 = b2.display_warnings()
    assert any("首句" in w for w in warns2), (
        f"首句超长却没报截断：{warns2}"
    )


# ---------------------------------------------------------------- 单行保证
#
# ⚠️ 这一组盯住一个**真 bug**（用户报的"待办与最后一条记录的第二行文字重叠"）：
#
#   `_fit` 原来的实现是「先量宽度，没超就直接 return 原串」，
#   而它内部用的 `_tw` 会把换行**压平**之后再量 ——
#   于是"原文含换行、但压平后没超宽"时，`_fit` **原样返回带换行的字符串**。
#
#   调用方把它交给 `d.text`，而 `d.text` 遇到 `\n` 会**画多行** ——
#   文字就溢出了那条固定高度的条目，盖到下一个板块上。
#
#   触发条件一点不罕见：简报的 summary 本来就**支持多行**（保留段落），
#   首行短、后面跟几行列表时就会命中。


def test_fit_never_returns_multiline_text(fonts):
    """``_fit`` 的输出**必须永远是单行**。

    这是上面那个重叠 bug 的防线。判据直接查输出里有没有换行符 ——
    只要有，调用方画出来就是多行，必然溢出固定高度的条目。
    """
    from kindlewarden.render import TEXT_W, _fit

    img = Image.new("L", (10, 10), WHITE)
    d = ImageDraw.Draw(img)
    face = fonts.get(32)

    cases = [
        ("首行极短 + 长后续", "短\n" + "很长" * 200),
        ("首行短 + 列表", "kindlewarden · 好了\n- 一\n- 二\n- 三"),
        ("纯列表", "- 第一点\n- 第二点\n- 第三点"),
        ("首行刚好不超", "一二三四五\n六七八九十"),
        ("CRLF 换行", "第一行\r\n第二行\r\n第三行"),
        ("结尾换行", "内容\n"),
        ("多个空行", "第一段\n\n\n第二段"),
        ("全角空格行", "　\n　\n内容"),
    ]
    for name, text in cases:
        for max_w in (TEXT_W, 200, 50, 1):
            out = _fit(d, text, face, max_w)
            assert "\n" not in out and "\r" not in out, (
                f"{name}（max_w={max_w}）返回了多行文本：{out!r} —— "
                f"调用方拿它去 d.text 会画多行，溢出条目"
            )


def test_fit_output_always_fits_the_width(fonts):
    """顺带保证截断之后确实没超宽（压平不能破坏这条）。"""
    from kindlewarden.render import _fit, _tw

    img = Image.new("L", (10, 10), WHITE)
    d = ImageDraw.Draw(img)
    face = fonts.get(32)

    for text in ("短\n很长" * 50, "- 一\n- 二\n- 三", "无换行但很长" * 60):
        for max_w in (900, 400, 120):
            out = _fit(d, text, face, max_w)
            assert _tw(d, out, face) <= max_w, (
                f"{out!r} 宽 {_tw(d, out, face)} 超过 {max_w}"
            )


def test_brief_meta_line_stays_inside_its_item(fonts):
    """简报的元信息行**不许画出条目范围**（这正是用户看到的那个重叠）。

    用一个"首行短、后面是多行列表"的摘要 —— 那是触发旧 bug 的数据形状。
    """
    from kindlewarden import layout, render
    from kindlewarden.layout import BRIEF_TWO_LINE_MIN_H

    board = Board(
        briefs=[Brief(id="b1", ts=TS, status="ok", title="标题",
                      summary="短。\n\n- 要点一\n- 要点二\n- 要点三",
                      project="proj")],
        todos=[Todo(id="t1", ts=TS, text="待办")],
        total_briefs=1, total_todos=1)

    render.set_canvas(layout.DEVICE_W, layout.DEVICE_H)
    try:
        img, lay = render_board(board, Config(), now=TS, fonts=fonts)
    finally:
        render.reset_canvas()

    assert lay.briefs is not None and lay.todos is not None
    z = lay.briefs
    item_h = z.item_h
    assert item_h >= BRIEF_TWO_LINE_MIN_H, "竖屏应该是两行模式"

    # 像素检查：条目底边到待办标题之间必须是空白
    px = img.load()
    y0 = z.y + item_h          # 条目底
    y1 = lay.todos.label_y     # 待办标题顶
    for y in range(y0 - 4, y1):
        ink = sum(1 for x in range(layout.MARGIN,
                                   layout.CANVAS_W - layout.MARGIN, 3)
                  if px[x, y] < 240)
        assert ink <= 2, (
            f"简报条目底 {y0} 之后、待办标题 {y1} 之前的第 {y} 行有墨迹"
            f"（{ink} 个像素）—— 文字溢出了条目，会盖住待办"
        )


def test_todo_text_stays_inside_its_item(fonts):
    """待办文字必须完整落在自己的条目内 —— **两个方向都要**。

    踩过：``_draw_todo`` 的签名里**根本没有条目高度**，几个元素都用写死的
    偏移（文字 y+8、勾选框 y+32、删除线 y+30），那套是按竖屏 64px 调的。
    横屏把待办压到 56px 之后，40 号字的墨迹从 y+21 一直画到 **y+65** ——
    **溢出条目 9px**，最后一条渗进了下面环境区上方的留白里。

    这和「``_fit`` 返回多行导致溢出」是同一类问题：
    **画在哪必须由布局算出来的高度决定，不能在绘制里另写一套。**

    判据：待办区底边之下、环境区之上，不许有任何墨迹。
    """
    from kindlewarden import layout, render

    board = Board(
        briefs=[Brief(id="b1", ts=TS, status="ok", title="标题",
                      summary="摘要", project="p")],
        # 用满几条，让"最后一条溢出"有机会暴露
        todos=[Todo(id=f"t{i}", ts=TS, text=f"待办事项 {i} 的内容",
                    done=(i == 0), done_ts=TS if i == 0 else 0)
               for i in range(4)],
        total_briefs=1, total_todos=4)

    for w, h, name in ((layout.DEVICE_W, layout.DEVICE_H, "竖屏"),
                       (layout.DEVICE_H, layout.DEVICE_W, "横屏")):
        render.set_canvas(w, h)
        try:
            img, lay = render_board(board, Config(), now=TS, fonts=fonts)
        finally:
            render.reset_canvas()

        z = lay.todos
        assert z is not None, f"{name}: 没有待办区"
        zone_bottom = z.y + z.h
        limit = (lay.ambient_y if lay.ambient_y is not None
                 else layout.CANVAS_H - layout.MARGIN)

        # ⚠️ 只看条目底边**紧邻的一小段**，不要把整段空白都扫进来 ——
        #    内容区终止线（content_rule_y）是**故意**画在留白里的装饰线，
        #    它离底边远，但扫太宽就会把它误判成溢出。
        #    溢出的特征是"紧贴底边往下渗"，所以窗口取 16px 足够
        #    （横屏那个 bug 溢出 9px，竖屏 1px）。
        window_end = min(zone_bottom + 16, limit)
        px = img.load()
        offenders = []
        for y in range(zone_bottom, window_end):
            if lay.content_rule_y is not None and y == lay.content_rule_y:
                continue                      # 装饰线，不是溢出
            ink = sum(1 for x in range(layout.MARGIN,
                                       layout.CANVAS_W - layout.MARGIN, 3)
                      if px[x, y] < 240)
            if ink > 2:
                offenders.append(y)

        assert not offenders, (
            f"{name}（条目高 {z.item_h}）：待办区底边 {zone_bottom} 紧下方"
            f"出现了墨迹 {offenders} —— 文字溢出了条目，"
            f"会渗进下面的留白/环境区"
        )


def test_ambient_content_is_vertically_centred(fonts):
    """环境区的日期/时钟必须在该区里**垂直居中**（两个方向 × 有时钟/无时钟）。

    踩过：原来用写死的偏移（按竖屏 132px 调的），横屏压到 74px 后
    日期会画到分区外面去。根子是"偏移量"和"分区高度"各写各的 ——
    改一个就错位。

    判据：分区内墨迹的上留白与下留白之差不超过几个像素，
    而且不许溢出分区。
    """
    from kindlewarden import layout, render

    board = Board(briefs=[Brief(id="b1", ts=TS, status="ok", title="t",
                                summary="s")],
                  todos=[Todo(id="t1", ts=TS, text="待办")],
                  total_briefs=1, total_todos=1)

    for w, h, name in ((layout.DEVICE_W, layout.DEVICE_H, "竖屏"),
                       (layout.DEVICE_H, layout.DEVICE_W, "横屏")):
        for show_clock in (False, True):
            render.set_canvas(w, h)
            try:
                cfg = Config()
                cfg.ambient.show_clock = show_clock
                cfg.ambient.weather = "晴 22°C"
                img, lay = render_board(board, cfg, now=TS, fonts=fonts)
                amb_h = layout.metrics()["ambient_h"]
            finally:
                render.reset_canvas()

            assert lay.ambient_y is not None
            px = img.load()
            rows = [y for y in range(lay.ambient_y, lay.ambient_y + amb_h)
                    if sum(1 for x in range(layout.MARGIN,
                                            layout.CANVAS_W - layout.MARGIN, 3)
                           if px[x, y] < 240) > 2]

            assert rows, f"{name} clock={show_clock}: 环境区里没有墨迹"
            pad_top = min(rows) - lay.ambient_y
            pad_bot = (lay.ambient_y + amb_h) - max(rows)

            assert max(rows) < lay.ambient_y + amb_h, (
                f"{name} clock={show_clock}: 环境区内容溢出分区"
                f"（区 {lay.ambient_y}~{lay.ambient_y + amb_h}，"
                f"墨迹到 {max(rows)}）"
            )
            assert abs(pad_top - pad_bot) <= 8, (
                f"{name} clock={show_clock}: 环境区内容没居中 —— "
                f"上留白 {pad_top}px、下留白 {pad_bot}px（区高 {amb_h}）"
            )
