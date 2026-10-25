#!/usr/bin/env python3
"""从整合包 zip 生成 cogcraft 客户端清单。

核心思路：不靠文件名猜模组身份，直接算 sha1 去 Modrinth 反查。
Modrinth 有 hash 查询接口，命中就能拿到官方 CDN 直链 —— 这部分流量
完全不经过我们的服务器，正是「只托管 JSON」的关键。

用法:
    python3 tools/build_manifest.py \
        --full  互传/像素工坊v4.6.6-高配版.zip \
        --simple 互传/像素工坊v4.6.6-Simple.zip \
        --base-url https://www.cogcraft.top/client/files \
        --out manifest.json
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
import time
import zipfile

MODRINTH = "https://api.modrinth.com/v2"
# FTB 官方 maven：FTB 全家桶不在 Modrinth，但这里能直链，且自带 .sha1 可校验
FTB_MAVEN = "https://maven.ftb.dev/releases/dev/ftb/mods"
UA = "cogcraft-launcher-builder/1.0 (contact: cogcraft.top)"

# 这些后缀被 www.cogcraft.top 的安全规则挡掉（防数据库/配置泄露）。
# 规则不能动，改成「服务器上换个名字存」：URL 指向 xxx.cfg.dl，
# 客户端落地路径仍是 config/xxx.cfg，两边互不影响。
BLOCKED_EXT = {"db", "sqlite", "sqlite3", "db-wal", "db-shm", "mdb", "md",
               "markdown", "log", "bak", "old", "orig", "save", "sql", "sh",
               "ini", "env", "conf", "cfg", "yml", "yaml"}
STORE_SUFFIX = ".dl"


def url_path(rel: str) -> str:
    """URL 里的路径：中文文件名必须百分号编码，否则客户端拿到的 URL 不可请求。"""
    from urllib.parse import quote
    return quote(rel, safe="/")


def store_path(rel: str) -> str:
    """自托管文件在服务器上的存放路径。"""
    ext = os.path.splitext(rel)[1].lstrip(".").lower()
    return rel + STORE_SUFFIX if ext in BLOCKED_EXT else rel


# 这些文件是本地残留/缓存，不该进清单
EXCLUDE_PATTERNS = [
    r"\.bak", r"\.backup", r"\.old$", r"\.cache\.json$", r"\.part$",
    r"(^|/)\.DS_Store$", r"(^|/)Thumbs\.db$", r"\.log$", r"crash-",
]


def should_exclude(path: str) -> bool:
    return any(re.search(p, path, re.I) for p in EXCLUDE_PATTERNS)


def sha1_of_zip_entry(z: zipfile.ZipFile, name: str) -> tuple[str, int]:
    h = hashlib.sha1()
    n = 0
    with z.open(name) as f:
        while True:
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            n += len(b)
    return h.hexdigest(), n


def read_pack(zip_path: str) -> dict:
    """读出一个整合包的所有文件 → {相对路径: (sha1, size)}"""
    out: dict[str, tuple[str, int]] = {}
    with zipfile.ZipFile(zip_path) as z:
        for name in z.namelist():
            if name.endswith("/"):
                continue
            if not name.startswith("overrides/"):
                continue          # manifest.json / mcbbs.packmeta 是整合包自己的元数据
            rel = name[len("overrides/"):]
            if not rel or should_exclude(rel):
                continue
            out[rel] = sha1_of_zip_entry(z, name)
    return out


def modrinth_lookup(sha1: str) -> dict | None:
    """按 sha1 查 Modrinth。命中返回 {url, version, project_id}，否则 None。"""
    import requests
    try:
        r = requests.get(f"{MODRINTH}/version_file/{sha1}",
                         params={"algorithm": "sha1"}, timeout=20,
                         headers={"User-Agent": UA})
        if r.status_code != 200:
            return None
        v = r.json()
        files = v.get("files") or []
        # 选主文件（primary=True），没有就取第一个
        f = next((x for x in files if x.get("primary")), files[0] if files else None)
        if not f or not f.get("url"):
            return None
        # Modrinth 的 sha1 我们本地已经算过，用本地的更可信
        return {
            "url": f["url"],
            "version_number": v.get("version_number"),
            "project_id": v.get("project_id"),
            "filename": f.get("filename"),
        }
    except Exception:
        return None


def ftb_lookup(filename: str, sha1: str) -> dict | None:
    """按文件名匹配 FTB maven 构件，并用官方 .sha1 校验内容一致才采用。

    两个关键点：
    1. 整合包里的文件名带中文标注前缀（如 `[FTB任务]ftb-quests-neoforge-...`），
       必须先剥掉，否则会把前缀当成构件名的一部分去拼 URL。
    2. 只有内容完全一致才能直链替换，否则启动器的 hash 校验会判定文件损坏、
       每次启动都重下同一个文件。
    """
    import urllib.request
    fn = re.sub(r"^\[[^\]]*\]", "", filename)
    if not fn.endswith(".jar") or "-neoforge-" not in fn:
        return None
    stem = fn[:-4]
    i = stem.find("-neoforge-")
    artifact = stem[:i] + "-neoforge"
    ver = stem[i + len("-neoforge-"):]
    if not ver or "/" in ver:
        return None
    base = f"{FTB_MAVEN}/{artifact}/{ver}/{artifact}-{ver}"
    try:
        req = urllib.request.Request(base + ".jar.sha1",
                                     headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            remote = r.read().decode("utf-8", "replace").strip().split()[0].lower()
    except Exception:
        return None
    if remote != sha1.lower():
        return None
    return {"url": base + ".jar", "source": "ftb"}


CF_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cf_index.json")


def cf_lookup(filename: str, sha1: str) -> dict | None:
    """CurseForge 官方 CDN 直链。

    tools/cf_index.json 是人工核实过的 {文件名: {fileID, sha1, ...}}，
    每条都实际下载过并比对 sha1。CF 的 CDN 地址不需要任何 API key，
    但没有 fileID 就拼不出来；fileID 靠 api.cfwidget.com 反查得到
    （官方 api.curseforge.com 要 key，官网前端接口被 Cloudflare 挡）。
    """
    if not os.path.exists(CF_INDEX):
        return None
    idx = json.load(open(CF_INDEX, encoding="utf-8"))
    # 整合包里的文件名常带 `[暮色森林]` 这类中文前缀，索引里存的是剥掉前缀的名字
    rec = idx.get(filename) or idx.get(re.sub(r"^\[[^\]]*\]", "", filename))
    if not rec or rec.get("sha1") != sha1:
        return None
    fid = rec["fileID"]
    # URL 里必须是 CF 官方的原始文件名 —— 整合包里的 `[中文前缀]` 是后加的，
    # 带上去 CF CDN 直接连不通（实测 curl 返回 0，不是 404，很容易误判成网络问题）。
    name = rec.get("name") or filename
    return {
        "url": f"https://mediafilez.forgecdn.net/files/{fid // 1000}/{fid % 1000}/{name}",
        "source": "curseforge",
    }


def mcimirror_of(url: str) -> str | None:
    """cdn.modrinth.com 的国内镜像。实测路径相同、内容一致（sha1 校验兜底）。"""
    if "://cdn.modrinth.com/" in url:
        return url.replace("://cdn.modrinth.com/", "://mod.mcimirror.top/", 1)
    return None


def build(args) -> dict:
    print(f"[1/4] 读取整合包…", file=sys.stderr)
    full = read_pack(args.full)
    simple = read_pack(args.simple)
    print(f"      高配版 {len(full)} 个文件 / Simple {len(simple)} 个文件", file=sys.stderr)

    full_mods = {k: v for k, v in full.items() if k.startswith("mods/")}
    simple_mods = {k: v for k, v in simple.items() if k.startswith("mods/")}
    print(f"      高配版 {len(full_mods)} 模组 / Simple {len(simple_mods)} 模组", file=sys.stderr)

    # Simple 必须完全包含于 full，否则「切换版本」语义会崩
    extra = set(simple) - set(full)
    if extra:
        print(f"      ⚠ Simple 里有 {len(extra)} 个高配版没有的文件：", file=sys.stderr)
        for e in sorted(extra)[:5]:
            print(f"          {e}", file=sys.stderr)

    # 只查 full 的所有 mod（simple 是子集）
    print(f"[2/4] 去 Modrinth 反查 {len(full_mods)} 个模组的直链…", file=sys.stderr)
    resolved: dict[str, dict | None] = {}
    done = [0]

    def work(item):
        rel, (sha1, size) = item
        info = modrinth_lookup(sha1)
        done[0] += 1
        if done[0] % 20 == 0:
            print(f"      {done[0]}/{len(full_mods)}", file=sys.stderr)
        return rel, info

    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
        for rel, info in ex.map(work, full_mods.items()):
            resolved[rel] = info

    # Modrinth 没命中的，试 CurseForge 官方 CDN（fileID 表见 tools/cf_index.json）
    cf_got = {}
    for rel in [k for k, v in resolved.items() if not v]:
        info = cf_lookup(os.path.basename(rel), full[rel][0])
        if info:
            cf_got[rel] = info
            resolved[rel] = info
    print(f"      CurseForge CDN 补到 {len(cf_got)} 个", file=sys.stderr)

    # 还没命中的，再试 FTB 官方 maven
    ftb_got = {}
    for rel in [k for k, v in resolved.items() if not v]:
        info = ftb_lookup(os.path.basename(rel), full[rel][0])
        if info:
            ftb_got[rel] = info
            resolved[rel] = info
    print(f"      FTB maven 补到 {len(ftb_got)} 个", file=sys.stderr)

    hit = {k: v for k, v in resolved.items() if v}
    miss = [k for k, v in resolved.items() if not v]
    print(f"      命中 Modrinth {len(hit)} 个，需自托管 {len(miss)} 个", file=sys.stderr)
    for m in sorted(miss):
        print(f"        ✗ {os.path.basename(m)}", file=sys.stderr)

    # ── 组装文件条目 ──
    self_bytes = [0]
    mr_bytes = [0]

    def entry(rel: str, sha1: str, size: int, variant_id: str) -> dict:
        info = resolved.get(rel)
        e = {"path": rel, "sha1": sha1, "size": size}
        if info:
            mr_bytes[0] += size
            src = info.get("source", "modrinth")
            e["source"] = src
            e["url"] = info["url"]
            # 备用地址只给「真的能用」的：Modrinth → 国内镜像；
            # CurseForge → HK2 兜底副本（正常不走）。FTB 国内直连没问题，不设备用。
            if src == "modrinth":
                m = mcimirror_of(info["url"])
                if m:
                    e["mirror"] = m
            elif src == "curseforge" and args.mirror_url:
                e["mirror"] = (f"{args.mirror_url.rstrip('/')}/{variant_id}/"
                               f"{url_path(store_path(rel))}")
        else:
            self_bytes[0] += size
            e["source"] = "self"
            # 按版本分目录！两个整合包里有 16 个同名文件内容不同
            # （options.txt / servers.dat / sodium-options.json …），
            # 共用同一个 URL 会让另一版每次哈希校验失败、无限重下。
            e["url"] = (f"{args.base_url.rstrip('/')}/{variant_id}/"
                        f"{url_path(store_path(rel))}")
        return e

    full_files = [entry(k, v[0], v[1], "full") for k, v in sorted(full.items())]
    simple_files = [entry(k, v[0], v[1], "simple") for k, v in sorted(simple.items())]
    # 自托管字节只算一次（两版共有的文件不重复算）
    total_self = sum(1 for f in full_files if f["source"] == "self")
    total_self_bytes = sum(f["size"] for f in full_files if f["source"] == "self")

    delete = sorted(set(full) - set(simple))

    manifest = {
        "schema": 1,
        "client_id": "cogcraft",
        "client_name": "cogcraft 客户端",
        "channel": "stable",
        "updated": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime()),
        "notice": "",
        "minecraft": {"version": args.mc_version, "type": "release"},
        "modloader": {
            "type": "neoforge",
            "version": args.neoforge,
            "installer_url": f"https://bmclapi2.bangbang93.com/maven/net/neoforged/"
                             f"neoforge/{args.neoforge}/neoforge-{args.neoforge}-installer.jar",
        },
        "server": {
            "name": args.server_name,
            "address": args.server_addr,
            "auto_join": True,
        },
        "jvm": {"extra_args": ["-Djava.net.preferIPv4Stack=true"]},
        "variants": [
            {
                "id": "full",
                "name": "高配版",
                "desc": f"{len(full_mods)} 个模组 · 完整视觉与体验",
                "mods_count": len(full_mods),
                "min_ram_mb": 6144,
                "files": full_files,
                "delete": [],
            },
            {
                "id": "simple",
                "name": "流畅版",
                "desc": f"{len(simple_mods)} 个模组 · 关闭重负载视觉模组",
                "mods_count": len(simple_mods),
                "min_ram_mb": 3072,
                "files": simple_files,
                "delete": delete,
            },
        ],
    }
    return manifest, {
        "full_files": len(full_files), "simple_files": len(simple_files),
        "full_mods": len(full_mods), "simple_mods": len(simple_mods),
        "modrinth_hits": len(hit), "ftb_hits": len(ftb_got), "cf_hits": len(cf_got),
        "self_hosted": total_self,
        "self_hosted_bytes": total_self_bytes,
        "modrinth_bytes": sum(f["size"] for f in full_files if f["source"] == "modrinth"),
        "delete_count": len(delete),
        "extra_in_simple": sorted(extra),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", required=True)
    ap.add_argument("--simple", required=True)
    ap.add_argument("--out", default="manifest.json")
    ap.add_argument("--base-url", default="https://www.cogcraft.top/client/files",
                    help="我们自己托管的文件根地址（只用于没上 Modrinth 的文件）")
    ap.add_argument("--mirror-url", default="",
                    help="备用下载根地址（例如 https://mirror.cogcraft.top/modpack），"
                         "主地址失败时客户端自动回退，只在这时才消耗我们带宽")
    ap.add_argument("--mc-version", default="1.21.1")
    ap.add_argument("--neoforge", default="21.1.264")
    ap.add_argument("--server-name", default="像素工坊")
    ap.add_argument("--server-addr", default="cogcraft.top:25565")
    args = ap.parse_args()

    print("[3/4] 组装清单…", file=sys.stderr)
    mf, stats = build(args)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(mf, f, ensure_ascii=False, indent=1)

    print("[4/4] 完成\n", file=sys.stderr)
    print(f"  输出        {args.out}  ({os.path.getsize(args.out)/1024:.0f} KB)")
    print(f"  高配版      {stats['full_mods']} 模组 / {stats['full_files']} 文件")
    print(f"  流畅版      {stats['simple_mods']} 模组 / {stats['simple_files']} 文件")
    print(f"  切版需删除  {stats['delete_count']} 个文件")
    print(f"  CurseForge  {stats['cf_hits']} 个  {stats.get('cf_bytes',0)/1048576:.1f} MB 由 CF 官方 CDN 出货"
          + (f"（其中 FTB maven {stats.get('ftb_hits',0)} 个）" if stats.get('ftb_hits') else ""))
    print(f"  需自托管    {stats['self_hosted']} 个  {stats['self_hosted_bytes']/1048576:.1f} MB")
    if args.mirror_url:
        print(f"  备用镜像    {args.mirror_url}（可选，需上传 {stats['self_hosted_bytes']/1048576:.0f} MB 自托管文件")
        print(f"              + {stats['modrinth_bytes']/1048576:.0f} MB Modrinth 文件作为容灾）")
    if stats["extra_in_simple"]:
        print(f"  ⚠ Simple 多出 {len(stats['extra_in_simple'])} 个文件（应为其子集）")


if __name__ == "__main__":
    main()
