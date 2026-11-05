#!/usr/bin/env python3
"""selfupdate 安装环节的回归测试（在本机 Linux 上跑，模拟 macOS 的替换流程）。

测的是「会毁掉启动器」的那部分逻辑，所以重点在两条路：
    成功：旧进程退出 → 换目录 → 新版写启动标记 → 删备份
    回滚：新版起不来（不写标记）→ 目录换回去 → 旧版重新拉起

用假的 `open` 顶替 macOS 的 open：真 open 会去启动 .app，假的就是把里面的
可执行文件跑起来。这样 Linux 上就能把整条链路走完，不需要 mac 机器。

跑法：python3 ci/test_selfupdate.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "engine"))

import selfupdate as su  # noqa: E402

RESULTS: list[tuple[bool, str]] = []


def ck(label: str, cond, extra: str = "") -> None:
    RESULTS.append((bool(cond), label))
    print(("  ✅ " if cond else "  ❌ ") + label + (f"   {extra}" if extra else ""))


def fake_unit(work: str, name: str, tag: str, writes_marker: bool) -> str:
    """造一个假的 .app 单元：Contents/MacOS/cogcraft 是个会留痕的脚本。"""
    unit = os.path.join(work, name)
    exe = os.path.join(unit, "Contents", "MacOS", "cogcraft")
    os.makedirs(os.path.dirname(exe), exist_ok=True)
    # 真实的新版本由 write_start_marker() 建目录再写文件，这里照做
    marker_line = ('mkdir -p "$(dirname "$FAKE_MARKER")" && touch "$FAKE_MARKER"'
                   if writes_marker else "true")
    with open(exe, "w", encoding="utf-8") as f:
        f.write(f'#!/bin/bash\n{marker_line}\necho {tag}\nsleep 2\n')
    os.chmod(exe, 0o755)
    with open(os.path.join(unit, "Contents", "version.txt"), "w") as f:
        f.write(tag)
    return unit


def make_zip(unit: str, zip_path: str) -> str:
    """把 unit 目录打成 zip —— 顶层就是 unit 自己的名字（和 ditto --keepParent 一致）。"""
    base = os.path.dirname(unit)
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for dirpath, _d, files in os.walk(unit):
            for fn in files:
                full = os.path.join(dirpath, fn)
                z.write(full, os.path.relpath(full, base))
    return zip_path


def install_fake_open(bindir: str) -> None:
    """假 open：把 <bundle>/Contents/MacOS/cogcraft 拉起来（macOS open 的等价物）。"""
    p = os.path.join(bindir, "open")
    with open(p, "w", encoding="utf-8") as f:
        f.write('#!/bin/bash\n'
                'T="$1"\n'
                'E="$T/Contents/MacOS/cogcraft"\n'
                '[ -x "$E" ] && ( "$E" >/dev/null 2>&1 & )\n'
                'exit 0\n')
    os.chmod(p, 0o755)


def wait_for(pred, timeout: float, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


def main() -> int:
    work = tempfile.mkdtemp(prefix="su_test_")
    bindir = os.path.join(work, "bin")
    state = os.path.join(work, "state")
    apps = os.path.join(work, "apps")
    for d in (bindir, state, apps):
        os.makedirs(d, exist_ok=True)
    install_fake_open(bindir)
    os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")

    # 全部按 macOS 走（Linux 上没有 .app，但目录语义一致）
    su.sys.platform = "darwin"
    su.WAIT_EXIT_SEC = 8
    su.WAIT_BOOT_SEC = 6

    root = os.path.join(apps, "cogcraft.app")
    fake_unit(apps, "cogcraft.app", "OLD", writes_marker=False)
    try:
        os.makedirs(root)
    except FileExistsError:
        pass

    # ── 1. 结构校验 ────────────────────────────────────────────
    print("\n【1】更新包结构校验")
    zgood = make_zip(fake_unit(work, "good/cogcraft.app", "NEW", True),
                     os.path.join(work, "good.zip"))
    ok = True
    try:
        new = su.unpack(zgood, root)
        ok = os.path.exists(os.path.join(new, "Contents", "MacOS", "cogcraft"))
    except Exception as e:                                  # noqa: BLE001
        ok = False
        print("    解压异常:", e)
    ck("合法包能解压且结构正确", ok)
    ck("解压后主程序仍有可执行权限（权限位被还原）",
       os.access(os.path.join(new, "Contents", "MacOS", "cogcraft"), os.X_OK))
    ck("解压落在同级的 .cogcraft-update 里（保证同卷）",
       su.staging_path(root).startswith(os.path.join(apps, ".cogcraft-update")))

    # 缺可执行文件的包必须被拒，且不留半成品
    bad = os.path.join(work, "bad")
    os.makedirs(os.path.join(bad, "cogcraft.app", "Contents"), exist_ok=True)
    open(os.path.join(bad, "cogcraft.app", "Contents", "version.txt"), "w").write("X")
    zbad = make_zip(os.path.join(bad, "cogcraft.app"), os.path.join(work, "bad.zip"))
    raised = False
    try:
        su.unpack(zbad, root)
    except ValueError:
        raised = True
    ck("缺可执行文件的包被拒绝", raised)
    ck("被拒的包没有留下半成品目录", not os.path.isdir(su.staging_path(root)))

    # 顶层目录不唯一也必须拒
    multi = os.path.join(work, "multi")
    for n in ("a", "b"):
        os.makedirs(os.path.join(multi, n), exist_ok=True)
        open(os.path.join(multi, n, "f.txt"), "w").write("x")
    with zipfile.ZipFile(os.path.join(work, "multi.zip"), "w") as z:
        for n in ("a", "b"):
            z.write(os.path.join(multi, n, "f.txt"), f"{n}/f.txt")
    raised = False
    try:
        su.unpack(os.path.join(work, "multi.zip"), root)
    except ValueError:
        raised = True
    ck("顶层目录不唯一的包被拒绝", raised)

    # ── 2. 成功路径 ────────────────────────────────────────────
    print("\n【2】成功路径：旧进程退出 → 换目录 → 新版报活 → 删备份")
    # 旧版里有用户数据（macOS 的数据就在 app 内部，换包时必须保住）
    datadir = os.path.join(root, "Contents", "Resources", "data")
    os.makedirs(datadir, exist_ok=True)
    with open(os.path.join(datadir, "settings.json"), "w", encoding="utf-8") as f:
        f.write('{"player":"王樱博","mem":8192}')

    old_proc = subprocess.Popen(["sleep", "30"])
    new = su.unpack(zgood, root)
    ck("新版包里没有 data 目录（所以必须靠脚本搬）",
       not os.path.isdir(os.path.join(new, "Contents", "Resources", "data")))
    marker = su.marker_path(state, "1.0.8")
    script, argv = su.build_script(root, state, "1.0.8", pid=old_proc.pid)
    ck("生成了接手脚本", os.path.exists(script), os.path.basename(script))
    ck("脚本语法正确（bash -n）",
       subprocess.run(["bash", "-n", script]).returncode == 0)
    src = open(script, encoding="utf-8").read()
    ck("脚本按 PID 等旧进程（不会误伤另一个实例）", "kill -0" in src and str(old_proc.pid) in src)
    ck("脚本先备份再就位（有回滚点）", src.index('mv "$TARGET" "$BAK"') < src.index('mv "$NEW" "$TARGET"'))

    env = dict(os.environ, FAKE_MARKER=marker)
    stdouth = open(os.path.join(work, "p1.out"), "w")
    runner = subprocess.Popen(argv, env=env, stdout=stdouth, stderr=subprocess.STDOUT)
    time.sleep(1.5)
    old_proc.kill()                       # 模拟用户关掉启动器
    old_proc.wait()                       # 必须收尸：僵尸进程 kill -0 仍然成功
    ck("旧进程退出前没有动过文件（还在等）", os.path.exists(os.path.join(root, "Contents", "version.txt"))
       and open(os.path.join(root, "Contents", "version.txt")).read() == "OLD")
    runner.wait(timeout=40)
    stdouth.close()

    ver_txt = open(os.path.join(root, "Contents", "version.txt")).read()
    ck("目录已换成新版", ver_txt == "NEW", f"version.txt={ver_txt}")
    ck("旧版备份被清理", not os.path.exists(su.backup_path(root)))
    ck("staging 已消失", not os.path.exists(su.staging_path(root)))
    log = open(os.path.join(state, "update.log"), encoding="utf-8").read()
    ck("日志记录了全过程", "旧进程已退出" in log and "更新成功" in log)
    # 最关键的一条：换包不能把用户数据弄丢（否则更新一次设置全没）
    kept = os.path.join(root, "Contents", "Resources", "data", "settings.json")
    ck("用户数据跟着搬到了新版", os.path.exists(kept))
    if os.path.exists(kept):
        ck("用户数据内容完好", "王樱博" in open(kept, encoding="utf-8").read())
    ck("日志里记录了数据搬迁", "用户数据已跟着搬到新版" in log)

    # ── 3. 回滚路径 ────────────────────────────────────────────
    print("\n【3】回滚路径：新版起不来 → 换回旧版")
    # 把目录恢复成「旧版」，再造一个「不会写标记」的新包
    shutil.rmtree(root)
    fake_unit(apps, "cogcraft.app", "OLD2", writes_marker=False)
    zbad2 = make_zip(fake_unit(work, "broken/cogcraft.app", "BROKEN", False),
                     os.path.join(work, "broken.zip"))
    su.unpack(zbad2, root)

    old_proc2 = subprocess.Popen(["sleep", "30"])
    marker2 = su.marker_path(state, "1.0.9")
    script2, argv2 = su.build_script(root, state, "1.0.9", pid=old_proc2.pid)
    env2 = dict(os.environ, FAKE_MARKER=marker2)
    out2 = open(os.path.join(work, "p2.out"), "w")
    runner2 = subprocess.Popen(argv2, env=env2, stdout=out2, stderr=subprocess.STDOUT)
    time.sleep(1.0)
    old_proc2.kill()
    old_proc2.wait()
    runner2.wait(timeout=60)
    out2.close()

    ver2 = open(os.path.join(root, "Contents", "version.txt")).read()
    ck("新版没起来时目录被换回旧版", ver2 == "OLD2", f"version.txt={ver2}")
    ck("备份被还原（不是被删）", not os.path.exists(su.backup_path(root)))
    log2 = open(os.path.join(state, "update.log"), encoding="utf-8").read()
    ck("日志里有回滚记录", "回滚" in log2)

    # ── 4. 超时放弃：旧进程赖着不走，一个文件都不许动 ────────────
    print("\n【4】旧进程不退出 → 放弃，且不动任何文件")
    su.WAIT_EXIT_SEC = 3
    shutil.rmtree(root)
    fake_unit(apps, "cogcraft.app", "KEEP", writes_marker=False)
    su.unpack(zgood, root)
    stubborn = subprocess.Popen(["sleep", "60"])
    script3, argv3 = su.build_script(root, state, "1.1.0", pid=stubborn.pid)
    r3 = subprocess.run(argv3, env=dict(os.environ, FAKE_MARKER=su.marker_path(state, "1.1.0")),
                        capture_output=True, timeout=60)
    ver3 = open(os.path.join(root, "Contents", "version.txt")).read()
    ck("超时后退出码非 0", r3.returncode != 0)
    ck("超时后原目录原封不动", ver3 == "KEEP", f"version.txt={ver3}")
    ck("超时后没建备份", not os.path.exists(su.backup_path(root)))
    stubborn.kill()
    stubborn.wait()
    su.WAIT_EXIT_SEC = 8

    # ── 5. preflight ──────────────────────────────────────────
    print("\n【5】安装前可行性检查")
    su.is_frozen = lambda: False
    pf1 = su.preflight()
    ck("开发模式拒绝自动更新", not pf1["ok"] and "开发模式" in pf1["reason"])
    su.is_frozen = lambda: True
    su.install_root = lambda: root
    ro = os.path.join(work, "readonly")
    os.makedirs(ro, exist_ok=True)
    real_root = os.path.join(ro, "cogcraft.app")
    fake_unit(ro, "cogcraft.app", "RO", False)   # 先造好再变只读
    os.chmod(ro, 0o555)
    su.install_root = lambda: real_root
    pf2 = su.preflight()
    ck("父目录不可写时拒绝（dmg 里直接运行的情况）",
       (not pf2["ok"]) and ("权限" in pf2["reason"] or "不可写" in pf2["reason"]), pf2["reason"][:60])
    os.chmod(ro, 0o755)
    su.install_root = lambda: root

    # ── 6. 启动时清残局 ────────────────────────────────────────
    print("\n【6】启动时清理上一轮的残留")
    os.makedirs(os.path.dirname(su.staging_path(root)), exist_ok=True)
    os.makedirs(su.staging_path(root), exist_ok=True)
    open(os.path.join(su.staging_path(root), "x.txt"), "w").write("x")
    os.makedirs(os.path.dirname(su.backup_path(root)), exist_ok=True)
    os.makedirs(su.backup_path(root), exist_ok=True)
    gone = su.cleanup_leftovers(root)
    ck("清理了 staging 和 backup", len(gone) == 2, f"清了 {len(gone)} 处")
    ck("清理后目录确实没了",
       not os.path.exists(su.staging_path(root)) and not os.path.exists(su.backup_path(root)))

    # ── 收尾 ─────────────────────────────────────────────────
    for p in (root,):
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
    shutil.rmtree(work, ignore_errors=True)

    passed = sum(1 for ok, _ in RESULTS if ok)
    print(f"\n{'='*50}\n结果：{passed}/{len(RESULTS)} 通过")
    if passed != len(RESULTS):
        print("失败项：")
        for ok, label in RESULTS:
            if not ok:
                print("  -", label)
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
