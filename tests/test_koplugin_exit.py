"""「退不出去」事故的回归测试。

事故经过
--------
第一版插件只注册了 ``tap`` 触摸区域，打开之后**没有任何办法退出**，
只能长按电源键重启设备。

根因（两条叠加）
----------------
1. **以为框架会给出口，其实没有。** ``InputContainer:_init`` 里有：

       if Device:hasKeys() then
           self.key_events.Home = { { "Home" } }
       end

   PW3 是纯触摸设备，``Device:hasKeys()`` 为假 —— 所以
   **"按 Home 键退出"在 Kindle 上从来没被设上**。我默认它在了。

2. **全屏模态窗口没留出口。** ``covers_fullscreen = true`` + ``modal = true``
   把整个屏幕都吃掉了，而我只注册了 tap（tap 被用来点子卡片），
   等于把唯一的路也堵死。

教训
----
**全屏模态窗口必须自己留出口，不能指望框架给。**
而且出口要**分层**，从最好发现到最兜底，各层依赖不同的东西：

| 层 | 依赖 | 服务端挂了还能用吗 |
|---|---|---|
| 看得见的「退出」按钮 | 插件自己画（TextWidget + 字体） | ✅ 能 |
| 任意方向滑动 | 手势识别 | ✅ 能 |
| 服务端给的动作区域 | 服务端渲染的 PNG | ❌ 不能 |

第一层是**插件自己画的**，这是关键 —— 服务端连不上时画面全白，
如果退出入口只存在于服务端给的图里，用户面对的是一块没有任何提示的白屏。
"""

from __future__ import annotations

import re

import pytest


@pytest.fixture(scope="module")
def lua_exit(lua: str) -> str:
    """插件源码（已剥注释），供本文件内的检查使用。"""
    return lua


# ---------------------------------------------------------------- 出口必须存在


def test_screen_is_fullscreen_and_modal(lua_exit):
    """先确认前提：这**确实**是个吃掉整屏的窗口。

    如果哪天不是了（比如改成非模态浮层），下面那些"必须自己留出口"的
    约束就可以重新评估 —— 所以这里先把前提钉住，而不是让它悄悄变。
    """
    assert "covers_fullscreen = true" in lua_exit, "前提变了：它不再是全屏窗口"
    assert "modal = true" in lua_exit, "前提变了：它不再是模态窗口"


def test_there_is_an_exit_gesture_that_does_not_need_the_server(lua_exit):
    """必须有一个**不依赖服务端**的退出手势。

    这是最兜底的一层：服务端挂了、图下载失败、画面全白 ——
    仍然必须能退出去。
    """
    zones = lua_exit.split("registerTouchZones", 1)
    assert len(zones) == 2, "找不到 registerTouchZones"
    body = zones[1].split("})", 1)[0]

    assert 'ges = "swipe"' in body, \
        "没有注册滑动退出 —— 服务端挂掉时用户就退不出去了"
    assert "on_swipe" in body, "滑动没有接到退出处理"


def test_swipe_and_pan_are_both_registered(lua_exit):
    """``swipe`` 和 ``pan`` 都要注册。

    手势识别有阈值：``SWIPE_INTERVAL_MS = 900``、``PAN_THRESHOLD = 35``
    （按 DPI 缩放）。滑得慢一点、短一点就会被识别成 ``pan`` 而不是 ``swipe``,
    那样"滑动退出"就**静默失效**了 —— 而退不出去要重启设备。

    用一个手势覆盖不了"所有有意图的拖动"，所以两个都收。
    """
    body = lua_exit.split("registerTouchZones", 1)[1].split("})", 1)[0]
    assert 'ges = "pan"' in body, \
        "没注册 pan —— 划得慢/短的话会被识别成 pan，退出就失效了"
    assert 'ges = "swipe"' in body


def test_hold_and_touch_are_not_registered_as_exit(lua_exit):
    """**不许**把 ``hold`` 或 ``touch`` 注册成退出。

    这两个都试过，取舍不对：

    * ``touch`` —— "一碰就发"，会让**每次点击卡片都变成退出**
    * ``hold``  —— ``HOLD_INTERVAL_MS = 500``：手指按住超过半秒就触发。
      而 ``TAP_INTERVAL_MS = 0``，tap 没有最小时长。
      于是"点卡片时稍微按久一点"会**变成退出而不是打开详情**。

    判断依据：退出入口的代价是"多点一次"，而误退出的代价是
    "正常操作用不了"。**宁可少收手势，也不要侵占 tap。**
    """
    body = lua_exit.split("registerTouchZones", 1)[1].split("})", 1)[0]
    assert 'ges = "hold"' not in body, \
        "注册了 hold —— 点卡片按久一点就会变成退出，破坏正常操作"
    assert 'ges = "touch"' not in body, \
        "注册了 touch —— 一碰就退出，卡片根本点不了"


def test_exit_gesture_does_not_check_direction(lua_exit):
    """退出滑动**不判断方向**。

    这块屏是纯展示的，拖动没有别的含义。误退出的代价只是
    "从菜单再点一次"，而退不出去要重启设备 —— 两者差得远，所以宁可宽松。
    """
    body = lua_exit.split("function WardenScreen:on_swipe", 1)[1].split("\nend", 1)[0]
    assert "do_exit" in body, "滑动没有触发退出"
    for token in ("ges.direction", "direction ==", "direction ~="):
        assert token not in body, f"滑动退出去判断方向了（{token}）—— 没必要，且会漏掉"


def test_all_gesture_names_used_are_real(lua_exit):
    """插件用到的每个手势名，都必须是 KOReader 真的会发出的。

    ``GestureRange:match`` 是按 ``gs.ges == self.ges`` 做**字符串比较**的。
    写一个不存在的名字（比如 ``swipe_left``）不会报错，
    只会**永远不命中** —— 表现为"怎么划都没反应"。

    设备上真实的手势名（从 ``device/gesturedetector.lua`` 读到的 21 个）：
    touch / tap / pan / hold / swipe / pinch / spread / rotate / hold_pan /
    double_tap / inward_pan / outward_pan / pan_release / hold_release /
    two_finger_* / multiswipe
    """
    real = {
        "touch", "tap", "pan", "hold", "swipe", "pinch", "spread", "rotate",
        "hold_pan", "double_tap", "inward_pan", "outward_pan", "pan_release",
        "hold_release", "two_finger_hold", "two_finger_hold_release",
        "two_finger_tap", "two_finger_pan", "two_finger_hold_pan",
        "two_finger_swipe", "two_finger_pan_release",
        "two_finger_hold_pan_release", "multiswipe",
    }
    used = set(re.findall(r'ges\s*=\s*"([a-z_]+)"', lua_exit))
    assert used, "找不到任何手势注册"
    unknown = sorted(used - real)
    assert not unknown, (
        f"这些手势名 KOReader 不会发出：{unknown}\n"
        "写错不会报错，只会永远不命中（表现为「怎么操作都没反应」）"
    )


def test_there_is_a_visible_exit_button_painted_by_the_plugin(lua_exit):
    """必须有一个**插件自己画的**可见退出入口。

    这是从事故里学到的最关键一点：服务端连不上时画面是全白的，
    退出入口如果只在服务端渲染的图里，用户面对的就是一块
    **没有任何提示的白屏**，不知道能滑动。
    """
    assert "EXIT_ZONE" in lua_exit, "没有退出按钮的区域定义"
    assert "_paint_exit_button" in lua_exit, "没有绘制退出按钮"
    assert "exit_widget" in lua_exit, "没有退出按钮控件"

    # 它必须在 paintTo 里**无条件**画 —— 不能只在有画面时才画
    body = lua_exit.split("function WardenScreen:paintTo", 1)[1].split(
        "function WardenScreen:_paint_exit_button", 1)[0]
    assert "_paint_exit_button" in body, "paintTo 里没有画退出按钮"
    # 不能把绘制放进 image_ready 分支里
    guarded = re.search(
        r"if\s+self\.image_ready\s+and\s+self\.image_widget\s+then(.*?)\bend\b",
        body, re.S)
    if guarded:
        assert "_paint_exit_button" not in guarded.group(1), (
            "退出按钮被放进了 image_ready 分支 —— 服务端挂掉（白屏）时它就不见了，"
            "那正是最需要它的时候"
        )


def test_exit_button_is_clickable_even_without_an_image(lua_exit):
    """点退出按钮**不能**受 ``image_ready`` 影响。

    白屏时最需要退出，如果按钮的命中判断排在
    ``if not self.image_ready then ... return`` 之后，它就永远点不到。
    """
    body = lua_exit.split("function WardenScreen:on_tap", 1)[1].split(
        "function WardenScreen:on_swipe", 1)[0]

    i_exit = body.find("EXIT_ZONE")
    i_ready = body.find("if not self.image_ready")
    assert i_exit != -1, "on_tap 里没有判断退出按钮"
    assert i_ready != -1, "on_tap 里没有 image_ready 判断"
    assert i_exit < i_ready, (
        "退出按钮的命中判断排在了 image_ready 检查之后 —— "
        "白屏时按钮点不动，而那正是最需要它的时候"
    )


def test_exit_button_stays_on_screen(lua_exit):
    """退出按钮**绝不能超出屏幕** —— 屏幕外的部分点不到。

    这是从几版实现里总结出来的硬约束：

    * 第一版把按钮取在 ``y=1402``，**侵入了状态栏 6px**
    * 第二版换成由控件实际尺寸算矩形，但加了边框后按钮变高，
      如果只钉底边就会**溢出屏幕底部**
    * 第三版（用户报的）退出区只给了 36px，而带边框的按钮约 49px ——
      **按钮下边缘画到了页面外**

    现在的规则有两层：

      1. 服务端给的 ``exit_zone`` 保证在画布内
      2. 插件侧 ``_clamp_rect`` + 绘制前的二次夹紧 —— **这是根本保证**：
         不管控件多高、字体怎么变，底边都不许超过屏幕底边

    第 2 条才是关键：不能指望"尺寸刚好"，得有个兜底把位置夹住。
    """
    # 1. 必须有由控件尺寸推出矩形的函数
    assert "function WardenScreen:_compute_exit_rect" in lua_exit, \
        "没有算退出按钮矩形的函数"

    # 2. 必须**夹紧**到画布内（这是"不超出屏幕"的根本保证）
    assert "function WardenScreen:_clamp_rect" in lua_exit, \
        "没有夹紧函数 —— 控件比预期高时按钮会画出画布"
    clamp = lua_exit.split("function WardenScreen:_clamp_rect", 1)[1].split(
        "\nend", 1)[0]
    assert "IMG_H" in clamp, "夹紧时没有用 IMG_H 作为下边界"
    assert "IMG_W" in clamp, "夹紧时没有用 IMG_W 作为右边界"
    # 高度不够时要整体上移，而不是硬裁
    assert "need_h" in clamp, "夹紧逻辑没有按控件真实高度调整"

    # 3. 绘制前要**再夹一次**（控件尺寸是惰性计算的，第一次量可能偏小）
    paint = lua_exit.split("function WardenScreen:_paint_exit_button", 1)[1].split(
        "\nend", 1)[0]
    assert "size.h" in paint and "screen_h" in paint, (
        "绘制前没有按控件真实高度二次夹紧 —— "
        "惰性尺寸会让矩形偏矮，按钮仍可能溢出"
    )

    # 4. 兜底值要在画布内
    m = re.search(
        r"local EXIT_ZONE_FALLBACK = \{([^}]*)\}", lua_exit)
    assert m, "解析不了 EXIT_ZONE_FALLBACK"
    # 允许 IMG_W/IMG_H 表达式，这里只检查它引用了这两个常量（说明有边界意识）
    fallback = m.group(1)
    assert "IMG_W" in fallback and "IMG_H" in fallback, (
        "兜底矩形没有用 IMG_W/IMG_H —— 可能写死了会溢出的坐标"
    )

    # 5. 绘制和命中必须同源（都由 exit_rect 决定）
    assert "exit_rect" in paint, "绘制没用 exit_rect"
    tap = lua_exit.split("function WardenScreen:on_tap", 1)[1].split(
        "function WardenScreen:on_swipe", 1)[0]
    assert "exit_rect" in tap, (
        "命中判断没用 exit_rect —— 会和画出来的位置错位"
    )

    # 5. 绘制和命中必须用同一个矩形
    paint = lua_exit.split("function WardenScreen:_paint_exit_button", 1)[1].split(
        "\nend", 1)[0]
    tap = lua_exit.split("function WardenScreen:on_tap", 1)[1].split(
        "function WardenScreen:on_swipe", 1)[0]
    assert "exit_rect" in paint, "绘制没用 exit_rect"
    assert "exit_rect" in tap, "命中判断没用 exit_rect —— 会和画出来的位置错位"


def test_exit_is_wired_to_the_plugin_so_it_can_reopen(lua_exit):
    """退出必须通知插件，否则**退出后再也打不开**。

    这是事故的"下半场"，容易漏：屏幕可以**自己**关闭（滑动退出，
    或 KOReader 从别处关掉它）。如果只在 ``toggle`` 里清 ``self.screen``，
    那么这条路径上 ``self.screen`` 仍然指着那个已关闭的对象，
    下次点菜单会去 close 一个死对象 —— **新窗口永远打不开**。
    """
    # 唯一的关闭函数，而且是幂等的（先清引用再 close）
    assert "function Warden:close_screen" in lua_exit, "没有统一的关闭函数"
    body = lua_exit.split("function Warden:close_screen", 1)[1].split("\nend", 1)[0]
    i_clear = body.find("self.screen = nil")
    i_close = body.find("UIManager:close")
    assert i_clear != -1 and i_close != -1, "close_screen 没做清理和关闭"
    assert i_clear < i_close, (
        "要先清引用再 close —— close 会触发 onCloseWidget 回调回来，"
        "此时已经是无操作（幂等）"
    )

    # 屏幕关闭时回调插件
    close_body = lua_exit.split("function WardenScreen:onCloseWidget", 1)[1].split("\nend", 1)[0]
    assert "on_closed" in close_body, "屏幕关闭时没有通知插件"

    # 屏幕里的退出走回调，而不是直接 close 自己
    exit_body = lua_exit.split("function WardenScreen:do_exit", 1)[1].split("\nend", 1)[0]
    assert "on_exit" in exit_body, "do_exit 没有走插件的回调"

    # toggle 里两个回调都要接上
    toggle_body = lua_exit.split("function Warden:toggle", 1)[1].split("\nend\n", 1)[0]
    assert "on_exit" in toggle_body and "on_closed" in toggle_body, \
        "toggle 要给新屏幕接上两个回调"


def test_exit_button_creation_is_defensive(lua_exit):
    """退出按钮的创建/绘制要能失败而不连累主功能。

    新加的东西绝不能让核心功能变脆：字体缺失时 ``Font:getFace``
    返回 nil，如果直接拿去构造 TextWidget 会抛错、整屏画不出来。
    按钮建不起来只是少一个入口 —— 滑动那条路必须还在。
    """
    init_body = lua_exit.split("function WardenScreen:init", 1)[1].split(
        "function WardenScreen:getSize", 1)[0]
    assert "pcall" in init_body, "退出按钮的创建没有 pcall 保护"

    paint_body = lua_exit.split("function WardenScreen:_paint_exit_button", 1)[1].split(
        "\nend", 1)[0]
    assert "pcall" in paint_body, "退出按钮的绘制没有 pcall 保护"
    assert "if not self.exit_widget then" in paint_body, "没有判空"


def test_first_run_tells_the_user_how_to_exit(lua_exit):
    """首次打开要**主动告诉用户怎么退出**。

    事故里用户只能靠猜，猜不到就只能重启设备。
    一个全屏窗口如果不说明出口，等于把用户困住。

    ⚠️ 提示必须是**画布提示**，不能是 InfoMessage 模态弹窗 ——
    这里特别讽刺：一个"教你怎么退出"的弹窗，本身会**盖住退出按钮**，
    等于在最需要它的时候把它藏起来。（旧测试要求 InfoMessage，那个要求是错的。）
    """
    assert "kindlewarden_hint_shown" in lua_exit, "没有「只提示一次」的标记"

    init_body = lua_exit.split("function WardenScreen:init", 1)[1].split(
        "function WardenScreen:getSize", 1)[0]
    assert "notice" in init_body, "首次打开没有提示"
    assert "退出" in init_body, "提示里没有说明怎么退出"
    assert "UIManager:show" not in init_body, (
        "首次提示用了模态弹窗 —— 它会盖住退出按钮（正是要提示用户去点的那个）"
    )
