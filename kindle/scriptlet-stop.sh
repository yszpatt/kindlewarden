#!/bin/sh
# Name: kindlewarden 停止
# Author: kindlewarden
# DontUseFBInk
#
# 停止常驻的副屏客户端。KUAL 已废弃，所以停止也做成 Scriptlet。

CLIENT_DIR=/mnt/us/extensions/kindlewarden

if [ -f "$CLIENT_DIR/stop.sh" ]; then
    sh "$CLIENT_DIR/stop.sh"
    eips 2 3 "kindlewarden stopped" 2>/dev/null
else
    eips 2 3 "kindlewarden: missing stop.sh" 2>/dev/null
fi
exit 0
