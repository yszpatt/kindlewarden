"""审计：插件里所有可能"卡住 / 退不出去 / 不休眠"的路径。

这是对用户两条底线的**系统性核对**，而不是逐条打补丁：

    不要出现无法退出的情况，不要出现无法解除休眠状态的情况

脚本静态分析插件源码，把每一处风险点列出来并判定。
"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, r"C:\workspace\kindlewarden\tools")
from check_lua import strip_comments  # noqa: E402

P = r"C:\workspace\kindlewarden\koplugin\kindlewarden.koplugin\main.lua"
raw = open(P, encoding="utf-8").read()
code = strip_comments(raw)

bad = 0
warn = 0


def ok(msg):
    print(f"  OK   {msg}")


def fail(msg):
    global bad
    bad += 1
    print(f"  BAD  {msg}")


def caution(msg):
    global warn
    warn += 1
    print(f"  ??   {msg}")


print("=" * 68)
print("场景 1：长时间放置 → 待机")
print("=" * 68)
if "function WardenScreen:onSuspend" in code:
    ok("处理了 onSuspend（待机时撤掉定时器，避免醒来集中触发）")
else:
    fail("没有处理 onSuspend")

if "function WardenScreen:onResume" in code:
    res = code.split("function WardenScreen:onResume", 1)[1].split("\nend\n", 1)[0]
    if "scheduleIn" in res:
        ok("唤醒后延迟刷新（给 WiFi 重连留时间）")
    else:
        fail("唤醒后立刻刷新 —— 会撞上还没连上的 WiFi")
    if "_closed" in res:
        ok("唤醒回调检查了屏幕是否已关闭")
    else:
        fail("唤醒回调没检查 _closed —— 可能操作已销毁的对象")
else:
    fail("没有处理 onResume")

# 空闲时不该持续刷屏（会妨碍自动休眠）
body = code.split("function WardenScreen:refresh", 1)[1].split(
    "function WardenScreen:_fetch", 1)[0]
if "need_redraw" in body:
    ok("空闲时不重画（不会因持续刷屏妨碍自动休眠）")
else:
    fail("每轮无条件重画 —— 会妨碍自动休眠并耗电")

print()
print("=" * 68)
print("场景 2：待机后网络断开")
print("=" * 68)
if "NetworkMgr:isConnected" in code:
    ok("检查网络状态")
else:
    caution("没检查网络状态（会直接发起请求然后超时）")

timeouts = [int(m) for m in re.findall(r"HTTP_TIMEOUT_\w+\s*=\s*(\d+)", code)]
if timeouts and max(timeouts) <= 8:
    ok(f"HTTP 超时短（{timeouts}）—— 主线程不会被长时间冻结")
else:
    fail(f"HTTP 超时太长（{timeouts}）—— 冻结期间点退出没反应")

if "ui/widget/infomessage" not in code:
    ok("没有 InfoMessage 模态弹窗（不会阻止休眠）")
else:
    fail("还在用 InfoMessage —— 弹窗期间不会自动休眠")

print()
print("=" * 68)
print("场景 3：运行中断开服务器")
print("=" * 68)
shows = re.findall(r"UIManager:show\((\w+)", code)
bad_shows = sorted(set(shows) - {"s", "dialog"})
if not bad_shows:
    ok(f"只有允许的 UIManager:show（{sorted(set(shows))}）")
else:
    fail(f"有会挡退出按钮的模态弹窗：{bad_shows}")

refresh = code.split("function WardenScreen:refresh", 1)[1].split(
    "function WardenScreen:_fetch", 1)[0]
if "pcall" in refresh:
    ok("刷新流程包了 pcall（Lua 错误不会让界面卡死）")
else:
    fail("刷新流程没有 pcall")

if "_paint_status_text" in code:
    ok("错误画在画布上（非模态、不拦输入、不挡退出按钮）")
else:
    fail("错误没有画布提示")

print()
print("=" * 68)
print("场景 4：启动就连不上服务器")
print("=" * 68)
paint = code.split("function WardenScreen:paintTo", 1)[1].split(
    "function WardenScreen:_paint_all", 1)[0]
if "pcall" in paint and "_paint_exit_button" in paint:
    ok("绘制失败时兜底补画退出按钮（白屏也能退出）")
else:
    fail("绘制失败时没有兜底 —— 可能白屏且没有出口")

tap = code.split("function WardenScreen:on_tap", 1)[1].split(
    "function WardenScreen:on_swipe", 1)[0]
i_exit = tap.find("exit_rect")
i_ready = tap.find("if not self.image_ready")
if i_exit != -1 and i_ready != -1 and i_exit < i_ready:
    ok("退出按钮的命中判断排在 image_ready 之前（白屏时也能点）")
else:
    fail("退出按钮在 image_ready 之后判断 —— 白屏时点不动")

if 'ges = "swipe"' in code and 'ges = "pan"' in code:
    ok("滑动退出注册了 swipe + pan（慢拖也能退）")
else:
    fail("滑动退出手势不全")

if 'ges = "hold"' not in code and 'ges = "touch"' not in code:
    ok("没有注册 hold/touch（不会破坏点卡片的正常操作）")
else:
    fail("注册了 hold/touch —— 点卡片会被误判成退出")

print()
print("=" * 68)
print("场景 5：其他")
print("=" * 68)
close = code.split("function WardenScreen:onCloseWidget", 1)[1].split("\nend", 1)[0]
if "_closed" in close and "unschedule" in close:
    ok("关闭时撤定时器并打标记")
else:
    fail("关闭时清理不完整")

if "function Warden:close_screen" in code:
    cs = code.split("function Warden:close_screen", 1)[1].split("\nend", 1)[0]
    if cs.find("self.screen = nil") < cs.find("UIManager:close"):
        ok("关闭是幂等的（先清引用再 close）→ 退出后能重开")
    else:
        fail("close_screen 顺序不对 —— 可能退出一次就再也打不开")
else:
    fail("没有统一的关闭函数")

exit_body = code.split("function WardenScreen:do_exit", 1)[1].split("\nend", 1)[0]
if not any(x in exit_body for x in ("http", "get_url", "isConnected")):
    ok("退出流程不依赖网络")
else:
    fail("退出流程里有网络操作 —— 断网就退不出去")

if "self.notice" in code and "notice_until" in code:
    ok("临时提示有超时（不会永久占据屏幕）")
    todo = code.split('elseif item.kind == "todo" then', 1)[1].split(
        "elseif item.kind ==", 1)[0]
    if "scheduleIn" in todo:
        ok("提示到期会主动擦除")
    else:
        caution("提示没有主动擦除 —— 会留到下次轮询")
else:
    caution("没有临时提示机制")

# 定时器泄漏检查
sched = code.count("scheduleIn")
unsched = code.count("unschedule")
print(f"  --   scheduleIn {sched} 处 / unschedule {unsched} 处（人工核对是否配对）")

print()
print("=" * 68)
print(f"结论：{bad} 项不合格，{warn} 项需注意")
print("=" * 68)
sys.exit(1 if bad else 0)
