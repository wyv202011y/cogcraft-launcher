"""启动器自更新 —— 安装环节（下载之后：解压、替换、重启、回滚）。

为什么单独一个模块：
    updater.py 管「检查 + 下载 + 校验」，本模块管「把新包换上去」。
    这两件事的风险等级完全不同 —— 前者出错只是这次没更新，后者出错启动器就没了。
    所以安装环节单独放、单独限制、单独留回滚点。

核心难点是两件事：
    1. 进程不能替换自己（尤其 Windows 会把正在运行的 exe 锁住），
       所以必须「外部脚本接手 + 主进程先退出」。
    2. 换坏了要给用户留退路，所以旧目录只改名不删，新版本起不来就换回去。

两平台的做法其实一样（都是整目录替换）：
    macOS   ~/Applications/CogCraft.app      ← ditto 打的 zip，顶层是 cogcraft.app
    Windows ...\\cogcraft\\cogcraft.exe        ← PyInstaller onedir，顶层是 cogcraft/
    统一成「替换单元」：一个目录，整体换掉。

安全纪律（任何一条不满足就拒绝安装，宁可让用户手动下载）：
    - 必须是从打包产物里运行的（frozen）；开发模式跑 python app.py 没有意义
    - 启动器所在目录必须可写（从 dmg 里直接运行、装在只读卷上的，拒绝）
    - 新包结构和当前一致（mac 必须有 Contents/MacOS/，win 必须有 .exe）
    - 临时目录必须和启动器同卷（跨卷 mv 会退化成复制，断电就是两边都不完整）
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import time
import zipfile

# ── 替换单元的名字（zip 内部顶层目录 / ditto 打的 bundle 名）──
MAC_BUNDLE_NAME = "cogcraft.app"
WIN_DIR_NAME = "cogcraft"
WIN_EXE_NAME = "cogcraft.exe"

# 脚本等旧进程消失的上限、等新版本自报启动成功的上限（秒）
WAIT_EXIT_SEC = 60
WAIT_BOOT_SEC = 45


# ────────────────────────── 环境判定 ──────────────────────────

def is_frozen() -> bool:
    """是否从 PyInstaller 打包产物运行。

    开发模式（python app.py）下 self_update 一律不可用 —— 那时候「替换自己」
    替换的是一堆源码，没有任何意义，还会把工作区搞坏。
    """
    return bool(getattr(sys, "frozen", False))


def install_root() -> str:
    """当前启动器的「整体替换单元」绝对路径，非 frozen 返回空串。

    macOS: .../CogCraft.app
    Windows: .../cogcraft          （含 cogcraft.exe 的那一层）
    """
    if not is_frozen():
        return ""
    exe = os.path.realpath(sys.executable)
    if sys.platform == "darwin":
        p = exe
        for _ in range(6):                    # 往上找 .app，最多几层
            p = os.path.dirname(p)
            if p.lower().endswith(".app"):
                return p
            if not p or p == os.path.dirname(p):
                break
        return ""
    return os.path.dirname(exe)


def _exe_inside(unit: str) -> str:
    """替换单元里那个真正跑起来的可执行文件（用于结构校验）。"""
    if sys.platform == "darwin":
        return os.path.join(unit, "Contents", "MacOS", "cogcraft")
    return os.path.join(unit, WIN_EXE_NAME)


def preflight() -> dict:
    """安装前的可行性检查。返回 {ok, reason, root, staged, backup}。

    任何一条不满足都在这里拦住，绝不带病往下走 —— 换一半失败比不换糟糕得多。
    """
    out = {"ok": False, "reason": "", "root": "", "staged": "", "backup": "",
           "frozen": is_frozen(), "platform": sys.platform}

    if not is_frozen():
        out["reason"] = "当前是开发模式运行，自动更新只在正式安装包里可用"
        return out

    root = install_root()
    out["root"] = root
    if not root or not os.path.isdir(root):
        out["reason"] = "找不到启动器所在的安装目录"
        return out

    if not os.access(_exe_inside(root), os.X_OK):
        out["reason"] = "启动器目录结构异常（找不到可执行文件）"
        return out

    parent = os.path.dirname(root)
    if not os.access(parent, os.W_OK):
        out["reason"] = (f"没有写入权限：{parent} 不可写。"
                         "如果启动器还在下载的 dmg/压缩包里，请先拖进「应用程序」文件夹")
        return out

    staged = staging_path(root)
    out["staged"] = staged
    out["backup"] = backup_path(root)
    # 同卷检查：跨卷 mv 不是原子操作，中途断电会两边都不完整
    try:
        if os.stat(parent).st_dev != os.stat(os.path.dirname(staged)).st_dev:
            out["reason"] = "临时目录与启动器不在同一磁盘，拒绝自动更新（请手动下载）"
            return out
    except OSError as e:
        out["reason"] = f"检查磁盘时出错：{e}"
        return out

    out["ok"] = True
    return out


def staging_path(root: str) -> str:
    """新包解压位置。放在启动器同级目录里 —— 保证同卷，也避免和用户家目录跨盘。"""
    return os.path.join(os.path.dirname(root), ".cogcraft-update", os.path.basename(root))


def backup_path(root: str) -> str:
    """旧版本备份位置（与 root 同级，同卷改名是瞬时的）。"""
    return os.path.join(os.path.dirname(root), ".cogcraft-old", os.path.basename(root))


# ────────────────────────── 解压与校验 ──────────────────────────

def _top_dir(paths: list[str]) -> str:
    """zip 里所有条目的公共顶层目录名（mac: cogcraft.app / win: cogcraft）。"""
    tops = {p.split("/")[0] for p in paths if p.strip("/")}
    if len(tops) != 1:
        raise ValueError(f"更新包结构异常：顶层目录不唯一（{sorted(tops)[:4]}）")
    return tops.pop()


def _extract_all(z: zipfile.ZipFile, dest: str) -> None:
    """按条目解压，并还原 Unix 权限位与符号链接。

    为什么不能直接用 extractall：
        - 它**丢掉权限位**。macOS 的包是 ditto 打的，里面 Contents/MacOS/cogcraft
          是 755；extractall 解出来变成 644，新版一启动就被拒（Permission denied），
          然后触发回滚 —— 每次点更新都白折腾一遍。
        - 它把**符号链接当普通文件**解出（内容是链接目标的文本）。JRE 里的
          dylib 链接全断，启动器起来就报找不到库。
    这两样在 macOS 包里都有，所以必须自己逐条处理。
    """
    for info in z.infolist():
        target = z.extract(info, dest)
        mode = (info.external_attr >> 16) & 0xFFFF
        if not mode:
            continue
        if stat.S_ISLNK(mode):
            try:
                link = z.read(info).decode("utf-8")
                if os.path.lexists(target):
                    os.remove(target)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                os.symlink(link, target)
            except OSError:
                pass
        else:
            try:
                os.chmod(target, stat.S_IMODE(mode))
            except OSError:
                pass


def unpack(zip_path: str, root: str) -> str:
    """解压更新包到 staging，校验结构，返回新替换单元路径。

    失败会清掉半成品再抛异常 —— 绝不留一个看起来像新版本、其实缺文件的目录。
    """
    staged = staging_path(root)
    work = os.path.dirname(staged)                 # <parent>/.cogcraft-update
    if os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        top = _top_dir(names)
        _extract_all(z, work)

    extracted = os.path.join(work, top)
    if not os.path.isdir(extracted):
        shutil.rmtree(work, ignore_errors=True)
        raise ValueError("更新包解压后找不到顶层目录")

    # 新单元必须和当前长得一样，否则脚本会把启动器换成个空壳
    want_exe = _exe_inside(extracted)
    if not os.path.exists(want_exe):
        shutil.rmtree(work, ignore_errors=True)
        raise ValueError(f"更新包结构不对：缺少 {os.path.relpath(want_exe, extracted)}")
    # 权限丢了也等于不能启动，必须在这里拦住（否则要等回滚才知道）
    if sys.platform == "darwin" and not os.access(want_exe, os.X_OK):
        try:
            os.chmod(want_exe, 0o755)
        except OSError:
            shutil.rmtree(work, ignore_errors=True)
            raise ValueError("更新包内的主程序没有可执行权限，拒绝安装")

    # 名字对不上（比如 win 包里叫别的目录名）就改成当前单元的名字，
    # 脚本里用的是固定名字，不能靠猜
    if os.path.basename(extracted) != os.path.basename(root):
        target = os.path.join(work, os.path.basename(root))
        if os.path.exists(target):
            shutil.rmtree(target, ignore_errors=True)
        os.rename(extracted, target)
        extracted = target

    if sys.platform != "darwin":
        os.chmod(_exe_inside(extracted), os.stat(_exe_inside(extracted)).st_mode | stat.S_IEXEC)
    return extracted


# ────────────────────────── 启动标记（判断新版真的活了）──────────────────────────

def marker_path(state_dir: str, version: str) -> str:
    """新版本启动成功后会写这个文件；替换脚本等它出现才认为成功。

    只靠「进程存在」判断不可靠 —— 进程可能起来了但界面是白的、或者刚起就崩。
    """
    return os.path.join(state_dir, "update-check", f"started-{version}.ok")


def write_start_marker(state_dir: str, version: str) -> None:
    """新版本启动时调用：留下「我这个版本活着」的证据。"""
    try:
        p = marker_path(state_dir, version)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(f"{version}\n{int(time.time())}\n{os.getpid()}\n")
    except Exception:
        pass


def clear_start_marker(state_dir: str, version: str) -> None:
    try:
        os.remove(marker_path(state_dir, version))
    except OSError:
        pass


# ────────────────────────── 接手脚本 ──────────────────────────

_BASH_TMPL = r"""#!/bin/bash
# 由 cogcraft 启动器自动生成，唯一职责：等旧进程退出 → 换目录 → 拉起新版 → 验证 → 必要时回滚。
# 不用 set -e：任何一步失败都必须走到回滚，而不是直接退出。
set -u
LOG=__LOG__
TARGET=__TARGET__
NEW=__NEW__
BAK=__BAK__
MARKER=__MARKER__
PID=__PID__
APP=__APP__

mkdir -p "$(dirname "$LOG")"
exec >> "$LOG" 2>&1
echo "=== $(date '+%F %T') 开始自更新 → $APP ==="

# 1) 等旧进程真的退出（按 PID，避免误伤用户另开的实例）
# 注意：进程死了但父进程还没收尸时是僵尸（Z），kill -0 依然成功。
# 光看 kill -0 会把僵尸当成活着，白等一场然后放弃更新 —— 必须排除 Z 状态。
alive() {
  kill -0 "$PID" 2>/dev/null || return 1
  case "$(ps -o stat= -p "$PID" 2>/dev/null | tr -d ' ')" in
    Z*) return 1 ;;
  esac
  return 0
}
i=0
while [ $i -lt __WAIT_EXIT__ ]; do
  alive || break
  sleep 1
  i=$((i + 1))
done
if alive; then
  echo "旧进程 __WAIT_EXIT__ 秒还没退出，放弃本次更新（文件未动）"
  exit 1
fi
echo "旧进程已退出"

# 2) 旧版本改名当备份（同卷 rename 是瞬时的；不删，这是回滚点）
rm -rf "$BAK"
mkdir -p "$(dirname "$BAK")"
if ! mv "$TARGET" "$BAK"; then
  echo "备份旧版本失败，中止（启动器未改动）"
  exit 1
fi

# 3) 新版就位
if ! mv "$NEW" "$TARGET"; then
  echo "新版就位失败，回滚"
  mv "$BAK" "$TARGET" && open "$TARGET"
  exit 1
fi

# 3.5) macOS 的数据（设置/缓存/证书）存在 app 内部，换包时必须跟着搬。
#      不搬的话：新 app 是全新的空目录，用户设置、Java 路径、证书全没了；
#      更糟的是下面成功后 rm -rf 备份，数据就永久丢了。
__KEEP_BLOCK__

# 4) 拉起新版，等它自报启动成功
rm -f "$MARKER"
open "$TARGET"
i=0
while [ $i -lt __WAIT_BOOT__ ]; do
  [ -f "$MARKER" ] && break
  sleep 1
  i=$((i + 1))
done

if [ -f "$MARKER" ]; then
  echo "更新成功，新版本已正常启动；清理备份"
  rm -rf "$BAK"
  rmdir "$(dirname "$NEW")" 2>/dev/null
else
  echo "新版 __WAIT_BOOT__ 秒内没有启动成功，回滚"
  rm -rf "$TARGET"
  mv "$BAK" "$TARGET" && open "$TARGET"
fi
"""

_BAT_TMPL = r"""@echo off
rem 由 cogcraft 启动器自动生成，唯一职责：等旧进程退出 → 换目录 → 拉起新版 → 验证 → 必要时回滚。
setlocal
set "LOG=__LOG__"
set "TARGET=__TARGET__"
set "NEW=__NEW__"
set "BAK=__BAK__"
set "MARKER=__MARKER__"
set "PID=__PID__"
set "EXE=__EXE__"

if not exist "__LOGDIR__" mkdir "__LOGDIR__"
echo === %DATE% %TIME% 开始自更新 >> "%LOG%"

rem 1) 等旧进程退出。按 PID 精确等，不用 imagename（用户可能开了两个实例）
set /a i=0
:wait_exit
tasklist /FI "PID eq %PID%" /NH 2>nul | find "%PID%" >nul
if errorlevel 1 goto exited
set /a i+=1
if %i% GEQ __WAIT_EXIT__ goto timeout
timeout /t 1 /nobreak >nul
goto wait_exit

:timeout
echo 旧进程 __WAIT_EXIT__ 秒还没退出，放弃本次更新（文件未动） >> "%LOG%"
exit /b 1

:exited
echo 旧进程已退出 >> "%LOG%"

rem 2) 旧版本改名当备份（同卷 rename）
if exist "%BAK%" rmdir /s /q "%BAK%"
if not exist "__BAKDIR__" mkdir "__BAKDIR__"
move "%TARGET%" "%BAK%" >nul 2>&1
if errorlevel 1 goto bak_fail

rem 3) 新版就位
move "%NEW%" "%TARGET%" >nul 2>&1
if errorlevel 1 goto rollback

rem 3.5) Windows 不搬数据：用户数据在 %APPDATA%\cogcraft，本来就不在安装目录里，
rem      整目录替换碰不到它。（macOS 才需要搬，那边数据在 app 内部。）

rem 4) 拉起新版，等它自报启动成功
if exist "%MARKER%" del /q "%MARKER%"
start "" "%TARGET%\%EXE%"
set /a i=0
:wait_boot
if exist "%MARKER%" goto ok
set /a i+=1
if %i% GEQ __WAIT_BOOT__ goto rollback
timeout /t 1 /nobreak >nul
goto wait_boot

:bak_fail
echo 备份旧版本失败，中止（启动器未改动） >> "%LOG%"
exit /b 1

:ok
echo 更新成功，新版本已正常启动；清理备份 >> "%LOG%"
rmdir /s /q "%BAK%" >nul 2>&1
exit /b 0

:rollback
echo 新版未启动成功，回滚 >> "%LOG%"
if exist "%TARGET%" rmdir /s /q "%TARGET%"
move "%BAK%" "%TARGET%" >nul 2>&1
start "" "%TARGET%\%EXE%"
exit /b 1
"""


def _q(p: str) -> str:
    """bash 单引号转义（路径里可能有空格、括号、中文）。"""
    return "'" + str(p).replace("'", "'\"'\"'") + "'"


def _winq(p: str) -> str:
    """bat 双引号里的值，把 % 转义掉（否则路径里的 % 会被当变量展开）。"""
    return str(p).replace("%", "%%").replace('"', "")


# macOS 上跟着 app 一起被替换掉、但必须保下来的目录（相对替换单元）
KEEP_DIRS_MAC = (os.path.join("Contents", "Resources", "data"),)


def download_dir(root: str) -> str:
    """更新包下载位置。和替换单元同卷（mv 才是原子的），又不和 staging 混在一起
    —— staging 每次解压前会被整个清空，包放里面会被顺手删掉。
    """
    return os.path.join(os.path.dirname(root), ".cogcraft-dl")


def build_script(root: str, state_dir: str, version: str, pid: int | None = None,
                 keep_dirs=None) -> tuple[str, list[str]]:
    """生成接手脚本，返回 (脚本路径, 启动它的命令行)。

    脚本写在 staging 的同级目录里（同卷），由调用方用 start_new_session / DETACHED
    拉起，然后主进程立刻退出。
    """
    staged = staging_path(root)
    backup = backup_path(root)
    marker = marker_path(state_dir, version)
    pid = int(pid or os.getpid())
    log = os.path.join(state_dir, "update.log")

    if keep_dirs is None:
        keep_dirs = KEEP_DIRS_MAC if sys.platform == "darwin" else ()
    keep_block = ""
    for rel in keep_dirs:
        keep_block += (
            f'if [ -d "$BAK/{rel}" ]; then\n'
            f'  rm -rf "$TARGET/{rel}"\n'
            f'  mkdir -p "$(dirname "$TARGET/{rel}")"\n'
            f'  if mv "$BAK/{rel}" "$TARGET/{rel}"; then echo "用户数据已跟着搬到新版：{rel}";\n'
            f'  else echo "警告：用户数据迁移失败，备份目录暂不删除"; fi\n'
            f'fi\n'
        )

    if sys.platform == "darwin":
        body = _BASH_TMPL
        for k, v in {
            "__LOG__": _q(log), "__TARGET__": _q(root), "__NEW__": _q(staged),
            "__BAK__": _q(backup), "__MARKER__": _q(marker), "__PID__": str(pid),
            "__APP__": _q(version),
            "__WAIT_EXIT__": str(WAIT_EXIT_SEC), "__WAIT_BOOT__": str(WAIT_BOOT_SEC),
            "__KEEP_BLOCK__": keep_block,
        }.items():
            body = body.replace(k, v)
        path = os.path.join(os.path.dirname(staged), "update.sh")
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.chmod(path, 0o755)
        return path, ["/bin/bash", path]

    # Windows
    logdir = os.path.dirname(log)
    bakdir = os.path.dirname(backup)
    body = _BAT_TMPL
    for k, v in {
        "__LOG__": _winq(log), "__LOGDIR__": _winq(logdir),
        "__TARGET__": _winq(root), "__NEW__": _winq(staged),
        "__BAK__": _winq(backup), "__BAKDIR__": _winq(bakdir),
        "__MARKER__": _winq(marker), "__PID__": str(pid),
        "__EXE__": WIN_EXE_NAME,
        "__WAIT_EXIT__": str(WAIT_EXIT_SEC), "__WAIT_BOOT__": str(WAIT_BOOT_SEC),
    }.items():
        body = body.replace(k, v)
    path = os.path.join(os.path.dirname(staged), "update.bat")
    with open(path, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(body)
    return path, ["cmd", "/c", path]


def spawn_script(script: str, argv: list[str]) -> None:
    """把接手脚本拉起来，和当前进程彻底脱钩。"""
    kw = {}
    if os.name == "nt":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kw["creationflags"] = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, cwd=os.path.dirname(script) or None, **kw)


# ────────────────────────── 启动时收拾残局 ──────────────────────────

def cleanup_leftovers(root: str = "") -> list[str]:
    """清理上一次更新留下的临时/备份目录。

    - staging 还在：说明上次下载完但没装（用户反悔或进程意外结束）→ 删
    - backup 还在：说明新版启动成功但脚本没来得及删（或回滚后残留）→ 删
    返回被删掉的路径列表，方便写日志。
    """
    root = root or install_root()
    if not root:
        return []
    gone = []
    for p in (os.path.dirname(staging_path(root)), os.path.dirname(backup_path(root)),
              download_dir(root)):
        if os.path.isdir(p):
            before = sum(len(f) for _, _, f in os.walk(p))
            shutil.rmtree(p, ignore_errors=True)
            if not os.path.exists(p):
                gone.append(f"{p}（{before} 个文件）")
    return gone
