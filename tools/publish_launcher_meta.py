#!/usr/bin/env python3
"""生成启动器自更新元数据 `latest.json`（站点 /client/launcher/latest.json）。

为什么要单独一个工具：
    游戏文件和启动器本体的发布节奏完全不同 —— 清单更新一天可能好几次，
    启动器可能一个月才发一版。塞进 manifest.json 会让「检查更新」变成
    「每次都要下载整个清单」，也让两边的版本语义混在一起。

输入：
    --sums    CI 产出的 SHA256SUMS.txt（内容形如 `<sha256>  cogcraft-windows-x64.zip`）
    --zip-dir 或者直接给本地 zip 目录，由本工具现算 sha256
    二选一；两个都给时 sums 优先（CI 就是按它发布的）。

产物：
    latest.json —— 客户端 `updater.check()` 直接吃这个结构。

用法：
    python3 tools/publish_launcher_meta.py \\
        --version 1.0.5 \\
        --sums dist/SHA256SUMS.txt \\
        --base-url https://www.cogcraft.top/client/launcher/files \\
        --notes "修复 macOS 证书问题|新增自动更新" \\
        --out dist/latest.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# CI 产物名 → 元数据平台键
ASSET_PLATFORMS = {
    "cogcraft-windows-x64.zip": "windows-x64",
    "cogcraft-mac-arm64.zip": "macos-arm64",
    "cogcraft-mac-x64.zip": "macos-x64",
    "cogcraft-linux-x64.zip": "linux-x64",
}

# 站点上现有的目录布局（--site-layout 时按它拼地址，别再手写一串 --url-override）
PLATFORM_DIRS = {
    "windows-x64": "windows",
    "macos-arm64": "macos",
    "macos-x64": "macos",
    "linux-x64": "linux",
}


def app_version() -> str:
    """从 app.py 读 VERSION —— 版本号唯一来源，不在这里另写一份。"""
    try:
        with open(os.path.join(ROOT, "app.py"), encoding="utf-8") as f:
            m = re.search(r'^VERSION\s*=\s*"([^"]+)"', f.read(), re.M)
        return m.group(1) if m else ""
    except Exception:
        return ""


def read_sums(path: str) -> dict:
    """解析 `sha256sum` 格式，返回 {文件名: sha256}。"""
    out = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 64:
                out[os.path.basename(parts[-1])] = parts[0].lower()
    return out


def sha256_of(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="生成启动器自更新的 latest.json")
    ap.add_argument("--version", default="", help="本次发布版本；留空则读 app.py 的 VERSION")
    ap.add_argument("--tag", default="", help="对应的 git tag（只是记录，可选）")
    ap.add_argument("--sums", default="", help="SHA256SUMS.txt 路径")
    ap.add_argument("--zip-dir", default="", help="本地 zip 目录（不给 sums 时现算 sha256）")
    ap.add_argument("--base-url", default="https://www.cogcraft.top/client/launcher/files",
                    help="更新包在站点上的目录")
    ap.add_argument("--url-override", action="append", default=[],
                    metavar="PLATFORM=URL", help="单独指定某平台的下载地址（可多次）")
    ap.add_argument("--notes", default="", help="更新说明，多条用 | 分隔")
    ap.add_argument("--min-supported", default="", help="低于此版本强制更新（可选）")
    ap.add_argument("--site-layout", action="store_true",
                    help="按站点现有目录（/client/{windows,macos}/）拼地址，与 --base-url 二选一")
    ap.add_argument("--channel", default="stable")
    ap.add_argument("--out", default="", help="输出路径，默认 dist/latest.json")
    a = ap.parse_args()

    version = a.version or app_version()
    if not version:
        print("✗ 拿不到版本号：既没给 --version，也没从 app.py 读到 VERSION", file=sys.stderr)
        return 2

    sums: dict = {}
    if a.sums:
        if not os.path.exists(a.sums):
            print(f"✗ 找不到校验和文件：{a.sums}", file=sys.stderr)
            return 2
        sums = read_sums(a.sums)
    elif a.zip_dir:
        for name in os.listdir(a.zip_dir):
            if name.endswith(".zip"):
                sums[name] = sha256_of(os.path.join(a.zip_dir, name))
    else:
        print("✗ 需要 --sums 或 --zip-dir 之一", file=sys.stderr)
        return 2

    overrides = {}
    for s in a.url_override:
        if "=" in s:
            k, v = s.split("=", 1)
            overrides[k.strip()] = v.strip()

    platforms = {}
    sizes = {}
    for name, sha in sums.items():
        key = ASSET_PLATFORMS.get(name)
        if not key:
            continue
        site_path = site_name(name)
        if a.site_layout:
            url = (f"https://www.cogcraft.top/client/{PLATFORM_DIRS.get(key, '')}/"
                   f"{site_path}").replace("//", "/").replace("https:/", "https://")
        else:
            url = f"{a.base_url.rstrip('/')}/{site_path}"
        url = overrides.get(key) or url
        size = 0
        if a.zip_dir and os.path.exists(os.path.join(a.zip_dir, name)):
            size = os.path.getsize(os.path.join(a.zip_dir, name))
        else:
            size = head_size(url)
        platforms[key] = {"url": url, "size": size, "sha256": sha}
        sizes[key] = size

    if not platforms:
        print("✗ 没有任何匹配的产物（期望 cogcraft-<平台>-<架构>.zip）", file=sys.stderr)
        return 2

    notes = [s.strip() for s in a.notes.split("|") if s.strip()]
    meta = {
        "schema": 1,
        "channel": a.channel,
        "version": version,
        "tag": a.tag or "",
        "released": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "notes": notes,
        "min_supported": a.min_supported or "",
        "homepage": "https://www.cogcraft.top/client/",
        "platforms": platforms,
    }

    out = a.out or os.path.join(ROOT, "dist", "latest.json")
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
        f.write("\n")

    print(f"✓ 已生成 {out}")
    print(f"  版本 {version} · 通道 {a.channel} · min_supported {a.min_supported or '（无）'}")
    for k, v in platforms.items():
        size = f"{v['size'] / 1048576:.1f} MB" if v["size"] else "大小未知"
        print(f"  {k:<14} {size:<10} sha256 {v['sha256'][:16]}…  {v['url']}")
    have = set(platforms)
    miss = set(ASSET_PLATFORMS.values()) - have - {"macos-x64", "linux-x64"}
    if miss:
        print(f"  ⚠ 缺平台产物：{', '.join(sorted(miss))}")
    return 0


def site_name(asset: str) -> str:
    """站点上的文件名 = CI 产物原名，**不做任何重命名**。

    曾经想把 windows 缩成 win，结果站点上实际是 cogcraft-windows-x64.zip，
    HEAD 直接 404、大小取不到 —— 同一份产物两个名字就是两处要同步。
    """
    return asset


def head_size(url: str) -> int:
    """没有本地 zip 时用 HEAD 拿 Content-Length；拿不到就 0（客户端仍会做 sha256 校验）。"""
    try:
        import urllib.request
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=10) as r:
            return int(r.headers.get("Content-Length") or 0)
    except Exception:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
