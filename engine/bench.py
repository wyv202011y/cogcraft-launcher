"""cogcraft 性能微基准 —— 不查 CPU 型号表，直接实测。

设计要点
--------
1. 用**与游戏相同的 JRE** 跑（自带 JRE 优先），测的是这台机器跑 Java 的真实速度，
   包含 JIT 与 G1 行为，比任何第三方跑分更贴近 MC。
2. 每项测固定「时长」而非固定「次数」，且固定堆 512MB + G1 → 跨机器可比。
3. 分数 = 各指标相对基准机的**加权几何平均**，避免某项极差被其他项掩盖。
4. 基准机 = 本项目开发机 i7-12700K（baseline.json 里的实测值）。只有一个基准点，
   没有 CPU 型号表 —— 新增机器不需要改任何表。
"""
from __future__ import annotations
import json, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH_DIR = os.path.join(HERE, "bench")
JAVA_FILE = os.path.join(BENCH_DIR, "Bench.java")
CLASS_FILE = os.path.join(BENCH_DIR, "Bench.class")
BASELINE_FILE = os.path.join(BENCH_DIR, "baseline.json")

# 各项指标的权重：MC 主线程是单线程 tick，所以单核权重最高；
# 内存延迟决定区块/对象图遍历；GC 停顿决定「卡不卡」；拷贝带宽权重最低。
WEIGHTS = {"int": 0.40, "mem": 0.30, "gc": 0.20, "copy": 0.10}

# 分档阈值（相对基准机的倍率）。⚠ 暂定值，等 1~2 台低端实机样本后校准。
THRESHOLD_FULL = 0.45

JVM_FLAGS = ["-Xms512m", "-Xmx512m", "-XX:+UseG1GC", "-XX:-UsePerfData"]


def _candidates(java_hint: str | None):
    """按优先级给出可能的 java 可执行文件。"""
    out = []
    if java_hint:
        out.append(java_hint)
    # 自带 JRE（打包后与启动器同目录）
    for rel in ("runtime/bin/java.exe", "runtime/bin/java",
                "jre/bin/java.exe", "jre/bin/java"):
        out.append(os.path.join(os.path.dirname(HERE), rel))
    # 系统 java
    import shutil
    w = shutil.which("java")
    if w:
        out.append(w)
    return out


def find_java(java_hint: str | None = None) -> str | None:
    for p in _candidates(java_hint):
        if p and os.path.isfile(p):
            return p
    return None


def run(java: str, timeout: float = 40.0) -> dict:
    """跑一次微基准，返回原始指标。失败抛 RuntimeError。"""
    if os.path.isfile(CLASS_FILE):
        cmd = [java] + JVM_FLAGS + ["-cp", BENCH_DIR, "Bench"]
    else:
        cmd = [java] + JVM_FLAGS + [JAVA_FILE]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           cwd=BENCH_DIR)
    except subprocess.TimeoutExpired:
        raise RuntimeError("性能测试超时（%ds），这台机器可能负载过高" % timeout)
    out = (r.stdout or "").strip()
    line = ""
    for ln in out.splitlines():
        ln = ln.strip()
        if ln.startswith("{") and ln.endswith("}"):
            line = ln
    if not line:
        raise RuntimeError("性能测试没有返回结果：%s" % ((r.stderr or out)[-300:]))
    return json.loads(line)


def hw_fingerprint(hw: dict | None) -> str:
    """硬件指纹：只取会显著影响性能的字段。

    用途：缓存了上一次的实测分数后，判断硬件有没有变（加内存、换显卡、换机器）。
    变了就作废重测；没变就一直用缓存，不再打扰玩家。
    """
    import hashlib
    hw = hw or {}
    gpus = sorted((g.get("name") or "") for g in (hw.get("gpu") or []))
    cpu = hw.get("cpu") or {}
    ram = hw.get("ram") or {}
    key = "|".join([
        str(cpu.get("name") or ""),
        str(cpu.get("cores") or ""),
        str(cpu.get("threads") or ""),
        str(ram.get("total_mb") or ""),
        ",".join(gpus),
        str((hw.get("os") or "")),
        str(hw.get("arch") or ""),
    ])
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def load_baseline() -> dict:
    with open(BASELINE_FILE, encoding="utf-8") as f:
        return json.load(f)


def score(m: dict, base: dict | None = None) -> dict:
    """把原始指标换成「相对基准机的倍率」和总分。"""
    base = base or load_baseline()
    b = base["metrics"]
    # 越大越好的指标 / 越小越好的指标
    ratios = {
        "int":  m["int_mops"] / b["int_mops"],
        "mem":  b["chase_ns"] / m["chase_ns"],
        "gc":  (b["gc_pct"] + 0.5) / (m["gc_pct"] + 0.5),
        "copy": m["copy_gbs"] / b["copy_gbs"],
    }
    # 停顿单独判：超过基准机 2 倍就额外扣分（玩家感觉最明显的是这个）
    stall_pen = min(1.0, b.get("stall_ms", 1.0) / max(m.get("stall_ms", 1.0), 0.05))

    import math
    logsum = sum(WEIGHTS[k] * math.log(max(v, 1e-6)) for k, v in ratios.items())
    total = math.exp(logsum) * (0.85 + 0.15 * stall_pen)

    return {
        "ratios": {k: round(v, 3) for k, v in ratios.items()},
        "stall_penalty": round(stall_pen, 3),
        "score": round(total, 3),
        "threshold_full": THRESHOLD_FULL,
        "verdict": "full" if total >= THRESHOLD_FULL else "simple",
        "raw": m,
    }


def measure(java_hint: str | None = None, hw: dict | None = None) -> dict:
    """对外只暴露这一个函数。返回 {ok, java, ...score} 或 {ok:False, error}"""
    java = find_java(java_hint)
    if not java:
        return {"ok": False, "error": "找不到可用的 Java，无法跑性能测试"}
    try:
        m = run(java)
    except Exception as e:
        return {"ok": False, "error": str(e), "java": java}
    s = score(m)
    s["ok"] = True
    s["java"] = java
    s["hw_fp"] = hw_fingerprint(hw)
    return s


def human_summary(res: dict) -> str:
    """给人类看的一句话结论。"""
    if not res.get("ok"):
        return "性能测试未完成：" + res.get("error", "")
    m, r = res["raw"], res["ratios"]
    return (
        "综合性能 %.2f×（单核 %.2f× · 内存延迟 %.2f× · GC %.2f× · 带宽 %.2f×，"
        "最长停顿 %.2fms）→ 建议%s"
        % (res["score"], r["int"], r["mem"], r["gc"], r["copy"],
           m.get("stall_ms", 0), "高配版" if res["verdict"] == "full" else "流畅版")
    )


if __name__ == "__main__":
    hint = sys.argv[1] if len(sys.argv) > 1 else None
    res = measure(hint)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    print("\n" + human_summary(res))
