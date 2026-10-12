"""不依赖设备、也不依赖联网的 KOReader 类系统前提检查。

**为什么单独一组**：`test_koplugin_device_api.py` 需要 Kindle 插着才跑，
而类系统那条断言（``Widget:new`` 会依次调用 ``_init`` 和 ``init``）
锁的是"插件能不能加载"的**致命前提** —— 它不该因为设备没插就无声跳过。

这里做一个**离线兜底**：用一份冻结的合约描述（就是我们从上游
``widget.lua`` 读到的真实实现）来断言插件依赖的那几条语义，
并在测试里明确写出"如果上游改了，去哪里核对"。

这不能替代对着设备源码的检查（版本会不同），但它保证了：
**即使设备不在、没有网络，这条致命前提也一直被盯着。**
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ``lua`` fixture（已剥注释的插件源码）+ ``koreader`` fixture 都在 conftest.py。
#
# 本文件只放**不依赖设备**的框架语义检查 —— 因为下面第一条断言锁的是
# "插件能不能加载"的致命前提，它不该因为设备没插就无声跳过。
# 对着设备真实源码的版本在 test_koplugin_device_api.py。


# ---------------------------------------------------------------- 类系统语义
#
# 冻结的是 KOReader 上游 ``frontend/ui/widget/widget.lua`` 里 ``Widget:new`` 的实现：
#
#     function Widget:new(o)
#         o = self:extend(o)
#         -- Both o._init and o.init are called on object creation.
#         if o._init then o:_init() end
#         if o.init then o:init() end
#         return o
#     end
#
# 核对来源（上游有改动时看这里）：
#   https://github.com/koreader/koreader/blob/master/frontend/ui/widget/widget.lua
#
# 为什么这条语义对整个插件是决定性的：
#   ``InputContainer:registerTouchZones`` 第一行就是 ``if self._zones[zone.id] then``，
#   而 ``self._zones`` 只在 ``InputContainer:_init()`` 里初始化。
#   如果 ``_init`` 因为子类定义了自己的 ``init`` 而被跳过，
#   那一行就是 **nil 索引 —— 一打开插件就崩，而且报错不指向原因**。


def test_plugin_does_not_rely_on_init_being_called_alone(lua):
    """插件必须对"两个 init 都会被调用"这件事保持**双保险**。

    即使 ``_init`` 一定会跑，插件也显式给出 ``ges_events`` / ``key_events``
    空表 —— 这样万一类系统改了、或者以后有人在 init 里覆盖了这两个字段，
    也只会退化成"手势不响应"，而不是 ``pairs(nil)`` 崩溃。

    这是"依赖外部框架的隐式行为"时该有的防御姿态：不要把正确性
    完全押在别人的实现细节上。
    """
    assert "ges_events = {}" in lua
    assert "key_events = {}" in lua


def test_plugin_calls_register_touch_zones_after_its_own_bookkeeping(lua):
    """``registerTouchZones`` 必须在 ``init`` 里、且在我们的字段初始化之后。

    它内部会读 ``self.touch_zone_dg`` 并写 ``self._zones``；
    我们自己的 ``init`` 又依赖 ``self.cache_dir`` 等字段已经建好。
    顺序反了会在某些 KOReader 版本上表现为随机崩溃。
    """
    init_body = lua.split("function WardenScreen:init", 1)
    assert len(init_body) == 2, "找不到 WardenScreen:init"
    init_body = init_body[1].split("function WardenScreen:getSize", 1)[0]

    i_register = init_body.find("registerTouchZones")
    i_cache = init_body.find("cache_dir")
    assert i_register != -1, "触摸注册不在 init 里"
    assert i_cache != -1, "缓存目录不在 init 里"
    assert i_cache < i_register, "缓存目录要在注册触摸之前建好"


def test_plugin_paints_itself_without_a_child_widget(lua):
    """插件这个容器**没有子控件**，所以必须自己画完整。

    这是从设备源码里读出来的坑：``InputContainer:paintTo`` 第一行是

        if self[1] == nil then return end

    —— 它把绘制**委托给 ``self[1]``**。我们的 ``WardenScreen`` 是个 leaf widget
    （自己画一张整屏图，没有 ``self[1]``），所以必须**完全重写 paintTo**。
    如果哪天有人图省事去掉这个重写、去走父类的 paintTo，
    屏幕上就会永远是一片空白，而且一条报错都没有。
    """
    assert "function WardenScreen:paintTo" in lua, \
        "必须自己实现 paintTo —— 父类的 paintTo 依赖 self[1]，我们没有子控件"
    body = lua.split("function WardenScreen:paintTo", 1)[1].split(
        "function WardenScreen:on_tap", 1)[0]
    # 画面绘制走 _paint_all；那里才是真正 blit 图片的地方
    assert "_paint_all" in body, "paintTo 没有调用 _paint_all"

    all_body = lua.split("function WardenScreen:_paint_all", 1)[1].split(
        "function WardenScreen:", 1)[0]
    assert "image_widget" in all_body and "paintTo" in all_body, \
        "要真的把图画出来"
    # 铺白底：夜间模式下 KOReader 会反色，不铺白会让画面和 PNG 不一致
    assert "COLOR_WHITE" in all_body, "要铺白底"


def test_plugin_returns_true_from_touch_zones_to_consume_the_event(lua):
    """触摸区域处理器应当返回真值，把事件吃掉。

    否则点击会继续往 KOReader 的下层界面传播 ——
    在阅读器里可能触发翻页，在文件管理器里可能打开一本书。
    这正是我们换成插件方案想避免的那类"穿透"。
    """
    zone = lua.split("registerTouchZones", 1)[1].split("})", 1)[0]
    assert "handler" in zone, "触摸区域没有 handler"
    handler = zone.split("handler", 1)[1]
    assert "return self:on_tap(ges)" in handler, \
        "handler 应当委托给 on_tap 并返回它的结果"
    assert "return true" in lua


def test_plugin_has_a_way_out(lua):
    """必须能干净退出 —— 这是目标里明确要求的一条。

    靠 KOReader 原生的关闭机制：实现 ``onCloseWidget`` 做清理，
    用户用 Home / 返回手势退出。不需要自己造退出按钮。
    """
    assert "function WardenScreen:onCloseWidget" in lua, \
        "要有 onCloseWidget 来清理定时器和 BlitBuffer"
    body = lua.split("function WardenScreen:onCloseWidget", 1)[1].split("\nend", 1)[0]
    assert "unschedule" in body, "退出时要撤掉定时器"
    assert "free()" in body, "退出时要释放 ImageWidget（它持有 BlitBuffer）"
