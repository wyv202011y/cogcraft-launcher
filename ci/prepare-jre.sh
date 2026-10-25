#!/usr/bin/env bash
# 把 Temurin 21 JRE 解到 ./runtime，供 PyInstaller 打进包里。
#
# 为什么不用 tar --strip-components：macOS 的 JDK/JRE 是 bundle 布局
#   jdk-21.x-jre/Contents/Home/bin/java      （Linux 是 jdk-21.x-jre/bin/java）
# 层数不同，写死数字必然踩坑。所以解完用 find 定位 bin/java，再整体挪上来。
set -euo pipefail

ARCH="${1:?用法: prepare-jre.sh <aarch64|x64>}"
URL="https://api.adoptium.net/v3/binary/latest/21/ga/mac/${ARCH}/jre/hotspot/normal/eclipse"

echo "下载 JRE: $URL"
curl -fsSL --retry 3 --retry-delay 2 -o /tmp/jre.tar.gz "$URL"
ls -lh /tmp/jre.tar.gz

rm -rf /tmp/jrex runtime
mkdir -p /tmp/jrex
tar xzf /tmp/jre.tar.gz -C /tmp/jrex

JAVA_BIN="$(find /tmp/jrex -type f -name java -path '*/bin/java' | head -1)"
if [ -z "$JAVA_BIN" ]; then
  echo "JRE 包里找不到 bin/java，实际结构："
  find /tmp/jrex -maxdepth 4 -type d | head -20
  exit 1
fi
echo "找到: $JAVA_BIN"

JAVA_HOME_DIR="$(cd "$(dirname "$JAVA_BIN")/.." && pwd)"
echo "JAVA_HOME 候选: $JAVA_HOME_DIR"

mv "$JAVA_HOME_DIR" runtime
ls -l runtime/bin/java
./runtime/bin/java -version
echo "JRE 就绪：runtime/bin/java"
