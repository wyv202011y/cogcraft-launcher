"""启动器自更新：版本对比、更新元数据、包下载与校验。

设计要点（与 manifest.py 同一条纪律：不在别处另建一套）：
- 检查（只读、随时可调）与安装（写盘）分离
- 服务器只托管一个小 JSON：`<站点>/client/launcher/latest.json`
- 网络一律走 net.py —— 整个启动器只有一套网络栈与一套根证书
- **版本号唯一来源是 app.py 的 `VERSION`**；CI 从这里读，不要另存一份
- 拉不到元数据不属于错误，只是「这次没检查到」，绝不阻塞启动
"""

from __future__ import annotations

import hashlib
import json
import os
import platform as _pf
import re
import sys

META_SCHEMA = 1

# 平台标识 → 元数据里 platforms 的键
_PLATFORMS = {
    ("darwin", "arm64"): "macos-arm64",
    ("darwin", "aarch64"): "macos-arm64",
    ("darwin", "x86_64"): "macos-x64",
    ("win32", "amd64"): "windows-x64",
    ("win32", "x86_64"): "windows-x64",
    ("win32", "arm64"): "windows-arm64",
    ("linux", "x86_64"): "linux-x64",
    ("linux", "aarch64"): "linux-arm64",
}


def platform_key() -> str:
    """当前机器在元数据里的平台键，例：`windows-x64`。"""
    m = (_pf.machine() or "").lower()
    return _PLATFORMS.get((sys.platform, m), f"{sys.platform}-{m or 'unknown'}")


# ────────────────────────── 版本号 ──────────────────────────

_VER_RE = re.compile(r"(\d+(?:\.\d+)*)")


def parse_version(v) -> tuple[int, ...]:
    """把 `1.0.4` / `v1.0.4-20261103` / `1.0.4+build` 解析成可比较的元组。

    取不到数字就返回 (0,)，宁可判成「老版本」也不要抛 —— 检查更新失败不该拦住启动。
    """
    if not v:
        return (0,)
    m = _VER_RE.search(str(v))
    if not m:
        return (0,)
    parts = [int(x) for x in m.group(1).split(".") if x != ""]
    while len(parts) > 1 and parts[-1] == 0:      # 1.0.0 == 1.0
        parts.pop()
    return tuple(parts) or (0,)


def compare(a, b) -> int:
    """a>b 返回 1，相等 0，a<b 返回 -1。"""
    ta, tb = parse_version(a), parse_version(b)
    n = max(len(ta), len(tb))
    ta = ta + (0,) * (n - len(ta))
    tb = tb + (0,) * (n - len(tb))
    return (ta > tb) - (ta < tb)


def is_newer(latest, current) -> bool:
    return compare(latest, current) > 0


# ────────────────────────── 元数据 ──────────────────────────

def fetch_meta(url: str, timeout: float = 8.0) -> dict:
    """拉取 latest.json。失败抛异常，由调用方决定降级。

    URL 上带一个时间戳破缓存：这个文件必须每次都拿到最新的，
    否则「发了新版没人看到」这种问题排查起来极其费劲（客户端会以为自己是最新）。
    """
    import net
    import time as _t
    sep = "&" if "?" in url else "?"
    raw = net.get_bytes(f"{url}{sep}t={int(_t.time())}", timeout=timeout, retries=1)
    meta = json.loads(raw.decode("utf-8"))
    if not isinstance(meta, dict) or not meta.get("version"):
        raise ValueError("更新元数据缺少 version 字段")
    return meta


def check(meta: dict, current: str, key: str | None = None,
          channel: str = "stable") -> dict:
    """对比元数据与当前版本，返回给界面用的结论（纯函数，不发网络）。

    返回：
        available   是否有新版
        latest      远端版本
        current     本地版本
        mandatory   是否为强制更新（当前版本低于 min_supported）
        notes       更新说明（列表或字符串）
        pkg         {"url","size","sha256"} 或 None（本平台没有可用包）
        url         给「手动下载」用的页面地址
    """
    key = key or platform_key()
    latest = str(meta.get("version") or "")
    out = {
        "available": False,
        "current": str(current or ""),
        "latest": latest,
        "mandatory": False,
        "notes": meta.get("notes") or [],
        "released": meta.get("released") or "",
        "url": meta.get("homepage") or meta.get("download_page") or "",
        "platform": key,
        "pkg": None,
        "error": None,
    }

    if meta.get("channel") and channel and meta["channel"] != channel:
        out["error"] = f"当前通道 {channel} 与元数据通道 {meta['channel']} 不同"
        return out

    if isinstance(out["notes"], str):
        out["notes"] = [out["notes"]]

    if not is_newer(latest, current):
        return out

    out["available"] = True
    low = meta.get("min_supported")
    if low and compare(current, low) < 0:
        out["mandatory"] = True

    plats = meta.get("platforms") or {}
    pkg = plats.get(key) or {}
    if pkg.get("url"):
        out["pkg"] = {
            "url": pkg["url"],
            "size": int(pkg.get("size") or 0),
            "sha256": (pkg.get("sha256") or "").lower(),
        }
    else:
        out["error"] = f"本平台（{key}）暂无更新包，请到官网手动下载"
    return out


# ────────────────────────── 下载与校验 ──────────────────────────

def sha256_of_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def download_pkg(pkg: dict, dest_dir: str, progress=None) -> str:
    """把更新包下到 dest_dir，校验 sha256 + 字节数，返回本地路径。

    校验失败删掉半成品并抛异常 —— 绝不把没验过的包留给安装环节。
    """
    import net
    url = pkg.get("url")
    if not url:
        raise ValueError("更新包没有下载地址")
    os.makedirs(dest_dir, exist_ok=True)
    name = os.path.basename(url.split("?")[0]) or "update.zip"
    dest = os.path.join(dest_dir, name)
    if os.path.exists(dest):
        os.remove(dest)

    if progress:
        progress(0.0, "开始下载更新包…")
    net.download_to(url, dest, timeout=300.0, retries=3)
    size = os.path.getsize(dest)

    want_size = int(pkg.get("size") or 0)
    if want_size and size != want_size:
        os.remove(dest)
        raise ValueError(f"更新包大小不符：期望 {want_size} 实际 {size}")
    want_sha = (pkg.get("sha256") or "").lower()
    if want_sha:
        if progress:
            progress(0.9, "校验更新包…")
        got = sha256_of_file(dest)
        if got != want_sha:
            os.remove(dest)
            raise ValueError(f"更新包校验失败：期望 {want_sha[:12]}… 实际 {got[:12]}…")
    if progress:
        progress(1.0, f"更新包已就绪（{size / 1048576:.1f} MB）")
    return dest
