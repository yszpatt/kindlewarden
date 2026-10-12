#!/bin/sh
# Name: kindlewarden 副屏
# Author: kindlewarden
# DontUseFBInk
#
# 这是一个 Kindle Scriptlet（由 WinterBreak 自带的 SH_Integration 提供）：
# 放在 /mnt/us/documents/ 下，会以「书」的形式出现在书库里，**点击即可运行**。
#
# 为什么不用 KUAL：官方已明确 KUAL 废弃（"KUAL ... is now obsolete and does not work"），
# 现在启动应用的机制就是 Scriptlet。
#
# 为什么加 DontUseFBInk：Scriptlet 默认会把 stdout/stderr 接到 FBInk 打到屏幕上。
# 我们的客户端是**常驻进程**，画面完全由它自己绘制，不能让日志去抢屏幕。

CLIENT_DIR=/mnt/us/extensions/kindlewarden

if [ ! -f "$CLIENT_DIR/start.sh" ]; then
    # DontUseFBInk 之后 stdout 没人看，所以错误用 eips 直接打屏
    eips 2 3 "kindlewarden: missing $CLIENT_DIR/start.sh" 2>/dev/null
    exit 1
fi

sh "$CLIENT_DIR/start.sh"
exit 0
