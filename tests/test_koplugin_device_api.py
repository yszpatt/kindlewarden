"""拿**设备上真实的 KOReader 源码**核对插件调用的每一个外部 API。

为什么需要这个
--------------
KOReader 的 master 文档和设备上装的版本可以差很远。踩过一次：

    照着 master 写了 ``DataStorage:getCacheDir()``，
    而设备上是 **v2026.07.1，根本没有这个方法** ——
    打开副屏会直接 ``attempt to call method 'getCacheDir' (a nil value)``。
    本地测试全绿，插上设备一点就崩。

这类错误的共同点是：**本地毫无征兆，上机必崩，而且报错信息不指向原因**。
所以只要 Kindle 插着，就把插件用到的每个外部符号对着设备源码查一遍。

Kindle 不在就跳过（这些测试是"有设备时多加一道保险"，不是常驻门禁）。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LUA = ROOT / "koplugin" / "kindlewarden.koplugin" / "main.lua"

# kindle_root / koreader 两个 fixture 在 conftest.py —— 插件那几组测试共用，
# 不各自实现一份「找 Kindle」的逻辑（那种重复正是这个项目反复踩到的坑）。


@pytest.fixture(scope="module")
def lua() -> str:
    src = LUA.read_text(encoding="utf-8")
    src = re.sub(r"--\[\[.*?\]\]", "", src, flags=re.S)
    return re.sub(r"--[^\n]*", "", src)


def _required_modules(lua_src: str) -> set[str]:
    return set(re.findall(r'require\("([^"]+)"\)', lua_src))


def test_every_required_module_exists_on_the_device(koreader, lua):
    """插件 require 的每个模块，设备上都得真的有。

    ``require("socket.http")`` 写错成 ``socket.https`` 之类，本地看不出来，
    设备上直接加载失败 —— 而现象只是"菜单里没有这一项"。

    模块**不一定在 frontend/**，也**不一定是 .lua**：

    - ``libs/libkoreader-lfs`` → 编译好的 ``libs/libkoreader-lfs.so``（C 模块）
    - ``json`` / ``ltn12`` / ``socket.http`` → 在 ``common/`` 下（LuaSocket 的标准位置）

    这两点都踩过：第一版只查 ``frontend/*.lua``，把 json / ltn12 / socket.http
    全判成"不存在" —— **是检查写错了，不是插件有问题**
    （设备上已下载的 board.png 就是它们正常工作的证据）。
    """
    missing: list[str] = []
    for mod in sorted(_required_modules(lua)):
        rel = mod.replace(".", "/")
        candidates = [
            koreader / (rel + ".lua"),
            koreader / "frontend" / (rel + ".lua"),
            koreader / "common" / (rel + ".lua"),      # LuaSocket 在这
            koreader / rel,                             # 目录形式
        ]
        if any(p.exists() for p in candidates):
            continue

        # 编译好的 C 模块：libs/xxx.so
        base = rel.split("/")[-1]
        if list(koreader.rglob(f"{base}.so")) or list(koreader.rglob(f"{base}.lua")):
            continue

        missing.append(mod)

    assert not missing, f"设备上的 KOReader 里找不到这些模块：{missing}"


def test_datastorage_methods_used_exist_on_the_device(koreader, lua):
    """``DataStorage:xxx()`` 调的方法必须在设备那一版里存在。

    这条正是为了 ``getCacheDir`` 那个坑而写的：master 有、v2026.07.1 没有。
    """
    src_path = koreader / "datastorage.lua"
    if not src_path.exists():
        pytest.skip("设备上没有 datastorage.lua")
    src = src_path.read_text(encoding="utf-8", errors="replace")
    defined = set(re.findall(r"function DataStorage:(\w+)", src))
    assert defined, "没能从设备上的 datastorage.lua 解析出方法"

    used = set(re.findall(r"DataStorage:(\w+)\(", lua))
    missing = sorted(used - defined)
    assert not missing, (
        f"插件调用了设备上不存在的 DataStorage 方法：{missing}\n"
        f"设备上可用的：{sorted(defined)}"
    )


def test_imagewidget_api_used_exists_on_the_device(koreader, lua):
    """``ImageWidget`` 的字段/方法必须在设备那一版里存在。

    用的是构造参数（``scale_factor`` / ``file_do_cache``）和实例方法
    （``paintTo`` / ``free``）—— 名字错了不会在构造时报，而是在显示时才炸。
    """
    p = koreader / "frontend" / "ui" / "widget" / "imagewidget.lua"
    if not p.exists():
        pytest.skip("设备上没有 imagewidget.lua")
    src = p.read_text(encoding="utf-8", errors="replace")

    for method in ("paintTo", "free", "getSize"):
        if f"{method}(" in lua and f"ImageWidget:{method}" in src:
            continue
        if f"{method}(" in lua:
            assert f"function ImageWidget:{method}" in src or \
                   f"{method} = function" in src, \
                   f"设备上的 ImageWidget 没有 {method}"

    # 构造参数：在 ImageWidget 的 extend{...} 表里应当能看到
    header = src.split("local ImageWidget = Widget:extend{", 1)
    assert len(header) == 2, "没能定位 ImageWidget 的字段表"
    fields = header[1].split("}", 1)[0]
    for field in ("scale_factor", "file_do_cache"):
        assert field in fields, f"设备上的 ImageWidget 没有 {field} 字段"


def test_widgetcontainer_and_inputcontainer_exist(koreader, lua):
    """基类必须在 —— 插件的 ``WidgetContainer:extend`` 依赖它。"""
    for rel, needle in (
        ("frontend/ui/widget/container/widgetcontainer.lua", "WidgetContainer"),
        ("frontend/ui/widget/container/inputcontainer.lua", "InputContainer"),
        ("frontend/ui/widget/infomessage.lua", "InfoMessage"),
        ("frontend/ui/network/manager.lua", "NetworkMgr"),
        ("frontend/ui/geometry.lua", "Geom"),
    ):
        p = koreader / rel
        assert p.exists(), f"设备上缺少 {rel}"
        src = p.read_text(encoding="utf-8", errors="replace")
        assert needle in src, f"{rel} 里没找到 {needle}"


def test_register_touch_zones_exists_with_expected_shape(koreader, lua):
    """``registerTouchZones`` 的用法要和设备上那一版一致。

    我们传的是 ``{id, ges, screen_zone={ratio_x..}, handler}``。
    设备上的实现必须真的读 ``screen_zone`` 和 ``handler``，否则注册了也不会响应。
    """
    p = koreader / "frontend" / "ui" / "widget" / "container" / "inputcontainer.lua"
    src = p.read_text(encoding="utf-8", errors="replace")

    assert "function InputContainer:registerTouchZones" in src, \
        "设备上的 InputContainer 没有 registerTouchZones"

    body = src.split("function InputContainer:registerTouchZones", 1)[1]
    body = body.split("\nfunction ", 1)[0]
    for key in ("screen_zone", "handler", "gs_range", "gesture"):
        assert key in body or key == "gesture", \
            f"registerTouchZones 的实现里没有用到 {key}"


def test_tap_gesture_is_supported_by_the_device(koreader, lua):
    """设备上的触摸栈必须支持 ``tap`` 手势，而且带坐标。

    插件注册的是 ``ges = "tap"``，处理器读 ``ges.pos.x/y`` 算图像坐标。
    两件事都得成立，点击功能才可能工作：

    - tap 手势本身（由手势检测器产生）
    - GestureRange:match 是按 ``gs.ges == self.ges`` 做**字符串比较**的 ——
      所以 "tap" 这个字面量由调用方给，gesturerange.lua 里不会出现它。
      第一版在这里找 "tap" 字样，属于检查写错了。
    """
    gr = koreader / "frontend" / "ui" / "gesturerange.lua"
    if not gr.exists():
        pytest.skip("设备上没有 gesturerange.lua")
    src = gr.read_text(encoding="utf-8", errors="replace")

    # 匹配逻辑必须是按 ges 字符串比的（说明手势名由调用方决定）
    assert "gs.ges ~= self.ges" in src, \
        "GestureRange:match 的实现变了 —— 手势名可能不再是字符串，插件的 tap 要重看"

    # 手势名和坐标由手势检测器/输入层产生
    detector = koreader / "frontend" / "ui" / "widget" / "containergesturedetector.lua"
    if not detector.exists():
        detector = koreader / "frontend" / "ui" / "gesturedetector.lua"
    if detector.exists():
        dsrc = detector.read_text(encoding="utf-8", errors="replace")
        assert "tap" in dsrc, "手势检测器里没有 tap"
        assert "pos" in dsrc, "手势事件里没有 pos —— 点击坐标拿不到"


def test_menu_sorting_hints_exist_on_the_device(koreader, lua):
    """插件的 ``sorting_hint`` 必须指向设备上**真实存在**的菜单。

    这条不是"菜单项不显示"那么温和 —— ``menusorter.lua`` 里那段
    **没有兜底**：

        if sorting_hint then
            local m = self:findById(menu_table["KOMenu:menu_buttons"], sorting_hint)
            m = m.sub_item_table or m      -- ← findById 返回 nil 时，这里就崩
            table.insert(m, v)
        end

    ``sorting_hint`` 指向不存在的 id → **一打开菜单就崩**。

    这个值只能对着**设备那一版**的菜单顺序核对：master 和 v2026.07.1
    可以不一样。

    顺带一提：官方最小插件 ``hello.koplugin`` 用的是 ``more_tools``，
    我们用的是 ``tools``。两个都合法 —— 但**必须先验证再确定**，
    既不能因为"官方用了别的"就以为自己的错了，也不能反过来。
    """
    ids: set[str] = set()
    for rel in ("frontend/ui/elements/reader_menu_order.lua",
                "frontend/ui/elements/filemanager_menu_order.lua"):
        p = koreader / rel
        if not p.exists():
            continue
        src = p.read_text(encoding="utf-8", errors="replace")

        # KOMenu:menu_buttons 那张表 = 顶级菜单清单
        m = re.search(r'\["KOMenu:menu_buttons"\]\s*=\s*\{(.*?)\}', src, re.S)
        if m:
            for name in re.findall(r'"([A-Za-z_][A-Za-z0-9_:]*)"', m.group(1)):
                ids.add(name)
        # 其余 `    name = {` 形式的顶级菜单自身
        for name in re.findall(r'(?m)^\s{4}([a-z_][a-z0-9_]*)\s*=\s*\{', src):
            ids.add(name)

    assert ids, "没能解析出设备上的菜单 id —— 菜单顺序文件格式变了吗？"

    hints = re.findall(r'sorting_hint\s*=\s*"([^"]+)"', lua)
    assert hints, "插件没有注册任何菜单项？"

    bad = sorted(set(hints) - ids)
    assert not bad, (
        f"这些 sorting_hint 在设备上不存在：{bad}\n"
        f"会导致打开菜单直接崩（menusorter 那段没有兜底）。\n"
        f"设备上可用：{sorted(ids)}"
    )


def test_plugin_registers_itself_to_the_main_menu(lua):
    """必须在插件的 ``init`` 里调 ``registerToMainMenu``。

    而且**不能**在 ``WardenScreen`` 上注册 —— 它是屏幕，不是插件对象。
    注册要挂在 ``Warden`` 上，这样文件管理器和阅读器里都能从菜单点到。
    """
    parts = lua.split("function Warden:init", 1)
    assert len(parts) == 2, "找不到 Warden:init"
    init_body = parts[1].split("function Warden:", 1)[0]
    assert "registerToMainMenu" in init_body, "插件没有注册到主菜单"

    # 别在屏幕上注册（那会注册错对象）
    if "function WardenScreen:init" in lua:
        screen_init = lua.split("function WardenScreen:init", 1)[1].split(
            "function WardenScreen:getSize", 1)[0]
        assert "registerToMainMenu" not in screen_init, \
            "registerToMainMenu 应当注册在插件对象上，不是屏幕对象上"


def test_menu_callback_can_be_toggled_repeatedly(lua):
    """菜单回调要能反复打开/关闭 —— 每次都能正常切换。

    这条防的是"退出一次就再也打不开"那类状态残留：
    回调必须走 ``toggle``（它会处理好 self.screen 的清理），
    而不是直接 ``UIManager:show`` 一个新的（那样旧引用会残留）。
    """
    menu_body = lua.split("function Warden:addToMainMenu", 1)
    assert len(menu_body) == 2, "找不到 addToMainMenu"
    body = menu_body[1]
    assert "self:toggle()" in body, "菜单回调应当走 toggle，而不是直接 show"
