#!/bin/sh
# 启动副屏客户端
#
# 由 documents/kindlewarden-start.sh 这个 scriptlet 调用，
# 也可以直接手动跑： sh /mnt/us/extensions/kindlewarden/start.sh

DIR=$(dirname "$0")
cd "$DIR" || exit 1

LOG=/mnt/us/kindlewarden/warden.log
mkdir -p /mnt/us/kindlewarden 2>/dev/null

note() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') [start.sh] $*" >> "$LOG" 2>/dev/null
}

# 已经在跑就别重复启动
if [ -f /tmp/warden.pid ]; then
    OLD=$(cat /tmp/warden.pid 2>/dev/null)
    if [ -n "$OLD" ] && kill -0 "$OLD" 2>/dev/null; then
        note "已有实例在运行（pid $OLD），本次不重复启动"
        exit 0
    fi
fi

# 脱离 scriptlet 的进程树。
#
# ⚠️ scriptlet（SH_Integration）跑完就退出，如果它的清理动作按进程组来杀，
#    普通后台进程会被一起带走。setsid 建新会话是最强的隔离手段。
#    `</dev/null` 也必要 —— 否则子进程还挂在 scriptlet 的 stdin 上。
if command -v setsid >/dev/null 2>&1; then
    setsid sh warden.sh </dev/null >/dev/null 2>&1 &
    note "已用 setsid 启动"
elif command -v nohup >/dev/null 2>&1; then
    nohup sh warden.sh </dev/null >/dev/null 2>&1 &
    note "已用 nohup 启动（没有 setsid，隔离性较弱）"
else
    sh warden.sh </dev/null >/dev/null 2>&1 &
    note "已直接后台启动（既没有 setsid 也没有 nohup，容易被带走）"
fi

# 启动后自检：3 秒后进程还在不在？
#
# 这一条是专门用来回答「客户端到底是自己退出的，还是被 scriptlet 的清理带走的」——
# 之前日志里只有「启动」然后什么都没有，两种可能分不清，白跑了一轮排查。
sleep 3
PID=$(cat /tmp/warden.pid 2>/dev/null)
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    note "自检通过：客户端 pid=$PID 启动 3 秒后仍在运行"
else
    note "自检失败：客户端在启动后 3 秒内就消失了"
fi

exit 0
