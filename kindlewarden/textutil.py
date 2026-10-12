"""文本宽度度量与截断。

副屏上真正的长度约束是**像素**，不是字符数 —— 中文全角、英文半角、
数字的宽度都不同。但模型层需要一个人能理解的近似度量，所以这里提供
「显示宽度」：东亚全角字 = 1.0，其余 = 0.5。

它与渲染器用的像素测量保持单调一致，因此可以放心用来做模型层的软约束。
"""

from __future__ import annotations

import unicodedata

_WIDE = ("W", "F")


def char_width(ch: str) -> float:
    """单字符的显示宽度：全角 1.0，半角 0.5。"""
    return 1.0 if unicodedata.east_asian_width(ch) in _WIDE else 0.5


def display_width(s: str) -> float:
    """字符串的显示宽度（以「一个汉字」为 1.0 单位）。"""
    return sum(char_width(c) for c in s)


def fit_display(s: str, limit: float) -> tuple[str, bool]:
    """按显示宽度截断。

    返回 ``(结果, 是否被截断)``。截断时追加省略号，并为其预留 1.0 宽度。
    注意这里用显示宽度近似像素截断；渲染器仍会做一次精确的像素级截断兜底。
    """
    s = (s or "").strip()
    if display_width(s) <= limit:
        return s, False

    out: list[str] = []
    used = 0.0
    budget = limit - 1.0
    for ch in s:
        w = char_width(ch)
        if used + w > budget:
            break
        out.append(ch)
        used += w
    return "".join(out).rstrip() + "…", True


def one_line(s: str) -> str:
    """压成单行：把换行/制表符折叠为空格，压缩连续空白。

    简报是 agent 生成的自由文本，换行几乎必然出现；而副屏上每个字段
    都是单行显示，不处理就会出现「看不见的换行把版面撑坏」。

    ⚠️ 只用于**真正的单行字段**（title / project / tags / todo.text）。
       正文（summary）要保留段落结构，用 :func:`multi_line`。
    """
    return " ".join((s or "").split())


def multi_line(s: str) -> str:
    """清理多行文本：**保留段落结构**，但去掉会破坏版面的脏东西。

    和 :func:`one_line` 的区别就是"换行算不算结构"：

    * ``one_line``    —— 换行是噪音，压成空格（给单行字段用）
    * ``multi_line``  —— 换行是**结构**，保留（给正文用）

    具体做四件事：

    1. 统一换行符（``\\r\\n`` / ``\\r`` → ``\\n``），去掉不可见控制字符
    2. 每行去首尾空白；行内的连续空白压成一个空格
    3. 压掉**连续的空行**（最多留一个）—— agent 常留一堆空行，
       不处理会在详情页上留出大片莫名其妙的空白
    4. 去掉首尾空行

    为什么要专门做这件事：踩过一次 —— summary 和 title 一样走 ``one_line``，
    于是 agent 分的段落在**入库那一刻就没了**，详情页渲染得再对也显示不出层次。
    只看看板还发现不了（看板本来就是单行截断）。
    """
    import re as _re

    text = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    # 去掉除 \n 和 \t 以外的控制字符（\t 随后会被当空白压掉）
    text = "".join(ch for ch in text
                   if ch == "\n" or ch == "\t" or ord(ch) >= 32)

    out: list[str] = []
    blank_run = 0
    for raw in text.split("\n"):
        line = _re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if not line:
            blank_run += 1
            # 连续空行最多保留一个 —— 空行是段落分隔，不需要堆叠
            if blank_run == 1 and out:
                out.append("")
            continue
        blank_run = 0
        out.append(line)

    # 去掉尾部空行
    while out and not out[-1]:
        out.pop()

    return "\n".join(out)


def cap_chars(s: str, limit: int) -> tuple[str, bool]:
    """按**字符数**封顶，用于存储防滥用。

    注意这跟 :func:`fit_display` 是两回事：
    ``fit_display`` 是**显示**约束（按视觉宽度，让内容在板上放得下），
    这里只是防止有人往事件日志里灌一篇论文。
    存储保留全量，显示截断由渲染器按像素决定 —— 这样管理页才看得到完整内容。
    """
    s = s or ""
    if len(s) <= limit:
        return s, False
    return s[:limit].rstrip() + "…", True
