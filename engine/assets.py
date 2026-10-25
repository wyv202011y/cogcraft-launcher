#!/usr/bin/env python3
"""
cogcraft 拼装引擎 —— 资源(assets)下载器

Minecraft 的 assets 由一份索引(assetIndex)描述，包含 3900+ 个对象，
每个对象按 sha1 前两位分目录存放：
    assets/indexes/<indexId>.json
    assets/objects/<hash[0:2]>/<hash>

国内走 BMCLAPI 镜像（实测 ~9MB/s），并发下载 + 已存在则跳过。
"""

import json, os, sys, hashlib, urllib.request, concurrent.futures, time

BMCLAPI = "https://bmclapi2.bangbang93.com"
UA = "cogcraft-launcher/1.0"
CTX = None


def _get(url, timeout=30, retries=3):
    """取远程内容。实现收敛在 net.py（IPv4 优先 + 退避重试）。"""
    import net
    return net.get_bytes(url, timeout=timeout, retries=retries)


def ensure_asset_index(mc_version, game_dir, version_json=None):
    """获取并写入 assets/indexes/<id>.json。

    返回 (index_id, 完整索引 dict) —— 注意返回的是**整个索引**，
    对象字典在 index["objects"] 里，别直接拿返回值当对象字典用。
    """
    idx_url = f"{BMCLAPI}/version/{mc_version}/json"
    vj = json.loads(_get(idx_url))
    ai = vj["assetIndex"]
    idx_id = ai["id"]

    idx_dir = os.path.join(game_dir, "assets", "indexes")
    os.makedirs(idx_dir, exist_ok=True)
    idx_path = os.path.join(idx_dir, f"{idx_id}.json")

    if not os.path.exists(idx_path):
        # BMCLAPI 的 asset index 路径
        for url in (f"{BMCLAPI}/assets/indexes/{idx_id}.json", ai["url"]):
            try:
                data = _get(url)
                json.loads(data)          # 校验
                with open(idx_path, "wb") as f:
                    f.write(data)
                break
            except Exception:
                continue
    return idx_id, json.loads(open(idx_path).read())


def download_objects(objects, game_dir, workers=16, progress=None):
    """并发下载所有 asset 对象"""
    obj_root = os.path.join(game_dir, "assets", "objects")
    os.makedirs(obj_root, exist_ok=True)

    tasks = []
    for name, o in objects.items():
        h = o["hash"]
        sub = os.path.join(obj_root, h[:2])
        dst = os.path.join(sub, h)
        if os.path.exists(dst) and os.path.getsize(dst) == o["size"]:
            continue
        tasks.append((h, o["size"], dst))

    total_bytes = sum(t[1] for t in tasks)
    done = {"n": 0, "bytes": 0}
    lock = None

    def work(item):
        h, size, dst = item
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        data = _get(f"{BMCLAPI}/assets/{h[:2]}/{h}", timeout=60)
        if len(data) != size:
            raise IOError(f"{h} 大小不符: 期望 {size} 实得 {len(data)}")
        tmp = dst + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, dst)
        done["n"] += 1
        done["bytes"] += size
        if progress and done["n"] % 200 == 0:
            progress(done["n"], len(tasks), done["bytes"], total_bytes)

    if not tasks:
        return {"skipped_all": True, "files": len(objects)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, t) for t in tasks]
        errs = []
        for f in concurrent.futures.as_completed(futs):
            try:
                f.result()
            except Exception as e:
                errs.append(str(e))
    return {"downloaded": done["n"], "total": len(tasks),
            "bytes": done["bytes"], "errors": errs[:5], "error_count": len(errs)}


if __name__ == "__main__":
    mc = sys.argv[1] if len(sys.argv) > 1 else "1.21.1"
    gd = sys.argv[2] if len(sys.argv) > 2 else "."

    def show(n, total, b, tb):
        pct = b / tb * 100 if tb else 0
        print(f"  [{n}/{total}] {b/1048576:.1f}/{tb/1048576:.1f} MB ({pct:.1f}%)")

    t0 = time.time()
    idx_id, index = ensure_asset_index(mc, gd)
    objs = index["objects"]
    print(f"资源索引: {idx_id}   对象数: {len(objs)}")
    r = download_objects(objs, gd, progress=show)
    print(f"结果: {json.dumps(r, ensure_ascii=False)}")
    print(f"用时: {time.time()-t0:.1f}s")
