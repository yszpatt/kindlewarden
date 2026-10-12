#!/bin/sh
# 停止副屏客户端

PIDFILE=/tmp/warden.pid

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE" 2>/dev/null)
    if [ -n "$PID" ]; then
        kill "$PID" 2>/dev/null
        # 客户端用分片睡眠，所以 TERM 最多 1 秒就会被处理、跑完 cleanup（含恢复屏保）。
        # 给它 5 秒，别急着 -9 —— 否则 cleanup 里的恢复动作来不及做，
        # 日志里也看不到「已停止」，排查时就分不清「被杀了」还是「还活着」。
        sleep 5
        kill -9 "$PID" 2>/dev/null
    fi
    rm -f "$PIDFILE"
fi

# 顺带清掉可能残留的进程
for p in $(ps | grep -E '[w]arden\.sh' | awk '{print $1}'); do
    kill -9 "$p" 2>/dev/null
done

# 恢复屏保
lipc-set-prop com.lab126.powerd preventScreenSaver 0 2>/dev/null

exit 0
