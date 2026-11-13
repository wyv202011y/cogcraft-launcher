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

def _save_cache(cache_dir: str | None, url: str, data: dict,
                etag: str = "", last_modified: str = "") -> None:
    if not cache_dir:
        return
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, "manifest.cache.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"_fetched_at": int(time.time()), "_url": url,
                       "_etag": etag, "_last_modified": last_modified, "data": data},
                      f, ensure_ascii=False)
    except Exception:
        pass


def fetch(url: str, timeout: float = 15.0, cache_dir: str | None = None,
          conditional: bool = False) -> dict:
    """下载清单。网络失败时若有本地缓存则降级使用（离线也能进游戏）。

    `conditional=True` 时带上缓存里的 ETag / Last-Modified 先去问一声：
    服务端回 304 就直接用本地缓存，并打上 `_not_modified=True`。
    运行中每 5 分钟轮询一次靠的就是它 —— 清单 227 KB，不这么做的话
    一天要给每个玩家白灌 60 MB 流量，还白白拖着界面等。
    """
    cached_path = os.path.join(cache_dir, "manifest.cache.json") if cache_dir else None
    cached_blob: dict | None = None
    if cached_path and os.path.exists(cached_path):
        try:
            with open(cached_path, encoding="utf-8") as f:
                cached_blob = json.load(f)
        except Exception:
            cached_blob = None

    err = None
    try:
        # 走 net.py，不引入 requests：整个启动器只留一套网络栈。
        # 之前清单用 requests（自带 certifi 根证书）、下载用 urllib（系统根证书），
        # macOS 上就是两条链信任锚不同 —— 清单能拉、下载全崩，还多背一个依赖。
        import net
        if conditional and cached_blob and cached_blob.get("data"):
            got = net.get_conditional(
                url, timeout=timeout,
                etag=str(cached_blob.get("_etag") or ""),
                last_modified=str(cached_blob.get("_last_modified") or ""))
            if got.get("status") == 304:
                data = dict(cached_blob["data"])
                data["_not_modified"] = True
                data["_cached_at"] = cached_blob.get("_fetched_at")
                return data
            data = json.loads((got.get("body") or b"{}").decode("utf-8"))
            _save_cache(cache_dir, url, data,
                        str(got.get("etag") or ""), str(got.get("last_modified") or ""))
            return data

        # 首次（或不做条件请求时）也要用 get_conditional —— 得把响应头里的
        # ETag / Last-Modified 存进缓存，否则下一次 conditional 请求永远发不出去，
        # 运行中每 5 分钟就会白白重下整份清单。
        got = net.get_conditional(url, timeout=timeout)
        data = json.loads((got.get("body") or b"{}").decode("utf-8"))
        _save_cache(cache_dir, url, data,
                    str(got.get("etag") or ""), str(got.get("last_modified") or ""))
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


def load_cache(path: str) -> dict:
    """读「文件指纹缓存」：{rel: [size, mtime, sha1]}。

    存在的意义只有一个 —— 别每次启动都把 465MB 重算一遍 sha1。
    缓存损坏一律当空缓存，绝不因为缓存坏了拦住启动。
    """
    try:
        with open(path, encoding="utf-8") as f:
            blob = json.load(f)
        files = blob.get("files")
        return files if isinstance(files, dict) else {}
    except Exception:
        return {}


def save_cache(path: str, kind: str, files: dict) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"kind": kind, "files": files}, f)
        os.replace(tmp, path)
    except Exception:
        pass


def plan_sync(variant: dict, game_dir: str, verify_hash: bool = True,
              cache: dict | None = None) -> dict:
    """算出「哪些要下、哪些已就绪、哪些要删」。

    cache（可选）是文件指纹缓存 {rel: [size, mtime, sha1]}：size 与 mtime
    都没变、且缓存的 sha1 与清单一致时，直接判定就绪**不重算哈希**。
    首次校验或文件被动过才会真算，算完把新指纹写回 cache（调用方负责落盘）。

    返回 {download, ready, delete, bytes_needed, hashed, trusted}
    """
    download, ready = [], []
    bytes_needed = 0
    hashed = 0
    trusted = 0
    errors: list = []   # 校验读文件失败的文件，记下来供上层写日志

    for f in variant.get("files", []):
        rel = f["path"].lstrip("/").replace("\\", "/")
        dest = os.path.join(game_dir, rel.replace("/", os.sep))
        if not os.path.exists(dest):
            download.append(f)
            bytes_needed += int(f.get("size") or 0)
            continue
        want_size = f.get("size")
        try:
            st = os.stat(dest)
        except Exception:
            download.append(f)
            bytes_needed += int(f.get("size") or 0)
            continue
        size = st.st_size
        if want_size and size != int(want_size):
            download.append(f)
            bytes_needed += int(want_size)
            continue
        want_sha = (f.get("sha1") or "").lower()
        mtime = int(st.st_mtime)
        if cache is not None and want_sha:
            c = cache.get(rel)
            if (isinstance(c, list) and len(c) == 3
                    and int(c[0]) == size and int(c[1]) == mtime
                    and str(c[2]).lower() == want_sha):
                ready.append(f)
                trusted += 1
                continue
        if verify_hash and want_sha:
            try:
                got = _sha1_file(dest)
                hashed += 1
            except Exception as e:
                # 读不出来就说明这份文件不可信，必须重下 —— 而且要把它的大小一起
                # 计入 bytes_needed。漏了这一步，界面就会出现
                # 「开始准备（15 个文件）」配「需要下载 0 MB」这种自相矛盾的提示。
                errors.append("%s: %s" % (rel, e))
                download.append(f)
                bytes_needed += int(f.get("size") or 0)
                continue
            if got != want_sha:
                download.append(f)
                bytes_needed += int(f.get("size") or 0)
                continue
            if cache is not None:
                cache[rel] = [size, mtime, want_sha]
        ready.append(f)

    # 清单明确要求删除的（例如从高配切到流畅版要移除那 18 个模组）
    delete = []
    for rel in variant.get("delete", []):
        dest = os.path.join(game_dir, rel.lstrip("/").replace("/", os.sep))
        if os.path.exists(dest):
            delete.append(rel)

    return {"download": download, "ready": ready, "delete": delete,
            "bytes_needed": bytes_needed, "hashed": hashed, "trusted": trusted,
            "errors": errors}
