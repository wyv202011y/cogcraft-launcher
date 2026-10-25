#!/usr/bin/env python3
"""
cogcraft 拼装引擎 —— 启动命令生成器

把 vanilla(1.21.1) 与 neoforge-21.1.264 两份 version json 合成一条完整的 java 启动命令。

关键点（都是坑）：
1. inheritsFrom 合并：子版本覆盖 mainClass，库和参数取并集
2. libraries 的 rules 必须按目标操作系统过滤，否则会把 win/linux/mac 的 natives 全塞进去
3. NeoForge 用模块化启动，jvm 参数里有 -p <模块路径> 和 --add-opens/--add-exports，必须原样保留
4. ${} 变量替换，缺一个就启动失败
5. -DignoreList 里的 ${version_name} 指 NeoForge 版本名
"""

import json, os, platform, shlex


def os_name() -> str:
    s = platform.system().lower()
    return {"windows": "windows", "darwin": "osx", "linux": "linux"}.get(s, s)


def rule_allows(rules, osn, features) -> bool:
    if not rules:
        return True
    allowed = False
    for r in rules:
        action = r.get("action")
        match = True
        osr = r.get("os")
        if osr:
            if "name" in osr and osr["name"] != osn:
                match = False
            if "arch" in osr and osr["arch"] not in platform.machine():
                match = False
        feat = r.get("features")
        if feat:
            for k, v in feat.items():
                if bool(features.get(k)) != bool(v):
                    match = False
        if match:
            allowed = (action == "allow")
    return allowed


def maven_to_path(name: str) -> str:
    """group:artifact:version[:classifier][@ext] -> 相对路径"""
    ext = "jar"
    if "@" in name:
        name, ext = name.split("@", 1)
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    fn = f"{artifact}-{version}" + (f"-{classifier}" if classifier else "") + f".{ext}"
    return "/".join(group.split(".") + [artifact, version, fn])


def library_path(lib) -> str:
    d = lib.get("downloads") or {}
    art = d.get("artifact")
    if art and art.get("path"):
        return art["path"]
    return maven_to_path(lib["name"])


def load_version(game_dir: str, version_id: str) -> dict:
    p = os.path.join(game_dir, "versions", version_id, f"{version_id}.json")
    with open(p) as f:
        return json.load(f)


def merge_versions(game_dir: str, child_id: str) -> dict:
    """递归合并 inheritsFrom"""
    child = load_version(game_dir, child_id)
    chain = [child]
    cur = child
    seen = {child_id}
    while cur.get("inheritsFrom"):
        pid = cur["inheritsFrom"]
        if pid in seen:
            break
        seen.add(pid)
        parent = load_version(game_dir, pid)
        chain.append(parent)
        cur = parent

    merged = {
        "id": child["id"],
        "mainClass": child.get("mainClass"),
        "libraries": [],
        "arguments": {"game": [], "jvm": []},
        "assets": None,
        "assetIndex": None,
        "downloads": {},
    }
    # 库：从父到子，同 name 以子为准
    libs = {}
    order = []
    for v in reversed(chain):
        for lib in v.get("libraries", []):
            key = lib.get("name", "")
            if key not in libs:
                order.append(key)
            libs[key] = lib
    merged["libraries"] = [libs[k] for k in order]

    # 参数：父在前子在后
    for v in reversed(chain):
        a = v.get("arguments", {})
        merged["arguments"]["game"] += a.get("game", [])
        merged["arguments"]["jvm"] += a.get("jvm", [])
        if v.get("assets"):
            merged["assets"] = v["assets"]
        if v.get("assetIndex"):
            merged["assetIndex"] = v["assetIndex"]

    # mainClass 子覆盖父
    for v in chain:
        if v.get("mainClass"):
            merged["mainClass"] = v["mainClass"]
            break
    return merged


def flatten(arg_list, osn, features, sub):
    """把带 rules 的参数列表展开成纯字符串列表。

    sub 负责变量替换。关键点：像 ["--quickPlayPath", "${quickPlayPath}"] 这种
    「选项+值」成组的参数，如果值替换成空串，必须整组丢掉，
    否则会留下一个没带参数的选项，joptsimple 直接抛
    OptionMissingRequiredArgumentException。
    """
    out = []
    for a in arg_list:
        if isinstance(a, str):
            s = sub(a)
            if s != "":
                out.append(s)
        elif isinstance(a, dict):
            if rule_allows(a.get("rules"), osn, features):
                val = a.get("value")
                vals = val if isinstance(val, list) else ([val] if val is not None else [])
                subs = [sub(v) for v in vals]
                if any(x == "" for x in subs):
                    continue
                out.extend(subs)
    return out


def build_command(game_dir, neoforge_id, java_exe, opts):
    """生成启动命令（列表），opts 里放玩家相关变量"""
    osn = opts.get("os_name") or os_name()
    sep = ";" if osn == "windows" else ":"
    merged = merge_versions(game_dir, neoforge_id)

    features = {
        "is_demo_user": False,
        "has_custom_resolution": bool(opts.get("width")),
        "has_quick_plays_support": True,
        "is_quick_play_singleplayer": False,
        "is_quick_play_multiplayer": bool(opts.get("server_address")),
        "is_quick_play_realms": False,
    }

    lib_root = os.path.join(game_dir, "libraries")
    cp_entries = []
    for lib in merged["libraries"]:
        if not rule_allows(lib.get("rules"), osn, features):
            continue
        rel = library_path(lib)
        full = os.path.join(lib_root, rel.replace("/", os.sep))
        cp_entries.append(full)

    # 注意：不要把 vanilla 的 1.21.1.jar 加进 classpath！
    # NeoForge 通过 "production client provider" 从 -DlibraryDirectory 里的
    # client-<ver>-srg.jar 提供 minecraft 模块；再塞原版 jar 会导致
    # "Modules minecraft and _1._21._1 export package ... " 模块解析冲突。
    # 版本文件里的 -DignoreList=client-extra,<version_name>.jar 也在表达同一件事。

    natives_dir = os.path.join(game_dir, "versions", neoforge_id, "natives")
    os.makedirs(natives_dir, exist_ok=True)
    assets_root = os.path.join(game_dir, "assets")
    asset_index = (merged.get("assetIndex") or {}).get("id") or merged.get("assets")

    variables = {
        "library_directory": lib_root,
        "classpath_separator": sep,
        "classpath": sep.join(cp_entries),
        "version_name": neoforge_id,
        "version_type": "cogcraft",
        "natives_directory": natives_dir,
        "launcher_name": "cogcraft",
        "launcher_version": "1.0.0",
        "game_directory": game_dir,
        "assets_root": assets_root,
        "assets_index_name": asset_index,
        "auth_player_name": opts.get("player_name", "Player"),
        "auth_uuid": opts.get("uuid", "0" * 32),
        "auth_access_token": opts.get("access_token", "0"),
        "clientid": opts.get("clientid", ""),
        "auth_xuid": opts.get("xuid", ""),
        "user_type": opts.get("user_type", "legacy"),
        "resolution_width": opts.get("width", ""),
        "resolution_height": opts.get("height", ""),
        "quickPlayPath": "",
        "quickPlaySingleplayer": "",
        "quickPlayMultiplayer": opts.get("server_address") or "",
        "quickPlayRealms": "",
    }

    def sub(s):
        for k, v in variables.items():
            s = s.replace("${" + k + "}", str(v))
        return s

    jvm_args = flatten(merged["arguments"]["jvm"], osn, features, sub)
    game_args = flatten(merged["arguments"]["game"], osn, features, sub)

    cmd = [java_exe]
    cmd += [f"-Xmx{opts.get('max_memory_mb', 6144)}M"]
    cmd += opts.get("extra_jvm", [])
    cmd += jvm_args
    cmd.append(merged["mainClass"])
    cmd += game_args

    # 丢掉空参数（分辨率/quickPlay 为空时）
    cmd = [c for c in cmd if c != ""]
    return cmd, merged


if __name__ == "__main__":
    import sys
    gd = sys.argv[1] if len(sys.argv) > 1 else "."
    nf = sys.argv[2] if len(sys.argv) > 2 else "neoforge-21.1.264"
    cmd, merged = build_command(gd, nf, "/usr/lib/jvm/java-21-openjdk-amd64/bin/java", {
        "player_name": "Tester",
        "max_memory_mb": 2048,
        "server_address": "",
        "extra_jvm": ["-Djava.net.preferIPv4Stack=true"],
    })
    print(f"mainClass: {merged['mainClass']}")
    print(f"库总数(合并后): {len(merged['libraries'])}")
    print(f"命令参数个数: {len(cmd)}")
    print("\n完整命令:")
    print(" \\\n  ".join(shlex.quote(c) if " " in c else c for c in cmd))
