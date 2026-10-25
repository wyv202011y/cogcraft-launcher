#!/usr/bin/env bash
# PyInstaller 打包 macOS .app。产出 dist/cogcraft-macos-<arch>.zip
set -euo pipefail

ARCH="${1:?用法: build-macos.sh <arm64|x64>}"
JRE_ARCH="${2:-$ARCH}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$SCRIPT_DIR"

python3 -m pip install --upgrade pip
python3 -m pip install pywebview pyinstaller

bash ci/prepare-jre.sh "$JRE_ARCH"

# onedir 而不是 onefile：pywebview 在 onefile 下解压慢、易被杀软误判
pyinstaller --noconfirm --windowed --onedir \
  --name cogcraft \
  --osx-bundle-identifier top.cogcraft.launcher \
  --hidden-import webview.platforms.cocoa \
  --add-data "ui:ui" \
  --add-data "engine:engine" \
  --add-data "runtime:runtime" \
  --add-data "manifest.example.json:." \
  app.py

ls -la dist/
# ditto 保留 .app 的符号链接与扩展属性，zip 用不了
ditto -c -k --sequesterRsrc --keepParent dist/cogcraft.app "dist/cogcraft-macos-${ARCH}.zip"
ls -lh "dist/cogcraft-macos-${ARCH}.zip"
