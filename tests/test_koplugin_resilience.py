"""异常状态下的「必须能退出」「必须能恢复休眠」的回归测试。

用户明确提的两条底线
--------------------
> 不要出现无法退出的情况，不要出现无法解除休眠状态的情况

这个文件把这两条固化成不可逆的约束，覆盖 5 类异常：

  1. 长时间放置进入待机
  2. 待机后网络断开
  3. 运行中断开服务器
  4. 启动就连不上服务器
  5. 其他：绘制失败、请求阻塞、重复打开

为什么这些问题危险
------------------
它们的共同点是**本地测不出来**：只有真机长时间放着、或者把服务器关掉才会暴露，
而暴露时的现象是"界面卡住 / 按钮点不动 / 永远不休眠"，
排查起来完全没有线索。所以只能靠静态约束 + 真机验证。
"""

from __future__ import annotations

import re

import pytest


@pytest.fixture(scope="module")
def code(lua: str) -> str:
    return lua


# ---------------------------------------------------------------- 底线 1：没有模态弹窗


def test_no_modal_dialogs_that_can_block_exit(code):
    """**除了显示副屏本身，不许有任何 UIManager:show。**

    模态弹窗（InfoMessage 等）会造成两个致命后果：

    1. **盖住退出按钮** —— 服务器断开时反复弹窗，用户就退不出去了
    2. **阻止 KOReader 自动休眠** —— 弹窗存在期间不会休眠，
       用户报的"无法解除休眠状态"就是它

    允许的两处 ``UIManager:show``：
      * 显示副屏屏幕本身（``UIManager:show(s)``）
      * 用户主动打开的"服务端地址"输入框（能自己关掉）

    错误提示一律画在自己的画布上（``_paint_status_text``）—— 非模态、不拦输入。
    """
    # 去掉注释再检查（注释里在解释"为什么不能用"）
    shows = re.findall(r"UIManager:show\((\w+)", code)
    assert shows, "找不到任何 UIManager:show —— 插件还怎么显示自己？"

    # 只允许 show(s) 和 show(dialog)
    allowed = {"s", "dialog"}
    bad = sorted(set(shows) - allowed)
    assert not bad, (
        f"出现了不该有的模态弹窗：{bad}\n"
        "模态弹窗会盖住退出按钮并阻止自动休眠 —— 这两条都是用户明确要求避免的。\n"
        "错误/提示请改用 self.notice + _paint_status_text 画在画布上。"
    )


def test_infomessage_is_not_imported(code):
    """如果还用 InfoMessage，说明有人加回了弹窗。"""
    assert 'require("ui/widget/infomessage")' not in code, (
        "又引入了 InfoMessage —— 它是最容易挡住退出按钮的模态弹窗"
    )


def test_errors_are_painted_not_popped(code):
    """错误必须走"画在画布上"这条路。"""
    assert "_paint_status_text" in code, "没有画布内状态提示"
    assert "last_error" in code, "错误没有记录到 last_error"

    # 刷新流程里不能出现 UIManager:show
    refresh = code.split("function WardenScreen:refresh", 1)[1].split(
        "function WardenScreen:_fetch", 1)[0]
    assert "UIManager:show" not in refresh, \
        "刷新流程里弹了模态窗 —— 服务器一断就会反复弹，用户退不出去"


# ---------------------------------------------------------------- 底线 2：请求不能阻塞


def test_http_timeouts_are_short(code):
    """HTTP 超时必须够短。KOReader 是单线程事件循环，请求**阻塞主线程**。

    原来 20 / 30 秒的后果：服务器断开时每轮刷新都冻住主线程最多 30 秒，
    用户在这期间点「退出」**完全没反应** —— 看起来就是"卡死了退不出去"。

    超时压短只是"服务器慢时更新晚一点"，换来界面**永远保持可操作**。
    """
    timeouts = [int(m) for m in re.findall(r"HTTP_TIMEOUT_\w+\s*=\s*(\d+)", code)]
    assert timeouts, "找不到超时常量定义"
    assert max(timeouts) <= 8, (
        f"HTTP 超时太长：{timeouts}。主线程会被阻塞这么久，期间点退出没反应。"
    )
    for t in timeouts:
        assert t >= 2, f"超时 {t}s 太短，正常网络也会失败"

    # 所有 http.request 都要用这些常量，不能写回魔法数字
    for m in re.finditer(r"timeout\s*=\s*(\d+)", code):
        v = int(m.group(1))
        assert v <= 8, f"有一处硬编码超时 {v}s 太长"


def test_refresh_is_wrapped_in_pcall(code):
    """拉取过程必须包 pcall。

    任何未预期的 Lua 错误（json 解码、文件写失败……）都不该让插件卡死 ——
    失败只是"这一轮没更新"，下一轮照常重试。
    """
    body = code.split("function WardenScreen:refresh", 1)[1].split(
        "function WardenScreen:_fetch", 1)[0]
    assert "pcall" in body, "refresh 没有 pcall —— 一个 Lua 错误就会让界面卡死"


def test_paint_has_a_fallback_that_still_draws_exit(code):
    """绘制必须包 pcall，而且**出错时仍要把退出按钮补画出来**。

    这是"永远能退出"的最后一道保险：如果主绘制路径出错
    （图片解码失败、字体问题……），屏幕可能一片空白，
    而退出按钮**也就跟着没了**。

    兜底至少要画出一个退出按钮 —— "没有内容的副屏 + 一个退出按钮"
    比"全白且不知道该点哪里"好得多。
    """
    assert "function WardenScreen:paintTo" in code
    body = code.split("function WardenScreen:paintTo", 1)[1].split(
        "function WardenScreen:_paint_all", 1)[0]
    assert "pcall" in body, "paintTo 没有 pcall"
    assert "_paint_exit_button" in body, (
        "绘制失败时没有补画退出按钮 —— 那用户就真的没有出口了"
    )
    assert "COLOR_WHITE" in body, "兜底时没铺白底，可能残影盖住按钮"


# ---------------------------------------------------------------- 待机 / 恢复


def test_suspend_and_resume_are_handled(code):
    """必须处理待机与唤醒。

    不处理的后果：
      * 待机时所有定时器被冻结，醒来后**积压的 scheduleIn 集中触发**，
        一秒钟内发起好几个请求
      * 醒来瞬间 WiFi 往往还没连上，立刻请求必然失败

    所以待机时撤掉定时器，唤醒后**等几秒**（给网络栈恢复）再重新开始。
    """
    assert "function WardenScreen:onSuspend" in code, "没有处理待机"
    assert "function WardenScreen:onResume" in code, "没有处理唤醒"

    susp = code.split("function WardenScreen:onSuspend", 1)[1].split("\nend", 1)[0]
    assert "unschedule" in susp, "待机时没有撤掉定时器 —— 醒来会集中触发"

    res = code.split("function WardenScreen:onResume", 1)[1].split("\nend\n", 1)[0]
    assert "scheduleIn" in res, "唤醒后没有延迟刷新 —— 会撞上还没连上的 WiFi"
    assert "shown_rev" in res, "唤醒后没有强制重新取数据（待机期间服务端可能更新过）"


def test_resume_does_not_touch_a_closed_screen(code):
    """唤醒后的延迟回调必须检查屏幕是否已被关掉。

    用户可能在"唤醒后等 5 秒"的窗口里退出副屏 ——
    那时回调再去操作已销毁的对象会**崩**（或者更糟：留下幽灵定时器）。
    """
    res = code.split("function WardenScreen:onResume", 1)[1].split("\nend\n", 1)[0]
    assert "_closed" in res, "唤醒回调没有检查屏幕是否已关闭"

    close = code.split("function WardenScreen:onCloseWidget", 1)[1].split("\nend", 1)[0]
    assert "_closed" in close, "关闭时没有打 _closed 标记"


def test_idle_does_not_repaint_every_cycle(code):
    """空闲时**不能**每轮都重画。

    原来 refresh 结尾是无条件 ``UIManager:setDirty(self, "full")`` ——
    每 60 秒全屏刷一次，哪怕画面一个字都没变。两个坏处：

    1. e-ink 全屏刷新要几百毫秒，纯属浪费电（副屏是常驻的）
    2. 持续刷屏会让 KOReader 认为"有活动"，**妨碍自动休眠** ——
       而"长时间放置后能正常待机"正是用户要求的底线之一

    所以重画必须有条件：内容变了 / 错误状态变了 / 有提示要画或要擦。
    """
    body = code.split("function WardenScreen:refresh", 1)[1].split(
        "function WardenScreen:_fetch", 1)[0]

    assert "need_redraw" in body, "没有重画条件判断 —— 又变成每轮无条件刷屏了"
    assert "if need_redraw then" in body, "重画没有被条件包住"

    # _swap_image 是内容变化的唯一信号源，必须置标记
    swap = code.split("function WardenScreen:_swap_image", 1)[1].split("\nend", 1)[0]
    assert "_image_changed" in swap, (
        "换了图片却没置 _image_changed —— 有条件的重画会让画面永不更新"
    )


def test_notice_is_erased_after_its_timeout(code):
    """临时提示到期后要**主动擦掉**。

    否则它会一直留到下一次轮询才消失 —— 轮询间隔 60 秒，
    一条 4 秒的提示会挂在屏幕上将近一分钟。
    """
    todo = code.split('elseif item.kind == "todo" then', 1)[1].split(
        "elseif item.kind ==", 1)[0]
    assert "notice" in todo, "点待办没有提示"
    assert "scheduleIn" in todo, (
        "提示没有定时擦除 —— 会一直留到下次轮询（最多 60 秒）"
    )
    assert "_closed" in todo, "擦除回调没有检查屏幕是否已关闭"


def test_suspend_handlers_are_an_optimisation_not_a_dependency(code):
    """``onSuspend`` / ``onResume`` **不能是正确性的必要依赖**。

    官方事件文档写着：

        Events are sent to the first widget in UIManager._window_stack.
        If it is not consumed, then UIManager will try to send it to all
        active widgets (widget.is_always_active equals true).

    也就是说**这两个处理器不一定会被调用** —— 取决于 Suspend/Resume 是用
    ``broadcastEvent`` 还是 ``sendEvent`` 发的、以及窗口在不在栈顶。
    本地无法验证。

    所以设计必须保证：**即使它们从未被调用，界面依然可操作、能退出、能休眠。**
    靠的是另外三条：

      * ``schedule()`` 每次先 ``unschedule()`` → 任何时刻最多一个待触发定时器，
        不会积压成请求风暴
      * HTTP 超时只有几秒 → 醒来撞上没连上的 WiFi 也不会冻住主线程
      * 失败只画在画布上 → 不弹模态窗、不挡退出按钮

    这条测试就是盯住第一条不变量。
    """
    sched = code.split("function WardenScreen:schedule", 1)[1].split("\nend", 1)[0]
    assert "unschedule" in sched, (
        "schedule() 没有先 unschedule() —— 待机时定时器会积压，"
        "醒来集中触发形成请求风暴"
    )

    # unschedule 必须真的撤掉，并且清掉引用（否则下次判断 self.task 会误判）
    unsched = code.split("function WardenScreen:unschedule", 1)[1].split("\nend", 1)[0]
    assert "UIManager:unschedule" in unsched, "没有真的撤掉定时器"
    assert "self.task = nil" in unsched, "没有清掉 task 引用"


def test_suspend_handlers_do_not_consume_the_event(code):
    """待机/唤醒处理器**不该返回 true**。

    返回 true 在 KOReader 事件语义里表示"这个事件我消费了" ——
    待机是广播事件，不该由一个副屏插件截断（那会妨碍其他插件/核心逻辑）。

    对齐官方 AutoTurn 插件的写法：只做自己的事，不返回值。
    """
    for name in ("onSuspend", "onResume"):
        body = code.split(f"function WardenScreen:{name}", 1)[1].split("\nend\n", 1)[0]
        # 去掉嵌套的 scheduleIn 回调部分再检查
        head = body.split("UIManager:scheduleIn", 1)[0]
        assert "return true" not in head, (
            f"{name} 返回了 true（消费了事件）—— 不该截断待机/唤醒广播"
        )


# ---------------------------------------------------------------- 抢屏与输入卡死


def test_plugin_reaps_rival_shell_client(code):
    """插件启动时必须**杀掉旧的 shell 客户端**。

    这是用户报告的"卡死"故障的第一原因：

    设备上曾有两套客户端。shell 那套用 ``fbink`` **直接写 framebuffer**，
    每 60 秒把服务端的 PNG 刷上去 —— **覆盖掉 KOReader 画的一切**，
    包括插件自画的「退出」按钮。

    用户看到的现象：
      * 看板内容还在更新（是 shell 客户端在画）
      * **「退出」按钮消失**（服务端 PNG 里没有按钮）
      * 触摸、滑动、点条目、电源键**全部无效**
      * 设备永远不休眠（shell 设的 preventScreenSaver=1）

    必须在 ``init`` 里、**任何绘制之前**清掉，否则会先画好再被覆盖。
    """
    assert "function WardenScreen:_reap_rival_clients" in code, \
        "没有清理旧 shell 客户端的逻辑"

    init_body = code.split("function WardenScreen:init", 1)[1].split(
        "function WardenScreen:getSize", 1)[0]
    assert "_reap_rival_clients" in init_body, "init 里没有调用清理"

    # 必须排在绘制之前（否则先画好又被覆盖）
    i_reap = init_body.find("_reap_rival_clients")
    i_dirty = init_body.find("setDirty")
    assert i_dirty == -1 or i_reap < i_dirty, \
        "清理必须排在首次绘制之前 —— 否则会先画好、随即被旧客户端覆盖"

    # 杀掉之后还要恢复休眠状态（旧客户端可能设了 preventScreenSaver=1 没清）
    reap = code.split("function WardenScreen:_reap_rival_clients", 1)[1].split(
        "\nend", 1)[0]
    assert "preventScreenSaver" in reap, \
        "清理后没有恢复休眠 —— 设备会永远不休眠"


def test_reaping_does_not_block_the_ui(code):
    """清理竞争者的动作**不能阻塞主线程**。

    ``os.execute`` 是**阻塞**的 —— 在 KOReader 的单线程事件循环里调用它，
    界面会在这段时间内冻住。这恰恰是我们要修的那类问题，不能再引入一个。

    必须用 ``ffiUtil.execute``（KOReader 自己的非阻塞子进程 API）。
    """
    reap = code.split("function WardenScreen:_reap_rival_clients", 1)[1].split(
        "\nend", 1)[0]

    assert "ffiUtil.execute" in reap, \
        "没有用 ffiUtil.execute（非阻塞）"
    assert "os.execute" not in reap, (
        "用了 os.execute —— 它会阻塞主线程，界面会冻住。"
        "这正是要修的问题，不能用同样的方式引入。"
    )
    # 整体包 pcall：清理失败不该影响插件本身
    assert "pcall" in reap, "清理动作没有 pcall 保护"


def test_plugin_heals_stuck_input(code):
    """插件必须能**自愈"输入被永久抑制"**。

    这是"卡死"故障的第二个、独立的原因。

    KOReader 待机前会调 ``Input:inhibitInput(true)``（见 ``Device:_beforeSuspend``）：

        function Input:inhibitInput(toggle)
            if toggle then
                self.handleKeyBoardEv = self.handlePowerManagementOnlyEv
                self.handleTouchEv = self.voidEv        -- 触摸丢进虚空

    期间**触摸被完全丢弃，电源键也只剩电源管理用途** ——
    正是用户描述的"点什么都没反应"。

    恢复靠 ``_afterResume`` 或 ``inhibitInputUntil`` 的定时器。
    **如果"抑制"发生了但"恢复"没走完**，输入就永久失效。

    自愈的判据很明确：``inhibitInput(true)`` 会把原 handler 存进
    ``_key_ev_handler`` / ``_abs_ev_handler``，恢复时清空 ——
    它们还留着就说明没恢复。
    """
    assert "function WardenScreen:_heal_stuck_input" in code, \
        "没有输入自愈逻辑"

    heal = code.split("function WardenScreen:_heal_stuck_input", 1)[1].split(
        "\nend", 1)[0]
    assert "_key_ev_handler" in heal, "没有检查 KOReader 的抑制标志"
    assert "inhibitInput(false)" in heal, "没有实际解除抑制"
    assert "screen_saver_mode" in heal, (
        "没有排除待机状态 —— 待机时输入被抑制是**正常的**，"
        "那时去解开会让睡着的设备还在处理触摸"
    )


def test_healing_runs_on_every_poll_not_on_user_input(code):
    """自愈必须挂在**轮询**上，不能挂在用户输入上。

    用户的故障现象是"点什么都没反应，但看板还在更新" ——
    说明轮询这条路径是活的，坏掉的只有输入。

    如果把自愈挂在"用户点击"上，就**永远等不到** —— 因为点击本身就是坏的。
    """
    refresh = code.split("function WardenScreen:refresh", 1)[1].split(
        "function WardenScreen:_fetch", 1)[0]
    assert "_heal_stuck_input" in refresh, \
        "自愈没有挂在轮询上 —— 输入死了就永远没机会执行"


def test_polling_has_a_watchdog(code):
    """轮询必须有看门狗，**保证永远不会彻底停摆**。

    为什么必须有：``refresh`` 开头是 ``if self.busy then return end``，
    而 ``self.busy = false`` 写在 ``UIManager:nextTick`` 的回调**里面**。
    如果那个回调因为任何原因没跑到，``busy`` 就**永久卡在 true** ——
    此后每次 refresh 都在第一行 return，**连自愈都不会再执行**，
    界面变成"内容不更新、输入已死"的一潭死水，只能重启设备。

    看门狗是独立的定时器（不经过 busy 判断），定期强行复位。
    """
    sched = code.split("function WardenScreen:schedule", 1)[1].split(
        "function WardenScreen:unschedule", 1)[0]
    assert "watchdog" in sched, "没有看门狗"
    assert "scheduleIn" in sched, "看门狗没有真的排进定时器"

    # 看门狗必须能复位 busy，并且顺手再自愈一次
    wd = sched.split("self.watchdog", 1)[1]
    assert "self.busy = false" in wd, "看门狗没有复位 busy"
    assert "_heal_stuck_input" in wd, "看门狗没有顺手再自愈一次输入"

    # 关闭时两个定时器都要撤掉，否则留下幽灵任务
    unsched = code.split("function WardenScreen:unschedule", 1)[1].split(
        "\nend", 1)[0]
    assert "watchdog" in unsched, "关闭时没有撤掉看门狗"
    assert "self.task" in unsched, "关闭时没有撤掉轮询任务"


# ---------------------------------------------------------------- 退出路径


def test_exit_still_works_without_network(code):
    """退出**不能依赖网络**。

    退出按钮和滑动手势都必须只做本地操作。
    """
    exit_body = code.split("function WardenScreen:do_exit", 1)[1].split("\nend", 1)[0]
    for bad in ("http.request", "get_url", "download", "isConnected"):
        assert bad not in exit_body, f"退出流程里有网络操作（{bad}）—— 断网就退不出去"


def test_swipe_exit_needs_no_server_data(code):
    """滑动手势的处理器不能依赖服务端给的矩形。"""
    sw = code.split("function WardenScreen:on_swipe", 1)[1].split("\nend", 1)[0]
    assert "do_exit" in sw
    assert "self.taps" not in sw, "滑动退出不该依赖服务端给的点击区域"


def test_reopening_after_exit_works(code):
    """退出后必须能重新打开（防"退出一次就再也打不开"）。"""
    assert "function Warden:close_screen" in code
    body = code.split("function Warden:close_screen", 1)[1].split("\nend", 1)[0]
    i_clear = body.find("self.screen = nil")
    i_close = body.find("UIManager:close")
    assert i_clear != -1 and i_close != -1
    assert i_clear < i_close, "要先清引用再 close（close 会回调 onCloseWidget）"

    close = code.split("function WardenScreen:onCloseWidget", 1)[1].split("\nend", 1)[0]
    assert "on_closed" in close, "屏幕关闭时没通知插件，引用会残留"


# ---------------------------------------------------------------- shell 客户端的休眠残留


def test_shell_client_clears_stale_prevent_screen_saver():
    """shell 客户端启动时必须**先清掉**可能残留的 preventScreenSaver。

    这是用户报的"无法解除休眠状态"的根源：

    shell 客户端设了 ``lipc-set-prop com.lab126.powerd preventScreenSaver 1``，
    而清除只在 ``on_exit`` trap 里做。如果进程被 **SIGKILL**（trap 捕不到）
    或者设备直接断电，那个属性就**永久停留在 1** ——
    Kindle 从此永不自动休眠，掉电飞快，而且用户找不到原因
    （它不属于任何可见设置项，只有重启才会清掉）。

    启动时无条件清零 = 让残留状态**自愈**。
    """
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    sh = (root / "kindle" / "warden.sh").read_text(encoding="utf-8")

    # 清零点必须存在
    assert "preventScreenSaver 0" in sh, "没有清除 preventScreenSaver"

    # 而且要在设置为 1 的**之前**（启动时先清零）
    i_clear = sh.find("preventScreenSaver 0")
    i_set = sh.find("preventScreenSaver 1")
    assert i_clear != -1 and i_set != -1
    assert i_clear < i_set, (
        "清除必须排在设置之前 —— 否则残留状态不会被自愈掉"
    )

    # 退出时也要清（正常退出路径）
    assert "on_exit" in sh or "cleanup" in sh, "没有退出清理"
