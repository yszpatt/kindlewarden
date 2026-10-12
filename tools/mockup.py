#!/usr/bin/env python3
"""
kindlewarden 版面 mockup 渲染器

用途：设计验证。渲染 1072x1448 的副屏画面，检查版面、字号、信息密度。
这不是 P0 的正式渲染器，但布局逻辑按 docs/layout-spec.md 实现，
可以直接演进成正式版本。

用法：
    python tools/mockup.py
输出：
    mockups/out/mockup_alert.png      有告警状态
    mockups/out/mockup_idle.png       无告警状态
    mockups/out/*_16gray.png          量化到 16 灰阶（模拟设备实际观感）
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- 画布常量

W, H = 1072, 1448
MARGIN = 40
CW = W - 2 * MARGIN          # 内容宽 992
TOP = MARGIN
BOT = H - MARGIN             # 1408

GAP = 24                     # 主要分区之间的间距
ALERT_H = 200
AMBIENT_H = 132
STATUS_H = 76
LABEL_H = 46                 # 板块标题（含下方间距）
BRIEF_ITEM_H = 128
TODO_ITEM_H = 60

# 设备是 16 灰阶，量化网格是 255/15 = 17 的倍数。
# 所有灰色都对齐到该网格，保证文字不产生抖动噪点。
BLACK = 0
GRAY = 119                   # 17*7  次级文字
LGRAY = 153                  # 17*9  更弱的文字
RULE = 221                   # 17*13 分隔线
WHITE = 255

FONT_REG, FONT_BOLD = None, None
_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

# ---------------------------------------------------------------- 字体

CANDIDATES = [
    (r"C:\Windows\Fonts\Noto Sans SC (TrueType).otf",
     r"C:\Windows\Fonts\Noto Sans SC Bold (TrueType).otf"),
    (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"),
    (r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simhei.ttf"),
]


def load_fonts() -> None:
    global FONT_REG, FONT_BOLD
    for reg, bold in CANDIDATES:
        if os.path.exists(reg) and os.path.exists(bold):
            FONT_REG, FONT_BOLD = reg, bold
            return
    raise SystemExit("找不到可用的中文字体")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REG
    key = (path, size)
    if key not in _FONT_CACHE:
        _FONT_CACHE[key] = ImageFont.truetype(path, size)
    return _FONT_CACHE[key]


# ---------------------------------------------------------------- 文本工具

def text_w(d: ImageDraw.ImageDraw, s: str, f: ImageFont.FreeTypeFont) -> float:
    return d.textlength(s, font=f)


def fit(d: ImageDraw.ImageDraw, s: str, f: ImageFont.FreeTypeFont, max_w: float) -> str:
    """按像素宽度截断（不能按字符数：中文全角/英文半角/数字宽度都不同）"""
    if text_w(d, s, f) <= max_w:
        return s
    ell = "…"
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if text_w(d, s[:mid] + ell, f) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (s[:lo] + ell) if lo > 0 else ell


def text_right(d, x_right, y, s, f, fill):
    d.text((x_right - text_w(d, s, f), y), s, font=f, fill=fill)


# ---------------------------------------------------------------- 矢量符号
# 不用字体 glyph：中文字体常缺 ✓◐✗ 等符号，且细笔画量化到 16 灰阶后会糊。

def sym_check(d, cx, cy, s, color=BLACK):
    w = max(4, s // 6)
    d.line([(cx - s * 0.40, cy + s * 0.02),
            (cx - s * 0.12, cy + s * 0.30),
            (cx + s * 0.42, cy - s * 0.32)],
           fill=color, width=w, joint="curve")


def sym_cross(d, cx, cy, s, color=BLACK):
    w = max(4, s // 6)
    a = s * 0.34
    d.line([(cx - a, cy - a), (cx + a, cy + a)], fill=color, width=w)
    d.line([(cx - a, cy + a), (cx + a, cy - a)], fill=color, width=w)


def sym_half(d, cx, cy, s, color=BLACK):
    r = s * 0.38
    box = [cx - r, cy - r, cx + r, cy + r]
    d.pieslice(box, 90, 270, fill=color)          # 左半实心
    d.ellipse(box, outline=color, width=max(4, s // 7))


def sym_dot(d, cx, cy, s, color=GRAY):
    r = s * 0.15
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)


def sym_checkbox(d, cx, cy, s, checked=False, color=BLACK):
    a = s * 0.36
    box = [cx - a, cy - a, cx + a, cy + a]
    d.rounded_rectangle(box, radius=s * 0.12, outline=color, width=max(4, s // 7))
    if checked:
        w = max(4, s // 7)
        d.line([(cx - a * 0.55, cy + a * 0.02),
                (cx - a * 0.12, cy + a * 0.48),
                (cx + a * 0.60, cy - a * 0.45)],
               fill=color, width=w, joint="curve")


def sym_warn(d, cx, cy, s, color=WHITE):
    """实心警告三角 + 挖空的感叹号（用于反白告警条）"""
    pts = [(cx, cy - s * 0.46), (cx + s * 0.50, cy + s * 0.36),
           (cx - s * 0.50, cy + s * 0.36)]
    d.polygon(pts, fill=color)
    bw = max(5, s // 9)
    d.rectangle([cx - bw / 2, cy - s * 0.20, cx + bw / 2, cy + s * 0.12], fill=BLACK)
    r = bw * 0.85
    d.ellipse([cx - r, cy + s * 0.20 - r, cx + r, cy + s * 0.20 + r], fill=BLACK)


def sym_sun(d, cx, cy, s, color=GRAY):
    r = s * 0.22
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=max(3, s // 11))
    import math
    for i in range(8):
        a = math.radians(i * 45)
        x1, y1 = cx + math.cos(a) * s * 0.32, cy + math.sin(a) * s * 0.32
        x2, y2 = cx + math.cos(a) * s * 0.46, cy + math.sin(a) * s * 0.46
        d.line([(x1, y1), (x2, y2)], fill=color, width=max(3, s // 12))


SYMBOLS = {"ok": sym_check, "partial": sym_half, "failed": sym_cross, "info": sym_dot}

# ---------------------------------------------------------------- 数据模型

@dataclass
class Brief:
    status: str          # ok | partial | failed | info
    title: str
    summary: str = ""
    project: str = ""
    time: str = ""


@dataclass
class Todo:
    text: str
    done: bool = False
    time: str = ""


@dataclass
class Alert:
    title: str
    detail: str = ""


@dataclass
class Board:
    alert: Alert | None = None
    briefs: list[Brief] = field(default_factory=list)
    todos: list[Todo] = field(default_factory=list)
    clock: str = "14:32"
    date: str = "2026年10月10日 周五"
    weather: str = ""
    brief_count: int = 0
    todo_count: int = 0
    age: str = "3 分钟"
    health: str = "源正常"


# ---------------------------------------------------------------- 布局

def draw_alert(d: ImageDraw.ImageDraw, a: Alert, y: int) -> None:
    d.rectangle([MARGIN, y, MARGIN + CW, y + ALERT_H], fill=BLACK)

    ix, iy = MARGIN + 34, y + 46
    sym_warn(d, ix, iy, 52, color=WHITE)

    tx = MARGIN + 78
    d.text((tx, y + 26), "需要你介入", font=font(30), fill=LGRAY)

    title = fit(d, a.title, font(58, True), MARGIN + CW - tx - 34)
    d.text((tx, y + 64), title, font=font(58, True), fill=WHITE)

    if a.detail:
        det = fit(d, a.detail, font(30), MARGIN + CW - tx - 34)
        d.text((tx, y + 138), det, font=font(30), fill=LGRAY)


def draw_label(d: ImageDraw.ImageDraw, text: str, y: int, extra: int = 0) -> None:
    """板块标题。extra > 0 时在右侧提示还有多少条没显示
    —— e-ink 不能滚动，必须明说，而不是假装没有。"""
    d.text((MARGIN, y), text, font=font(28), fill=LGRAY)
    if extra > 0:
        text_right(d, MARGIN + CW, y, f"还有 {extra} 条", font(28), LGRAY)
    d.line([(MARGIN, y + 38), (MARGIN + CW, y + 38)], fill=RULE, width=1)


def draw_brief(d: ImageDraw.ImageDraw, b: Brief, y: int, h: int) -> None:
    title_f, meta_f = font(44, True), font(32)

    cy = y + 34
    SYMBOLS.get(b.status, sym_dot)(d, MARGIN + 22, cy, 46)

    tx = MARGIN + 58
    time_f = font(30)
    time_w = text_w(d, b.time, time_f) + 20 if b.time else 0

    if b.status == "info":
        title_color, meta_color = GRAY, LGRAY
    elif b.status == "failed":
        title_color, meta_color = BLACK, GRAY
    else:
        title_color, meta_color = BLACK, GRAY

    suffix = "（有遗留）" if b.status == "partial" else ""
    title = fit(d, b.title + suffix, title_f, CW - 58 - time_w)
    d.text((tx, y + 6), title, font=title_f, fill=title_color)

    if b.time:
        text_right(d, MARGIN + CW, y + 16, b.time, time_f, LGRAY)

    meta = " · ".join(x for x in (b.project, b.summary) if x)
    if meta:
        d.text((tx, y + 72), fit(d, meta, meta_f, CW - 58), font=meta_f, fill=meta_color)

    d.line([(MARGIN, y + h - 8), (MARGIN + CW, y + h - 8)], fill=RULE, width=1)


def draw_todo(d: ImageDraw.ImageDraw, t: Todo, y: int) -> None:
    f = font(40)
    cy = y + 30
    sym_checkbox(d, MARGIN + 22, cy, 40, checked=t.done)

    tx = MARGIN + 58
    time_f = font(30)
    time_w = text_w(d, t.time, time_f) + 20 if t.time else 0
    color = LGRAY if t.done else BLACK

    txt = fit(d, t.text, f, CW - 58 - time_w)
    d.text((tx, y + 6), txt, font=f, fill=color)

    if t.done:
        st = d.textlength(txt, font=f)
        d.line([(tx, y + 30), (tx + st, y + 30)], fill=color, width=2)

    if t.time:
        text_right(d, MARGIN + CW, y + 14, t.time, time_f, LGRAY)


def draw_ambient(d: ImageDraw.ImageDraw, b: Board, y: int) -> None:
    d.text((MARGIN, y + 12), b.clock, font=font(56, True), fill=BLACK)

    df = font(32)
    dw = text_w(d, b.date, df)
    d.text(((W - dw) / 2, y + 34), b.date, font=df, fill=GRAY)

    if b.weather:
        wf = font(32)
        ww = text_w(d, b.weather, wf)
        icon = 40
        x0 = MARGIN + CW - ww - icon - 14
        sym_sun(d, x0 + icon / 2, y + 50, icon)
        d.text((x0 + icon + 14, y + 34), b.weather, font=wf, fill=GRAY)


def draw_status(d: ImageDraw.ImageDraw, b: Board, y: int) -> None:
    f = font(26)
    d.text((MARGIN, y + 24), f"简报 {b.brief_count} 条 · 待办 {b.todo_count} 项",
           font=f, fill=GRAY)
    text_right(d, MARGIN + CW, y + 24, f"数据 {b.age}前 · {b.health}", f, GRAY)


def render_empty(b: Board) -> Image.Image:
    """空态是一个独立模式，不套用常规分区。

    理由：没有内容就没有"锚点稳定"的顾虑；而且空屏必须一眼看出是
    「设计过的空闲」而不是「崩了」—— 板块标题下面空空如也正是那个失败模式。"""
    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)

    area_top, area_bot = TOP, BOT - STATUS_H - GAP
    cy = (area_top + area_bot) / 2

    f_title, f_sub = font(100, True), font(38)
    f_clock, f_date = font(150, True), font(38)

    t1 = "空闲"
    d.text(((W - text_w(d, t1, f_title)) / 2, cy - 300), t1, font=f_title, fill=GRAY)

    t2 = "副屏已就绪，等待简报"
    d.text(((W - text_w(d, t2, f_sub)) / 2, cy - 160), t2, font=f_sub, fill=LGRAY)

    d.text(((W - text_w(d, b.clock, f_clock)) / 2, cy + 10), b.clock,
           font=f_clock, fill=BLACK)

    right = b.date + (f"  ·  {b.weather}" if b.weather else "")
    d.text(((W - text_w(d, right, f_date)) / 2, cy + 200), right,
           font=f_date, fill=GRAY)

    hint = "通过 MCP 发布简报后，会显示在这里"
    f_hint = font(28)
    hw = text_w(d, hint, f_hint)
    d.text(((W - hw) / 2, BOT - STATUS_H - GAP - 60), hint, font=f_hint, fill=LGRAY)

    draw_status(d, b, BOT - STATUS_H)
    return img


def render(b: Board) -> Image.Image:
    if not b.alert and not b.briefs and not b.todos:
        return render_empty(b)

    img = Image.new("L", (W, H), WHITE)
    d = ImageDraw.Draw(img)

    # ---- 自底向上固定分区
    y = BOT
    status_y = y - STATUS_H
    y = status_y - GAP
    ambient_y = y - AMBIENT_H
    y = ambient_y - GAP
    elastic_bottom = y

    # ---- 自顶向下
    elastic_top = TOP
    if b.alert:
        draw_alert(d, b.alert, TOP)
        elastic_top = TOP + ALERT_H + GAP

    elastic_h = elastic_bottom - elastic_top

    # ---- 弹性区按 flex 2:1 分配
    avail = elastic_h - 2 * LABEL_H - GAP
    briefs_h = int(avail * 2 / 3)
    todos_h = avail - briefs_h

    n_briefs = max(0, briefs_h // BRIEF_ITEM_H)
    n_todos = max(0, todos_h // TODO_ITEM_H)

    briefs_y = elastic_top + LABEL_H
    draw_label(d, "最近完成", elastic_top, extra=len(b.briefs) - n_briefs)
    for i, br in enumerate(b.briefs[:n_briefs]):
        draw_brief(d, br, briefs_y + i * BRIEF_ITEM_H, BRIEF_ITEM_H)

    todos_y = briefs_y + briefs_h + GAP + LABEL_H
    draw_label(d, "待办", todos_y - LABEL_H, extra=len(b.todos) - n_todos)
    for i, td in enumerate(b.todos[:n_todos]):
        draw_todo(d, td, todos_y + i * TODO_ITEM_H)

    # 内容区终止线：让欠载时的留白读起来是"有意为之"，而不是"漏了东西"
    d.line([(MARGIN, ambient_y - 16), (MARGIN + CW, ambient_y - 16)], fill=RULE, width=1)

    draw_ambient(d, b, ambient_y)
    draw_status(d, b, status_y)
    return img


def quantize16(img: Image.Image) -> Image.Image:
    """模拟设备的 16 灰阶量化（FBInk 的软件抖动会做得更好，这里只做粗验）"""
    return img.point(lambda v: round(v / 255 * 15) * 17)


# ---------------------------------------------------------------- 样例数据

BRIEFS = [
    Brief("ok", "修复了 auth 模块 token 刷新的竞态条件",
          "定时器刷新改为惰性刷新，补 4 个并发测试", "backend", "14:12"),
    Brief("ok", "迁移用户表到 PostgreSQL 16",
          "停机 40 秒，回滚脚本已就绪", "infra", "13:48"),
    Brief("partial", "重构订单服务",
          "主流程已通，退款路径未覆盖", "backend", "13:20"),
    Brief("failed", "部署 staging 失败",
          "镜像构建超时，需人工确认", "infra", "12:55"),
    Brief("info", "调研了 e-ink 局部刷新方案",
          "结论：当前刷新频率下收益不足", "research", "11:30"),
]

TODOS = [
    Todo("补 auth 模块的删除用户测试用例"),
    Todo("更新 README 的部署说明", done=True, time="14:20"),
    Todo("确认 staging 构建超时的根因"),
    Todo("归档 Q3 的性能报告"),
]

ALERT = Alert("允许执行 rm -rf build", "session: refactor-auth · 已等待 3 分钟")


def main() -> None:
    load_fonts()
    out = Path("mockups/out")
    out.mkdir(parents=True, exist_ok=True)

    variants = {
        "mockup_alert": Board(alert=ALERT, briefs=BRIEFS, todos=TODOS,
                              weather="24°C 晴",
                              brief_count=len(BRIEFS), todo_count=len(TODOS),
                              age="3 分钟"),
        "mockup_idle": Board(alert=None, briefs=BRIEFS, todos=TODOS,
                             weather="24°C 晴",
                             brief_count=len(BRIEFS), todo_count=len(TODOS),
                             age="7 分钟"),
    }

    for name, board in variants.items():
        img = render(board)
        img.save(out / f"{name}.png")
        quantize16(img).save(out / f"{name}_16gray.png")
        print(f"{out / (name + '.png')}")

    # 空态
    empty = Board(alert=None, briefs=[], todos=[], weather="", age="1 分钟")
    render(empty).save(out / "mockup_empty.png")
    print(f"{out / 'mockup_empty.png'}")


if __name__ == "__main__":
    main()
