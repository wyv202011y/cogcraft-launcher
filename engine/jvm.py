"""内存与 JVM 参数策略 —— 启动器的「安全阀」。

核心目标（按优先级）：
1. 玩家点「进入游戏」不能因为堆开太大而启动失败
2. 不能把玩家的系统内存吃光导致整机卡死
3. 不能因为堆开太小而在游戏里 OOM 崩溃
4. 这些约束玩家可以手动调，但调不过硬上限，且必须看到明确的警告

所有结论都基于 hardware.detect() 的真实数据，不猜。
"""

from __future__ import annotations

# ─────────────── 档位定义 ───────────────

# 物理内存 → (simple 推荐MB, full 推荐MB, full 是否允许)
MEMORY_TIERS: list[tuple[int, int, int, bool]] = [
    # (物理内存上限MB, simple, full, full允许)
    (6 * 1024,        2560,  2560, False),   # 6G 以下：只能跑精简
    (8 * 1024,        4096,  4096, False),   # 8G：精简，勉强
    (12 * 1024,       4096,  6144, True),    # 8~12G：高配可行但偏紧
    (16 * 1024,       5120,  8192, True),    # 12~16G：舒服
    (24 * 1024,       6144,  8192, True),    # 16~24G
    (32 * 1024,       8192, 10240, True),    # 24~32G
    (10 ** 9,        10240, 12288, True),    # 32G+：客户端 12G 足够，再多收益递减
]

MIN_XMX_MB = 2048          # 整合包 120+ 模组，低于此基本必 OOM
SYSTEM_RESERVE_MB = 4096   # 给操作系统和浏览器留的量
HARD_RATIO = 0.6           # 最多占物理内存 60%
MAX_XMX_CAP = 16384        # 客户端绝对上限：128G 机器也不给它开 57G 堆，纯浪费


def _tier_for(total_mb: int, variant: str) -> tuple[int, bool]:
    for ceiling, simple, full, full_ok in MEMORY_TIERS:
        if total_mb <= ceiling:      # 必须用 <=，否则正好 12G/16G 的机器会跳过整档
            return (simple if variant == "simple" else full), full_ok
    return (10240 if variant == "simple" else 12288), True


def hard_ceiling(total_mb: int) -> int:
    """绝对不允许超过的堆上限：比例、预留、绝对上限三者取最小。"""
    by_ratio = int(total_mb * HARD_RATIO)
    by_reserve = max(MIN_XMX_MB, total_mb - SYSTEM_RESERVE_MB)
    base = min(by_ratio, by_reserve) if total_mb > SYSTEM_RESERVE_MB else by_ratio
    return max(MIN_XMX_MB, min(base, MAX_XMX_CAP))


# ─────────────── 版本选择 ───────────────

def recommend_variant(hw: dict, bench: dict | None = None) -> tuple[str, str]:
    """根据硬件 + 性能实测推荐整合包版本，返回 (variant, 理由)。

    判据分两层，不能混：
      1. 硬约束（bench 也改不了）：物理内存太小会 OOM、32 位 Java 堆上限 1.5G。
         这些不是「快慢」问题，是「能不能跑」问题。
      2. 性能分档：有实测就信实测（微基准分数），没有才退回硬件启发式。
         实测比猜型号准得多——同型号不同散热/降频/内存频率能差一倍。
    """
    total = (hw.get("ram") or {}).get("total_mb") or 0
    threads = (hw.get("cpu") or {}).get("threads") or 0
    gpus = hw.get("gpu") or []
    vram = max([g.get("vram_mb") or 0 for g in gpus] or [0])
    java = hw.get("java") or {}

    # ── 第一层：硬约束 ──
    if total and total < 8 * 1024:
        return "simple", f"物理内存仅 {total // 1024} GB，高配版会 OOM"
    if java.get("bits") == 32:
        return "simple", "检测到 32 位 Java（堆上限 1.5G），只能跑精简版"
    if total and total < 12 * 1024:
        return "simple", f"物理内存 {total // 1024} GB，精简版更稳"

    # ── 第二层：性能分档，实测优先 ──
    if bench and bench.get("ok"):
        sc = bench.get("score", 0)
        r = bench.get("ratios") or {}
        detail = "单核 %.2f× · 内存 %.2f× · GC %.2f×· 停顿 %.1fms" % (
            r.get("int", 0), r.get("mem", 0), r.get("gc", 0),
            (bench.get("raw") or {}).get("stall_ms", 0))
        if bench.get("verdict") == "simple":
            return "simple", f"性能实测 {sc:.2f}× 低于 {bench.get('threshold_full', 0.45)} 阈值（{detail}）"
        return "full", f"性能实测 {sc:.2f}×，通过高配门槛（{detail}）"

    # ── 兜底：没跑实测才用启发式 ──
    if threads and threads < 4:
        return "simple", f"CPU 仅 {threads} 线程，高配版会明显卡顿"
    if vram and vram > 0 and vram < 2048:
        return "simple", f"显存仅 {vram} MB，高配版的光影/材质负载过重"
    if not gpus:
        return "simple", "未检测到可用显卡，保守选择精简版"
    return "full", "硬件满足高配版要求（未跑性能实测）"


# ─────────────── JVM 参数 ───────────────

# Aikar 的 G1 参数（Minecraft 社区广泛验证），去掉 AlwaysPreTouch：
# 客户端开 AlwaysPreTouch 会让启动白屏多 10~30 秒，且立刻占满物理内存。
G1_ARGS = [
    "-XX:+UseG1GC",
    "-XX:+ParallelRefProcEnabled",
    "-XX:MaxGCPauseMillis=200",
    "-XX:+UnlockExperimentalVMOptions",
    "-XX:+DisableExplicitGC",
    "-XX:G1NewSizePercent=30",
    "-XX:G1MaxNewSizePercent=40",
    "-XX:G1HeapRegionSize=8M",
    "-XX:G1ReservePercent=20",
    "-XX:G1HeapWastePercent=5",
    "-XX:G1MixedGCCountTarget=4",
    "-XX:G1MixedGCLiveThresholdPercent=90",
    "-XX:G1RSetUpdatingPauseTimePercent=5",
    "-XX:SurvivorRatio=32",
    "-XX:MaxTenuringThreshold=1",
    "-XX:+PerfDisableSharedMem",
    "-XX:+UseStringDeduplication",
]

ZGC_ARGS = [
    "-XX:+UseZGC",
    "-XX:+ZGenerational",
    "-XX:+DisableExplicitGC",
    "-XX:+UseStringDeduplication",
]

# 与内存无关、每台机器都该带的
COMMON_ARGS = [
    # 前面实测过：域名只返回 IPv6 时无 IPv6 的机器会卡 40 秒
    "-Djava.net.preferIPv4Stack=true",
    "-Dfile.encoding=UTF-8",
]


def _gc_args(xmx_mb: int, prefer_zgc: bool, java_major: int | None = None) -> tuple[str, list[str]]:
    """GC 选择。>12G 才考虑 ZGC（ZGC 自身要吃 20~30% 额外内存给小堆不划算）。

    -XX:+ZGenerational 是 JDK 21 才有的分代 ZGC，JDK 17 上加了会直接启动失败。
    """
    if prefer_zgc and xmx_mb >= 12288:
        args = list(ZGC_ARGS)
        if java_major is not None and java_major < 21:
            args = [a for a in args if a != "-XX:+ZGenerational"]
        return "ZGC", args
    return "G1", list(G1_ARGS)


def recommend(hw: dict, variant: str, user_xmx: int | None = None,
              prefer_zgc: bool = False) -> dict:
    """产出最终 JVM 配置。

    返回:
      xmx_mb / xms_mb / gc / jvm_args / warnings / variant / ceiling_mb / tier
    """
    warnings: list[dict] = []
    ram = hw.get("ram") or {}
    total = ram.get("total_mb") or 0
    avail = ram.get("avail_mb") or 0
    java = hw.get("java") or {}

    if not total:
        # 探测失败时采取保守值，不能因为读不到内存就把玩家机器搞崩
        total = 8192
        warnings.append({"level": "warn",
                         "text": "无法读取物理内存，已按 8 GB 保守配置"})

    base, full_ok = _tier_for(total, variant)
    ceiling = hard_ceiling(total)

    # 32 位 Java 是硬约束，优先于一切（必须在任何 clamp 之前收窄上限）
    java_major = java.get("major")
    if java.get("bits") == 32:
        ceiling = min(ceiling, 1536)
        warnings.append({"level": "danger",
                         "text": "检测到 32 位 Java，堆内存最大只能用 1.5 GB。"
                                 "请使用启动器自带的 64 位 Java。"})

    if variant == "full" and not full_ok:
        warnings.append({"level": "warn",
                         "text": f"物理内存 {total // 1024} GB 跑高配版会比较吃力，"
                                 f"建议改用精简版"})

    xmx = base if user_xmx is None else int(user_xmx)
    if xmx > ceiling:
        if user_xmx is not None:
            warnings.append({"level": "warn",
                             "text": f"你设置的内存 {xmx} MB 超过本机安全上限 {ceiling} MB，"
                                     f"已自动下调（否则可能导致游戏启动失败或系统卡死）"})
        else:
            auto_clamped = True
        xmx = ceiling
    else:
        auto_clamped = False
    # 只有当上限本身容得下时才上调，否则 32 位机器会出现「上调到 1536 → 又说低于 2048」的自相矛盾
    if xmx < MIN_XMX_MB and ceiling >= MIN_XMX_MB:
        warnings.append({"level": "warn",
                         "text": f"内存低于 {MIN_XMX_MB} MB 时游戏极可能内存溢出，已上调"})
        xmx = MIN_XMX_MB
    xmx = max(512, xmx)

    # 可用内存复核：别的程序占太多的话，开这么大堆照样起不来
    if avail and xmx + 1536 > avail:
        warnings.append({"level": "warn",
                         "text": f"当前可用内存只有 {avail} MB，建议先关闭浏览器/其他游戏再启动"})

    # 页面文件（Windows）：关掉页面文件 + 大堆 = 启动直接失败
    pf = hw.get("pagefile_mb")
    if pf == 0 or pf == -1:
        if xmx > total * 0.5:
            warnings.append({"level": "warn",
                             "text": "系统页面文件似乎未启用，大内存配置可能启动失败"})

    # 磁盘空间：assets + 库 + 整合包大约需要 3~5 GB
    if hw.get("disk", {}).get("free_mb"):
        free = hw["disk"]["free_mb"]
        if free < 6000:
            warnings.append({"level": "warn",
                             "text": f"安装盘剩余空间仅 {free} MB，客户端完整安装约需 3~5 GB"})

    gc_name, gc_list = _gc_args(xmx, prefer_zgc, java_major)

    # 小堆不要开 StringDeduplication（收益低于开销）
    if xmx < 4096 and "-XX:+UseStringDeduplication" in gc_list:
        gc_list.remove("-XX:+UseStringDeduplication")

    xms = min(2048, xmx)

    jvm_args = [
        f"-Xms{xms}M",
        f"-Xmx{xmx}M",
        *gc_list,
        *COMMON_ARGS,
    ]

    # 装载器相关：避免偶发的证书/补丁校验失败
    jvm_args += [
        "-Dfml.ignoreInvalidMinecraftCertificates=true",
        "-Dfml.ignorePatchDiscrepancies=true",
    ]

    return {
        "variant": variant,
        "xmx_mb": xmx,
        "xms_mb": xms,
        "gc": gc_name,
        "jvm_args": jvm_args,
        "warnings": warnings,
        "ceiling_mb": ceiling,
        "total_ram_mb": total,
        "avail_ram_mb": avail,
        "auto_clamped": auto_clamped,
    }


def describe(hw: dict) -> str:
    """给人看的硬件摘要（启动器首页显示用）。"""
    cpu = hw.get("cpu") or {}
    ram = hw.get("ram") or {}
    gpus = hw.get("gpu") or []
    gpu_s = gpus[0]["name"] if gpus else "未检测到"
    vram = max([g.get("vram_mb") or 0 for g in gpus] or [0])
    if vram:
        gpu_s += f" ({vram} MB)"
    total = ram.get("total_mb")
    return (
        f"CPU  {cpu.get('name', '未知')}"
        f"（{cpu.get('threads') or '?'} 线程）\n"
        f"内存 {total // 1024 if total else '?'} GB"
        f"（可用 {ram.get('avail_mb', 0) // 1024} GB）\n"
        f"显卡 {gpu_s}\n"
        f"磁盘 {hw.get('disk', {}).get('type', '未知')}"
    )


# ─────────────── 崩溃后处理：OOM 识别 ───────────────

# 整合包 130+ 模组，常见的 OOM 形态不止一种。
# 用正则而不是纯 substring：像 "Could not reserve enough space for 8388608KB object heap"
# 中间夹了数字，写死整句会漏。
_OOM_PATTERNS = [
    (r"java\.lang\.OutOfMemoryError", "heap"),
    (r"Java heap space", "heap"),
    (r"GC overhead limit exceeded", "heap"),
    (r"Unable to allocate", "heap"),
    (r"Could not reserve enough space", "system"),
    (r"insufficient memory for the Java Runtime", "system"),
    (r"Error occurred during initialization of VM", "system"),
    (r"CompressedOops", "system"),
]


def scan_log_for_oom(log_path: str, tail_bytes: int = 2 * 1024 * 1024) -> dict | None:
    """检查客户端日志/崩溃报告里有没有内存溢出。命中返回信息，否则 None。

    只读日志尾部 2MB —— 大日志整读会卡住 UI 线程。
    """
    import os
    import re
    if not os.path.exists(log_path):
        return None
    try:
        size = os.path.getsize(log_path)
        with open(log_path, "rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
            raw = f.read()
        text = raw.decode("utf-8", "replace")
    except Exception:
        return None

    hits, kinds = [], set()
    for pat, kind in _OOM_PATTERNS:
        m = re.search(pat, text)
        if m:
            hits.append(m.group(0)[:60])
            kinds.add(kind)
    if not hits:
        return None

    # 只要出现「系统给不出内存」就按 system 处理：此时加堆只会更糟
    kind = "system" if "system" in kinds else "heap"
    return {"kind": kind, "patterns": hits, "log": log_path}


def next_xmx_try(current_xmx: int, hw: dict, step_mb: int = 2048) -> tuple[int, str]:
    """OOM 之后下一次该用多少堆。返回 (新值, 说明)。

    关键：如果是系统给不出内存，加堆只会更糟 —— 必须反过来降。
    """
    ceiling = hard_ceiling((hw.get("ram") or {}).get("total_mb") or 8192)
    if current_xmx >= ceiling:
        return current_xmx, "已经是本机安全上限，再调大会导致启动失败，建议改用精简版"
    nxt = min(current_xmx + step_mb, ceiling)
    return nxt, f"内存已从 {current_xmx} MB 提升到 {nxt} MB"
