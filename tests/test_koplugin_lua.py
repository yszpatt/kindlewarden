"""KOReader 插件（Lua）与服务端之间的跨语言契约。

Lua 在这台机器上跑不起来（没有 lua 解释器），插件只能在设备上验证。
但**它依赖的服务端契约可以在这里测死** —— 而跨语言的地方恰恰最容易悄悄坏掉：

  - 插件里写的端点路径，服务端真的有吗？（写错就是设备上 404，本地毫无察觉）
  - 插件假定的画布尺寸，和服务端布局引擎一致吗？（不一致 → 点击全部偏移）
  - 插件有没有避开那几个已知会崩/算错的坑？

这些都是纯静态检查，但它们拦住的都是"只有插上设备才发现"的问题。

fixture（``lua`` / ``koreader``）在 conftest.py，和另外两组插件测试共用。

三组插件测试的分工：

| 文件 | 管什么 |
|---|---|
| ``test_koplugin_lua.py`` | 插件 ↔ 服务端：端点、画布尺寸、打包/部署映射 |
| ``test_koplugin_framework_contract.py`` | 插件 ↔ KOReader 框架：类系统语义、leaf widget 的绘制、退出清理 |
| ``test_koplugin_device_api.py`` | 对着**设备上真实源码**核对每个外部符号（Kindle 不在则 skip） |
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from kindlewarden.layout import CANVAS_H, CANVAS_W

ROOT = Path(__file__).resolve().parent.parent
LUA_FILE = ROOT / "koplugin" / "kindlewarden.koplugin" / "main.lua"
SERVER = ROOT / "kindlewarden" / "server.py"


@pytest.fixture(scope="module")
def server_src() -> str:
    return SERVER.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 端点契约


def _server_routes(src: str) -> set[str]:
    """从 server.py 里抽出所有被路由的路径字面量。"""
    routes: set[str] = set()
    # 形如： if path in ("/rev", "/api/rev"):
    for group in re.findall(r"path in \(([^)]*)\)", src):
        routes |= set(re.findall(r'"([^"]+)"', group))
    return routes


def test_every_endpoint_the_plugin_calls_exists(lua, server_src):
    """插件调用的每个端点，服务端都必须有对应路由。

    这是最典型的跨语言失配：Lua 里写 ``/screen.json``，服务端叫 ``/api/screen``，
    本地看两边都没问题，插上设备才发现全是 404。

    插件把所有端点集中在 ``EP`` 表里，就是为了让这条检查能逐条对照 ——
    如果哪天有人改成字符串拼接凑路径，这条测试会立刻发现（见下一条）。
    """
    routes = _server_routes(server_src)
    assert routes, "没能从 server.py 抽出任何路由（正则失效了？）"

    table_body = lua.split("local EP = {", 1)
    assert len(table_body) == 2, "找不到 EP 端点表"
    table_body = table_body[1].split("}", 1)[0]

    used = set(re.findall(r'"(/[A-Za-z0-9_\.\-]+)"', table_body))
    assert used, "EP 表里没解析出端点（正则失效了？）"

    missing = sorted(u for u in used if u not in routes)
    assert not missing, f"插件调用了服务端不存在的端点：{missing}"


def test_plugin_does_not_build_paths_by_concatenation(lua):
    """端点不能靠字符串拼接凑出来。

    ``"/detail" .. ".json"`` 这种写法会让上面那条"端点和路由对照"的检查失效 ——
    等于把唯一一道本地防线关掉，端点写错又要插上设备才知道。
    """
    # 允许 server .. EP.xxx .. "?query"（EP 是完整路径）
    # 不允许把路径本身拼出来
    assert '"/detail"' not in lua, "不要在代码里拼 /detail 这种半截路径"
    assert '.. ".json"' not in lua and '.. ".png"' not in lua, \
        "端点必须是 EP 表里的完整字面量，不要拼接后缀"


def test_plugin_uses_the_json_endpoints_it_needs(lua):
    """明确盯住这几个端点 —— 少了任何一个，点击功能就不工作。"""
    for endpoint in ("/rev", "/screen.json", "/screen.png",
                     "/detail.json", "/detail.png"):
        assert endpoint in lua, f"插件没有使用 {endpoint}"


# ---------------------------------------------------------------- 坐标契约


def test_plugin_canvas_size_matches_the_server(lua):
    """插件里的 IMG_W/IMG_H 必须等于布局引擎的画布尺寸。

    不一致的后果是**所有点击都偏移**，而且偏多少取决于缩放比 ——
    在设备上表现为"点上面那条打开了下面那条"，极难排查。
    """
    m = re.search(r"local IMG_W,\s*IMG_H\s*=\s*(\d+),\s*(\d+)", lua)
    assert m, "找不到 IMG_W/IMG_H 定义"
    assert int(m.group(1)) == CANVAS_W, "宽度和服务端不一致"
    assert int(m.group(2)) == CANVAS_H, "高度和服务端不一致"


def test_no_duplicate_method_definitions(lua):
    """**不许重复定义同名方法。**

    Lua 里重复定义**不报错** —— 后面的静默覆盖前面的。
    这个特性很危险：你以为改好了某个函数，但文件后面还留着一个旧版本，
    于是**改动静默失效**，而且完全看不出原因。

    实测踩过：给 ``_paint_all`` 加了诊断逻辑（把绘制失败的原因显示到屏幕上），
    但文件里还留着旧的一份 —— 新版被覆盖回去，加的东西等于没加，
    而语法检查和当时所有测试都是绿的。

    这类问题只能靠"扫描重复定义"来抓。
    """
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "tools"))
    from check_lua import check_duplicate_methods

    raw = LUA_FILE.read_text(encoding="utf-8")
    dups = check_duplicate_methods(raw)
    assert not dups, (
        "有重复定义的方法 —— Lua 里后面的会静默覆盖前面的：\n  "
        + "\n  ".join(dups)
    )


def test_paint_failures_are_visible_on_screen(lua):
    """绘制失败时，**要把原因显示在屏幕上**，不能只留一片白。

    这是踩过的坑：主绘制失败时兜底只画白底 + 退出按钮，
    用户看到的是一片空白，完全不知道发生了什么 ——
    而日志在设备里，要插 USB 才能读。我只能远程反复猜，猜了好几轮。

    现在兜底会把错误写到屏幕上，用户一眼就能说出来。
    另外，**画面绘制这一步要单独包 pcall**：
    ``image_widget`` 为 nil 时是无异常的静默白屏，不检查就查不出来。
    """
    # 兜底路径要有诊断输出
    assert "_paint_diagnostic" in lua, "兜底没有把错误画到屏幕上"

    paint_body = lua.split("function WardenScreen:paintTo", 1)[1].split(
        "function WardenScreen:_paint_all", 1)[0]
    assert "_paint_diagnostic" in paint_body, \
        "paintTo 的兜底路径没有调用诊断绘制"

    # 画面绘制要单独 pcall，并且区分"组件不存在"和"绘制抛异常"
    all_body = lua.split("function WardenScreen:_paint_all", 1)[1].split(
        "function WardenScreen:", 1)[0]
    assert "paint_error" in all_body, \
        "画面绘制没有记录失败原因（静默白屏就查不出来了）"
    assert "not self.image_widget" in all_body, (
        "没有区分'组件不存在'这种情况 —— image_widget 为 nil 时是静默白屏，"
        "不抛异常，不检查就完全不知道"
    )
    assert "pcall" in all_body, "画面绘制没有 pcall 保护"


def test_diagnostic_shows_the_image_path(lua):
    """诊断信息要包含**图片路径和是否存在**。

    白屏最常见的原因是"图没下载下来"或"路径不对"。
    直接在屏幕上显示路径，用户一看就知道是不是这个问题 ——
    省掉一轮"插 USB 读日志"的往返。
    """
    diag = lua.split("function WardenScreen:_paint_diagnostic", 1)[1].split(
        "\nfunction ", 1)[0]
    assert "img_path" in diag, "诊断里没有显示图片路径"
    assert "io.open" in diag, "诊断里没有检查图片是否存在"


def test_all_methods_are_defined_after_the_class(lua):
    """**所有 ``function Xxx:method()`` 必须排在 ``local Xxx = ...`` 之后。**

    ⚠️ 这是踩过的坑，而且**语法检查抓不到** —— 它不是语法错误，
    是运行时的作用域错误。

    实测：把 ``_reap_rival_clients`` / ``_heal_stuck_input`` 插到了
    类定义**之前**，KOReader 报

        main.lua:156: attempt to index global 'WardenScreen' (a nil value)

    Lua 逐行执行，那条 ``function WardenScreen:xxx`` 执行时
    ``WardenScreen`` 还是 nil。后果是 **KOReader 直接丢弃整个插件** ——
    菜单项凭空消失，用户报"插件里的程序看不到了"，
    而从现象上完全看不出是定义顺序的问题。

    本地 `pytest` 当时全绿、Lua 语法也通过 —— 这个错误只能靠
    "检查定义顺序"这类静态分析拦下来。
    """
    import sys
    from pathlib import Path as _Path

    sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "tools"))
    from check_lua import check_definition_order

    raw = LUA_FILE.read_text(encoding="utf-8")
    problems = check_definition_order(raw)
    assert not problems, (
        "定义顺序有问题 —— 语法没错，但 KOReader 加载插件时会失败：\n  "
        + "\n  ".join(problems)
    )


def test_load_stage_does_not_call_undefined_helpers(lua):
    """模块加载期**不能调用**在文件后面才定义的局部函数。

    和上面那条同类：Lua 逐行执行，模块顶层（``local xxx = foo()``
    这种立即求值）如果引用了尚未定义的 ``local function``，
    加载就会报 ``attempt to call a nil value``，整个插件被丢弃。

    这里只做粗查：确认顶层的 ``local X = someFunc(...)`` 里的
    ``someFunc`` 不是本文件后面才定义的名字。
    """
    lines = lua.splitlines()

    # 收集所有 `local function name` / `local name = function` 的定义行
    defined_at: dict[str, int] = {}
    for i, line in enumerate(lines):
        m = re.match(r"^local\s+function\s+(\w+)", line)
        if m:
            defined_at.setdefault(m.group(1), i)

    # 顶层立即求值： `local X = name(`  或  `local X = name{`
    for i, line in enumerate(lines):
        m = re.match(r"^local\s+\w+\s*=\s*(\w+)\s*[({]", line)
        if not m:
            continue
        called = m.group(1)
        if called in defined_at and defined_at[called] > i:
            raise AssertionError(
                f"第 {i + 1} 行在加载期调用了 `{called}`，"
                f"但它到第 {defined_at[called] + 1} 行才定义 —— "
                f"加载时会 attempt to call a nil value，插件被丢弃"
            )


def test_plugin_does_not_use_autoscale(lua):
    """不能用 ``scale_factor = 0``（自适应缩放）。

    自适应会把图像居中并产生偏移，点击换算必须额外减掉那个偏移 ——
    少减一次就全部偏移。插件自己算缩放比，让绘制原点可预测。
    """
    assert "scale_factor = 0" not in lua, "不要用自适应缩放，点击坐标会算错"
    assert re.search(r"self\.scale\s*=\s*math\.min", lua), "应当自己算缩放比"


def test_plugin_converts_tap_once_through_scale(lua):
    """点击换算应当是「减原点、除缩放」这一次变换。

    如果中间又叠了别的偏移（比如又减一次 origin），就是双重偏移。
    """
    body = lua.split("function WardenScreen:on_tap", 1)[1].split("function WardenScreen:handle_hit", 1)[0]
    assert "/ self.scale" in body
    # 除以 scale 的次数应当正好两次：x 一次、y 一次
    assert body.count("/ self.scale") == 2, "换算次数不对，可能叠了多余偏移"
    assert "self.origin_x" in body and "self.origin_y" in body


# ---------------------------------------------------------------- 已知的坑


def test_plugin_declares_empty_gesture_tables(lua):
    """必须显式给出 ``ges_events`` / ``key_events`` 空表。

    ``InputContainer.onGesture`` 会 ``pairs(self.ges_events)``，
    ``onKeyPress`` 会 ``pairs(self.key_events)``。这两个表平时由
    ``InputContainer:_init()`` 创建 —— 而 ``_init`` 确实会跑（见下一条测试），
    显式给空表是**双保险**：万一将来 KOReader 改了类系统，
    或者有人在 ``init`` 里覆盖了这两个字段，也不会退化成 ``pairs(nil)`` 崩溃。
    """
    assert "ges_events = {}" in lua
    assert "key_events = {}" in lua


def test_class_system_calls_both_init_and_underscore_init(koreader):
    """KOReader 的对象创建会**依次**调用 ``_init`` 和 ``init``。

    这是插件能不能加载的致命前提。``Widget:new`` 的实现必须是：

        if o._init then o:_init() end
        if o.init then o:init() end

    为什么这条决定了整个插件的生死：``registerTouchZones`` 的第一行就是
    ``if self._zones[zone.id] then``，而 ``_zones`` 只在
    ``InputContainer:_init`` 里被初始化。如果 ``_init`` 因为子类定义了
    ``init`` 而被跳过，那一行就是 **nil 索引，一打开插件就崩**。

    我一开始正是这么担心的，读了 ``widget.lua`` 才确认两者都会调用、
    且 ``_init`` 在前 —— 所以 ``registerTouchZones`` 在 ``init`` 里调用是安全的。

    这条测试锁住那个前提：KOReader 哪天改成"只调其中一个"，
    这里必须失败，而不是等设备上一点就崩。
    """
    p = koreader / "frontend" / "ui" / "widget" / "widget.lua"
    if not p.exists():
        pytest.skip("设备上没有 widget.lua")
    src = p.read_text(encoding="utf-8", errors="replace")

    assert "function Widget:new" in src, "类系统变了，插件的加载前提要重新确认"
    body = src.split("function Widget:new", 1)[1].split("\nend", 1)[0]

    assert "_init" in body, "Widget:new 不再调用 _init —— 插件的触摸注册会崩"
    assert "init" in body, "Widget:new 不再调用 init"

    # _init 必须在 init 之前 —— 否则 registerTouchZones 会在 _zones 还没建好时跑
    i_under = body.find("_init")
    i_plain = body.find("if o.init")
    assert i_under != -1 and i_plain != -1, "找不到两个 init 的调用点"
    assert i_under < i_plain, (
        "调用顺序反了：_init 必须在 init 之前，"
        "否则 InputContainer:_init 里的 _zones 还没建好，"
        "registerTouchZones 就会 nil 索引崩溃"
    )


def test_plugin_registers_touch_zones_inside_init(lua):
    """``registerTouchZones`` 必须在 ``init`` 里调用（不能在类定义表里）。

    它依赖 ``InputContainer:_init`` 先建好 ``_zones`` / ``touch_zone_dg``，
    而 ``_init`` 是在 ``new`` 时才跑的。写在 ``extend{}`` 的表里
    （那只是类定义，不产生实例）等于永远不会执行。
    """
    init_body = lua.split("function WardenScreen:init", 1)
    assert len(init_body) == 2, "找不到 WardenScreen:init"
    init_body = init_body[1].split("function WardenScreen:getSize", 1)[0]
    assert "registerTouchZones" in init_body, "触摸注册要在 init 里"


def test_plugin_requires_widgetcontainer(lua):
    """插件基类是 WidgetContainer，漏 require 会在加载时直接报错。"""
    assert 'require("ui/widget/container/widgetcontainer")' in lua
    assert "WidgetContainer:extend" in lua


def test_plugin_uses_official_touch_zone_api(lua, server_src):
    """用官方 ``registerTouchZones``，而不是自己塞 ges_events。

    卡片位置是服务端算的，没法表达成静态的 screen_ratio，
    所以注册一个整屏 tap 区域，命中判断自己按服务端给的矩形做。
    """
    assert "registerTouchZones" in lua
    assert 'ges = "tap"' in lua


def test_plugin_frees_the_image_widget_on_swap(lua):
    """换图必须 free 旧的 ImageWidget。

    ImageWidget 内部持有 BlitBuffer；只改 ``file`` 字段不会重新读盘，
    而且不 free 会泄漏显存 —— Kindle 内存本来就紧。
    """
    assert "image_widget:free()" in lua


def test_plugin_unschedules_on_close(lua):
    """关闭时要撤掉定时器，否则定时器还持有已关闭的 widget。"""
    body = lua.split("function WardenScreen:onCloseWidget", 1)[1]
    assert "unschedule" in body.split("end", 1)[0] or "self:unschedule()" in body


def test_plugin_handles_missing_network(lua):
    """没网时要给提示，而不是静默什么都不做。"""
    assert "NetworkMgr" in lua
    assert "isConnected" in lua


def test_plugin_server_is_configurable(lua):
    """服务端地址要可配 —— IP 变了不该要求改代码。"""
    assert "kindlewarden_server" in lua
    assert "DEFAULT_SERVER" in lua


def test_plugin_uses_a_stable_cache_dir(lua):
    """缓存目录必须用 ``getFullDataDir()``。

    这里踩过一个**只有读设备真实源码才能发现**的坑：

    - ``getCacheDir()`` 在设备上的 KOReader **v2026.07.1 里不存在** ——
      照着 master 文档写会在打开副屏时直接崩（attempt to call method，
      a nil value）。设备的 git-rev 是 v2026.07.1。
    - ``getDataDir()`` 在 Kindle 上走 else 分支返回 ``"."``（相对路径），
      随启动方式变化，下载的图会不知道写到哪儿去。
    - ``getFullDataDir()`` 专门把 ``"."`` 解析成绝对路径，这个版本里有。

    顺带好处：落在 ``/mnt/us/koreader/kindlewarden/`` 是 USB 可见的，
    插上电脑就能看到插件下载下来的 PNG —— 排查时很有用。
    """
    assert "getFullDataDir()" in lua, "要用 getFullDataDir()"
    assert "getCacheDir" not in lua, "设备上的 KOReader 版本没有这个方法"
    assert "getDataDir()" not in lua, "getDataDir() 在 Kindle 上返回 '.'"
    assert "lfs.mkdir" in lua, "缓存子目录要自己建"


def test_plugin_requires_are_at_module_scope(lua):
    """``require`` 要放在模块顶部。

    塞进 ``init()`` 里的话，每次打开副屏都会重新 require 一遍所有依赖，
    而且 init 每次都可能走不同分支 —— 依赖关系看不出全貌。
    """
    head = lua.split("-- ---------------------------------------------------------------- HTTP", 1)[0]
    assert 'require("datastorage")' in head
    assert 'require("libs/libkoreader-lfs")' in head
    assert 'require("socket.http")' in head
    assert 'require("ltn12")' in head
    # init 里不该再出现 require("datastorage")
    assert 'require("datastorage")' not in lua.split("function WardenScreen:init", 1)[1].split(
        "function WardenScreen:getSize", 1)[0]


# ---------------------------------------------------------------- 打包契约


def test_package_bakes_server_into_the_plugin(tmp_path, monkeypatch):
    """打包时要把服务端地址写进插件的 ``DEFAULT_SERVER``。

    和写进 ``config.sh`` 是同一个目的：**省掉最容易填错的一步**。
    用户拿到的东西应该是「拷进去就能用」，而不是「先找到一行改 IP」。

    同时验证换行符 —— Lua 对 CRLF 比 shell 宽容，但 KOReader 里混着两种
    换行迟早会咬人（比如以后加了 shebang 或用 git 比对）。
    """
    import argparse

    from kindlewarden.cli import cmd_package
    from kindlewarden.config import Config

    out = tmp_path / "dist"
    args = argparse.Namespace(server="http://10.0.0.5:9999",
                              out=str(out))
    assert cmd_package(args, Config()) == 0

    plugin = out / "koreader" / "plugins" / "kindlewarden.koplugin" / "main.lua"
    assert plugin.exists(), "打包产物里没有 KOReader 插件"

    text = plugin.read_text(encoding="utf-8")
    assert 'local DEFAULT_SERVER = "http://10.0.0.5:9999"' in text, \
        "服务端地址没有烘进插件"

    raw = plugin.read_bytes()
    assert not raw[:3] == b"\xef\xbb\xbf", "插件不能有 BOM"
    assert b"\r\n" not in raw, "插件不该有 CRLF"

    # 插件的 Lua 语法必须仍然合法 —— 替换地址不能把文件写坏
    from luaparser import ast
    ast.parse(text)


def test_deploy_installs_only_the_plugin_by_default(tmp_path, monkeypatch):
    """``warden deploy`` 默认**只装插件**，并清掉设备上残留的旧 shell 客户端。

    这是踩过一次严重故障后定下的规矩：

    两套客户端会**抢同一块屏幕**。shell 那套用 fbink 直接写 framebuffer，
    每 60 秒把服务端 PNG 刷上去，覆盖掉 KOReader 画的一切 ——
    包括插件自画的「退出」按钮。

    实测现象（用户报告）：
      * 看板内容还在更新
      * **「退出」按钮消失**
      * 触摸、滑动、点条目、连电源键**全都没反应**，看起来就是死机
      * 而且 shell 设的 preventScreenSaver=1 让设备永远不休眠

    所以默认不装旧的，而且**主动删掉设备上可能残留的** ——
    那些 file 是"再点一次就复发"的入口。
    """
    import argparse

    from kindlewarden.cli import cmd_deploy, cmd_package
    from kindlewarden.config import Config

    out = tmp_path / "dist"
    args = argparse.Namespace(server="http://10.0.0.5:8080", out=str(out))
    assert cmd_package(args, Config()) == 0

    # 造假 Kindle，并**预置一份旧的 shell 客户端**（模拟残留）
    kindle = tmp_path / "fake_kindle"
    (kindle / "documents").mkdir(parents=True)
    (kindle / "system").mkdir(parents=True)
    (kindle / "extensions" / "kindlewarden").mkdir(parents=True)
    (kindle / "extensions" / "kindlewarden" / "warden.sh").write_text(
        "# 旧的\n", encoding="utf-8")
    (kindle / "documents" / "kindlewarden-start.sh").write_text(
        "# 旧的\n", encoding="utf-8")

    rc = cmd_deploy(argparse.Namespace(
        out=str(out), kindle=str(kindle), with_shell=False), Config())
    assert rc == 0

    # 插件到位
    assert (kindle / "koreader" / "plugins" / "kindlewarden.koplugin"
            / "main.lua").exists(), "插件没到位"

    # 旧的被清掉了 —— 这是防"抢屏幕"复发的关键
    assert not (kindle / "extensions" / "kindlewarden" / "warden.sh").exists(), \
        "旧的 shell 客户端还在 —— 它会和插件抢屏幕，导致退出按钮消失、触摸失效"
    assert not (kindle / "documents" / "kindlewarden-start.sh").exists(), \
        "书库里的 scriptlet 还在 —— 用户误点一下就会让故障复发"

    # 拷过去的插件地址也必须已经替换过
    text = (kindle / "koreader" / "plugins" / "kindlewarden.koplugin"
            / "main.lua").read_text(encoding="utf-8")
    assert "10.0.0.5:8080" in text

    # 插件里必须带"清理竞争者"的逻辑（双保险：即使有人手工拷了旧的，插件启动时也会杀掉）
    assert "_reap_rival_clients" in text, \
        "插件没有清理竞争者的逻辑 —— 用户手工启动旧客户端后就没人救场了"


def test_deploy_can_still_install_shell_client_on_request(tmp_path):
    """``--with-shell`` 时仍然装旧的（给没有 KOReader 的场景留退路）。"""
    import argparse

    from kindlewarden.cli import cmd_deploy, cmd_package
    from kindlewarden.config import Config

    out = tmp_path / "dist"
    args = argparse.Namespace(server="http://10.0.0.5:8080", out=str(out))
    assert cmd_package(args, Config()) == 0

    kindle = tmp_path / "fake_kindle"
    (kindle / "documents").mkdir(parents=True)
    (kindle / "system").mkdir(parents=True)

    rc = cmd_deploy(argparse.Namespace(
        out=str(out), kindle=str(kindle), with_shell=True), Config())
    assert rc == 0

    assert (kindle / "extensions" / "kindlewarden" / "warden.sh").exists()
    assert (kindle / "documents" / "kindlewarden-start.sh").exists()


def test_deploy_refuses_stale_artifacts(tmp_path):
    """``deploy`` 必须拦住**过期的打包产物**。

    踩过一次很浪费时间的坑：改了插件源码却忘了重新 ``package``，
    直接跑 ``deploy`` —— deploy 从 ``dist/`` 拷贝，于是设备上装的是**旧版本**，
    而输出全是「✓ 部署成功」。

    我据此以为改动生效了，然后花了一轮排查"为什么新功能没出现"。

    这类问题的共同点：**成功消息不区分「拷过去了」和「拷的是新的」**。
    所以 deploy 要自己比一次内容，不一致就停下。
    """
    import argparse

    from kindlewarden.cli import cmd_deploy, cmd_package
    from kindlewarden.config import Config

    out = tmp_path / "dist"
    args = argparse.Namespace(server="http://10.0.0.5:8080", out=str(out))
    assert cmd_package(args, Config()) == 0

    kindle = tmp_path / "fake_kindle"
    (kindle / "documents").mkdir(parents=True)
    (kindle / "system").mkdir(parents=True)

    plugin = (out / "koreader" / "plugins" / "kindlewarden.koplugin"
              / "main.lua")
    assert plugin.exists()
    original = plugin.read_text(encoding="utf-8")

    # 把产物改旧（模拟"忘了重新打包"）
    plugin.write_text(original.replace("self.exit_zone",
                                       "self.exit_zone_DISABLED"),
                      encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        cmd_deploy(argparse.Namespace(
            out=str(out), kindle=str(kindle), with_shell=False), Config())

    msg = str(exc.value)
    assert "不一致" in msg, "报错要说清是产物和源不一致"
    assert "package" in msg, "要告诉用户先重新打包"

    # 恢复产物后应该能正常部署
    plugin.write_text(original, encoding="utf-8")
    assert cmd_deploy(argparse.Namespace(
        out=str(out), kindle=str(kindle), with_shell=False), Config()) == 0


def test_deploy_refuses_when_no_kindle_found(tmp_path, monkeypatch):
    """找不到 Kindle 时要给**可操作**的错误，而不是一句"失败"。

    这条路径现实中会遇到，而且原因通常是"用了充电线"——
    错误信息必须把这点直接说出来。
    """
    import argparse

    from kindlewarden import cli
    from kindlewarden.config import Config

    out = tmp_path / "dist"
    (out / "documents").mkdir(parents=True)
    (out / "documents" / "x.sh").write_text("#!/bin/sh\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_find_kindle_volume", lambda: None)
    with pytest.raises(SystemExit) as exc:
        cli.cmd_deploy(argparse.Namespace(out=str(out), kindle=None), Config())

    msg = str(exc.value)
    assert "能传数据" in msg, "要提醒可能是充电线"
    assert "warden usb" in msg, "要指向诊断命令"
    assert "--kindle" in msg, "要给出手动指定盘符的退路"


def test_package_fails_loudly_if_plugin_structure_changed(tmp_path, monkeypatch):
    """插件结构变了导致定位不到 DEFAULT_SERVER 时，要**明确报错**。

    否则会静默产出一个地址没替换的插件 —— 用户拷进去发现连不上，
    却以为是网络问题。
    """
    import argparse

    from kindlewarden import cli
    from kindlewarden.config import Config

    # 临时把插件源指到一个没有 DEFAULT_SERVER 的假文件
    fake_root = tmp_path / "repo"
    (fake_root / "kindle").mkdir(parents=True)
    (fake_root / "koplugin" / "kindlewarden.koplugin").mkdir(parents=True)
    (fake_root / "koplugin" / "kindlewarden.koplugin" / "main.lua").write_text(
        "return {}\n", encoding="utf-8")

    # kindle/ 下的 shell 文件也要存在，否则会先在别处失败
    for name in ("warden.sh", "config.sh", "scriptlet-start.sh",
                 "scriptlet-stop.sh"):
        (fake_root / "kindle" / name).write_text("#!/bin/sh\n", encoding="utf-8")
    (fake_root / "kindle" / "extensions" / "kindlewarden").mkdir(parents=True)
    (fake_root / "kindle" / "extensions" / "kindlewarden" / "start.sh").write_text(
        "#!/bin/sh\n", encoding="utf-8")

    # cmd_package 用 Path(__file__).parent.parent 定位仓库根，
    # 所以把 cli 模块的 __file__ 指到假仓库里就能让它去读那份假插件
    import kindlewarden
    monkeypatch.setattr(kindlewarden.cli, "__file__",
                        str(fake_root / "kindlewarden" / "cli.py"))

    args = argparse.Namespace(server="http://x", out=str(tmp_path / "o"))
    with pytest.raises(SystemExit) as exc:
        cli.cmd_package(args, Config())
    assert "DEFAULT_SERVER" in str(exc.value), "报错信息要指出是哪个标记找不到"
