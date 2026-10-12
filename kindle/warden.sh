#!/bin/sh
# kindlewarden —— Kindle 侧客户端
#
# 职责：轮询 /rev，**只在 rev 变化时**下载并重绘；连不上时显示缓存的离线画面。
#
# 这个脚本刻意保持「哑」：所有排版、字体、中文渲染都在服务端完成，
# Kindle 端只做「下载 PNG → blit」。这是架构上的核心取舍 ——
# 把复杂度放在能 ssh 进去调试的机器上，而不是放在最难改的设备上。
#
# ⚠️ 本脚本尚未在真机上验证过。标了 [ver] 的命令需要上机确认参数。
#    部署步骤见 docs/kindle-deploy.md

set -u

HERE=$(dirname "$0")
# shellcheck source=/dev/null
[ -f "$HERE/config.sh" ] && . "$HERE/config.sh"

SERVER="${WARDEN_SERVER:-http://192.168.1.50:8080}"
STATE_DIR="${WARDEN_STATE_DIR:-/mnt/us/kindlewarden}"
WGET_OPTS="${WARDEN_WGET_OPTS:--q -T 10}"
LOG="$STATE_DIR/warden.log"
REVFILE="$STATE_DIR/rev"
PIDFILE="/tmp/warden.pid"
PNG="/tmp/warden-screen.png"
OFFLINE="/tmp/warden-offline.png"

FAIL_THRESHOLD="${WARDEN_FAIL_THRESHOLD:-3}"
REDRAW_EVERY="${WARDEN_REDRAW_EVERY:-60}"
STOP_FRAMEWORK="${WARDEN_STOP_FRAMEWORK:-0}"
DEFAULT_INTERVAL=120

# 容错：config.sh 里要是写错了，别让整个脚本因为一个非数字而崩掉
case "$REDRAW_EVERY" in ''|*[!0-9]*) REDRAW_EVERY=60;; esac
case "$FAIL_THRESHOLD" in ''|*[!0-9]*) FAIL_THRESHOLD=3;; esac

mkdir -p "$STATE_DIR" 2>/dev/null

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG" 2>/dev/null
    # 日志轮转：超过 2000 行就留最后 500 行
    if [ "$(wc -l < "$LOG" 2>/dev/null || echo 0)" -gt 2000 ]; then
        tail -n 500 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
    fi
}

nap() {
    # 分片睡眠。
    #
    # ⚠️ shell 在 sleep 期间**不执行 trap**。主循环原来的 `sleep "$NEXT"` 可能睡
    #    300 秒，于是收到停止信号后要等 5 分钟才会跑 cleanup ——
    #    表现就是「点了停止没反应」，而且日志里永远看不到「已停止」。
    #    分片之后 TERM 最多 1 秒就被处理。
    n=${1:-60}
    case "$n" in ''|*[!0-9]*) n=60;; esac
    while [ "$n" -gt 0 ]; do
        sleep 1
        n=$((n - 1))
    done
}

# ---------------------------------------------------------------- 单实例

if [ -f "$PIDFILE" ]; then
    OLDPID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$OLDPID" ] && kill -0 "$OLDPID" 2>/dev/null; then
        log "已有实例在运行（pid $OLDPID），本次退出"
        exit 0
    fi
fi
echo $$ > "$PIDFILE"

cleanup() {
    rm -f "$PIDFILE"
    # 停过 framework 就要起回来，否则用户面对一块不响应任何操作的屏幕
    start_framework
    # 恢复屏保，避免用户后来发现 Kindle 永远不睡了
    lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null || true
}

# EXIT trap 覆盖**所有**退出路径：正常结束、set -u 踩到未定义变量、
# 语法问题、显式 exit…… 全都会留下一行日志。
#
# 为什么必须加：上一轮日志里只有「启动」和一次「周期重绘」，然后什么都没有。
# 那既可能是「被 SIGKILL」也可能是「自己静默退出了」—— 两者没法区分，
# 白跑了一轮排查。加了之后：
#   - 日志里有「进程结束（exit=…）」 → 自己退出的，退出码会说明原因
#   - 日志里**没有**这一行          → 被强杀了（SIGKILL 捕不到）
on_exit() {
    rc=$?
    log "进程结束（exit=$rc，pid=$$）"
    cleanup
}
trap on_exit EXIT
trap 'log "收到停止信号（pid=$$）"; exit 0' INT TERM

# ---------------------------------------------------------------- 找 fbink
#
# ⚠️ 越狱包（WinterBreak 带的 libkh）把 fbink 放在 /mnt/us/libkh/bin/fbink，
#    **不在 PATH 里**。裸调 `fbink` 会直接 command not found，
#    而那时脚本只会记一条"绘制失败"，看不出是路径问题。
#    所以这里主动找一遍。

find_fbink() {
    if [ -n "${WARDEN_FBINK:-}" ]; then
        if [ -x "$WARDEN_FBINK" ] || command -v "$WARDEN_FBINK" >/dev/null 2>&1; then
            echo "$WARDEN_FBINK"; return 0
        fi
        log "警告：config.sh 里指定的 WARDEN_FBINK 不可执行：$WARDEN_FBINK"
    fi
    for c in fbink \
             /mnt/us/libkh/bin/fbink \
             /mnt/us/extensions/kindlewarden/bin/fbink \
             /usr/bin/fbink \
             /usr/local/bin/fbink \
             /mnt/us/koreader/fbink; do
        case "$c" in
            # 绝对路径直接测可执行位；裸名字才走 PATH 查找。
            # 对绝对路径用 command -v 在 busybox 上行为不保证一致。
            /*) [ -x "$c" ] && { echo "$c"; return 0; } ;;
            *)  command -v "$c" >/dev/null 2>&1 && { echo "$c"; return 0; } ;;
        esac
    done
    return 1
}

FBINK=$(find_fbink)
if [ -n "$FBINK" ]; then
    log "fbink: $FBINK"
else
    log "警告：找不到 fbink，只能退回 eips（画面可能不如预期）"
fi

# ---------------------------------------------------------------- 绘制

blit() {
    # $1 = PNG 路径
    # $2 = 1 表示闪刷（清残影、引起注意）；0 表示不闪（用于周期性抢回屏幕）
    #   -g file=...,dither  软件 ordered 8x8 抖动，把 8bit 灰度映射到 16 灰阶
    #   -W GC16             16 级灰阶、全质量波形
    #   -q                  安静模式
    if [ -n "$FBINK" ]; then
        if [ "$2" = "1" ]; then
            "$FBINK" -g "file=$1,dither" -W GC16 -f -q 2>/dev/null && return 0
        else
            "$FBINK" -g "file=$1,dither" -W GC16 -q 2>/dev/null && return 0
        fi
    fi
    # 退路：老设备上 eips 也许可用
    log "fbink 失败，尝试 eips"
    eips -g "$1" 2>/dev/null && return 0
    return 1
}

# ---------------------------------------------------------------- framework

FW_STOPPED=0

stop_framework() {
    # 只在 WARDEN_STOP_FRAMEWORK=1 时做。
    # 停掉之后画面独占：既不会被抢屏，触摸也不会再误触到底下的书。
    [ "$STOP_FRAMEWORK" = "1" ] || return 0
    [ "$FW_STOPPED" = "0" ] || return 0

    if [ -x /etc/init.d/framework ]; then
        /etc/init.d/framework stop 2>/dev/null && FW_STOPPED=1
    fi
    if [ "$FW_STOPPED" = "0" ] && command -v stop >/dev/null 2>&1; then
        stop framework 2>/dev/null && FW_STOPPED=1
    fi

    if [ "$FW_STOPPED" = "1" ]; then
        log "已停掉 framework —— 画面独占，触摸不会再误触"
    else
        log "警告：停 framework 没成功，仍然会有误触"
    fi
}

start_framework() {
    [ "$FW_STOPPED" = "1" ] || return 0
    if [ -x /etc/init.d/framework ]; then
        /etc/init.d/framework start 2>/dev/null && FW_STOPPED=0
    fi
    if [ "$FW_STOPPED" = "1" ] && command -v start >/dev/null 2>&1; then
        start framework 2>/dev/null && FW_STOPPED=0
    fi
    [ "$FW_STOPPED" = "0" ] && log "framework 已恢复"
}

# ---------------------------------------------------------------- 屏幕维护

maybe_redraw() {
    # 需要时周期重绘一次，把被 framework 盖掉的画面抢回来。
    #
    # ⚠️ 这个检查**不能**只放在主循环开头。
    #    主循环的睡眠长度是服务端给的 next_poll_after —— **空闲时是 300 秒**。
    #    那样 REDRAW_EVERY=60 实际会变成「最多 300 秒才生效一次」，
    #    现象就是「画面没了，等很久也不回来」。上一版就是这么错的。
    [ "$REDRAW_EVERY" -gt 0 ] || return 0

    NOW=$(date +%s)
    [ $((NOW - LAST_DRAW)) -ge "$REDRAW_EVERY" ] || return 0

    if [ ! -f "$PNG" ]; then
        # 画面文件没了（设备重启过、/tmp 被清过）→ 强制下轮重新下载
        log "画面文件丢失，强制重新下载"
        rm -f "$REVFILE"
        LAST_DRAW=$NOW
        return 0
    fi

    if blit "$PNG" 0; then
        LAST_DRAW=$NOW
        log "周期重绘（已 ${REDRAW_EVERY}s 无变化）"
    else
        log "周期重绘失败"
    fi
}

# ---------------------------------------------------------------- 主循环

log "启动 pid=$$，服务端 $SERVER，重绘周期=${REDRAW_EVERY}s，客户端目录 $HERE"

# 先无条件**清掉**可能残留的"禁止休眠"标记。
#
# ⚠️ 为什么启动时要做这件事：
#    上一轮如果被 SIGKILL（或者设备直接断电/重启），``on_exit`` trap 不会执行，
#    ``preventScreenSaver`` 就会**永久停留在 1** ——
#    从此 Kindle **永远不会自动休眠**，掉电飞快，而且用户找不到原因
#    （这个属性不属于任何可见设置项，重启才会清掉）。
#
#    启动时清零，等于把这个残留状态**自愈**掉：
#    每次重新启动客户端都会先回到"允许休眠"，再由下面的循环按需设回去。
#    这样最坏情况也只是"客户端没跑的时候能正常休眠"。
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null

# 阻止屏保。社区常用写法，需要在设备上确认属性名。
lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null || \
    log "警告：preventScreenSaver 设置失败，屏幕可能会休眠"

fails=0
showed_offline=0
LAST_DRAW=0

while :; do
    # 每轮都重新阻止屏保 —— powerd 会把它重置掉，设一次不管用。
    # （参考实现 alexishida/kindle-dashboard 的注释同样写着
    #  「powerd 有时会重置，所以每个周期都要重新设置一次」。）
    lipc-set-prop com.lab126.powerd preventScreenSaver 1 2>/dev/null

    # ?c=kindle 用来把「设备在拉」和「管理页在拉」区分开 ——
    # 否则管理页自己的轮询会让「Kindle 最后在线」永远显示「刚刚」。
    RES=$(wget $WGET_OPTS -O - "$SERVER/rev?c=kindle" 2>/dev/null)

    if [ -z "$RES" ]; then
        fails=$((fails + 1))
        log "拉取失败（第 $fails 次）"
        # 连续失败达到阈值就切到离线画面。
        # **诚实性要求**：不能让旧画面留在屏幕上冒充实时状态。
        if [ "$fails" -ge "$FAIL_THRESHOLD" ] && [ -f "$OFFLINE" ]; then
            if [ "$showed_offline" = "0" ]; then
                blit "$OFFLINE" 1 && showed_offline=1
            fi
        fi
        nap 30
        continue
    fi
    # 首次成功时顺手缓存离线画面（服务端渲染的中文，设备端不需要 CJK 字体）
    if [ ! -f "$OFFLINE" ]; then
        wget $WGET_OPTS -O "$OFFLINE" "$SERVER/offline.png" 2>/dev/null
    fi

    # 从断连恢复：强制重绘一次，把离线画面换掉
    if [ "$showed_offline" = "1" ]; then
        rm -f "$REVFILE"
        showed_offline=0
        log "连接已恢复"
    fi
    fails=0

    # JSON 解析：先把所有空白去掉再匹配。
    #
    # ⚠️ 这里踩过一个坑：服务端用 Python 的 json.dumps 输出，默认格式是
    #    `"global_rev": "abc123"` —— **冒号后面有空格**。
    #    如果 sed 写成 `"global_rev":"..."`（无空格），结果是拉取成功、
    #    日志里一条错都没有，但永远解析不出 rev，屏幕一直不动。
    #    这种「看起来一切正常」的失败最难查，所以宁可先压平空白。
    #    rev 是十六进制、next_poll_after 是整数，都不可能含空白。
    FLAT=$(echo "$RES" | tr -d '[:space:]')
    NEW=$(echo "$FLAT" | sed -n 's/.*"global_rev":"\([^"]*\)".*/\1/p')

    if [ -z "$NEW" ]; then
        # 拉到了响应却解析不出 rev —— 多半是 wget 参数不对，或服务端格式变了。
        # **必须显式报出来**：否则脚本会安静地永远不刷新，而日志里看不到任何异常。
        log "解析 rev 失败，响应前 120 字符：$(echo "$RES" | cut -c1-120)"
        nap 60
        continue
    fi

    OLD=$(cat "$REVFILE" 2>/dev/null)

    # rev 比对是省电的核心：没变化就零下载
    if [ "$NEW" != "$OLD" ]; then
        if wget $WGET_OPTS -O "$PNG" "$SERVER/screen.png?c=kindle" 2>/dev/null; then
            if blit "$PNG" 1; then
                echo "$NEW" > "$REVFILE"
                LAST_DRAW=$(date +%s)
                log "已更新画面 rev=$NEW"
                # 第一次画上之后再停 framework，这样中途失败还看得见界面
                stop_framework
            else
                log "绘制失败"
            fi
        else
            log "下载画面失败"
        fi
    fi

    # 服务端告诉客户端下次何时来 —— 它最清楚事件流的活跃度
    NEXT=$(echo "$FLAT" | sed -n 's/.*"next_poll_after":\([0-9]*\).*/\1/p')
    [ -z "$NEXT" ] && NEXT=$DEFAULT_INTERVAL

    # ---- 分片等待，期间持续维护屏幕
    #
    # 关键：**重绘检查必须和轮询周期解耦**。
    # 空闲时 NEXT 是 300 秒，如果整段 sleep 掉，REDRAW_EVERY=60 就形同虚设 ——
    # 画面被 framework 盖掉后要等 5 分钟才可能回来，看起来就是彻底失效。
    # 拆成 5 秒一片，每片都问一次「该不该重绘」。
    #
    # 用 sleep 分片而不是一次长睡眠还有个原因：shell 在 sleep 期间不执行 trap，
    # 分片之后停止信号最多 5 秒就被处理。
    WAIT="$NEXT"
    case "$WAIT" in ''|*[!0-9]*) WAIT=$DEFAULT_INTERVAL;; esac
    while [ "$WAIT" -gt 0 ]; do
        STEP=5
        [ "$WAIT" -lt "$STEP" ] && STEP="$WAIT"
        sleep "$STEP"
        WAIT=$((WAIT - STEP))
        maybe_redraw
    done
done
