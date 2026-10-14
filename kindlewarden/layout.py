"""版面布局引擎（纯函数）。

实现依据：``docs/layout-spec.md``。

三条设计要点
------------
1. **分区锚点稳定**：ambient 与 statusbar 永远贴底。内容欠载时剩余空间在
   弹性区内部吸收为留白，而不是让分区浮动 —— 每次刷新都是全屏重绘，
   位置跳变会让人以为「出事了」。
2. **条数是算出来的，不是配的**：``N = 区域高度 // 单条高度``。
3. **欠载空间回流**：某板块内容不足时，空出的高度流给另一个弹性板块
   （用水位算法分配，见 :func:`waterfill`）。

本模块无 IO、无 Pillow 依赖，因此布局数学可以脱离渲染单独测试。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import PanelsConfig

# ---------------------------------------------------------------- 画布常量
#
# ⚠️ 这些名字是**可重新绑定**的（见 set_canvas）。
#
# Python 在**函数执行时**才解析模块全局名，所以重新赋值 `CANVAS_W` 之后，
# 所有引用它的函数都会自动用新值 —— 不需要把尺寸当参数穿遍几十个调用点。
#
# 为什么敢这么做（线程安全）：渲染全程在 BoardService._lock 里跑，
# 是**串行**的，不存在两个线程同时按不同尺寸渲染的情况。
# 这一点如果不成立，这个做法就是错的。

CANVAS_W, CANVAS_H = 1072, 1448
MARGIN = 40
CONTENT_W = CANVAS_W - 2 * MARGIN

#: 设备的物理画布 —— 无论什么模式，**最终交付给插件的图**都是这个尺寸。
#: 插件里 IMG_W/IMG_H 是硬编码的，服务端必须迁就它。
DEVICE_W, DEVICE_H = 1072, 1448


def set_canvas(w: int, h: int) -> None:
    """把「当前画布」改成 ``w × h``。横屏时按宽>高排版用。

    只影响本模块的全局名；``render.py`` 里那份导入副本要各自调一次
    （见 ``render.set_canvas``）。
    """
    global CANVAS_W, CANVAS_H, CONTENT_W
    CANVAS_W, CANVAS_H = w, h
    CONTENT_W = w - 2 * MARGIN


def reset_canvas() -> None:
    """恢复到设备的物理画布尺寸。"""
    set_canvas(DEVICE_W, DEVICE_H)

GAP = 12                 # 主要分区之间的间距
LABEL_H = 62             # 板块标题（含标题下那条分隔线 + 下方呼吸）
STATUS_H = 72
AMBIENT_H = 132
ALERT_H = 196
BRIEF_ITEM_H = 128
TODO_ITEM_H = 64

#: 板块标题文字距标题行顶部的距离。标题行高 62 = 0 + 34(字) + 8(线) + 20(呼吸)。
LABEL_PAD_TOP = 0
#: 板块标题下那条分隔线相对标题行顶部的位置。
LABEL_RULE_DY = 42

#: 内容区终止线与 ambient 区顶部之间的距离。
#: 独立于 GAP —— 这两个间距的语义不同，不该绑在一起（绑过一次，见 plan() 里的注释）。
CONTENT_RULE_GAP = 30

#: 终止线与最后一条内容之间至少要留出的空隙。留不出来就**不画**这条线
#: （内容已经填满，没有任何留白需要解释）。见 plan()。
MIN_RULE_CLEARANCE = 12

BRIEFS_WEIGHT = 2.0      # briefs:todos 的弹性权重比
TODOS_WEIGHT = 1.0

# ---------------------------------------------------------------- 横屏度量
#
# ⚠️ 为什么横屏要单独一套。
#
# 横屏画布是 1448×**1072** —— 宽而扁，竖向空间比竖屏（1448）少了 376px。
# 沿用竖屏的条目高度（简报 128、环境区 132）会把版面挤满：
# 简报只能显示 3 条，「待办」被顶到中间、离底部很远，
# 而上下两端却挤得没有呼吸（这是实测反馈）。
#
# 所以横屏用一套**更紧凑**的度量：
#   * 简报**只显示一行标题**（横屏很宽，标题一行放得下，不必再占第二行）
#   * 待办条目压到 56px
#   * 环境区压到 112px
#   * 「最近完成」封顶 4 条 —— 紧凑之后本来能塞下 5 条，
#     但留一点余量版面才透气，也让「待办」能贴到底部
#
# 这一套是**唯一真相**：plan() 用它算坐标，渲染器从 Zone.item_h 读回实际值。
# 两边不可能不一致 —— 这是"布局和绘制必须同源"的又一次应用。
LANDSCAPE_BRIEF_ITEM_H = 84
LANDSCAPE_TODO_ITEM_H = 56
#: 环境区（日期/时钟/天气）的高度。
#:
#: ⚠️ 这个值是**量出来的**，不是拍脑袋定的。
#:
#: 实测里面那一行内容的墨迹高度：
#:     时钟 56 号字 → 43px（最高）
#:     日期 38 号字 → 37px
#:     天气 32 号字 → 30px
#: 三者同处一行，取最高 43px，上下各留 ~15px 呼吸 → **73px** 就够。
#:
#: 之前给的是 112px —— 白白多出 41px。用户的原话是
#: 「待办下方的日期显示往下放，现在占用了太多的位置」。
#: 环境区是**贴底**锚定的，所以把它压矮，它的**顶边**就随之下移：
#:     112px → 环境区顶 836，待办最远到 824
#:      74px → 环境区顶 874，待办最远到 862
#: 一块改动同时满足"往下放"和"少占地方"。
LANDSCAPE_AMBIENT_H = 74
LANDSCAPE_ALERT_H = 168

# ⚠️ 这里**曾经**有个 `LANDSCAPE_MAX_BRIEFS = 4`，已经删掉了。
#
# 它同时踩了两个坑，都是实测打回来的：
#
#   1. **无视配置**。`board.max_briefs` 设成 6 或 8，横屏照样只显示 4 条 ——
#      用户在管理页改了个数，设备上毫无反应。
#   2. **浪费空间**。横屏简报是单行条目（84px），弹性区其实放得下 6 条；
#      卡在 4 条会在简报和待办之间留下 ~194px 空白。
#
# 现在横屏竖屏**同一套规则**：能显示几条由
#   配置的 max_briefs  和  实际可用高度
# 一起决定（都在 `plan()` 里）。想少显示就在配置里调小 ——
# 那是唯一该有的旋钮。删掉常量而不是留个 None 占位，
# 是为了让"这里曾经有个隐藏上限"这件事在代码里彻底消失。

#: 简报条目**低于这个高度就只画一行**（横屏 84 < 100，竖屏 128 > 100）。
#:
#: 用"高度"当开关而不是传一个布尔量：高度是**布局已经算好的事实**，
#: 渲染器照着它决定画几行，两边天然一致。传标志位的话，
#: 调用方漏传或传错就会出现"布局按单行留位、渲染画了两行"的重叠。
BRIEF_TWO_LINE_MIN_H = 100


def is_landscape() -> bool:
    """当前画布是不是横屏（宽>高）。

    直接从**画布尺寸**推断，而不是读配置 —— 这样 layout 和 render
    两个模块各自调用都会得到同样的答案，不需要把配置穿进去。
    """
    return CANVAS_W > CANVAS_H


def effective_max_briefs(board_max_briefs: int,
                         landscape_max_briefs: int) -> int:
    """当前方向下，「最近完成」实际能用几条上限。**唯一实现。**

    ⚠️ 必须有这么一个函数，不能让调用方各算各的。

    `plan()` 有两处调用（`render.render_board` 和 `screen.board_taps`），
    如果各自决定"横屏该限几条"，迟早会出现一边算一边不算 ——
    那正是之前踩过的坑：**画面画了 5 条、点击区域只认 4 条**，
    而且不报任何错。

    规则：
      * 竖屏 → 直接用 ``board.max_briefs``
      * 横屏 → 取 ``board.max_briefs`` 与 ``landscape_max_briefs`` 的较小值
      * ``landscape_max_briefs`` 为 0 或负数 → 表示不限，退回 ``board.max_briefs``
    """
    if not is_landscape():
        return board_max_briefs
    if landscape_max_briefs <= 0:
        return board_max_briefs
    return min(board_max_briefs, landscape_max_briefs)


def metrics() -> dict[str, int]:
    """当前方向下各分区/条目的高度。

    ``plan()`` 用它算坐标；渲染器**不**直接用它，而是从 ``Zone`` 读回
    （``Zone.item_h`` / ``Zone.h``）—— 这样渲染永远跟着布局走。

    ⚠️ 这里**不含"显示几条"的规则**。

    曾经横屏在这里额外封了「简报最多 4 条」，结果既**无视配置**
    （`board.max_briefs` 改成 6 也没用），又**浪费空间**
    （单行条目 84px，弹性区其实放得下 6 条，卡 4 条会留下 ~194px 空白）。

    能显示几条只该由两件事决定：
      * 配置的 ``max_briefs`` / ``max_todos``
      * 实际可用高度
    两者都由 `plan()` 处理，横屏竖屏**同一套规则**。
    """
    if is_landscape():
        return {
            "brief_item_h": LANDSCAPE_BRIEF_ITEM_H,
            "todo_item_h": LANDSCAPE_TODO_ITEM_H,
            "ambient_h": LANDSCAPE_AMBIENT_H,
            "alert_h": LANDSCAPE_ALERT_H,
        }
    return {
        "brief_item_h": BRIEF_ITEM_H,
        "todo_item_h": TODO_ITEM_H,
        "ambient_h": AMBIENT_H,
        "alert_h": ALERT_H,
    }


@dataclass(frozen=True)
class Zone:
    """一个已定位的板块。"""

    key: str
    label_y: int
    y: int                    # 内容起始 y
    h: int                    # 分配到的总高度（含标题）
    item_h: int
    capacity: int             # 能放几条
    shown: int                # 实际显示几条
    overflow: int             # 还有几条没显示


@dataclass(frozen=True)
class Layout:
    alert_y: int | None
    briefs: Zone | None
    todos: Zone | None
    ambient_y: int | None
    status_y: int | None
    #: 内容区终止线的 y；**None 表示不画**（内容已填满，无留白可解释）。
    content_rule_y: int | None
    empty: bool

    def zones(self) -> list[Zone]:
        return [z for z in (self.briefs, self.todos) if z is not None]


def fit_box(src_w: int, src_h: int, box_w: int, box_h: int,
            *, allow_upscale: bool = False) -> tuple[int, int]:
    """等比缩放，让内容完整放进 ``box`` 里（CSS 的 ``contain`` 语义）。

    用途是 Kindle 浏览器页：那里**不能靠 CSS 缩放**。
    ``max-height:100%`` 作用在替换元素上、且父容器是百分比高度时，
    老 WebKit 的解析并不可靠 —— 实测就是底部那行「更新于 HH:MM」被切掉，
    而那一行恰恰是判断画面有没有过期的唯一依据。

    所以改成服务端算好确切像素，直接写在 ``<img width= height=>`` 上 ——
    这是 HTML 3.2 就有的属性，没有浏览器会解析错。

    默认**不放大**：把小图拉大只会得到一张模糊的图，不如留白。
    """
    if src_w <= 0 or src_h <= 0 or box_w <= 0 or box_h <= 0:
        return max(1, src_w), max(1, src_h)

    scale = min(box_w / src_w, box_h / src_h)
    if not allow_upscale:
        scale = min(scale, 1.0)
    return max(1, int(round(src_w * scale))), max(1, int(round(src_h * scale)))


def waterfill(total: int, wants: dict[str, int],
              weights: dict[str, float]) -> dict[str, int]:
    """按权重分配 ``total``，但每项不超过它的 ``want``。

    多出来的份额自动流向还没被满足的项。这就是「欠载空间回流」的实现。
    """
    alloc = {k: 0 for k in wants}
    pending = set(wants)

    while pending:
        remaining = total - sum(alloc.values())
        if remaining <= 0:
            break
        wsum = sum(weights.get(k, 1.0) for k in pending)
        if wsum <= 0:
            break

        capped_any = False
        for key in list(pending):
            share = remaining * weights.get(key, 1.0) / wsum
            need = wants[key] - alloc[key]
            if share >= need:
                alloc[key] += need
                pending.discard(key)
                capped_any = True

        if not capped_any:
            # 所有剩余项都能分到，按份额分完即可
            for key in pending:
                alloc[key] += int(remaining * weights.get(key, 1.0) / wsum)
            break

    return alloc


def plan_empty(panels: PanelsConfig) -> Layout:
    """空态的版面。

    空态是**独立模式**，没有内容区 —— 复用常规分区会渲染出
    「两个板块标题下面空空如也」，看起来像崩溃而不是空闲。
    但 statusbar 的锚点信息仍然保留，便于调用方查询。
    """
    cursor = CANVAS_H - MARGIN
    status_y = cursor - STATUS_H if panels.statusbar else None
    # 空态没有内容区，终止线无意义 → None（渲染器不会画）
    return Layout(alert_y=None, briefs=None, todos=None, ambient_y=None,
                  status_y=status_y, content_rule_y=None, empty=True)


def plan(*, panels: PanelsConfig, has_alert: bool,
         n_briefs: int, n_todos: int,
         max_briefs: int = 6, max_todos: int = 8) -> Layout:
    """计算整个版面。

    ``n_*`` 是**可用于显示的条数**（已过老化过滤）；实际显示多少由可用高度决定。

    横屏时用一套更紧凑的度量（见 ``metrics()``），并且把「待办」**贴到底部**
    （紧跟环境区之上），而不是继续紧挨着「最近完成」——
    实测反馈是"待办应该更靠下"，这样两块内容之间的留白就成了天然的区隔。
    """
    m = metrics()
    alert_h = m["alert_h"]
    ambient_h = m["ambient_h"]

    # ---- 自底向上锚定固定分区
    cursor = CANVAS_H - MARGIN
    status_y: int | None = None
    ambient_y: int | None = None

    if panels.statusbar:
        status_y = cursor - STATUS_H
        cursor = status_y - GAP
    if panels.ambient:
        ambient_y = cursor - ambient_h
        cursor = ambient_y - GAP
    elastic_bottom = cursor

    # ---- 自顶向下
    top = MARGIN
    alert_y: int | None = None
    if has_alert and panels.alerts:
        alert_y = top
        top = top + alert_h + GAP
    elastic_top = top

    # ---- 弹性区
    # 只放**有内容**的板块：空板块如果仍然画标题，会在屏幕上留下一大片
    # 「标题 + 空白」，看起来像渲染失败而不是「暂时没有」。
    keys: list[str] = []
    if panels.briefs and n_briefs > 0:
        keys.append("briefs")
    if panels.todos and n_todos > 0:
        keys.append("todos")

    if not keys:
        # 没有内容板块 → 不画终止线（无事可"终止"，画了只是一个孤零零的横杠）
        return Layout(alert_y, None, None, ambient_y, status_y, None, empty=True)

    item_h = {"briefs": m["brief_item_h"], "todos": m["todo_item_h"]}
    weights = {"briefs": BRIEFS_WEIGHT, "todos": TODOS_WEIGHT}
    counts = {"briefs": n_briefs, "todos": n_todos}
    # 「显示几条」只由**配置**决定 —— 横屏竖屏同一套规则。
    # 这里曾经对横屏额外封了 4 条，既无视配置又浪费空间（见 metrics() 的说明）。
    caps = {"briefs": max_briefs, "todos": max_todos}

    # ---- 横屏：待办**贴底**
    #
    # 竖屏是「简报跟待办、一起在弹性区里垂直居中」——
    # 因为竖屏高，居中的留白读起来是刻意的。
    # 横屏扁，居中的结果就是「待办悬在中间、离底部很远」，
    # 而用户明确要求待办靠下。所以横屏先给待办**预留**它要的高度，
    # 简报拿剩下的 —— 待办就被压到底部了。
    pinned: dict[str, int] = {}
    if is_landscape() and len(keys) == 2:
        # 待办要多少给多少（它条数少、每条也矮），但不吃掉整个弹性区
        want_todos = min(counts["todos"], caps["todos"]) * item_h["todos"]
        avail_total = elastic_bottom - elastic_top
        avail_total -= len(keys) * LABEL_H + (len(keys) - 1) * GAP
        if avail_total < 0:
            avail_total = 0
        # 至少给简报留 2 条的位置，否则简报会被挤没
        floor_briefs = 2 * item_h["briefs"]
        take = min(want_todos, max(0, avail_total - floor_briefs))
        # 对齐到条目高度的整数倍
        take = (take // item_h["todos"]) * item_h["todos"]
        pinned["todos"] = take

    avail = elastic_h = elastic_bottom - elastic_top
    avail -= len(keys) * LABEL_H + (len(keys) - 1) * GAP
    if avail < 0:
        avail = 0

    wants = {k: min(counts[k], caps[k]) * item_h[k] for k in keys}
    if pinned:
        # 待办的高度已经定死，简报拿剩下的
        alloc = waterfill(avail - sum(pinned.values()),
                          {k: v for k, v in wants.items() if k not in pinned},
                          {k: weights[k] for k in keys if k not in pinned})
        alloc.update(pinned)
    else:
        alloc = waterfill(avail, wants, {k: weights[k] for k in keys})

    # 各分区**实际**占用的高度（分配到的可能多于能放下的）
    used: dict[str, int] = {}
    for key in keys:
        capacity = alloc[key] // item_h[key]
        used[key] = min(capacity, min(counts[key], caps[key])) * item_h[key]
    total_used = sum(LABEL_H + used[k] for k in keys) + GAP * (len(keys) - 1)

    # 内容填不满时在弹性区内**垂直居中**。
    #
    # 一开始的规则是「剩余空间一律作为尾部留白」，但实测下来：
    # 一条简报顶在最上面、下面跟着 950px 空白，看起来更像**加载失败**，
    # 而不是「暂时只有这一条」。居中之后同样的内容读起来就是「有意为之」。
    #
    # 底部锚点（ambient / statusbar）不受影响 —— 它们永远是贴底的。
    #
    # ⚠️ 横屏的「待办贴底」是**另一回事**：
    #    那里不是把整块居中，而是让 todos 这一块**吃掉多余的空白**，
    #    使它落到紧贴环境区之上。做法是把 slack 加到 todos **前面**的间隙里。
    slack = elastic_h - total_used
    if slack < 0:
        slack = 0

    y = elastic_top
    if pinned and slack:
        # 横屏：全部空白塞在 briefs 和 todos 之间 → todos 被压到底部
        pass
    elif slack:
        y = elastic_top + slack // 2

    zones: dict[str, Zone] = {}
    for i, key in enumerate(keys):
        cap = alloc[key] // item_h[key]
        want = min(counts[key], caps[key])
        shown = min(cap, want)
        zones[key] = Zone(
            key=key,
            label_y=y,
            y=y + LABEL_H,
            h=used[key],
            item_h=item_h[key],
            capacity=cap,
            shown=shown,
            overflow=max(0, counts[key] - shown),
        )
        # 横屏：把 slack 全部加在**简报之后**，于是待办落到最下面
        extra = slack if (pinned and i == 0) else 0
        y += LABEL_H + used[key] + extra + (GAP if i < len(keys) - 1 else 0)

    # 内容区终止线：让欠载时的留白读起来是「有意为之」，而不是「漏了东西」。
    #
    # ⚠ 两个坑，都踩过，都记在这里：
    #
    # 1. 这条线曾经写成 `ambient_y - GAP + 8` —— 它偷偷依赖 GAP。
    #    把 GAP 从 24 调到 12 之后，线与 ambient 之间只剩 4px，贴到了一起。
    #    现在用**独立常量** CONTENT_RULE_GAP 表达这个间距。
    #
    # 2. 内容**塞满**弹性区时，线会落到最后一条内容之上，像一条删除线横穿末行
    #    （alert=True + 内容满时实测差 -4px）。
    #    所以线和内容取不重叠的位置：留白不够就**不画** ——
    #    内容已经贴到 ambient 了，本来就没有"留白"需要解释。
    content_rule_y: int | None = None
    if ambient_y is not None:
        content_bottom = y - (GAP if keys else 0)   # 最后一条内容的底边
        proposed = ambient_y - CONTENT_RULE_GAP
        if proposed >= content_bottom + MIN_RULE_CLEARANCE:
            content_rule_y = proposed

    return Layout(
        alert_y=alert_y,
        briefs=zones.get("briefs"),
        todos=zones.get("todos"),
        ambient_y=ambient_y,
        status_y=status_y,
        content_rule_y=content_rule_y,
        empty=False,
    )
