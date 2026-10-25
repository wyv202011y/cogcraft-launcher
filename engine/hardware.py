"""跨平台硬件探测 —— 供启动器决定内存上限、JVM 档位、画质档位。

设计原则：
1. 绝不抛异常。任何一项探测失败都要优雅降级成 None，启动器不能因为
   读不到显卡型号就起不来。
2. Windows 上尽量合并成一次 PowerShell 调用（每次 subprocess 要 300~800ms，
   分十次调用就是好几秒的白屏）。
3. 只读取，不写入、不改任何系统设置。
"""

from __future__ import annotations

import ctypes
import json
import os
import platform
import shutil
import subprocess
import sys


# ────────────────────────── 通用工具 ──────────────────────────

def _run(cmd: list[str], timeout: float = 8.0) -> str:
    """执行命令并返回 stdout + stderr。

    注意：java -version / -XshowSettings 全部输出到 stderr，只读 stdout 会漏。
    """
    try:
        si = None
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            startupinfo=si, encoding="utf-8", errors="replace",
        )
        return (p.stdout or "") + (p.stderr or "")
    except Exception:
        return ""


def total_memory_mb() -> tuple[int | None, int | None]:
    """返回 (物理内存总量MB, 当前可用MB)。"""
    # Windows：GlobalMemoryStatusEx 最可靠，不依赖 wmic（Win11 已移除）
    if sys.platform == "win32":
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return (int(st.ullTotalPhys // 1048576), int(st.ullAvailPhys // 1048576))
        except Exception:
            pass

    # Linux
    if sys.platform.startswith("linux"):
        try:
            kv: dict[str, int] = {}
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        kv[parts[0].rstrip(":")] = int(parts[1])  # kB
            total = kv.get("MemTotal", 0) // 1024
            avail = kv.get("MemAvailable", kv.get("MemFree", 0)) // 1024
            return (total or None, avail or None)
        except Exception:
            pass

    # macOS
    if sys.platform == "darwin":
        try:
            total = int(_run(["sysctl", "-n", "hw.memsize"]).strip() or 0) // 1048576
            vm = _run(["vm_stat"])
            return (total or None, None)
        except Exception:
            pass

    return (None, None)


def _win_system_info() -> dict:
    """一次 PowerShell 拿 CPU / GPU / 磁盘类型 / 页面文件。"""
    ps = r"""
$ErrorActionPreference='SilentlyContinue'
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$gpu = @(Get-CimInstance Win32_VideoController | ForEach-Object {
  $n=$_.Name; $v=0
  try { $v = [int64]$_.AdapterRAM } catch {}
  [PSCustomObject]@{ name=$n; vram_mb=[int]($v/1MB) }
})
$disk = Get-PhysicalDisk | Select-Object -First 1
$pf = @(Get-CimInstance Win32_PageFileUsage | ForEach-Object { $_.AllocatedBaseSize })
[PSCustomObject]@{
  cpu_name   = $cpu.Name
  cpu_cores  = [int]$cpu.NumberOfCores
  cpu_threads= [int]$cpu.NumberOfLogicalProcessors
  cpu_mhz    = [int]$cpu.MaxClockSpeed
  gpus       = $gpu
  disk_type  = $(if($disk.MediaType){[string]$disk.MediaType}else{''})
  pagefile_mb= $(if($pf.Count -gt 0){[int]($pf | Measure-Object -Sum).Sum}else{-1})
} | ConvertTo-Json -Compress -Depth 4
"""
    raw = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=20.0)
    if not raw.strip():
        return {}
    try:
        return json.loads(raw.strip())
    except Exception:
        return {}


def _win_gpu_vram_from_registry() -> list[dict]:
    """AdapterRAM 在 >4GB 显卡上是错的（32 位溢出）。
    从注册表 HardwareInformation.qwMemorySize 读真实显存。"""
    try:
        import winreg
        out = []
        base = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, base) as root:
            for i in range(32):
                try:
                    with winreg.OpenKey(root, f"{i:04d}") as k:
                        name = winreg.QueryValueEx(k, "DriverDesc")[0]
                        try:
                            qw = winreg.QueryValueEx(k, "HardwareInformation.qwMemorySize")[0]
                            vram = int(qw) // 1048576
                        except Exception:
                            vram = 0
                        if name:
                            out.append({"name": str(name), "vram_mb": vram})
                except OSError:
                    continue
        return out
    except Exception:
        return []


def _linux_system_info() -> dict:
    info: dict = {}

    # CPU 型号与核心数
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            txt = f.read()
        cores = set()
        cur_phys = cur_core = None
        mhz = 0
        for line in txt.splitlines():
            if line.startswith("model name"):
                info.setdefault("cpu_name", line.split(":", 1)[1].strip())
            elif line.startswith("physical id"):
                cur_phys = line.split(":", 1)[1].strip()
            elif line.startswith("core id"):
                cur_core = line.split(":", 1)[1].strip()
                cores.add((cur_phys, cur_core))
            elif line.startswith("cpu MHz"):
                try:
                    mhz = max(mhz, int(float(line.split(":", 1)[1].strip())))
                except Exception:
                    pass
        if cores:
            info["cpu_cores"] = len(cores)
        if mhz:
            info["cpu_mhz"] = mhz
    except Exception:
        pass

    # 根分区磁盘类型（SSD / HDD）
    try:
        root_dev = os.stat("/").st_dev
        major, minor = os.major(root_dev), os.minor(root_dev)
        name = None
        with open("/proc/partitions", encoding="utf-8") as f:
            for line in f:
                p = line.split()
                if len(p) == 4 and p[0].isdigit():
                    try:
                        if int(p[0]) == major and int(p[1]) == minor:
                            name = p[3]
                            break
                    except ValueError:
                        continue
        if name:
            base = name.rstrip("0123456789")
            rot = f"/sys/block/{base}/queue/rotational"
            if os.path.exists(rot):
                with open(rot) as f:
                    info["disk_type"] = "HDD" if f.read().strip() == "1" else "SSD"
    except Exception:
        pass

    # 显卡
    try:
        out = _run(["lspci"], timeout=5.0)
        gpus = []
        for line in out.splitlines():
            if any(k in line for k in ("VGA compatible", "3D controller", "Display controller")):
                gpus.append({"name": line.split(": ", 1)[-1].strip(), "vram_mb": 0})
        if gpus:
            info["gpus"] = gpus
    except Exception:
        pass

    return info


def _darwin_system_info() -> dict:
    info: dict = {}
    try:
        name = _run(["sysctl", "-n", "machdep.cpu.brand_string"]).strip()
        if name:
            info["cpu_name"] = name
        nc = _run(["sysctl", "-n", "hw.physicalcpu"]).strip()
        if nc.isdigit():
            info["cpu_cores"] = int(nc)
        out = _run(["system_profiler", "SPDisplaysDataType"], timeout=15.0)
        gpus = []
        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Chipset Model:"):
                gpus.append({"name": s.split(":", 1)[1].strip(), "vram_mb": 0})
        if gpus:
            info["gpus"] = gpus
    except Exception:
        pass
    return info


def detect_java(java_exe: str | None = None) -> dict:
    """探测 Java：版本、位数、厂商。位数极其重要——32 位最大只能 1.5G 堆。"""
    exe = java_exe or shutil.which("java") or "java"
    res = {"path": exe, "version": None, "major": None, "bits": None, "vendor": None}
    if not exe or not (os.path.exists(exe) or shutil.which(exe)):
        return res
    out = _run([exe, "-XshowSettings:properties", "-version"], timeout=15.0)
    if not out:
        out = _run([exe, "-version"], timeout=15.0)
    for line in out.splitlines():
        s = line.strip()
        for key, field in (
            ("java.version =", "version"),
            ("java.vendor =", "vendor"),
            ("sun.arch.data.model =", "bits"),
        ):
            if s.startswith(key):
                val = s.split("=", 1)[1].strip()
                res[field] = int(val) if field == "bits" else val
    if res["version"]:
        # 1.8.0_392 → 8 ; 21.0.12 → 21
        parts = res["version"].split(".")
        try:
            major = int(parts[1]) if parts[0] == "1" else int(parts[0])
            res["major"] = major
        except Exception:
            pass
    return res


def _free_mb(path: str) -> int | None:
    """path 所在分区的剩余空间（MB）。"""
    try:
        u = shutil.disk_usage(path)
        return int(u.free // 1048576)
    except Exception:
        return None


def detect(java_exe: str | None = None) -> dict:
    """返回完整硬件报告。所有字段都可能为 None。"""
    ram_total, ram_avail = total_memory_mb()

    sysinfo: dict = {}
    if sys.platform == "win32":
        sysinfo = _win_system_info()
        if not sysinfo.get("gpus"):
            sysinfo["gpus"] = _win_gpu_vram_from_registry()
        else:
            # 用注册表的真实显存覆盖 AdapterRAM
            reg = {g["name"]: g["vram_mb"] for g in _win_gpu_vram_from_registry()}
            for g in sysinfo["gpus"]:
                if not g.get("vram_mb") and g["name"] in reg:
                    g["vram_mb"] = reg[g["name"]]
    elif sys.platform.startswith("linux"):
        sysinfo = _linux_system_info()
    elif sys.platform == "darwin":
        sysinfo = _darwin_system_info()

    gpus = sysinfo.get("gpus") or []
    # 过滤掉虚拟显卡（向日葵/Todesk/VMware/Parsec 之类会报一堆假显卡）
    junk = ("virtual", "vmware", "parsec", "sunlogin", "todesk", "meta display",
            "basic display", "remote", "oray")
    real_gpus = [g for g in gpus
                 if g.get("name") and not any(j in g["name"].lower() for j in junk)]
    if not real_gpus:
        real_gpus = gpus

    return {
        "os": {"windows": "windows", "darwin": "macos"}.get(sys.platform, "linux"),
        "os_release": platform.release(),
        "arch": platform.machine(),
        "cpu": {
            "name": sysinfo.get("cpu_name") or platform.processor() or "未知",
            "cores": sysinfo.get("cpu_cores"),
            "threads": os.cpu_count(),
            "mhz": sysinfo.get("cpu_mhz"),
        },
        "ram": {"total_mb": ram_total, "avail_mb": ram_avail},
        "gpu": real_gpus,
        "disk": {
            "type": (sysinfo.get("disk_type") or "未知"),
            # 安装盘剩余空间：判够不够放下 assets + libraries（约 3~5 GB）
            "free_mb": _free_mb(os.path.expanduser("~")),
        },
        "pagefile_mb": sysinfo.get("pagefile_mb"),  # Windows 才有，-1 = 未启用
        "java": detect_java(java_exe),
    }


if __name__ == "__main__":
    print(json.dumps(detect(), ensure_ascii=False, indent=2))
