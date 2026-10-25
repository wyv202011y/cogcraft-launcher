#!/usr/bin/env python3
"""
cogcraft 拼装引擎 —— 依赖库下载器

为什么需要: NeoForge 官方安装器只负责 NeoForge 自己的 47 个库 + 打补丁，
           vanilla 1.21.1 的 97 个库它一个都不下（实测 classpath 93 条里缺 44 条，
           lwjgl 都缺，导致启动报 NoClassDefFoundError）。
           这一步是启动器的职责。

下载源: 优先 BMCLAPI 镜像（国内 ~10MB/s），失败回退原始地址。
"""

import json, os, sys, time, urllib.request, ssl, concurrent.futures

BMCLAPI = "https://bmclapi2.bangbang93.com"
UA = "cogcraft-launcher/1.0"
CTX = ssl.create_default_context()


def rewrite_url(url: str) -> list:
    """给出候选 URL 列表（镜像优先，原始兜底）"""
    out = []
    if "libraries.minecraft.net/" in url:
        out.append(url.replace("https://libraries.minecraft.net/",
                               f"{BMCLAPI}/libraries/"))
    if "maven.neoforged.net/releases/" in url:
        out.append(url.replace("https://maven.neoforged.net/releases/",
                               f"{BMCLAPI}/maven/"))
    out.append(url)
    return out


def _fetch(url, timeout=60, retries=2):
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                return r.read()
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last


def ensure_libraries(merged: dict, game_dir: str, os_name: str,
                     rule_allows, library_path, workers=12):
    """把 merged['libraries'] 里所有缺失的构件下载到 game_dir/libraries/"""
    lib_root = os.path.join(game_dir, "libraries")
    tasks = []
    for lib in merged["libraries"]:
        if not rule_allows(lib.get("rules"), os_name, {}):
            continue
        rel = library_path(lib)
        dst = os.path.join(lib_root, rel.replace("/", os.sep))
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            continue
        art = (lib.get("downloads") or {}).get("artifact") or {}
        url = art.get("url")
        if not url:
            continue
        tasks.append((rel, dst, url, art.get("size"), art.get("sha1")))

    if not tasks:
        return {"needed": 0, "downloaded": 0, "errors": 0, "missing_after": 0}

    done = {"n": 0, "bytes": 0}
    errors = []

    def work(t):
        rel, dst, url, size, sha1 = t
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        last_err = None
        for u in rewrite_url(url):
            try:
                data = _fetch(u)
                if size and len(data) != size:
                    raise IOError(f"大小不符 期望{size} 实得{len(data)}")
                if sha1:
                    import hashlib
                    got = hashlib.sha1(data).hexdigest()
                    if got != sha1:
                        raise IOError(f"sha1 不符 {got[:8]} != {sha1[:8]}")
                tmp = dst + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dst)
                done["n"] += 1
                done["bytes"] += len(data)
                return None
            except Exception as e:
                last_err = f"{rel}: {e}"
        errors.append(last_err)
        return last_err

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, tasks))

    # 复核
    missing_after = 0
    for lib in merged["libraries"]:
        if not rule_allows(lib.get("rules"), os_name, {}):
            continue
        rel = library_path(lib)
        dst = os.path.join(lib_root, rel.replace("/", os.sep))
        if not os.path.exists(dst):
            missing_after += 1

    return {"needed": len(tasks), "downloaded": done["n"],
            "bytes": done["bytes"], "errors": len(errors),
            "error_samples": errors[:3], "missing_after": missing_after}


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from launch import merge_versions, rule_allows, library_path, os_name
    gd = sys.argv[1] if len(sys.argv) > 1 else "."
    nf = sys.argv[2] if len(sys.argv) > 2 else "neoforge-21.1.264"
    merged = merge_versions(gd, nf)
    t0 = time.time()
    r = ensure_libraries(merged, gd, os_name(), rule_allows, library_path)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(f"用时 {time.time()-t0:.1f}s")
