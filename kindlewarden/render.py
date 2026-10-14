"""渲染器：Board → 1072×1448 灰度 PNG。

布局数学在 :mod:`kindlewarden.layout`，本模块只负责「画」。

两个从 mockup 验证得来的硬约束
------------------------------
1. **灰阶必须对齐设备网格**：PW3 是 16 灰阶，量化网格是 ``255/15 = 17`` 的倍数。
   把灰色选在网格上，文字就不会被抖动成噪点。下面三个灰值全部精确落在网格上，
   mockup 的 16 灰阶量化版与原始版肉眼无差别，就是这么来的。
2. **文本截断按像素宽度**，不能按字符数 —— 中文全角、英文半角、数字宽度都不同，
   而 ``…`` 必须刚好放在边界内。
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .config import Config
from . import layout as _layout
from .layout import (BRIEF_TWO_LINE_MIN_H, GAP, LABEL_PAD_TOP, LABEL_RULE_DY,
                     MARGIN, STATUS_H, Layout, Zone, plan, plan_empty)
from .models import Alert, Board, Brief, Todo

# ---------------------------------------------------------------- 画布（可切换）
#
# ⚠️ 这些是**模块级变量，不是常量** —— 横屏时会通过 set_canvas() 改掉。
#
# 为什么这么做而不是把尺寸当参数传：
#   渲染器里有 50 多处引用 CANVAS_W/CANVAS_H，穿参数要改遍所有函数签名，
#   而且很容易漏掉一处 —— 漏掉的地方会静默按旧尺寸画，画面就错位了。
#
# 为什么安全：Python 在**函数执行时**才解析全局名，所以改这里之后
# 所有引用自动生效。线程安全由 BoardService._lock 保证（渲染是串行的）。

CANVAS_W = _layout.CANVAS_W
CANVAS_H = _layout.CANVAS_H
CONTENT_W = _layout.CONTENT_W


def set_canvas(w: int, h: int) -> None:
    """切换渲染画布尺寸（横屏时按宽>高排版）。

    同时更新 ``layout`` 模块的同名变量，免得两边不一致 ——
    布局按一种尺寸算、渲染按另一种尺寸画，画面必然错位。
    """
    global CANVAS_W, CANVAS_H, CONTENT_W, TEXT_W
    _layout.set_canvas(w, h)
    CANVAS_W = _layout.CANVAS_W
    CANVAS_H = _layout.CANVAS_H
    CONTENT_W = _layout.CONTENT_W
    TEXT_W = MARGIN + CONTENT_W - TEXT_X - 12


def reset_canvas() -> None:
    """恢复到设备物理画布（1072×1448）。"""
    set_canvas(_layout.DEVICE_W, _layout.DEVICE_H)

# ---------------------------------------------------------------- 颜色（对齐 17 的倍数）

BLACK = 0
GRAY = 119      # 17*7
LGRAY = 153     # 17*9
RULE = 221      # 17*13
WHITE = 255

# ---------------------------------------------------------------- 版面网格
#
# 整个画面的左边缘只有**两条**基准线，所有元素都吸附上去：
#
#     GUTTER_X —— 状态符号 / 勾选框的中心线
#     TEXT_X   —— 所有正文的左边缘
#
# 之前每类元素各用一个 `MARGIN + 22` / `MARGIN + 58` 的魔数，改一处就错位一处。
# 现在把它们提成具名常量，标题、摘要、待办文字共用同一条文字左边缘 ——
# 扫视时竖线对齐，是"规整"最主要的来源。

GUTTER_X = MARGIN + 24          # 图标列中心
TEXT_X = MARGIN + 58            # 正文左边缘
TEXT_W = MARGIN + CONTENT_W - TEXT_X - 12   # 正文可用宽度（右侧留 12px 呼吸）
# 各板块字号
F_LABEL = 30                    # 板块标题
F_BRIEF_TITLE = 44              # 简报标题（粗）
F_BRIEF_META = 32               # 简报摘要 / 项目（竖屏第二行）
F_TODO = 40                     # 待办条目
F_STAMP = 30                    # 右对齐的时间戳
F_STATUS = 26
#: 横屏单行模式下，右对齐的项目名。
#:
#: 比标题（44）明显小、和时间戳（30）同级 —— 它是**归属标注**不是内容，
#: 读的时候应该"扫得到"而不是"抢戏"。用弱化色（LGRAY/GRAY）配小字号，
#: 标题依然是这一行的视觉重心。
F_BRIEF_PROJECT = 30

#: 横屏单行时，项目名与时间戳之间的横向间隔。
PROJECT_GAP = 28

# ---------------------------------------------------------------- 字体


class FontBook:
    """中文字体解析与缓存。字体只存在于 host，**不需要传到 Kindle 上**。"""

    #: 明确列出的候选（先查这些，命中即用）。
    CANDIDATES = [
        (r"C:\Windows\Fonts\Noto Sans SC (TrueType).otf",
         r"C:\Windows\Fonts\Noto Sans SC Bold (TrueType).otf"),
        (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyhbd.ttc"),
        (r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simhei.ttf"),
        ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
         "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"),
        ("/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
         "/usr/share/fonts/noto-cjk/NotoSansCJK-Bold.ttc"),
        ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/PingFang.ttc"),
    ]

    #: 兜底：按**文件名**在常见字体目录里找。
    #:
    #: 为什么要兜底而不是把路径列全：各家发行版放 Noto CJK 的位置不一样
    #: （Debian 在 opentype/noto，有的在 noto-cjk，语言子集还叫
    #: NotoSansCJKsc-*）。**把路径穷举一遍既列不全也维护不动**，
    #: 而容器里没有交互式终端可以现场排查 —— 找不到字体就直接渲染失败。
    #: 所以按文件名模式去找，命中概率高得多。
    FONT_DIRS = [
        "/usr/share/fonts", "/usr/local/share/fonts",
        "/usr/share/fonts/opentype", "/usr/share/fonts/truetype",
    ]
    #: (正则, 是否粗体)。顺序即优先级。
    FONT_PATTERNS = [
        (r"NotoSansCJK.*-Regular\.(ttc|otf|ttf)$", False),
        (r"NotoSansCJK.*-Bold\.(ttc|otf|ttf)$", True),
        (r"NotoSansSC-Regular\.(ttf|otf)$", False),
        (r"NotoSansSC-Bold\.(ttf|otf)$", True),
        (r"SourceHanSans.*-Regular\.(ttc|otf|ttf)$", False),
        (r"SourceHanSans.*-Bold\.(ttc|otf|ttf)$", True),
        (r"wqy-zenhei\.ttc$", False),
        (r"wqy-microhei\.ttc$", False),
    ]

    def __init__(self, regular: str | None = None, bold: str | None = None):
        if regular and bold:
            self.regular, self.bold = regular, bold
        else:
            self.regular, self.bold = self._discover()
        self._cache: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}

    @classmethod
    def _scan(cls) -> list[Path]:
        """把常见字体目录下的字体文件列出来（只找一层，不递归整棵树）。"""
        found: list[Path] = []
        for d in cls.FONT_DIRS:
            p = Path(d)
            if not p.is_dir():
                continue
            try:
                found.extend(x for x in p.rglob("*") if x.is_file())
            except OSError:
                continue
        return found

    @classmethod
    def _discover(cls) -> tuple[str, str]:
        for reg, bold in cls.CANDIDATES:
            if Path(reg).exists() and Path(bold).exists():
                return reg, bold

        # 兜底：按文件名模式扫
        files = cls._scan()
        by_pattern: dict[str, str] = {}
        for path in files:
            name = path.name
            for pattern, _is_bold in cls.FONT_PATTERNS:
                if re.search(pattern, name, re.I) and pattern not in by_pattern:
                    by_pattern[pattern] = str(path)

        regular = None
        bold = None
        for pattern, is_bold in cls.FONT_PATTERNS:
            hit = by_pattern.get(pattern)
            if not hit:
                continue
            if is_bold and bold is None:
                bold = hit
            elif not is_bold and regular is None:
                regular = hit
            if regular and bold:
                break

        # 只有常规体（比如 wqy-zenhei）时，粗体退回常规体 —— 总比报错强
        if regular and not bold:
            bold = regular
        if regular:
            return regular, bold

        raise RuntimeError(
            "找不到可用的中文字体。请安装 Noto Sans CJK / 思源黑体"
            "（Debian/Ubuntu: apt-get install fonts-noto-cjk），"
            "或用 FontBook(regular=..., bold=...) 显式指定。"
            f"已扫描的目录：{', '.join(cls.FONT_DIRS)}"
        )

    def get(self, size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
        path = self.bold if bold else self.regular
        key = (path, size)
        if key not in self._cache:
            self._cache[key] = ImageFont.truetype(path, size)
        return self._cache[key]


# ---------------------------------------------------------------- 文本工具


def _text_v_offset(d: ImageDraw.ImageDraw, s: str,
                   f: ImageFont.FreeTypeFont) -> float:
    """让文字在**任意高度的框**里垂直居中时，``d.text`` 的 y 相对框中心的偏移。

    ⚠️ 为什么不能写死一个偏移量。

    踩过一次：按钮里的「‹ 上一页」用固定的 ``rect_top + 16`` 画，
    而按钮高 66px —— 结果是**上方留白远多于下方，文字明显偏下**（用户报的）。

    问题在于"文字高度"不等于"字号"：``d.text(..., y)`` 里的 ``y`` 是
    **基线框的顶边**，而墨迹只占其中一部分（``textbbox`` 的 top 通常是
    正数，表示基线框顶部到墨迹顶部还有空隙）。

    算法（用真实度量，不猜）::

        bbox = d.textbbox((0, 0), text, font)      # 相对 y=0 的包围盒
        墨迹中心相对传入 y 的偏移 = (bbox.top + bbox.bottom) / 2
        要让墨迹中心落在框中心 → y = 框中心 - 该偏移

    这条**只做一次减法**。第一版写成 ``-top + height/2`` 是重复扣减了
    ``top``（``textbbox`` 的 top 本来就是相对基线的），反而更偏 ——
    所以这里是量出来的公式，不是推导出来的。
    """
    s = " ".join((s or "").split())      # 多行压平（textbbox 不接受多行）
    try:
        _l, top, _r, bottom = d.textbbox((0, 0), s, font=f)
        return (top + bottom) / 2
    except (AttributeError, ValueError, OSError):
        # 老版本 Pillow 没有 textbbox：按字号粗略估（墨迹大约占中间六成）
        return f.size * 0.35


def _tw(d: ImageDraw.ImageDraw, s: str, f: ImageFont.FreeTypeFont) -> float:
    """文本的**像素宽度**。

    ⚠️ **必须先把多行压成单行再量。**

    Pillow 的 ``textlength()`` 对含换行的文本会直接抛：

        ValueError: can't measure length of multiline text

    这是实测踩到的：summary 支持多行之后，看板渲染走到这里就 500，
    而且 `/board` 整个端点挂掉（现象是管理页和 Kindle 都取不到数据）。

    为什么在这个基础函数里兜底而不是要求每个调用方自己清理：
    ``_tw`` 被渲染器到处调用（截断、右对齐、居中都靠它），
    要求每个调用点都记得"先压平"是不现实的 —— 漏一个就炸。
    在这里统一处理，一劳永逸。

    压平用空格替换换行：宽度量出来会略大于"最长那一行"，
    对于"会不会超出边界"这个判断来说是**偏保守**的，安全。
    """
    if "\n" in s or "\r" in s:
        s = " ".join(s.split())
    return d.textlength(s, font=f)


def _draw_button_label(d: ImageDraw.ImageDraw, rect: list[int], text: str,
                       font: ImageFont.FreeTypeFont,
                       pad_left: int = 30) -> None:
    """在按钮矩形里画一行**垂直居中**的文字。

    ⚠️ 别退回写死的偏移。

    踩过一次：三个按钮（上一页/下一页/返回）原来都用 ``rect_top + 16`` 画，
    而框高 64~66px —— 上方留白 27px、下方只有 8px，
    **文字明显偏下**（用户报的"文字靠下了一点"）。

    实测偏差：旧做法 19~24px，用 :func:`_text_v_offset` 之后降到 0~3px。
    """
    cx_off = _text_v_offset(d, text, font)
    center_y = rect[1] + (rect[3] - rect[1]) / 2
    d.text((rect[0] + pad_left, center_y - cx_off), text,
           font=font, fill=BLACK)


def _fit(d: ImageDraw.ImageDraw, s: str, f: ImageFont.FreeTypeFont, max_w: float) -> str:
    """按**像素宽度**截断成**单行**。二分查找，保证 ``…`` 恰好放在边界内。

    ⚠️⚠️ **必须先压成单行**，这是踩过的真 bug。

    ``_fit`` 的调用方拿到的字符串会**直接交给 ``d.text``**，而
    ``d.text`` 遇到 ``\\n`` 会**画多行** —— 于是文字溢出那条固定高度的
    条目，盖到下一个板块上。

    用户报的现象正是这个：**「待办与最近完成最后一条记录的第二行文字重叠」**。

    发生的条件是"压平后没超宽，但原文含换行"：

        首行短 + 换行   →  压平宽 560（< TEXT_W 922）
                        →  旧实现直接 return 原串（**含 2 个换行**）
                        →  d.text 画出 2 行、高 116px
                        →  从 y+66 开始画，一直画到 y+182
                        →  条目只有 128px，盖住了下面的板块

    而简报的 summary 是**支持多行**的（保留段落，见 models.multi_line），
    所以这个条件很容易满足 —— 不是什么罕见情况。

    压平用空格替换换行：对"会不会超宽"这个判断是**偏保守**的，安全。
    （和 ``_tw`` 里的处理一致 —— 那里也要压平，否则 Pillow 会抛
    ``ValueError: can't measure length of multiline text``。）
    """
    s = s or ""
    if "\n" in s or "\r" in s:
        s = " ".join(s.split())
    if _tw(d, s, f) <= max_w:
        return s
    ell = "…"
    lo, hi = 0, len(s)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if _tw(d, s[:mid] + ell, f) <= max_w:
            lo = mid
        else:
            hi = mid - 1
    return (s[:lo] + ell) if lo > 0 else ell


def _right(d: ImageDraw.ImageDraw, x_right: float, y: float, s: str,
           f: ImageFont.FreeTypeFont, fill: int) -> None:
    d.text((x_right - _tw(d, s, f), y), s, font=f, fill=fill)


def wrap_paragraphs(text: str) -> list[str]:
    """把文本切成**段落**。

    为什么需要：``wrap_px`` 会把 ``\\n`` 拉平成空格（那是给单行字段用的），
    于是简报里精心分的段落在详情页上全糊成一坨 —— 读起来很累。

    分段规则（尽量贴近人的直觉）：
      * 空行 → 分段
      * 单换行 → 也分段。简报是"一条一句"的短行文体，
        作者主动换行就是想让它另起一行，不该被合并
      * 行首的 ``-`` / ``*`` / ``·`` / ``1.`` 之类 → 由调用方识别成列表项
    """
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    return [line.strip() for line in text.split("\n") if line.strip()]


def _is_list_item(line: str) -> bool:
    """这一行看起来是不是列表项（决定要不要加项目符号）。"""
    return bool(re.match(r"^\s*(?:[-*·•]|\d+[.、)]|[（(]\d+[)）])\s*", line))


def _strip_list_marker(line: str) -> str:
    """去掉行首的列表符号（我们会自己画统一的符号）。"""
    return re.sub(r"^\s*(?:[-*·•]|\d+[.、)]|[（(]\d+[)）])\s*", "", line)


def wrap_px(d: ImageDraw.ImageDraw, text: str, f: ImageFont.FreeTypeFont,
            max_w: float, max_lines: int = 0) -> list[str]:
    """按**像素宽度**折行。

    看板上的字段是单行截断（版面所限），但**详情页要显示全文**，所以需要折行。

    CJK 可以逐字断行，但拉丁词/数字/路径不能从中间劈开 —— 所以先把连续的
    非 CJK 字符聚成一个"原子"，再按原子塞进每一行。

    ⚠️ 这个函数会把换行拉平成空格 —— 它设计给**单行字段**用。
       需要保留段落结构时用 :func:`wrap_paragraphs` 先切段，再逐段调用它。
    """
    text = (text or "").replace("\r", " ").replace("\n", " ")
    if not text.strip():
        return []

    # 切成原子：CJK 单字一个原子；连续的 ASCII 可见字符合成一个原子
    atoms: list[str] = []
    buf = ""
    for ch in text:
        if _is_cjk(ch) or ch == " ":
            if buf:
                atoms.append(buf)
                buf = ""
            atoms.append(ch)
        else:
            buf += ch
    if buf:
        atoms.append(buf)

    lines: list[str] = []
    cur = ""
    for atom in atoms:
        if atom == " " and not cur:
            continue                      # 行首不留空格
        trial = cur + atom
        if _tw(d, trial, f) <= max_w:
            cur = trial
            continue
        if cur:
            lines.append(cur.rstrip())
        cur = atom
        # 单个原子本身就超宽（比如一条很长的无空格路径）→ 硬切
        while _tw(d, cur, f) > max_w and len(cur) > 1:
            cut = len(cur)
            while cut > 1 and _tw(d, cur[:cut], f) > max_w:
                cut -= 1
            lines.append(cur[:cut])
            cur = cur[cut:]
        if max_lines and len(lines) >= max_lines:
            break

    if cur.strip():
        lines.append(cur.rstrip())

    if max_lines and len(lines) > max_lines:
        kept = lines[:max_lines]
        kept[-1] = _fit(d, kept[-1] + "…", f, max_w)
        return kept
    return lines


def _is_cjk(ch: str) -> bool:
    code = ord(ch)
    return (0x2E80 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7A3
            or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFF60
            or 0x20000 <= code <= 0x3FFFD)


# ---------------------------------------------------------------- 矢量符号
# 不用字体 glyph：中文字体常缺 ✓◐✗ 等符号，且细笔画量化到 16 灰阶后会糊。


def sym_check(d, cx, cy, s, color=BLACK):
    d.line([(cx - s * 0.40, cy + s * 0.02),
            (cx - s * 0.12, cy + s * 0.30),
            (cx + s * 0.42, cy - s * 0.32)],
           fill=color, width=max(4, s // 6), joint="curve")


def sym_cross(d, cx, cy, s, color=BLACK):
    a, w = s * 0.34, max(4, s // 6)
    d.line([(cx - a, cy - a), (cx + a, cy + a)], fill=color, width=w)
    d.line([(cx - a, cy + a), (cx + a, cy - a)], fill=color, width=w)


def sym_half(d, cx, cy, s, color=BLACK):
    r = s * 0.38
    box = [cx - r, cy - r, cx + r, cy + r]
    d.pieslice(box, 90, 270, fill=color)
    d.ellipse(box, outline=color, width=max(4, s // 7))


def sym_dot(d, cx, cy, s, color=GRAY):
    r = s * 0.15
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)


def sym_checkbox(d, cx, cy, s, checked=False, color=BLACK):
    a = s * 0.36
    d.rounded_rectangle([cx - a, cy - a, cx + a, cy + a],
                        radius=s * 0.12, outline=color, width=max(4, s // 7))
    if checked:
        d.line([(cx - a * 0.55, cy + a * 0.02),
                (cx - a * 0.12, cy + a * 0.48),
                (cx + a * 0.60, cy - a * 0.45)],
               fill=color, width=max(4, s // 7), joint="curve")


def sym_warn(d, cx, cy, s, color=WHITE):
    d.polygon([(cx, cy - s * 0.46), (cx + s * 0.50, cy + s * 0.36),
               (cx - s * 0.50, cy + s * 0.36)], fill=color)
    bw = max(5, s // 9)
    d.rectangle([cx - bw / 2, cy - s * 0.20, cx + bw / 2, cy + s * 0.12], fill=BLACK)
    r = bw * 0.85
    d.ellipse([cx - r, cy + s * 0.20 - r, cx + r, cy + s * 0.20 + r], fill=BLACK)


def sym_sun(d, cx, cy, s, color=GRAY):
    import math
    r = s * 0.22
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=max(3, s // 11))
    for i in range(8):
        a = math.radians(i * 45)
        d.line([(cx + math.cos(a) * s * 0.32, cy + math.sin(a) * s * 0.32),
                (cx + math.cos(a) * s * 0.46, cy + math.sin(a) * s * 0.46)],
               fill=color, width=max(3, s // 12))


SYMBOLS = {"ok": sym_check, "partial": sym_half, "failed": sym_cross, "info": sym_dot}


def sym_broken_link(d, cx, cy, s, color=BLACK):
    """断开的链路：两个错开的链环 + 中间一道缺口。

    离线屏专用。第一版画成同心圆弧，量化到 16 灰阶后糊成一团、
    读起来像个风车 —— 所以改成**两个分离的实体形状**：
    形状之间留出明显的白，才在灰阶下站得住。
    """
    w = max(10, s // 7)
    a = s * 0.30                       # 单个环的半径
    gap = s * 0.30                     # 两环错开的距离
    box_l = [cx - gap - a, cy - gap - a, cx - gap + a, cy - gap + a]
    box_r = [cx + gap - a, cy + gap - a, cx + gap + a, cy + gap + a]

    # 左下环：留出朝右上方的开口
    d.arc(box_l, start=300, end=210, fill=color, width=w)
    # 右上环：留出朝左下方的开口
    d.arc(box_r, start=120, end=30, fill=color, width=w)

# ---------------------------------------------------------------- 时间


_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
HEALTH_TEXT = {"ok": "源正常", "stale": "源陈旧", "down": "源不可用"}


def format_date(ts: float) -> str:
    dt = _dt.datetime.fromtimestamp(ts)
    return f"{dt.year}年{dt.month}月{dt.day}日 {_WEEKDAYS[dt.weekday()]}"


def format_clock(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime("%H:%M")


# ---------------------------------------------------------------- 各板块


def _draw_alert(d: ImageDraw.ImageDraw, a: Alert, y: int, f: FontBook) -> None:
    """告警条：全屏最强的注意力信号。

    黑底反白。内部也走和正文一样的图标列 / 文字列 —— 反白方块会放大任何
    对齐误差，所以这里尤其不能用临时坐标。

    ⚠️ 高度**从 layout.metrics() 读**，不用写死的常量。

    竖屏 196 的内部节奏：

        y+ 26  需要你介入        30px 反白弱化字（在图标列右侧）
        y+ 74  标题              56px 粗体
        y+146  详情              30px 反白弱化字

    横屏只有 168 —— 照搬竖屏的节奏会让最后一行（y+146）贴着底边。
    所以横屏按比例压缩节奏。

    ⚠ 图标**不能**放在 GUTTER_X：反白块里 52px 的三角会顶到 56px 的大标题上
    （踩过）。告警条的图标单独占一列，文字列随之右移，内部自成一套对齐。
    """
    ah = _layout.metrics()["alert_h"]
    d.rectangle([MARGIN, y, MARGIN + CONTENT_W, y + ah], fill=BLACK)

    # 横屏（ah=168）时内部节奏按比例收紧；竖屏保持原有的舒适间距
    compact = ah < _layout.ALERT_H

    icon_x = MARGIN + 54
    sym_warn(d, icon_x, y + 44, 44, color=WHITE)

    tx = MARGIN + 104
    title_w = MARGIN + CONTENT_W - tx - 28

    d.text((tx, y + (18 if compact else 26)), "需要你介入",
           font=f.get(30), fill=LGRAY)

    d.text((tx, y + (58 if compact else 74)),
           _fit(d, a.title, f.get(56, True), title_w),
           font=f.get(56, True), fill=WHITE)

    if a.detail:
        d.text((tx, y + (118 if compact else 146)),
               _fit(d, a.detail, f.get(30), title_w),
               font=f.get(30), fill=LGRAY)


def _draw_label(d: ImageDraw.ImageDraw, text: str, y: int, f: FontBook,
                overflow: int = 0) -> None:
    """板块标题行。

    视觉层级靠三件事建立，而不是靠把字放大：
    标题字用 GRAY（比正文的黑弱一级）、下方一条贯通的分隔线把它"钉"在内容上、
    溢出计数右对齐在同一条基线上。
    """
    ty = y + LABEL_PAD_TOP
    d.text((MARGIN, ty), text, font=f.get(F_LABEL, True), fill=GRAY)
    if overflow > 0:
        # e-ink 不能滚动，必须明说还有多少条没显示，而不是假装没有。
        # 放在标题行右端，不额外占高度。
        _right(d, MARGIN + CONTENT_W, ty + 4, f"还有 {overflow} 条",
               f.get(F_LABEL), LGRAY)
    d.line([(MARGIN, y + LABEL_RULE_DY), (MARGIN + CONTENT_W, y + LABEL_RULE_DY)],
           fill=RULE, width=1)


def _draw_brief(d: ImageDraw.ImageDraw, b: Brief, y: int, h: int, f: FontBook) -> None:
    """一条简报：状态符号 + 标题 + 项目名 + 时间戳。

    ⚠️ 画几行由**传进来的条目高度 ``h``** 决定，不是另传一个开关：

      竖屏 ``h=128`` → 标题（y+4）+ 元信息（y+66），末行分隔线在 y+120
      横屏 ``h=84``  → **单行**：标题 + 右侧项目名，分隔线在 y+76

    为什么用高度当判据：``h`` 是**布局已经算好的事实**，
    渲染照着它画，两边天然一致。如果改成传一个 ``compact=True`` 标志位，
    调用方漏传就会出现"布局按单行留位、渲染画了两行"—— 文字直接溢出到
    下一个板块上。这样的重叠踩过一次（见 ``_fit`` 的注释）。

    ⚠️ **横屏单行也必须带项目名。**

    踩过：横屏为了省高度只画标题，结果"不知道是哪个项目报过来的"——
    多条简报混在一起时完全分不清来源。横屏画布有 1448 宽，
    标题一行根本用不完，项目名放右侧绰绰有余。

    所以两种模式都显示项目名，只是**位置不同**：

      竖屏（两行）  项目名在第二行，和摘要拼成 ``project · summary``
      横屏（单行）  项目名**右对齐**，放在时间戳左边

    横屏放右边而不是放到标题前面：标题的左边缘要和待办共用同一条文字线
    （那是整版"规整"的主要来源），在它前面插东西会把这条线打断。
    """
    title_f = f.get(F_BRIEF_TITLE, True)
    two_line = h >= BRIEF_TWO_LINE_MIN_H

    # 符号与**标题**的第一行视觉居中（不是与整个条目居中）
    sym_cy = y + (34 if two_line else h // 2)
    SYMBOLS.get(b.status, sym_dot)(d, GUTTER_X, sym_cy, 46)

    time_f = f.get(F_STAMP)
    stamp = format_clock(b.ts)
    # 时间戳要占的横向空间（含它右边到版心边缘的留白）
    stamp_block = _tw(d, stamp, time_f) + 24

    suffix = "（有遗留）" if b.status == "partial" else ""
    title_color = GRAY if b.status == "info" else BLACK
    meta_color = LGRAY if b.status == "info" else GRAY

    if two_line:
        # ---- 竖屏：两行。项目名跟摘要在第二行
        title_y, stamp_y = y + 4, y + 16
        d.text((TEXT_X, title_y),
               _fit(d, b.title + suffix, title_f, TEXT_W - stamp_block),
               font=title_f, fill=title_color)
        _right(d, MARGIN + CONTENT_W, stamp_y, stamp, time_f, LGRAY)

        meta_f = f.get(F_BRIEF_META)
        meta = " · ".join(x for x in (b.project, b.summary) if x)
        if meta:
            # ⚠️ _fit 会**压成单行**再截断（它必须这么做 ——
            #    否则 d.text 遇到 \n 会画多行，溢出这条固定高度的条目）。
            d.text((TEXT_X, y + 66), _fit(d, meta, meta_f, TEXT_W),
                   font=meta_f, fill=meta_color)
    else:
        # ---- 横屏：单行。右侧依次是「项目名」「时间戳」
        title_y = y + (h - 49) // 2
        stamp_y = title_y + 12

        # 项目名右对齐在时间戳左边；它要占的宽度也要从标题里扣掉，
        # 否则长标题会压到项目名上（两个都是右对齐区的东西，会叠字）。
        proj_f = f.get(F_BRIEF_PROJECT)
        proj_txt = _fit(d, b.project or "", proj_f, TEXT_W * 0.3) \
            if b.project else ""
        proj_block = (_tw(d, proj_txt, proj_f) + PROJECT_GAP) if proj_txt else 0.0

        d.text((TEXT_X, title_y),
               _fit(d, b.title + suffix, title_f,
                    TEXT_W - stamp_block - proj_block),
               font=title_f, fill=title_color)
        _right(d, MARGIN + CONTENT_W, stamp_y, stamp, time_f, LGRAY)
        if proj_txt:
            # 贴着时间戳左侧放（时间戳块宽 = 文字宽 + 24 的留白）
            proj_right = MARGIN + CONTENT_W - stamp_block - PROJECT_GAP
            d.text((proj_right - _tw(d, proj_txt, proj_f), stamp_y),
                   proj_txt, font=proj_f, fill=meta_color)

    d.line([(MARGIN, y + h - 8), (MARGIN + CONTENT_W, y + h - 8)], fill=RULE, width=1)


def _draw_todo(d: ImageDraw.ImageDraw, t: Todo, y: int, h: int,
               f: FontBook) -> None:
    """一条待办。勾选框落在图标列上，文字落在正文列上 —— 与简报同一条竖线。

    ⚠️ **必须接收条目高度 ``h``**，所有元素按它居中。

    踩过：原来签名里没有 ``h``，几个元素都用写死的偏移
    （文字 y+8、勾选框 y+32、删除线 y+30、时间戳 y+22），
    那套是按竖屏的 64px 调的。横屏把待办压到 56px 之后，
    40 号字的墨迹从 y+21 一直画到 **y+65** —— **溢出条目 9px**，
    最后一条的文字就渗进了下面环境区上方的留白里。

    这和简报那个「_fit 返回多行导致溢出」是同一类问题：
    **画在哪，必须由布局算出来的高度决定，不能在绘制里另写一套。**
    现在改成从中线推：``cy = y + h / 2``，再用 :func:`_text_v_offset`
    把墨迹对准它 —— 竖屏 64 / 横屏 56 都自动正确。

    最后一条不画分隔线（由调用方控制），避免在板块尾部多出一条悬空的线。
    """
    tf = f.get(F_TODO)
    cy = y + h / 2                          # 条目中线，所有元素共用

    sym_checkbox(d, GUTTER_X, cy, 40, checked=t.done)

    time_f = f.get(F_STAMP)
    stamp = format_clock(t.done_ts) if (t.done and t.done_ts) else ""
    time_w = _tw(d, stamp, time_f) + 24 if stamp else 0
    color = LGRAY if t.done else BLACK

    txt = _fit(d, t.text, tf, TEXT_W - time_w)
    txt_y = cy - _text_v_offset(d, txt, tf)
    d.text((TEXT_X, txt_y), txt, font=tf, fill=color)

    if t.done:
        # 删除线落在文字的**视觉中线**上 —— 跟着文字走，不写死
        d.line([(TEXT_X, cy), (TEXT_X + _tw(d, txt, tf), cy)],
               fill=color, width=2)
    if stamp:
        _right(d, MARGIN + CONTENT_W,
               cy - _text_v_offset(d, stamp, time_f), stamp, time_f, LGRAY)


def _draw_ambient(d: ImageDraw.ImageDraw, cfg: Config, now: float, y: int,
                  f: FontBook) -> None:
    """环境区：时间 / 日期 / 天气。

    这一区**贴底**锚定，读法是"页码"式的：时间在左、日期居中、天气在右。
    时钟打开时，三者共用一条视觉中线（而不是各自随意摆）—— 之前时钟配左、
    日期居中、两者基线还差 22px，看起来像没对齐完。

    ⚠️ 所有元素都以**分区中线** ``cy`` 定位，不再用写死的偏移。

    踩过两次同类的坑：

      1. 用固定偏移画（按竖屏 132px 调的），横屏分区一改矮就**偏上**，
         底下空一截；
      2. 横屏压到 74px 后，原来那套 112px 的偏移会让日期**画到分区外面**去。

    根子在于"偏移量"和"分区高度"被**各写各的**，改一个就错位。
    改成从中线推：``cy = y + h / 2``，再用 :func:`_text_v_offset`
    把**墨迹**对准它 —— 这样任何高度都自动居中，不会再溢出。
    （``_text_v_offset`` 返回墨迹中心相对绘制 y 的偏移，
      也是按钮文字居中用的同一个函数。）
    """
    h = _layout.metrics()["ambient_h"]
    cy = y + h / 2                      # 分区中线，所有元素共用

    date_text = format_date(now)

    if cfg.ambient.show_clock:
        clock_f = f.get(56, True)
        date_f = f.get(32)
        clock = format_clock(now)
        d.text((MARGIN, cy - _text_v_offset(d, clock, clock_f)), clock,
               font=clock_f, fill=BLACK)
        # 日期在**内容区**居中，而不是在画布居中 —— 否则会与右侧天气挤在一起
        cx = MARGIN + CONTENT_W / 2
        d.text((cx - _tw(d, date_text, date_f) / 2,
                cy - _text_v_offset(d, date_text, date_f)), date_text,
               font=date_f, fill=GRAY)
    else:
        date_f = f.get(38)
        d.text((MARGIN, cy - _text_v_offset(d, date_text, date_f)), date_text,
               font=date_f, fill=GRAY)

    if cfg.ambient.weather:
        wf = f.get(32)
        ww = _tw(d, cfg.ambient.weather, wf)
        icon = 40
        x0 = MARGIN + CONTENT_W - ww - icon - 14
        sym_sun(d, x0 + icon / 2, cy, icon)
        d.text((x0 + icon + 14,
                cy - _text_v_offset(d, cfg.ambient.weather, wf)),
               cfg.ambient.weather, font=wf, fill=GRAY)


def _draw_status(d: ImageDraw.ImageDraw, board: Board, now: float, y: int,
                 f: FontBook, health: str) -> None:
    """底部状态栏。整幅画面的"落款"。

    顶部一条细线把它和 ambient 分成两段 —— 之前它们只是靠 22px 空白分开，
    在灰阶上读起来像同一块内容的第二行。
    """
    sf = f.get(F_STATUS)
    d.line([(MARGIN, y + 6), (MARGIN + CONTENT_W, y + 6)], fill=RULE, width=1)

    left = f"简报 {board.total_briefs} 条 · 待办 {board.total_todos} 项"
    # 绝对时刻，而不是「N 分钟前」：快照事实永远为真，不需要靠频繁刷新维持诚实。
    right = f"更新于 {format_clock(now)} · {HEALTH_TEXT.get(health, health)}"
    ty = y + 26
    d.text((MARGIN, ty), left, font=sf, fill=GRAY)
    _right(d, MARGIN + CONTENT_W, ty, right, sf, GRAY)


# ---------------------------------------------------------------- 空态


def _render_empty(img: Image.Image, d: ImageDraw.ImageDraw, cfg: Config,
                  now: float, f: FontBook, health: str) -> None:
    """空态是**独立模式**，不套用常规分区。

    理由：没有内容就没有「锚点稳定」的顾虑；而且空屏必须一眼看出是
    「设计过的空闲」而不是「崩了」。mockup 第一版就是反面教材 ——
    常规布局 + 空列表渲染出来是两个板块标题下面空空如也，看起来像崩溃。
    """
    area_top = MARGIN
    area_bot = CANVAS_H - MARGIN - (STATUS_H + GAP if cfg.panels.statusbar else 0)
    cy = (area_top + area_bot) / 2

    f_title, f_sub = f.get(120, True), f.get(38)

    t1 = "空闲"
    d.text(((CANVAS_W - _tw(d, t1, f_title)) / 2, cy - 250), t1,
           font=f_title, fill=GRAY)

    t2 = "副屏已就绪，等待简报"
    d.text(((CANVAS_W - _tw(d, t2, f_sub)) / 2, cy - 90), t2, font=f_sub, fill=LGRAY)

    tail = format_date(now) + (f"  ·  {cfg.ambient.weather}" if cfg.ambient.weather else "")
    if cfg.ambient.show_clock:
        fc = f.get(150, True)
        clock = format_clock(now)
        d.text(((CANVAS_W - _tw(d, clock, fc)) / 2, cy + 10), clock, font=fc, fill=BLACK)
        d.text(((CANVAS_W - _tw(d, tail, f_sub)) / 2, cy + 200), tail,
               font=f_sub, fill=GRAY)
    else:
        ft = f.get(46)
        d.text(((CANVAS_W - _tw(d, tail, ft)) / 2, cy + 60), tail, font=ft, fill=GRAY)

    if cfg.panels.statusbar:
        hint = "通过 MCP 发布简报后，会显示在这里"
        fh = f.get(28)
        d.text(((CANVAS_W - _tw(d, hint, fh)) / 2,
                CANVAS_H - MARGIN - STATUS_H - GAP - 60), hint, font=fh, fill=LGRAY)


# ---------------------------------------------------------------- 入口


def render_board(board: Board, cfg: Config, *, now: float,
                 fonts: FontBook | None = None, health: str = "ok") -> tuple[Image.Image, Layout]:
    """渲染副屏。返回 ``(图像, 布局)`` —— 布局一并返回便于测试和调试。"""
    f = fonts or FontBook()
    img = Image.new("L", (CANVAS_W, CANVAS_H), WHITE)
    d = ImageDraw.Draw(img)

    renderable = (
        (board.alert is not None and cfg.panels.alerts)
        or (bool(board.briefs) and cfg.panels.briefs)
        or (bool(board.todos) and cfg.panels.todos)
    )

    if not renderable:
        _render_empty(img, d, cfg, now, f, health)
        if cfg.panels.statusbar:
            _draw_status(d, board, now, CANVAS_H - MARGIN - STATUS_H, f, health)
        return img, plan_empty(cfg.panels)

    lay = plan(
        panels=cfg.panels,
        has_alert=board.alert is not None,
        n_briefs=len(board.briefs),
        n_todos=len(board.todos),
        # ⚠️ 走 effective_max_briefs，别直接传 cfg.board.max_briefs ——
        #    横屏另有上限，而这段代码在 render 和 screen 里各有一份，
        #    两边必须算出同一个数（否则画面和点击区域会错开）。
        max_briefs=_layout.effective_max_briefs(
            cfg.board.max_briefs, cfg.display.landscape_max_briefs),
        max_todos=cfg.board.max_todos,
    )

    if lay.alert_y is not None and board.alert is not None:
        _draw_alert(d, board.alert, lay.alert_y, f)

    if lay.briefs is not None:
        z = lay.briefs
        _draw_label(d, "最近完成", z.label_y, f, z.overflow)
        for i, brief in enumerate(board.briefs[:z.shown]):
            _draw_brief(d, brief, z.y + i * z.item_h, z.item_h, f)

    if lay.todos is not None:
        z = lay.todos
        _draw_label(d, "待办", z.label_y, f, z.overflow)
        for i, todo in enumerate(board.todos[:z.shown]):
            ty = z.y + i * z.item_h
            _draw_todo(d, todo, ty, z.item_h, f)
            # 条间细线，**最后一条不画** —— 否则板块尾部会多出一条悬空的线
            if i < z.shown - 1:
                d.line([(MARGIN, ty + z.item_h - 6),
                        (MARGIN + CONTENT_W, ty + z.item_h - 6)],
                       fill=RULE, width=1)

    # 内容区终止线：让欠载时的留白读起来是「有意为之」，而不是「漏了东西」。
    # content_rule_y 为 None 表示**不画**（内容已填满，没有留白要解释）。
    if lay.content_rule_y is not None:
        d.line([(MARGIN, lay.content_rule_y), (MARGIN + CONTENT_W, lay.content_rule_y)],
               fill=RULE, width=1)
    if lay.ambient_y is not None:
        _draw_ambient(d, cfg, now, lay.ambient_y, f)

    if lay.status_y is not None:
        _draw_status(d, board, now, lay.status_y, f, health)

    return img, lay


def render_offline(cfg: Config, *, now: float, fonts: FontBook | None = None) -> Image.Image:
    """设备离线时显示的图。

    Kindle 端在首次成功同步时把它缓存到本地，之后连不上服务就 blit 这一张。
    这样「连接中断」这个提示**不需要在 Kindle 上画中文** ——
    FBInk 的内置字体没有 CJK，而服务端渲染任意中文都是免费的。
    """
    f = fonts or FontBook()
    img = Image.new("L", (CANVAS_W, CANVAS_H), WHITE)
    d = ImageDraw.Draw(img)

    cy = CANVAS_H / 2 - 40

    # 一个断开的链路符号：让"连接中断"有一个视觉锚点，
    # 而不是三行居中文字浮在空屏上（那读起来像渲染没画完）。
    sym_broken_link(d, CANVAS_W / 2, cy - 300, 150)

    t1 = "连接中断"
    d.text(((CANVAS_W - _tw(d, t1, f.get(96, True))) / 2, cy - 150),
           t1, font=f.get(96, True), fill=BLACK)

    t2 = "副屏服务暂时无法访问"
    d.text(((CANVAS_W - _tw(d, t2, f.get(42))) / 2, cy + 10),
           t2, font=f.get(42), fill=GRAY)

    t3 = "设备会自动重试，无需处理"
    d.text(((CANVAS_W - _tw(d, t3, f.get(32))) / 2, cy + 96),
           t3, font=f.get(32), fill=LGRAY)

    # 底部一条横贯细线，把提示区"关"在画面里 —— 和其他模式共用同一种收尾语汇
    d.line([(MARGIN, CANVAS_H - MARGIN - 72), (MARGIN + CONTENT_W,
                                               CANVAS_H - MARGIN - 72)],
           fill=RULE, width=1)
    t4 = format_date(now)
    d.text(((CANVAS_W - _tw(d, t4, f.get(30))) / 2, CANVAS_H - MARGIN - 46),
           t4, font=f.get(30), fill=LGRAY)
    return img


# ---------------------------------------------------------------- 详情页
#
# 看板上的字段是单行截断的（版面所限），详情页要显示**全文**，所以要折行 + 分页。
#
# 为什么分页而不是滚动：e-ink 上翻页比拖动自然得多，而且 KOReader 那边只需要
# 把点击坐标映射到服务端给的矩形上 —— 服务端说"这块是返回、那块是下一页"，
# 插件不需要知道任何排版细节。这跟看板的"服务端决定排版"是同一条原则。

DETAIL_HEADER_H = 96
# 页脚高度加大到 104：翻页按钮要**完整待在底部边距之上**，
# 不能侵占最下面那条留给「退出」按钮的带子（见 EXIT_ZONE_*）。
DETAIL_FOOTER_H = 104
DETAIL_MARGIN = 48

# ---------------------------------------------------------------- 退出按钮保留区
#
# ⚠️ 这是**服务端和插件之间的契约**，只能有一处定义。
#
# 踩过一次：插件自己算「退出」按钮的位置（贴屏幕底边往上长），
# 而服务端渲染详情页时也在底部画「‹ 上一页 / 下一页 ›」——
# 两边**各算各的**，结果完全叠在一起：
#
#     翻页按钮  y 1366 ~ 1432
#     退出按钮  y 1387 ~ 1442     ← 重叠 45px，两个都点不准
#
# 现在改成：**底部边距（MARGIN 那条，y ≥ CANVAS_H - MARGIN）
# 专属于退出按钮**，服务端画任何东西都不许进来。
# 位置由这里定义一次，通过 ``exit_zone`` 字段发给插件。
#
# 为什么放在「底部边距」而不是别处：
#   * 它是画布上唯一一块**服务端保证不画东西**的区域
#     （内容按 MARGIN 留白，这是全项目一致的约定）
#   * 右下角是手指最容易够到的位置之一
#   * 看板和详情页上都在同一个位置，用户不用重新找
#
# ⚠️ 尺寸必须**塞得进底部边距**：底部边距是 ``MARGIN = 40`` 高的那条带子
#    （y 从 ``CANVAS_H - MARGIN`` 到 ``CANVAS_H``）。
#
#    这里踩过两次：
#      1. 第一版给 36px 高，而插件那个带边框的按钮实际 42~49px ——
#         按钮底部**溢出到画布外**，用户看到"下边缘跑到页面外"
#      2. 改成 48px 后虽然装得下按钮，但**顶边侵入了翻页按钮的横条**
#         （40px 的边距装不下 48px 的东西）
#
#    所以尺寸要同时满足：够放按钮（插件侧会把它压到 ~28px）+
#    不越出边距。34px 是这两条的交点。
EXIT_ZONE_W = 152
EXIT_ZONE_H = 34
EXIT_ZONE_MARGIN = 40


def exit_zone_rect_at(w: int, h: int) -> list[int]:
    """按**指定画布尺寸**算退出按钮保留区。

    为什么需要它（而不是只有 ``exit_zone_rect()``）：
    ``exit_zone_rect()`` 读的是"当前画布" ``CANVAS_W/CANVAS_H``，
    而横屏渲染时那两个被设成了 1448×1072（排版尺寸）——
    用它算出来的矩形右边界是 1408，**超出交付坐标系（宽 1072）**。

    交付坐标系永远是设备物理尺寸，所以服务端要交付时用
    ``exit_zone_rect_at(DEVICE_W, DEVICE_H)``。
    """
    right = w - EXIT_ZONE_MARGIN
    bottom = h - 4               # 离物理底边 4px，别贴着屏幕边缘
    top = max(h - EXIT_ZONE_MARGIN, bottom - EXIT_ZONE_H)
    return [right - EXIT_ZONE_W, top, right, bottom]


def exit_zone_rect() -> list[int]:
    """「退出」按钮在当前画布里的矩形（排版坐标系）。

    ⚠️ 这个读的是**当前画布**。横屏渲染时会返回排版坐标系的值，
    交付给插件之前要用 :func:`exit_zone_rect_at` 按物理尺寸重算。

    严格落在**底部边距**内（y ∈ [CANVAS_H - MARGIN, CANVAS_H]），
    所以它既不会挤压内容，也不会和翻页按钮重叠 ——
    服务端保证不在这个矩形里画任何东西。

    这个值是**唯一真相**：服务端渲染时避开它，插件按它画。
    插件侧还会**夹紧**（见 Lua 里的 ``_clamp_rect``），
    即使控件比预期高也不会画出画布。
    """
    return exit_zone_rect_at(CANVAS_W, CANVAS_H)


def detail_footer_rect() -> list[int]:
    """详情页底部那排翻页按钮占的横条。

    自检用：它**必须完全在退出按钮保留区之上**，
    否则两个按钮又会重叠。
    """
    top = CANVAS_H - DETAIL_FOOTER_H
    return [DETAIL_MARGIN, top, CANVAS_W - DETAIL_MARGIN,
            CANVAS_H - EXIT_ZONE_MARGIN]


def exit_zone_is_clear() -> bool:
    """自检：退出按钮保留区和任何服务端绘制都不该相交。

    ``detail_footer_rect()`` 的下边界就是 ``CANVAS_H - EXIT_ZONE_MARGIN``，
    而退出区从 ``CANVAS_H - EXIT_ZONE_MARGIN`` 往下 —— 正好**首尾相接不重叠**。

    这条自检在测试里跑，防止以后有人调布局时又让它们撞上。
    """
    ez = exit_zone_rect()
    ft = detail_footer_rect()
    # 竖直方向不重叠：footer 的底 <= 退出区的顶
    return ft[3] <= ez[1]


def render_detail(brief: Brief, cfg: Config, *, now: float,
                  fonts: FontBook | None = None,
                  page: int = 0) -> tuple[Image.Image, list[dict], int, list[int]]:
    """渲染简报详情的一页。

    返回 ``(图像, 点击区域列表, 总页数, 退出按钮保留区)``。

    点击区域和退出区的坐标都是**图像坐标**，插件按显示缩放换算即可。

    ⚠️ 第四个返回值（``exit_zone``）是**服务端和插件的契约**：
    服务端保证不在那个矩形里画任何东西，插件按它画「退出」按钮。
    踩过一次两个按钮重叠的坑（两边各算各的位置），所以现在由服务端定义一次。
    """
    f = fonts or FontBook()
    img = Image.new("L", (CANVAS_W, CANVAS_H), WHITE)
    d = ImageDraw.Draw(img)

    left = DETAIL_MARGIN
    right = CANVAS_W - DETAIL_MARGIN
    width = right - left

    # ---- 顶部：返回按钮（固定，每页都有）
    back_rect = [left, 32, left + 196, 32 + 64]
    d.rounded_rectangle(back_rect, radius=14, outline=GRAY, width=2)
    # ⚠️ 用 _draw_button_label 而不是写死偏移 —— 否则文字会偏下（用户报过）
    _draw_button_label(d, back_rect, "‹ 返回", f.get(36), pad_left=26)

    # 「任务详情」标签：和返回按钮**同一套垂直居中**，只是颜色更浅
    label = "任务详情"
    label_f = f.get(32)
    label_x = right - _tw(d, label, label_f)
    _voff = _text_v_offset(d, label, label_f)
    _cy = back_rect[1] + (back_rect[3] - back_rect[1]) / 2
    d.text((label_x, _cy - _voff), label, font=label_f, fill=LGRAY)
    sym_size = 40
    sym_cx = label_x - 26 - sym_size / 2
    SYMBOLS.get(brief.status, sym_dot)(d, sym_cx, back_rect[1] + 32, sym_size)

    # 头部与正文之间用一条贯通的细线分开 —— 详情页信息密，
    # 没有这条线时标题区会和正文粘在一起。
    d.line([(left, DETAIL_HEADER_H + 18), (right, DETAIL_HEADER_H + 18)],
           fill=RULE, width=1)

    # ---- 内容区：把详情拆成一串"块"，再按高度流进各页
    #
    # 这里的关键是**保留段落结构**。第一版把所有文字丢给 wrap_px，
    # 而它会把换行拉平成空格 —— 于是简报里的分点在详情页上糊成一整坨，
    # 读起来非常累。现在按段渲染，段间留白，列表项加统一的项目符号。
    ops: list[tuple[str, object]] = []

    def para(text: str, size: int, color: int, *, bold=False,
             gap_after: int = 16, indent: int = 0, line_gap: int = 0) -> None:
        """渲染一段（自动折行）。``line_gap`` 是行距增量，默认按字号自适应。"""
        font = f.get(size, bold)
        lines = wrap_px(d, text, font, width - indent)
        if not lines:
            return                    # 空内容不要留下一个孤零零的间距
        # 行距按字号比例给：小字太松会散，大字太紧会挤
        lg = line_gap or max(6, round(size * 0.42))
        for line in lines:
            ops.append(("line", (line, font, color, indent, lg)))
        ops.append(("gap", gap_after))

    def blocks(text: str, size: int, color: int, *, bold=False,
               gap_after: int = 22, bullet: bool = False) -> None:
        """把带换行的文本按**段落**渲染（这是简报正文的主通道）。

        段内正常折行；段与段之间留白；``-`` / ``1.`` 开头的行加项目符号。
        """
        paras = wrap_paragraphs(text)
        if not paras:
            return
        font = f.get(size, bold)
        lg = max(6, round(size * 0.42))
        for i, p in enumerate(paras):
            is_item = bullet and _is_list_item(p)
            if is_item:
                p = _strip_list_marker(p)
            # 列表项缩进一格放符号，正文对齐到符号右边
            bullet_w = round(size * 1.05) if is_item else 0
            lines = wrap_px(d, p, font, width - bullet_w)
            if not lines:
                continue
            if is_item:
                ops.append(("line", ("· " + lines[0], font, color, 0, lg)))
                for rest in lines[1:]:
                    ops.append(("line", ("  " + rest, font, color, 0, lg)))
            else:
                for line in lines:
                    ops.append(("line", (line, font, color, 0, lg)))
            # 段间距：列表项之间小一点，独立段落之间大一点
            ops.append(("gap", 10 if is_item else gap_after))

    def section_label(text: str) -> None:
        """小节标题（摘要 / 产出物 / 元数据）。"""
        ops.append(("label", (text, f.get(28))))
        ops.append(("gap", 10))

    def rule() -> None:
        ops.append(("rule", None))
        ops.append(("gap", 18))

    status_label = {"ok": "完成", "partial": "有遗留",
                    "failed": "失败", "info": "信息"}.get(brief.status, brief.status)

    # ---- 标题区
    #
    # 标题从 52 降到 44：52 时一个长标题能吃掉五行，把正文挤到下一页。
    # 详情页的价值在"读完内容"，标题只要醒目就够了。
    para(brief.title, 44, BLACK, bold=True, gap_after=6)

    meta_bits = [x for x in (status_label,
                             format_clock(brief.ts),
                             brief.project) if x]
    if brief.duration_s:
        mins = round(brief.duration_s / 60)
        meta_bits.append(f"{mins} 分钟" if mins >= 1 else "<1 分钟")
    para(" · ".join(meta_bits), 30, GRAY, gap_after=4)
    rule()

    # ⚠️ 这里**不再画 display_warnings()**。
    #
    # 那是"看板上会被截断"的提示 —— 只对**看板**有意义，
    # 而详情页显示的是全文，根本没有截断这回事。
    # 摆在这里只会让人以为"这条简报有问题"，纯属噪音。
    # （看板上仍然保留这个提示，那里才是它该出现的地方。）

    if brief.summary:
        section_label("摘要")
        blocks(brief.summary, 38, BLACK, gap_after=24)

    if brief.artifacts:
        section_label("产出物")
        for art in brief.artifacts:
            label = str(art.get("label") or art.get("ref") or "").strip()
            if not label:
                continue
            # 产出物常是长路径/URL —— 小一号字，够看清又不爆行
            blocks(label, 30, BLACK, gap_after=8, bullet=True)
        ops.append(("gap", 12))

    section_label("元数据")
    meta_lines = [f"简报 ID　{brief.id}"]
    if brief.duration_s:
        mins = round(brief.duration_s / 60)
        meta_lines.append(f"耗时　　　{mins} 分钟" if mins >= 1 else "耗时　　　<1 分钟")
    meta_lines.append(f"创建　　　{format_date(brief.ts)} {format_clock(brief.ts)}")
    for line in meta_lines:
        para(line, 28, GRAY, gap_after=6)

    # ---- 分页：把 op 流按可用高度切页
    body_top = DETAIL_HEADER_H + 52
    body_bottom = CANVAS_H - DETAIL_FOOTER_H - 24
    body_h = body_bottom - body_top

    def op_height(kind: str, payload) -> int:
        """一个 op 占多高 —— 分页和绘制**必须用同一套算法**。

        分开算的话，分页认为放得下而实际画出去，或者反过来留下大片空白。
        """
        if kind == "line":
            _text, font, _color, _indent, line_gap = payload
            return font.size + line_gap
        if kind == "label":
            _text, font = payload
            return font.size + 10          # 小标题下留一点空
        if kind == "gap":
            return int(payload)
        return 2                            # rule

    pages: list[list[tuple]] = [[]]
    used = 0
    for kind, payload in ops:
        h = op_height(kind, payload)
        if used + h > body_h and pages[-1]:
            pages.append([])
            used = 0
        pages[-1].append((kind, payload))
        used += h

    total = max(1, len(pages))
    page = max(0, min(page, total - 1))

    # ---- 画当前页（用和分页同一个 op_height）
    y = body_top
    for kind, payload in pages[page]:
        if kind == "line":
            text, font, color, indent, _line_gap = payload
            d.text((left + indent, y), text, font=font, fill=color)
        elif kind == "label":
            text, font = payload
            d.text((left, y), text, font=font, fill=LGRAY)
        elif kind == "rule":
            d.line([(left, y), (right, y)], fill=RULE, width=1)
        # "gap" 不画东西，只是占位
        y += op_height(kind, payload)

    # ---- 底部：翻页（只有多页时才画）
    taps: list[dict] = [{"kind": "back", "rect": back_rect}]

    # 退出按钮的保留区 —— 一并返回，让插件按**同一个矩形**画按钮。
    # 这样两边不可能再各算各的（那正是重叠的原因）。
    exit_zone = exit_zone_rect()

    if total > 1:
        # ⚠️ 翻页按钮必须**完整待在底部边距之上**，不许侵占退出按钮的保留区。
        #
        # 踩过一次：翻页按钮原来贴到 y=1432，而退出按钮在 y 1387~1442 ——
        # 重叠 45px，两个都点不准。现在 DETAIL_FOOTER_H 加大到 104，
        # 并且这里显式以上边界为准来算位置。
        #
        # ⚠️⚠️ 另一个坑：taps 里的 ``page`` **必须和 URL 参数同一个基准**。
        #
        # 原来这里给的是 **0 基准**（``page - 1`` / ``page + 1``），
        # 而插件的 ``self.page`` 是 **1 基准**（URL 里就是 ``page=1`` 表示第一页）。
        # 于是插件把 0 基准的值当 1 基准发回去：
        #
        #     服务端返回 {"page": 1}（0 基准 → 第 2 页）
        #     插件 self.page = 1，请求 ?page=1
        #     服务端按 1 基准解析 → 又是第 1 页   ← 永远翻不动
        #
        # 症状是「点下一页没反应」。现在统一成 **1 基准**，
        # 插件拿着就能直接用（不用做任何加减）。
        btn_h = 66
        btn_top = CANVAS_H - EXIT_ZONE_MARGIN - 8 - btn_h   # 再留 8px 间隙
        prev_rect = [left, btn_top, left + 200, btn_top + btn_h]
        next_rect = [right - 200, btn_top, right, btn_top + btn_h]
        label = f"{page + 1} / {total}"

        if page > 0:
            d.rounded_rectangle(prev_rect, radius=12, outline=GRAY, width=2)
            _draw_button_label(d, prev_rect, "‹ 上一页", f.get(34))
            # 1 基准：第 N 页就是 N
            taps.append({"kind": "page", "page": page, "rect": prev_rect})
        if page < total - 1:
            d.rounded_rectangle(next_rect, radius=12, outline=GRAY, width=2)
            _draw_button_label(d, next_rect, "下一页 ›", f.get(34))
            taps.append({"kind": "page", "page": page + 2, "rect": next_rect})

        # 页码标签也垂直居中（原来也是写死 +18，同样偏下）
        _lf = f.get(32)
        _lvoff = _text_v_offset(d, label, _lf)
        _lcy = prev_rect[1] + (prev_rect[3] - prev_rect[1]) / 2
        d.text(((CANVAS_W - _tw(d, label, _lf)) / 2, _lcy - _lvoff),
               label, font=_lf, fill=LGRAY)

    return img, taps, total, exit_zone


def quantize16(img: Image.Image) -> Image.Image:
    """预量化到设备的 16 灰阶调色板。

    我们的调色板本来就在网格上，所以这是**幂等**的；好处是 PNG 体积减半
    （实测 97KB → 65KB），且 FBInk 的软件抖动变成空操作。
    """
    return img.point(lambda v: round(v / 255 * 15) * 17)


def encode_png(img: Image.Image) -> bytes:
    """编码为设备端要用的 PNG（预量化 + 优化压缩）。"""
    import io
    buf = io.BytesIO()
    quantize16(img).save(buf, format="PNG", optimize=True)
    return buf.getvalue()
