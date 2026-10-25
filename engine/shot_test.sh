#!/bin/bash
# 无头启动客户端并截图（验证拼装引擎产出的客户端可正常运行）
set -u
GD=/home/server/.hermes/cache/scratch/nf_install_cn
OUT=/home/server/.hermes/cache/scratch/cogcraft_client_boot.png
RUNLOG=/home/server/.hermes/cache/scratch/mc_shot_run.log
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
export PATH="$JAVA_HOME/bin:$PATH"
export LIBGL_ALWAYS_SOFTWARE=1
export MESA_GL_VERSION_OVERRIDE=4.5
export MESA_GLSL_VERSION_OVERRIDE=450

: > "$RUNLOG"
rm -f "$OUT"

# 固定显示号，便于 ffmpeg 抓图
Xvfb :77 -screen 0 1280x720x24 >/dev/null 2>&1 &
XVFB_PID=$!
sleep 3
echo "Xvfb pid=$XVFB_PID" >> "$RUNLOG"

python3 - <<'PY' > /tmp/mc_cmd2.sh
import sys, shlex
sys.path.insert(0, "/home/server/Raid_5/个人文件夹/王樱博/启动器/engine")
from launch import build_command
cmd, m = build_command(
    "/home/server/.hermes/cache/scratch/nf_install_cn",
    "neoforge-21.1.264",
    "/usr/lib/jvm/java-21-openjdk-amd64/bin/java",
    {"player_name": "王樱博", "max_memory_mb": 2048, "server_address": "",
     "extra_jvm": ["-Djava.net.preferIPv4Stack=true"]})
print("CMD=(" + " ".join(shlex.quote(c) for c in cmd) + ")")
PY

source /tmp/mc_cmd2.sh
cd "$GD"
DISPLAY=:77 "${CMD[@]}" >> "$RUNLOG" 2>&1 &
MC_PID=$!
echo "客户端 pid=$MC_PID" >> "$RUNLOG"
echo "等待 60 秒初始化..."
sleep 60

if command -v ffmpeg >/dev/null 2>&1; then
  DISPLAY=:77 ffmpeg -y -f x11grab -video_size 1280x720 -i :77 -frames:v 1 "$OUT" >/dev/null 2>&1
fi
echo "截图: $(ls -la "$OUT" 2>/dev/null || echo 未生成)" >> "$RUNLOG"

# 收尾
kill "$MC_PID" 2>/dev/null
sleep 3
kill -9 "$MC_PID" 2>/dev/null
kill "$XVFB_PID" 2>/dev/null
echo "完成"
