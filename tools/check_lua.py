"""检查 KOReader 插件的 Lua 语法。

**为什么需要这个**：KOReader 用 LuaJIT（Lua 5.1 语法）。插件里少一个 ``end``、
括号不配对，KOReader 就加载不了 —— 而你在设备上看到的现象只是
「菜单里没有这一项」，完全无从排查。设备上也没有 lua 解释器。

所以在 host 上用 Python 的 luaparser 先解析一遍。它能抓语法错误，
但抓不到运行时错误（那只能上机）。

用法：
    python tools/check_lua.py [插件路径]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT = (Path(__file__).resolve().parent.parent
           / "koplugin" / "kindlewarden.koplugin" / "main.lua")


def strip_comments(src: str) -> str:
    """去掉 Lua 注释，只留代码。

    **凡是要检查"源码里有没有某种写法"的地方，都必须先过这一层。**

    踩过三次同一个坑：检查「不许出现 ``scale_factor = 0``」、
    「不许调用 ``getCacheDir``」这类规则时，注释里往往正好在解释
    **为什么不那么写**，于是注释把规则本身判成了违规。

    这里是唯一实现 —— 测试（tests/test_koplugin_lua.py）和
    tools/check_lua.py 都 import 它，免得几份各自漂移。
    """
    src = re.sub(r"--\[\[.*?\]\]", "", src, flags=re.S)   # 长注释
    return re.sub(r"--[^\n]*", "", src)                    # 行注释


def check_definition_order(src: str) -> list[str]:
    """检查 ``function Xxx:method()`` 是否都排在 ``local Xxx = ...`` 之后。

    ⚠️ **语法检查抓不到这类错误**，但它会让插件彻底加载失败。

    实测踩过：把两个方法插到了类定义**之前**，KOReader 报

        main.lua:156: attempt to index global 'WardenScreen' (a nil value)

    Lua 逐行执行，那条 ``function WardenScreen:xxx`` 执行时
    ``WardenScreen`` 还是 nil。后果是 **KOReader 直接丢弃整个插件** ——
    菜单项凭空消失，而且完全看不出是"定义顺序"的问题。

    返回问题描述列表，空列表表示没问题。
    """
    lines = src.splitlines()
    problems: list[str] = []

    # 收集每个类的定义行号：local Xxx = ...:extend{  或  local Xxx = {
    class_def: dict[str, int] = {}
    for i, line in enumerate(lines):
        m = re.match(r"^local\s+(\w+)\s*=\s*\w+:extend\s*\{", line)
        if not m:
            m = re.match(r"^local\s+(\w+)\s*=\s*\{", line)
        if m:
            class_def.setdefault(m.group(1), i)

    for i, line in enumerate(lines):
        m = re.match(r"^function\s+(\w+):(\w+)\s*\(", line)
        if not m:
            continue
        cls, method = m.group(1), m.group(2)
        if cls not in class_def:
            continue                      # 不是本文件定义的类，跳过
        if i < class_def[cls]:
            problems.append(
                f"第 {i + 1} 行 `function {cls}:{method}` 排在 "
                f"第 {class_def[cls] + 1} 行 `local {cls} = ...` **之前**："
                f"加载时会 attempt to index global '{cls}' (a nil value)，"
                f"整个插件会被 KOReader 丢弃"
            )

    return problems


def check_duplicate_methods(src: str) -> list[str]:
    """检查有没有**重复定义的方法**。

    Lua 里重复定义**不报错** —— 后面的静默覆盖前面的。
    这个特性很危险：你以为改好了某个函数，但文件后面还有一个旧版本，
    于是**改动静默失效**，而且完全看不出原因。

    实测踩过：给 ``_paint_all`` 加了诊断逻辑，但文件里还留着旧的一份，
    新版被覆盖回去，加的东西等于没加。

    返回问题描述列表，空列表表示没问题。
    """
    lines = src.splitlines()
    seen: dict[str, int] = {}
    problems: list[str] = []

    for i, line in enumerate(lines):
        m = re.match(r"^(?:local\s+)?function\s+(\w+)[:.](\w+)\s*\(", line)
        if not m:
            continue
        key = f"{m.group(1)}:{m.group(2)}"
        if key in seen:
            problems.append(
                f"`{key}` 被重复定义：第 {seen[key] + 1} 行和第 {i + 1} 行。"
                f"Lua 里后面的会**静默覆盖**前面的 —— "
                f"如果改的是前面那个，改动静默失效"
            )
        else:
            seen[key] = i

    return problems


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    path = Path(argv[1]) if len(argv) > 1 else DEFAULT
    if not path.exists():
        print(f"找不到：{path}")
        return 2

    src = path.read_text(encoding="utf-8")
    print(f"文件: {path}")
    print(f"大小: {len(src)} 字符 / {len(src.splitlines())} 行")
    print()

    try:
        from luaparser import ast
    except ImportError:
        print("需要 luaparser：pip install luaparser")
        return 2

    try:
        tree = ast.parse(src)
    except Exception as exc:                      # noqa: BLE001
        print("✗ 语法错误：")
        print(f"  {type(exc).__name__}: {exc}")
        return 1

    print("✓ 语法解析通过")

    # ⚠️ 语法通过**不代表能用** —— 定义顺序错了照样让插件加载失败，
    #    而那种错误 Lua 解析器是看不出来的（它是运行时的作用域问题）。
    order_problems = check_definition_order(src)
    if order_problems:
        print()
        print("✗ 定义顺序有问题（语法没错，但 KOReader 加载时会失败）：")
        for p in order_problems:
            print(f"  - {p}")
        return 1
    print("✓ 定义顺序正确（所有方法都在类定义之后）")

    # 重复定义同样语法合法，但会让"改动静默失效"
    dup_problems = check_duplicate_methods(src)
    if dup_problems:
        print()
        print("✗ 有重复定义的方法（后面的会静默覆盖前面的）：")
        for p in dup_problems:
            print(f"  - {p}")
        return 1
    print("✓ 没有重复定义的方法")

    stats: dict[str, int] = {}
    funcs: list[str] = []
    for node in ast.walk(tree):
        stats[type(node).__name__] = stats.get(type(node).__name__, 0) + 1

        if isinstance(node, (ast.Function, ast.Method)):
            name = getattr(node, "name", None)
            if isinstance(name, ast.Index):
                parts = []
                cur = name
                while isinstance(cur, ast.Index):
                    parts.append(getattr(cur.idx, "id", "?"))
                    cur = cur.value
                parts.append(getattr(cur, "id", "?"))
                funcs.append(".".join(reversed(parts)))
            elif isinstance(name, ast.Name):
                funcs.append(name.id)

    print()
    print("=== 结构统计（确认不是解析成空壳）===")
    for key in ("Function", "Method", "LocalAssign", "Assign", "Call",
                "Table", "If", "Forin", "Fornum", "While"):
        if stats.get(key):
            print(f"  {key:14} {stats[key]}")

    print()
    print("=== 定义的函数 ===")
    for name in funcs:
        print(f"  {name}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
