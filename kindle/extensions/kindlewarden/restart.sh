#!/bin/sh
# 重启副屏客户端

DIR=$(dirname "$0")
sh "$DIR/stop.sh"
sleep 1
sh "$DIR/start.sh"

exit 0
