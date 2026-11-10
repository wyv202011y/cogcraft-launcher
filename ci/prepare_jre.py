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

多源自动兜底
-----------
JRE 包约 180MB，从 api.adoptium.net 直连国内外都可能慢到不可用（国内实测经常
几十 KB/s 甚至卡死）。所以按顺序试多个源，任意一个成功就停：

  1. Adoptium 官方（api.adoptium.net）—— CI runner 上最快、版本最新
  2. 清华镜像（mirrors.tuna.tsinghua.edu.cn）—— 国内实测 90MB/s+

单源有总时长预算（COGCRAFT_JRE_BUDGET，默认 300s），慢到超预算就放弃换下一个，
不会无限挂。想强制指定顺序用 COGCRAFT_JRE_SOURCE：
  adoptium   只用官方
  tuna       只用清华
  tuna-first 国内镜像优先、官方兜底

runtime/ 已就绪时直接跳过下载 —— 重复跑构建不必再等一次 180MB。

用法：python3 ci/prepare_jre.py <平台> <架构>
     平台: mac | windows | linux      架构: x64 | aarch64
"""
from __future__ import annotations
import os
import re
import shutil
import ssl
import sys
import tarfile
import tempfile
import time
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

JRE_MAJOR = "21"
TUNA_BASE = "https://mirrors.tuna.tsinghua.edu.cn/Adoptium"
# 单源总时长预算。180MB 在 1MB/s 下约 3 分钟，留足余量。
DOWNLOAD_BUDGET = float(os.environ.get("COGCRAFT_JRE_BUDGET", "300"))


def _ssl_context() -> ssl.SSLContext:
    """优先用 certifi 带的根证书。

    macOS 上 python.org 的安装包不会自动跑 Install Certificates.command，
    系统默认 CA 是空的，urllib 直接报
    "unable to get local issuer certificate (_ssl.c:1028)"。
    本项目的打包流程本来就依赖 certifi（PyInstaller 要 --collect-data certifi，
    冻结包下载也用它当根证书），所以直接拿它当 CA 源，用户不必手动装证书。
    """
    try:
        import certifi
        cafile = certifi.where()
        if os.path.exists(cafile):
            return ssl.create_default_context(cafile=cafile)
    except Exception:
        pass
    return ssl.create_default_context()


def _get(url: str, timeout: int = 30):
    # 必须带自己的 User-Agent：urllib 默认的 "Python-urllib/3.x"
    # 会被 Adoptium → GitHub release 的链路直接 403（curl 就不会）
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def _adoptium_url(platform: str, arch: str) -> str:
    return (f"https://api.adoptium.net/v3/binary/latest/{JRE_MAJOR}/ga"
            f"/{platform}/{arch}/jre/hotspot/normal/eclipse")


def _tuna_url(platform: str, arch: str) -> str:
    """从清华镜像目录里挑出最新的 JRE 包。

    文件名形如 OpenJDK21U-jre_aarch64_mac_hotspot_21.0.13_9.tar.gz，
    版本段不能靠字符串排序（"21.0.9" 会排在 "21.0.13" 后面），按数字元组比。
    """
    base = f"{TUNA_BASE}/{JRE_MAJOR}/jre/{arch}/{platform}/"
    with _get(base, timeout=20) as r:
        html = r.read().decode("utf-8", "replace")
    names = re.findall(r'href="([^"]+\.(?:tar\.gz|zip))"', html)
    if not names:
        raise RuntimeError(f"{base} 里没有找到 JRE 包")

    def _ver(name: str):
        m = re.search(r"(\d+)\.(\d+)\.(\d+)(?:_|\.|\+)(\d+)", name)
        return tuple(int(x) for x in m.groups()) if m else (0, 0, 0, 0)

    return base + max(names, key=_ver)


def _candidates(platform: str, arch: str) -> list:
    """按 COGCRAFT_JRE_SOURCE 决定尝试顺序；取不到地址的源直接跳过。"""
    order = (os.environ.get("COGCRAFT_JRE_SOURCE") or "").strip().lower()
    resolvers = [("Adoptium 官方", _adoptium_url), ("清华镜像", _tuna_url)]
    if order == "tuna":
        resolvers = [("清华镜像", _tuna_url)]
    elif order == "tuna-first":
        resolvers = [("清华镜像", _tuna_url), ("Adoptium 官方", _adoptium_url)]

    out = []
    for name, fn in resolvers:
        try:
            out.append((name, fn(platform, arch)))
        except Exception as e:
            print(f"  ! {name} 取地址失败: {e}", flush=True)
    return out


def download(url: str, dest: str, budget: float = DOWNLOAD_BUDGET) -> None:
    """下载并自己计时。

    不能只靠 urlopen 的 timeout —— 那个只管「多久没收到数据」，
    管不住「一直在收但只有 20KB/s」。所以按总耗时设预算，超了就抛错换源。
    """
    print(f"  开始下载 {url}", flush=True)
    start = time.monotonic()
    done = 0
    total = 0
    last = 0.0
    # 握手/读超时 20s：国内直连 api.adoptium.net 实测 SSL 握手能卡到 45s，
    # 这个值同时也是「多久没收到数据就判定断流」的阈值，20s 足够又不会让人干等。
    with _get(url, timeout=20) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        while True:
            if time.monotonic() - start > budget:
                raise TimeoutError(
                    f"超过 {budget:.0f}s 预算，只下了 {done / 1048576:.1f}MB"
                    + (f" / {total / 1048576:.1f}MB" if total else ""))
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            now = time.monotonic()
            if now - last >= 3.0:
                last = now
                speed = done / max(now - start, 0.001) / 1048576
                pct = f"{done * 100 // total}% " if total else ""
                print(f"    {pct}{done / 1048576:.1f}MB  {speed:.1f}MB/s", flush=True)
    if total and done != total:
        raise IOError(f"大小不符：收到 {done} 字节，应为 {total}")
    print(f"  已保存 {done / 1048576:.1f} MB", flush=True)


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


def _java_rel() -> str:
    return os.path.join("runtime", "bin", "java.exe" if os.name == "nt" else "java")


def _verify() -> None:
    """真跑一次 java -version：确认这不是一个能解压但起不来的包。"""
    import subprocess
    rel = _java_rel()
    path = os.path.join(ROOT, rel)
    if not os.path.isfile(path):
        raise SystemExit(f"runtime/ 存在但找不到 {rel}，删掉 runtime/ 重跑")
    subprocess.run([path, "-version"], check=True)
    print(f"JRE 就绪: {rel}", flush=True)


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("用法: prepare_jre.py <mac|windows|linux> <x64|aarch64>")
    platform, arch = sys.argv[1], sys.argv[2]

    # 已经准备好就跳过：重跑构建不该再等一次 180MB
    if os.path.isfile(os.path.join(ROOT, _java_rel())):
        print("runtime/ 已就绪，跳过下载（要重下请先删掉 runtime/）", flush=True)
        _verify()
        return

    is_zip = platform == "windows"
    archive = os.path.join(tempfile.gettempdir(), "jre.zip" if is_zip else "jre.tar.gz")

    cands = _candidates(platform, arch)
    if not cands:
        raise SystemExit(
            "没有可用的下载源。可手动下载后解压到项目根的 runtime/ 目录：\n"
            f"  {TUNA_BASE}/{JRE_MAJOR}/jre/{arch}/{platform}/")

    errors = []
    for name, url in cands:
        print(f"[{name}]", flush=True)
        try:
            download(url, archive)
            break
        except Exception as e:
            errors.append(f"{name}: {e}")
            print(f"  ✗ {name} 不可用，换下一个源", flush=True)
    else:
        raise SystemExit(
            "所有下载源都失败了：\n  " + "\n  ".join(errors) +
            "\n\n可手动下载后解压到项目根的 runtime/ 目录：\n"
            f"  {TUNA_BASE}/{JRE_MAJOR}/jre/{arch}/{platform}/")

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

    _verify()


if __name__ == "__main__":
    main()
