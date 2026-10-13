#!/usr/bin/env bash
# GitLab MR 批量创建工具打包脚本
# 生成单文件可执行程序 gitlab_mr_tool，放到 dist/ 目录。
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v pyinstaller >/dev/null 2>&1; then
  echo "未安装 PyInstaller，正在安装..."
  pip3 install pyinstaller
fi

echo "开始打包..."
pyinstaller \
  --onefile \
  --windowed \
  --name gitlab_mr_tool \
  --clean \
  --noconfirm \
  gitlab_mr_tool.py

echo ""
echo "打包完成：$(pwd)/dist/gitlab_mr_tool"
