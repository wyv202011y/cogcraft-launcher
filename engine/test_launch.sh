#!/bin/bash
# cogcraft 拼装引擎 —— 无头启动验证
# 用 Xvfb + Mesa 软件渲染跑一次真实客户端，看能否初始化到可玩状态
set -u
GD=/home/server/.hermes/cache/scratch/nf_install_cn
LOG=/home/server/.hermes/cache/scratch/mc_launch_test.log
ENGINE=/home/server/Raid_5/个人文件夹/王樱博/启动器/engine
export JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
export PATH="$JAVA_HOME/bin:$PATH"

: > "$LOG"

# 生成启动命令（写成 bash 数组形式）
python3 - <<'PY' > /tmp/mc_cmd.sh
import sys, shlex
sys.path.insert(0, "/home/server/Raid_5/个人文件夹/王樱博/启动器/engine")
from launch import build_command
cmd, merged = build_command(
    "/home/server/.hermes/cache/scratch/nf_install_cn",
    "neoforge-21.1.264",
    "/usr/lib/jvm/java-21-openjdk-amd64/bin/java",
    {
        "player_name": "Tester",
        "max_memory_mb": 3072,
        "server_address": "",
        "extra_jvm": [
            "-Djava.net.preferIPv4Stack=true",
            "-Dorg.lwjgl.util.NoChecks=true",
        ],
    },
)
print("CMD=(" + " ".join(shlex.quote(c) for c in cmd) + ")")
print(f"# mainClass={merged['mainClass']} libs={len(merged['libraries'])}", file=sys.stderr)
PY

source /tmp/mc_cmd.sh
echo "参数个数: ${#CMD[@]}" > "$LOG"
echo "mainClass 位置: $(echo "${CMD[@]}" | grep -o 'cpw.mods.bootstraplauncher.BootstrapLauncher')" >> "$LOG"

# 软件渲染 + 虚拟显示
export LIBGL_ALWAYS_SOFTWARE=1
export GALLIUM_DRIVER=llvmpipe
export MESA_GL_VERSION_OVERRIDE=4.5
export MESA_GLSL_VERSION_OVERRIDE=450

echo "── 启动 ──" >> "$LOG"
cd "$GD"
timeout 150 xvfb-run -a -s "-screen 0 1280x720x24" "${CMD[@]}" >> "$LOG" 2>&1
echo "LAUNCH_EXIT=$?" >> "$LOG"
echo "── 结束 ──" >> "$LOG"
