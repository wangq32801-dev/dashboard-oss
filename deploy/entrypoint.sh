#!/bin/bash
# 容器入口：准备统一数据目录并启动后端
set -e

mkdir -p "${DASH_DATA_DIR:-/data}"

exec python3 dashboard-server.py "$@"
