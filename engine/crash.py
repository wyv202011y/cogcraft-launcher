"""崩溃报告 —— 收集、归类、上传到官网工单系统。

安全模型（重要）
--------------
客户端**不持有任何 API Key**。带 write 权限的 Agent Key 一旦打进安装包，
任何人都能反编译提取出来刷爆工单系统。所以走一个公开的
`/feedback/crash_api.php`，它只做一件事：把崩溃报告变成 bug 工单。

防线全在服务端：
  - 每 IP 每小时条数上限
  - 崩溃签名（栈顶 + 模组指纹哈希）去重：同一个崩溃 100 个玩家只开一张工单，只加计数
  - 单条请求体积上限
  - 只写 ticket，不给任何读权限
"""
from __future__ import annotations
import hashlib
import json
import os
import re
import time

# 崩溃类型判定：顺序有意义，先判最具体的
KIND_PATTERNS = [
    ("oom_heap",   [r"java\.lang\.OutOfMemoryError:\s*Java heap space"]),
    ("oom_meta",   [r"java\.lang\.OutOfMemoryError:\s*Metaspace",
                    r"java\.lang\.OutOfMemoryError:\s*Compressed class space"]),
    ("oom_native", [r"java\.lang\.OutOfMemoryError:\s*Unable to create new native thread",
                    r"Could not reserve enough space",
                    r"Native memory allocation.*failed"]),
    ("graphics",   [r"OpenGL", r"GLFW", r"Pixel format not accelerated",
                    r"No more available texture units", r"Iris.*shader",
                    r"org\.lwjgl"]),
    ("mixin",      [r"MixinApplyError", r"MixinTransformerError",
                    r"org\.spongepowered\.asm\.mixin"]),
    ("modconflict",[r"Missing or unsupported mandatory dependencies",
                    r"ModResolutionException", r"Duplicate mods",
                    r"Incompatible mod set"]),
    ("worldcorrupt",[r"Chunk file at .* is in the wrong location",
                     r"Region file.*corrupt", r"java\.util\.zip\.ZipException"]),
    ("jvm",        [r"UnsupportedClassVersionError", r"Error: Could not find or load main class",
                    r"unrecognized VM option", r"EXCEPTION_ACCESS_VIOLATION"]),
]

# 从堆栈里捞疑似元凶：出现在 at xx.yy 行里的非原版包名
_SKIP_PKG = ("java.", "jdk.", "sun.", "javax.", "net.minecraft.", "com.mojang.",
             "org.lwjgl.", "it.unimi.", "com.google.", "org.slf4j.", "io.netty.",
             # 加载器自己的包不算元凶
             "net.neoforged.", "net.minecraftforge.", "cpw.mods.", "org.spongepowered.",
             "dev.architectury.", "org.objectweb.")


def classify(text: str) -> tuple[str, str]:
    """返回 (类型, 人话原因)。类型用于统计聚合，原因用于工单标题。"""
    for kind, pats in KIND_PATTERNS:
        for p in pats:
            m = re.search(p, text, re.I)
            if m:
                return kind, m.group(0)[:120]
    m = re.search(r"^(\w[\w.]*(?:Exception|Error))", text, re.M)
    if m:
        return "other", m.group(1)[:120]
    return "unknown", "未能归类（可能是硬崩溃或直接被系统杀掉）"


def suspect_mods(text: str, limit: int = 6) -> list[str]:
    """从堆栈里提取非原版的包前缀，作为疑似冲突模组。"""
    seen, out = set(), []
    for m in re.finditer(r"^\s+at\s+([a-z][\w$]*\.[\w.$]+)\(", text, re.M):
        fq = m.group(1)
        if fq.startswith(_SKIP_PKG):
            continue
        pkg = ".".join(fq.split(".")[:2])
        if pkg not in seen:
            seen.add(pkg)
            out.append(pkg)
            if len(out) >= limit:
                break
    return out


def newest_crash(game_dir: str) -> tuple[str, str] | None:
    """找最新的崩溃报告；没有就退回 logs/latest.log。返回 (路径, 内容) 或 None。"""
    cands = []
    cr = os.path.join(game_dir, "crash-reports")
    if os.path.isdir(cr):
        for f in os.listdir(cr):
            if f.endswith(".txt"):
                p = os.path.join(cr, f)
                try:
                    cands.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    cands.sort(reverse=True)
    if cands:
        p = cands[0][1]
        try:
            return p, _read_head_tail(p, 400_000)
        except OSError:
            return None
    lg = os.path.join(game_dir, "logs", "latest.log")
    if os.path.isfile(lg):
        try:
            return lg, _read_head_tail(lg, 400_000)
        except OSError:
            return None
    return None


def _read_head_tail(path: str, cap: int) -> str:
    """读头 100KB + 尾 300KB：崩溃报告开头有环境信息，结尾有真正的堆栈。"""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if size <= cap:
            return f.read().decode("utf-8", "replace")
        head = f.read(100_000).decode("utf-8", "replace")
        f.seek(max(0, size - (cap - 100_000)))
        tail = f.read().decode("utf-8", "replace")
        return head + "\n\n[... 中间省略 ...]\n\n" + tail


def signature(text: str, variant: str, mods_count: int) -> str:
    """崩溃签名：同一类崩溃（同栈顶 + 同版本 + 同模组数）归并为一张工单。"""
    top = ""
    m = re.search(r"^(?:Caused by:\s*)?([\w.$]+(?:Exception|Error)[^\n]*)", text, re.M)
    if m:
        top = m.group(1)
    else:
        first_frames = re.findall(r"^\s+at\s+([\w$.]+)\(", text, re.M)[:3]
        top = "|".join(first_frames)
    raw = f"{variant}|{mods_count}|{top}"
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def build(game_dir: str, state: dict, launcher_ver: str = "0.1.0") -> dict | None:
    """组装一份崩溃报告。没有崩溃文件时返回 None。"""
    got = newest_crash(game_dir)
    if not got:
        return None
    path, text = got
    kind, reason = classify(text)
    hw = (state.get("hardware") or {})
    bench = (state.get("bench") or {})
    variant = state.get("variant") or "?"
    variants = state.get("variants") or []
    mods_count = 0
    for v in variants:
        if v.get("id") == variant:
            mods_count = v.get("mods_count") or 0
    mem = state.get("memory") or {}

    # 工单正文：≤2000 字（agent_api 的上限），只放定位需要的东西
    lines = [
        f"崩溃类型：{kind}",
        f"原因：{reason}",
        f"客户端：{variant}（{mods_count} 模组） · 启动器 {launcher_ver}",
        f"内存设置：-Xmx{mem.get('xmx_mb', '?')}M · {mem.get('gc', '?')}",
        f"Java：{(hw.get('java') or {}).get('version', '?')}"
        f"（{(hw.get('java') or {}).get('bits', '?')} 位）",
        f"CPU：{(hw.get('cpu') or {}).get('name', '?')}"
        f"（{(hw.get('cpu') or {}).get('threads', '?')} 线程）",
        f"内存：{int(((hw.get('ram') or {}).get('total_mb') or 0) / 1024)} GB",
        f"显卡：{', '.join(g.get('name', '?') for g in (hw.get('gpu') or [])[:2])}",
    ]
    if bench.get("score"):
        lines.append(f"性能实测：{bench['score']}×（停顿 "
                     f"{(bench.get('raw') or {}).get('stall_ms', '?')}ms）")
    sus = suspect_mods(text)
    if sus:
        lines.append("疑似相关模组包：" + "、".join(sus))

    # 堆栈摘要：取第一段异常 + Caused by
    stack = []
    for pat in (r"^[\w.$]+(?:Exception|Error)[^\n]*", r"^Caused by:[^\n]*"):
        for m in re.finditer(pat, text, re.M):
            s = m.group(0).strip()
            if s not in stack:
                stack.append(s)
            if len(stack) >= 6:
                break
        if len(stack) >= 6:
            break
    if stack:
        lines.append("")
        lines.append("堆栈摘要：")
        lines.extend(stack)

    content = "\n".join(lines)[:1950]
    title = f"[崩溃] {kind} · {variant} · {reason}"[:95]

    return {
        "title": title,
        "content": content,
        "kind": kind,
        "sig": signature(text, variant, mods_count),
        "variant": variant,
        "launcher_ver": launcher_ver,
        "mods_count": mods_count,
        "log_path": path,
        "log_tail": text[-12000:],       # 完整尾部另外落盘，不进工单正文
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_local(report: dict, appdata: str) -> str:
    """本地留一份，上传失败也不丢。"""
    d = os.path.join(appdata, "crash-reports")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{report['sig']}-{int(time.time())}.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write(report["title"] + "\n\n" + report["content"])
        f.write("\n\n──────── 完整日志尾部 ────────\n")
        f.write(report["log_tail"])
    return p


def upload(report: dict, endpoint: str, timeout: float = 15.0) -> dict:
    """上传到官网。失败不抛异常，返回 {ok:False,error}，由界面决定怎么提示。"""
    import urllib.request
    payload = json.dumps({
        "sig": report["sig"],
        "title": report["title"],
        "content": report["content"],
        "kind": report["kind"],
        "variant": report["variant"],
        "launcher_ver": report["launcher_ver"],
        "mods_count": report["mods_count"],
        "log_tail": report["log_tail"],
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(endpoint, data=payload, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("User-Agent", "cogcraft-launcher/" + report["launcher_ver"])
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        d = json.loads(body)
        return {"ok": bool(d.get("success")), "id": d.get("id"),
                "dedup": bool(d.get("dedup")), "raw": d}
    except Exception as e:
        return {"ok": False, "error": str(e)}


if __name__ == "__main__":
    import sys
    gd = sys.argv[1] if len(sys.argv) > 1 else "."
    got = newest_crash(gd)
    if not got:
        print("没有崩溃文件")
    else:
        p, t = got
        k, r = classify(t)
        print(f"文件: {p}\n类型: {k}\n原因: {r}\n疑似模组: {suspect_mods(t)}")
