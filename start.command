#!/bin/zsh
# 双击启动小账本。关闭这个终端窗口即可停止服务。
cd "$(dirname "$0")"
exec python3 server.py
