# kindlewarden Kindle 侧配置
#
# 改这里，不要改 warden.sh。

# 副屏服务的地址。
# ⚠️ 必须写 host 的**局域网 IP**，不能用 localhost / 127.0.0.1（那是 Kindle 自己）。
#    建议在路由器上给 host 做 DHCP 保留，避免 IP 漂移。
WARDEN_SERVER="http://192.168.1.50:8577"

# 状态目录（rev 缓存、日志）。/mnt/us 是 USB 可见分区，方便取日志。
WARDEN_STATE_DIR="/mnt/us/kindlewarden"

# 连续多少次拉取失败才切换到「连接中断」画面。
# 太小会在网络抖动时闪烁，太大则旧画面会长时间冒充实时状态。
WARDEN_FAIL_THRESHOLD=3

# wget 的参数。
#
# ⚠️ 这里用 `-T 10`（10 秒超时）而不是 `--timeout=10`，因为 Kindle 上的 wget
#    可能是 busybox 版，只认 `-T`。GNU wget 两者都认，所以 `-T` 是更安全的写法。
#
#    如果你的设备上 wget 连 `-T` 都不认，脚本会**安静地一直失败**（每次拉取
#    返回空 → 记一条失败日志 → 重试）。上机后先确认日志里不是清一色的「拉取失败」。
#    实在不行就在这里改成设备支持的写法。
WARDEN_WGET_OPTS="-q -T 10"

# fbink 可执行文件的路径。留空则自动查找。
#
# ⚠️ 越狱包（WinterBreak 带的 libkh）把 fbink 放在 /mnt/us/libkh/bin/fbink，
#    **不在 PATH 里**。所以脚本会自动在几个常见位置找一遍。
#    如果都找不到（或者你装的是别的位置），在这里写死绝对路径。
WARDEN_FBINK=""

# 无条件重绘的周期（秒）。0 = 关闭。
#
# ⚠️ **这个值不能设 0，除非你停掉了 framework。**
#
#    原因：fbink 是直接往 framebuffer 画的，而 Kindle 的 Java framework
#    也在画同一个 framebuffer。scriptlet 退出后 framework 会重绘书库界面，
#    把我们画的东西盖掉 —— 现象就是「画面闪一下就消失」。
#
#    我们的客户端原本只在 rev 变化时才画，所以被盖掉之后要等好几分钟才恢复。
#    设成非 0 之后，即使内容没变也会定期重画一次，把屏幕抢回来。
#
#    参考实现（alexishida/kindle-dashboard）同样是无条件每 45 秒重绘一次。
#
#    代价：每次重绘是一次 e-ink 刷新。60 秒是个折中 —— 再小会明显费电。
WARDEN_REDRAW_EVERY=60

# 是否停掉 Kindle 的 framework。默认 0（不停）。
#
# 为什么可能需要停：
#   fbink 是直接画 framebuffer 的，而 framework **也在画同一个、并且仍然处理触摸**。
#   所以即使我们的画面盖在上面，**点屏幕照样会打开底下的书** ——
#   实测就是这样误触开了一本书。对「只看不碰」的副屏来说这是硬伤。
#
# 停掉 framework 之后：画面完全独占，没有任何误触，也不再有抢屏。
#
# 代价（想清楚再开）：
#   - 运行期间 Kindle 不能当阅读器用（书库界面没了）
#   - **退出要长按电源键重启**（重启后 framework 自动回来）。
#     书库里的「kindlewarden 停止」在 framework 停掉后是点不到的。
#   - 客户端正常退出时会自动把 framework 起回来；被 SIGKILL 就只能重启
#
# 这是个取舍，没有两全的做法：要么能当阅读器但会被误触，要么独占但退出要重启。
WARDEN_STOP_FRAMEWORK=0
