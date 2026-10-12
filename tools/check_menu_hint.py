"""用设备上真实的 menusorter.lua 逻辑验证插件的菜单项能正确挂载。

为什么要做这个
--------------
``menusorter.lua`` 里这段**没有兜底**：

    if sorting_hint then
        local m = self:findById(menu_table["KOMenu:menu_buttons"], sorting_hint)
        m = m.sub_item_table or m          -- ← findById 返回 nil 时，这里就崩
        table.insert(m, v)
    end

``sorting_hint`` 指向不存在的菜单 id → **打开菜单就崩**（不是"菜单项不显示"那么温和）。

所以 ``sorting_hint`` 的取值必须对着**设备那一版真实的菜单顺序**核对，
而不是照文档猜。这个脚本把设备上的菜单顺序解析出来，
再检查插件用的 id 在不在里面。
"""

from __future__ import annotations

import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PLUGIN = r"C:\workspace\kindlewarden\koplugin\kindlewarden.koplugin\main.lua"


def find_kindle() -> str | None:
    for letter in "CDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        try:
            entries = set(os.listdir(root))
        except OSError:
            continue
        if {"documents", "system"} <= entries:
            return root
    return None


def collect_menu_ids(ko: str) -> set[str]:
    """从设备上的菜单顺序文件里解析出所有合法的菜单 id。

    文件形如::

        local order = {
            ["KOMenu:menu_buttons"] = {
                "navi", "typeset", "setting", "tools", "search",
                "filemanager", "main",
            },
            navi = { ... },          -- 每个顶级菜单自己的子项
            ...
        }

    所以合法 id 有两类：
      * ``KOMenu:menu_buttons`` 那张表里的 —— **顶级菜单**（sorting_hint 用这些）
      * 其余作为 key 出现的 —— 各个顶级菜单自己（同样可作 sorting_hint）
    """
    ids: set[str] = set()
    for rel in ("frontend/ui/elements/reader_menu_order.lua",
                "frontend/ui/elements/filemanager_menu_order.lua"):
        p = os.path.join(ko, *rel.split("/"))
        if not os.path.exists(p):
            continue
        src = open(p, encoding="utf-8", errors="replace").read()

        # 1. KOMenu:menu_buttons 那张表 —— 真正的顶级菜单清单
        m = re.search(r'\["KOMenu:menu_buttons"\]\s*=\s*\{(.*?)\}', src, re.S)
        if m:
            for name in re.findall(r'"([A-Za-z_][A-Za-z0-9_:]*)"', m.group(1)):
                ids.add(name)

        # 2. 其余 `name = {` 形式的 key（顶级菜单自身的定义）
        for name in re.findall(r'(?m)^\s{4}([a-z_][a-z0-9_]*)\s*=\s*\{', src):
            ids.add(name)

    return ids


def main() -> int:
    kindle = find_kindle()
    if kindle is None:
        print("Kindle 没插着 —— 跳过（这条检查需要设备上的菜单顺序文件）")
        return 0

    ko = os.path.join(kindle, "koreader")
    valid = collect_menu_ids(ko)
    if not valid:
        print("没能解析出菜单 id —— 菜单顺序文件的格式变了吗？")
        return 2

    print(f"设备上合法的菜单 id（{len(valid)} 个）：")
    for name in sorted(valid):
        print(f"  {name}")
    print()

    src = open(PLUGIN, encoding="utf-8").read()
    hints = re.findall(r'sorting_hint\s*=\s*"([^"]+)"', src)
    print(f"插件里用到的 sorting_hint：{hints or '（无）'}")
    print()

    bad = 0
    for hint in hints:
        ok = hint in valid
        if not ok:
            bad += 1
        print(f"  {'OK ' if ok else 'BAD'} {hint}")

    if bad:
        print()
        print(f"⚠ {bad} 个 sorting_hint 指向不存在的菜单 id —— "
              "打开 KOReader 菜单会直接崩（menusorter 那段没有兜底）")
        print(f"   可用的：{sorted(valid)}")
        return 1

    print()
    print("结论：全部 sorting_hint 都指向真实存在的菜单，不会崩")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
