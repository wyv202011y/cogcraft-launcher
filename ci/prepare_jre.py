#!/usr/bin/env python3
"""下载并准备自带 JRE，解到 ./runtime，供 PyInstaller 打进包里。

为什么用 Python 而不是 shell
---------------------------
三个平台的东西都不一样，写 shell 必然分叉：
  · macOS   → .tar.gz，布局 jdk-21.x-jre/Contents/Home/bin/java
  · Windows → .zip，   布局 jdk-21.x-jre/bin/java.exe
  · Linux   → .tar.gz，布局 jdk-21.x-jre/bin/java
写死 --strip-components 层数在 mac 上踩过两次坑。所以一律解到临时目录，
再用 find 定位 java，最后整体搬成 runtime/。不猜结构。

用法：python3 ci/prepare_jre.py <平台> <架构>
     平台: mac | windows | linux      架构: x64 | aarch64
"""
from __future__ import annotations
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request
import zipfile

# Windows runner 的控制台是 cp1252，直接 print 中文会 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 不要写死 /tmp：Windows runner 上 "/tmp" 会落到当前盘符根目录，常常不存在
TMP = os.path.join(tempfile.gettempdir(), "cogcraft_jre")
JAVA_NAMES = ("java", "java.exe")
UA = "cogcraft-launcher-ci/1.0 (+https://github.com/wyv202011y/cogcraft-launcher)"


def download(url: str, dest: str, tries: int = 3) -> None:
    for i in range(1, tries + 1):
        try:
            print(f"下载 {url}（第 {i} 次）", flush=True)
            # 必须带自己的 User-Agent：urllib 默认的 "Python-urllib/3.x"
            # 会被 Adoptium → GitHub release 的链路直接 403（curl 就不会）
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=180) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f)
            print(f"  已保存 {os.path.getsize(dest) / 1048576:.1f} MB", flush=True)
            return
        except Exception as e:
            print(f"  失败: {e}", flush=True)
            if i == tries:
                raise
            import time
            time.sleep(3 * i)


def extract(archive: str, dest: str) -> None:
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dest)
    else:
        with tarfile.open(archive) as t:
            t.extractall(dest)


def find_java(base: str) -> str:
    """找到真正的 java 可执行文件，不依赖目录层数。"""
    for dirpath, _dirs, files in os.walk(base):
        for name in JAVA_NAMES:
            if name in files and os.path.basename(dirpath) == "bin":
                p = os.path.join(dirpath, name)
                if os.name != "nt":
                    os.chmod(p, 0o755)
                return p
    raise SystemExit(f"在 {base} 里找不到 bin/java，实际结构：\n" +
                     "\n".join(os.path.join(dp, d) for dp, ds, _ in os.walk(base)
                               for d in ds[:1]))


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("用法: prepare_jre.py <mac|windows|linux> <x64|aarch64>")
    platform, arch = sys.argv[1], sys.argv[2]
    url = (f"https://api.adoptium.net/v3/binary/latest/21/ga/{platform}/{arch}"
           f"/jre/hotspot/normal/eclipse")
    is_zip = platform == "windows"
    archive = os.path.join(tempfile.gettempdir(), "jre.zip" if is_zip else "jre.tar.gz")

    download(url, archive)
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    extract(archive, TMP)

    java_bin = find_java(TMP)
    print(f"找到: {java_bin}", flush=True)
    java_home = os.path.dirname(os.path.dirname(java_bin))
    print(f"JAVA_HOME: {java_home}", flush=True)

    runtime = os.path.join(ROOT, "runtime")
    shutil.rmtree(runtime, ignore_errors=True)
    shutil.move(java_home, runtime)

    rel = os.path.join("runtime", "bin", "java.exe" if os.name == "nt" else "java")
    assert os.path.isfile(os.path.join(ROOT, rel)), f"搬运后找不到 {rel}"
    print(f"JRE 就绪: {rel}", flush=True)

    # 必须真跑一次：确认这不是一个能解压但跑不起来的包
    import subprocess
    subprocess.run([os.path.join(ROOT, rel), "-version"], check=True)


if __name__ == "__main__":
    main()
