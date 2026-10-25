"""客户端清单（manifest）的获取、校验与本地状态比对。

设计要点：
- 服务器只托管这一个 JSON（几十 KB），所有文件本体走第三方直链
- 清单带 schema 版本号，将来改结构不会把老启动器搞崩
- 校验失败要能明确告诉用户「哪一条错了」，不能只是「清单无效」
"""

from __future__ import annotations

import json
import os
import time

SCHEMA_VERSION = 1


class ManifestError(Exception):
    pass


# ────────────────────────── 获取 ──────────────────────────

def fetch(url: str, timeout: float = 15.0, cache_dir: str | None = None) -> dict:
    """下载清单。网络失败时若有本地缓存则降级使用（离线也能进游戏）。"""
    import requests

    err = None
    try:
        r = requests.get(url, timeout=timeout,
                         headers={"User-Agent": "cogcraft-launcher/1.0"})
        r.raise_for_status()
        data = r.json()
        if cache_dir:
            os.makedirs(cache_dir, exist_ok=True)
            with open(os.path.join(cache_dir, "manifest.cache.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"_fetched_at": int(time.time()), "_url": url, "data": data},
                          f, ensure_ascii=False)
        return data
    except Exception as e:
        err = e

    if cache_dir:
        cached = os.path.join(cache_dir, "manifest.cache.json")
        if os.path.exists(cached):
            try:
                with open(cached, encoding="utf-8") as f:
                    blob = json.load(f)
                data = dict(blob["data"])
                data["_offline"] = True
                data["_cached_at"] = blob.get("_fetched_at")
                return data
            except Exception:
                pass

    raise ManifestError(f"无法连接服务器获取客户端清单：{err}")


# ────────────────────────── 校验 ──────────────────────────

_REQUIRED = {
    "client_id": str,
    "minecraft": dict,
    "modloader": dict,
    "variants": list,
}


def validate(m: dict) -> dict:
    """校验并补齐默认值。返回规范化后的清单；不合格则抛 ManifestError。"""
    if not isinstance(m, dict):
        raise ManifestError("清单不是合法的 JSON 对象")

    schema = m.get("schema", 1)
    if not isinstance(schema, int) or schema > SCHEMA_VERSION:
        raise ManifestError(f"清单格式版本 {schema} 比启动器新，请更新启动器")

    problems = []
    for key, typ in _REQUIRED.items():
        if key not in m:
            problems.append(f"缺少字段 `{key}`")
        elif not isinstance(m[key], typ):
            problems.append(f"字段 `{key}` 类型应为 {typ.__name__}")
    if problems:
        raise ManifestError("清单元数据不完整：" + "；".join(problems))

    mc = m["minecraft"]
    if not mc.get("version"):
        raise ManifestError("清单未指定 Minecraft 版本")

    ml = m["modloader"]
    ml.setdefault("type", "neoforge")
    if not ml.get("version"):
        raise ManifestError("清单未指定模组加载器版本")

    if not m["variants"]:
        raise ManifestError("清单没有任何可用的客户端版本")

    for v in m["variants"]:
        for k in ("id", "name"):
            if not v.get(k):
                raise ManifestError(f"版本条目缺少 `{k}` 字段")
        v.setdefault("files", [])
        v.setdefault("delete", [])
        v.setdefault("min_ram_mb", 3072)
        bad = [f for f in v["files"]
               if not isinstance(f, dict) or not f.get("path") or not f.get("url")]
        if bad:
            raise ManifestError(f"版本 `{v['id']}` 有 {len(bad)} 个文件条目缺少 path 或 url")

    m.setdefault("server", {})
    m.setdefault("jvm", {})
    m["jvm"].setdefault("extra_args", [])
    return m


def pick_variant(m: dict, variant_id: str | None) -> dict:
    """取版本；找不到则回退到第一个（服务器通常把推荐版排在前面）。"""
    vars_ = m["variants"]
    if variant_id:
        for v in vars_:
            if v["id"] == variant_id:
                return v
    return vars_[0]


def variant_summary(m: dict) -> list[dict]:
    """给 UI 用的版本摘要。"""
    out = []
    for v in m["variants"]:
        files = v.get("files") or []
        out.append({
            "id": v["id"],
            "name": v.get("name", v["id"]),
            "desc": v.get("desc", ""),
            "mods_count": v.get("mods_count") or sum(
                1 for f in files if f.get("path", "").startswith("mods/")),
            "min_ram_mb": v.get("min_ram_mb", 3072),
            "total_files": len(files),
            "total_bytes": sum(int(f.get("size") or 0) for f in files),
        })
    return out


# ────────────────────────── 本地状态比对 ──────────────────────────

def _sha1_file(path: str, chunk: int = 1 << 20) -> str:
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def plan_sync(variant: dict, game_dir: str, verify_hash: bool = True) -> dict:
    """算出「哪些要下、哪些已就绪、哪些要删」。

    返回 {download:[...], ready:[...], delete:[...], bytes_needed:int}
    """
    download, ready = [], []
    bytes_needed = 0

    for f in variant.get("files", []):
        rel = f["path"].lstrip("/").replace("\\", "/")
        dest = os.path.join(game_dir, rel.replace("/", os.sep))
        if not os.path.exists(dest):
            download.append(f)
            bytes_needed += int(f.get("size") or 0)
            continue
        want_size = f.get("size")
        if want_size and os.path.getsize(dest) != int(want_size):
            download.append(f)
            bytes_needed += int(want_size)
            continue
        want_sha = (f.get("sha1") or "").lower()
        if verify_hash and want_sha:
            try:
                if _sha1_file(dest) != want_sha:
                    download.append(f)
                    bytes_needed += int(f.get("size") or 0)
                    continue
            except Exception:
                download.append(f)
                continue
        ready.append(f)

    # 清单明确要求删除的（例如从高配切到流畅版要移除那 18 个模组）
    delete = []
    for rel in variant.get("delete", []):
        dest = os.path.join(game_dir, rel.lstrip("/").replace("/", os.sep))
        if os.path.exists(dest):
            delete.append(rel)

    return {"download": download, "ready": ready, "delete": delete,
            "bytes_needed": bytes_needed}
