"""布局数学的测试。

这里冻结的是 ``docs/layout-spec.md`` 里的**实测数字**。
如果哪天改了分区高度或权重导致这些断言失败，说明版面确实变了 ——
那就该去更新规格文档，而不是悄悄改测试。
"""

from __future__ import annotations

import pytest

from kindlewarden import layout, render
from kindlewarden.config import PanelsConfig
from kindlewarden.layout import (CANVAS_H, CANVAS_W, GAP, MARGIN, STATUS_H,
                                 fit_box, plan)


def test_fit_box_contains_without_upscaling():
    """按 PW3 的浏览器视口算：宽度够但高度差约 60px（工具栏），所以整体缩一点。"""
    assert fit_box(CANVAS_W, CANVAS_H, 1072, 1388) == (1028, 1388)


def test_fit_box_does_not_upscale_by_default():
    """把小图拉大只会得到一张模糊的图，不如留白。"""
    assert fit_box(100, 100, 1000, 1000) == (100, 100)


def test_fit_box_upscales_when_asked():
    assert fit_box(100, 50, 400, 400, allow_upscale=True) == (400, 200)


def test_fit_box_preserves_aspect_ratio():
    w, h = fit_box(CANVAS_W, CANVAS_H, 800, 2000)
    assert w == 800
    assert abs(h / w - CANVAS_H / CANVAS_W) < 0.01


def test_fit_box_never_exceeds_the_box():
    for box_w, box_h in ((1072, 1388), (800, 600), (2000, 2000), (321, 999)):
        w, h = fit_box(CANVAS_W, CANVAS_H, box_w, box_h)
        assert w <= box_w and h <= box_h, (box_w, box_h, w, h)


def test_fit_box_handles_degenerate_input():
    assert fit_box(0, 0, 100, 100) == (1, 1)
    assert fit_box(CANVAS_W, CANVAS_H, 0, 0) == (CANVAS_W, CANVAS_H)


def test_alert_state_matches_spec():
    """有告警时：4 条简报（溢出 1 条）+ 4 条待办。"""
    lay = plan(panels=PanelsConfig(), has_alert=True, n_briefs=5, n_todos=4)
    assert lay.alert_y == MARGIN
    assert lay.briefs.shown == 4
    assert lay.briefs.overflow == 1
    assert lay.todos.shown == 4
    assert lay.todos.overflow == 0


def test_idle_state_matches_spec():
    """无告警时：5 条简报 + 4 条待办。"""
    lay = plan(panels=PanelsConfig(), has_alert=False, n_briefs=5, n_todos=4)
    assert lay.alert_y is None
    assert lay.briefs.shown == 5
    assert lay.todos.shown == 4
    assert lay.briefs.overflow == 0


def test_anchors_are_stable_regardless_of_content():
    """ambient 与 statusbar 永远贴底。

    这条是硬要求：每次刷新都是全屏重绘，位置跳变会让人以为「出事了」。
    """
    full = plan(panels=PanelsConfig(), has_alert=False, n_briefs=9, n_todos=9)
    bare = plan(panels=PanelsConfig(), has_alert=False, n_briefs=0, n_todos=0)
    assert full.ambient_y == bare.ambient_y
    assert full.status_y == bare.status_y
    assert full.status_y == CANVAS_H - MARGIN - STATUS_H


def test_alert_pushes_elastic_area_but_not_footer():
    """告警条只挤压中间的弹性区，底部锚点不动。"""
    with_alert = plan(panels=PanelsConfig(), has_alert=True, n_briefs=5, n_todos=4)
    without = plan(panels=PanelsConfig(), has_alert=False, n_briefs=5, n_todos=4)
    assert with_alert.briefs.label_y > without.briefs.label_y
    assert with_alert.ambient_y == without.ambient_y


def test_underload_flows_to_other_section():
    """briefs 内容不足时，空出的高度应该流给 todos（欠载回流）。"""
    few_briefs = plan(panels=PanelsConfig(), has_alert=False, n_briefs=1, n_todos=8)
    assert few_briefs.todos.shown == 8


def test_sparse_content_is_vertically_centered():
    """一条简报不该顶在最上面、下面留 950px 空白 —— 那看起来像加载失败。

    实测过：顶对齐 + 巨大尾部留白，比同样的内容居中更像故障。
    """
    sparse = plan(panels=PanelsConfig(), has_alert=False, n_briefs=1, n_todos=0)
    assert sparse.briefs.label_y > MARGIN + 200


def test_full_content_is_effectively_top_aligned():
    """内容填满时居中是个空操作 —— 不能把内容整体推下去。"""
    full = plan(panels=PanelsConfig(), has_alert=False, n_briefs=20, n_todos=20)
    assert full.briefs.label_y < MARGIN + 100


def test_centering_never_pushes_content_off_canvas():
    """居中的位移最多是余量的一半，不可能把内容推出画布。"""
    for n_b in (0, 1, 2, 6, 20):
        for n_t in (0, 1, 3, 8, 20):
            for alert in (False, True):
                lay = plan(panels=PanelsConfig(), has_alert=alert,
                           n_briefs=n_b, n_todos=n_t)
                for z in lay.zones():
                    assert z.label_y >= MARGIN
                    assert z.y + z.shown * z.item_h <= CANVAS_H - MARGIN


def test_empty_section_is_omitted_entirely():
    """回归测试：待办为空时不该还画一个「待办」标题。

    那会渲染出一大片「标题 + 空白」，看起来像渲染失败 ——
    和空态那个 bug 是同一类，只是发生在单个板块而不是整块板。
    """
    lay = plan(panels=PanelsConfig(), has_alert=False, n_briefs=1, n_todos=0)
    assert lay.todos is None
    assert lay.briefs is not None

    lay = plan(panels=PanelsConfig(), has_alert=False, n_briefs=0, n_todos=3)
    assert lay.briefs is None
    assert lay.todos is not None


def test_single_section_reclaims_the_other_ones_space():
    """一个板块为空时，它的「标题 + 间距」高度要回流给另一个板块。

    注意要用**会溢出**的数据量：水位算法按需分配，内容少时本来也占不满，
    容量差异体现不出来。
    """
    kwargs = dict(has_alert=False, max_briefs=12)
    alone = plan(panels=PanelsConfig(), n_briefs=12, n_todos=0, **kwargs)
    both = plan(panels=PanelsConfig(), n_briefs=12, n_todos=4, **kwargs)
    assert alone.briefs.capacity > both.briefs.capacity


def test_zones_are_contiguous_without_a_hole():
    """板块之间的空白不能超过 GAP。

    回归测试：分配时是「先给高度、再折算能放几条」，舍入余量曾经留在
    两个板块**中间**，渲染出来像一个空白洞，看起来像漏了几个条目。
    现在余量统一落到弹性区底部。
    """
    for n_b in range(0, 9):
        for n_t in range(0, 9):
            for alert in (False, True):
                lay = plan(panels=PanelsConfig(), has_alert=alert,
                           n_briefs=n_b, n_todos=n_t)
                if lay.briefs is None or lay.todos is None:
                    continue
                briefs_bottom = lay.briefs.y + lay.briefs.shown * lay.briefs.item_h
                gap = lay.todos.label_y - briefs_bottom
                assert gap <= GAP, (n_b, n_t, alert, gap)


def test_disabling_panel_reclaims_space():
    """关掉一个**有内容**的板块，它占的高度必须回流给弹性区。"""
    kwargs = dict(has_alert=False, n_briefs=20, n_todos=3, max_briefs=20)
    both = plan(panels=PanelsConfig(), **kwargs)
    only_briefs = plan(panels=PanelsConfig(todos=False), **kwargs)
    assert only_briefs.briefs.capacity > both.briefs.capacity


def test_zone_heights_never_leave_canvas():
    """任何内容组合下，最后一行的底边都不能越过画布。"""
    for n_b in (0, 1, 5, 30):
        for n_t in (0, 1, 5, 30):
            for alert in (False, True):
                lay = plan(panels=PanelsConfig(), has_alert=alert,
                           n_briefs=n_b, n_todos=n_t)
                for z in lay.zones():
                    bottom = z.y + z.shown * z.item_h
                    assert bottom <= CANVAS_H - MARGIN, (n_b, n_t, alert, z)
                    assert z.y >= MARGIN


def test_footer_always_at_bottom_even_with_no_panels():
    lay = plan(panels=PanelsConfig(briefs=False, todos=False),
               has_alert=False, n_briefs=3, n_todos=3)
    assert lay.empty
    assert lay.status_y == CANVAS_H - MARGIN - STATUS_H


# ---------------------------------------------------------------- 横屏版面
#
# 横屏画布是 1448×**1072**（宽而扁），竖向空间比竖屏少 376px。
# 沿用竖屏度量会把版面挤满。下面这一组盯住横屏专属的那套紧凑度量。
#
# ⚠️ 每条测试都要在 `render.set_canvas` 的上下文里跑，
#    否则量到的还是竖屏尺寸 —— 这也是横屏功能最容易搞错的地方。


@pytest.fixture
def landscape():
    """把画布切成横屏，结束时**一定**恢复。

    try/finally 不能省：画布是模块级全局变量，
    漏恢复会让后续所有测试都按横屏尺寸跑，症状是"别的测试莫名挂掉"。
    """
    render.set_canvas(layout.DEVICE_H, layout.DEVICE_W)
    try:
        yield
    finally:
        render.reset_canvas()


def test_metrics_differ_by_orientation(landscape):
    """横屏用一套更紧凑的度量。"""
    m = layout.metrics()
    assert m["brief_item_h"] < layout.BRIEF_ITEM_H, "横屏简报条目应该更矮（只一行）"
    assert m["todo_item_h"] < layout.TODO_ITEM_H, "横屏待办应该更紧凑"
    assert m["ambient_h"] < layout.AMBIENT_H, "横屏环境区应该更紧凑"
    assert m["max_briefs"] == layout.LANDSCAPE_MAX_BRIEFS

    # 竖屏时回到原值
    render.reset_canvas()
    p = layout.metrics()
    assert p["brief_item_h"] == layout.BRIEF_ITEM_H
    assert p["todo_item_h"] == layout.TODO_ITEM_H
    assert p["ambient_h"] == layout.AMBIENT_H


def test_landscape_brief_item_is_single_line(landscape):
    """横屏简报条目必须**矮到只画一行**（否则标题+摘要两天撑不下）。

    判据是条目高度低于 ``BRIEF_TWO_LINE_MIN_H`` ——
    渲染器就是按这个阈值决定画一行的（见 ``_draw_brief``）。
    """
    lay = plan(panels=PanelsConfig(), has_alert=False, n_briefs=8, n_todos=3)
    assert lay.briefs is not None
    assert lay.briefs.item_h < layout.BRIEF_TWO_LINE_MIN_H, (
        f"横屏条目高 {lay.briefs.item_h} 不小于 "
        f"{layout.BRIEF_TWO_LINE_MIN_H} —— 渲染器会按两行画，必然溢出"
    )


def test_landscape_shows_four_briefs(landscape):
    """横屏「最近完成」显示 4 条（用户明确要求）。"""
    lay = plan(panels=PanelsConfig(), has_alert=False, n_briefs=8, n_todos=3)
    assert lay.briefs.shown == layout.LANDSCAPE_MAX_BRIEFS == 4
    assert lay.briefs.overflow == 8 - 4


def test_landscape_todos_sit_at_the_bottom(landscape):
    """横屏待办**贴底**：它和环境区之间只隔一个 GAP。

    用户要求"待办更靠下"。竖屏是简报+待办一起在弹性区居中，
    横屏则把多余空白塞在简报之后，让待办落到紧贴环境区之上。
    """
    lay = plan(panels=PanelsConfig(), has_alert=False, n_briefs=8, n_todos=3)
    assert lay.todos is not None and lay.ambient_y is not None

    gap = lay.ambient_y - (lay.todos.y + lay.todos.h)
    assert gap == GAP, (
        f"待办底 {lay.todos.y + lay.todos.h} 到环境区顶 {lay.ambient_y} "
        f"的间隙是 {gap}，应为 {GAP} —— 待办没贴到底部"
    )


def test_landscape_slack_goes_between_briefs_and_todos(landscape):
    """横屏的多余空白应该落在**简报和待办之间**，而不是上下平分。"""
    lay = plan(panels=PanelsConfig(), has_alert=False, n_briefs=8, n_todos=3)
    b, t = lay.briefs, lay.todos

    # 简报**贴顶**（不是被居中推开）
    assert b.label_y == MARGIN, (
        f"简报标题在 {b.label_y}，应该贴顶 {MARGIN} —— "
        f"空白该全部留在简报下面"
    )
    # 简报与待办之间有明显空隙
    between = t.label_y - (b.y + b.h)
    assert between > GAP, (
        f"简报底 {b.y + b.h} 到待办标题 {t.label_y} 只有 {between}px —— "
        f"空白没有集中到中间，待办就没被压到底部"
    )


def test_landscape_zones_never_overlap_in_any_combination(landscape):
    """横扫：横屏下**任何内容组合**的分区都不许重叠。

    用户报过一次重叠（根因是渲染层的 ``_fit`` 返回多行，
    但分区坐标本身也不该重叠 —— 这里把这条底线钉死）。
    """
    for n_b in range(0, 13):
        for n_t in range(0, 11):
            for alert in (False, True):
                lay = plan(panels=PanelsConfig(), has_alert=alert,
                           n_briefs=n_b, n_todos=n_t)
                m = layout.metrics()

                zones = []
                if lay.alert_y is not None:
                    zones.append(("alert", lay.alert_y, lay.alert_y + m["alert_h"]))
                if lay.briefs:
                    z = lay.briefs
                    zones.append(("briefs", z.label_y, z.y + z.h))
                if lay.todos:
                    z = lay.todos
                    zones.append(("todos", z.label_y, z.y + z.h))
                if lay.ambient_y is not None:
                    zones.append(("ambient", lay.ambient_y,
                                  lay.ambient_y + m["ambient_h"]))
                if lay.status_y is not None:
                    zones.append(("status", lay.status_y,
                                  lay.status_y + STATUS_H))

                for i in range(len(zones) - 1):
                    n1, _a1, b1 = zones[i]
                    n2, a2, _b2 = zones[i + 1]
                    assert b1 <= a2, (
                        f"简报{n_b} 待办{n_t} 告警{alert}: "
                        f"{n1} 底 {b1} 与 {n2} 顶 {a2} 重叠 {b1 - a2}px"
                    )
                # 全部落在画布内
                for name, a, b in zones:
                    assert MARGIN <= a and b <= layout.CANVAS_H - MARGIN + 1, (
                        f"简报{n_b} 待办{n_t} 告警{alert}: "
                        f"{name} 的 {a}~{b} 超出画布"
                    )


def test_portrait_layout_is_unchanged_by_the_landscape_work():
    """竖屏版面不该被横屏的改动影响（显示模式可以是关的）。

    这条防的是"为了横屏改了全局常量，结果竖屏也跟着变"。
    """
    lay = plan(panels=PanelsConfig(), has_alert=False, n_briefs=5, n_todos=4)
    assert lay.briefs.item_h == layout.BRIEF_ITEM_H == 128
    assert lay.todos.item_h == layout.TODO_ITEM_H == 64
    # 竖屏仍然是"简报+待办一起居中"
    assert lay.briefs.shown == 5
