#!/usr/bin/env python3
"""用 PyInstaller 打当前平台的包，产出 dist/cogcraft-<平台>-<架构>.zip

PyInstaller 不能交叉编译，所以每个平台各用自己的 runner 打。
用法：python3 ci/build.py <arm64|x64>
"""
from __future__ import annotations
import os
import platform as pf
import subprocess
import sys
import zipfile

# Windows runner 的控制台是 cp1252，直接 print 中文会 UnicodeEncodeError
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SEP = ";" if os.name == "nt" else ":"          # --add-data 的分隔符各平台不同


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, cwd=ROOT)


def main() -> None:
    arch = sys.argv[1] if len(sys.argv) > 1 else ("arm64" if pf.machine() in ("arm64", "aarch64") else "x64")
    sysname = {"darwin": "mac", "win32": "windows", "linux": "linux"}.get(sys.platform, sys.platform)
    jre_platform = {"darwin": "mac", "win32": "windows"}.get(sys.platform, "linux")
    jre_arch = "aarch64" if arch == "arm64" else "x64"

    # certifi 必须显式装：--collect-data certifi 找不到包会让打包直接失败，
    # 而且下载用的根证书就来自它的 cacert.pem。
    run([sys.executable, "-m", "pip", "install", "--upgrade", "pip",
         "pywebview", "pyinstaller", "certifi"])
    run([sys.executable, os.path.join("ci", "prepare_jre.py"), jre_platform, jre_arch])

    args = [
        # 用 -m 而不是裸 pyinstaller：Windows 上 Scripts 目录未必在 PATH 里
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--windowed", "--onedir",
        "--name", "cogcraft",
        # 引擎与界面要跟着走；runtime 是自带 JRE（app.py 的 bundled_java() 找它）
        "--add-data", f"ui{SEP}ui",
        "--add-data", f"engine{SEP}engine",
        "--add-data", f"runtime{SEP}runtime",
        "--add-data", f"manifest.example.json{SEP}.",
        # 下载走 urllib，根证书必须和 requests 同源（certifi 的 cacert.pem）。
        # 不显式收进来，macOS 冻结包里就没有可信根证书，HTTPS 下载全崩。
        "--collect-data", "certifi",
    ]
    # 各平台的 pywebview 后端不一样，只隐藏导入对应的那个
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "top.cogcraft.launcher",
                 "--hidden-import", "webview.platforms.cocoa"]
    elif sys.platform == "win32":
        args += ["--hidden-import", "webview.platforms.winforms",
                 "--hidden-import", "clr",
                 "--collect-all", "webview"]
    # 图标：mac 必须 .icns（PyInstaller 会自动写进 CFBundleIconFile），win 用 .ico。
    # 上次打成默认 Python 图标就是因为这里没传 --icon、仓库里也没有素材。
    icon = None
    if sys.platform == "darwin" and os.path.exists(os.path.join(ROOT, "assets", "icon.icns")):
        icon = os.path.join("assets", "icon.icns")
    elif sys.platform == "win32" and os.path.exists(os.path.join(ROOT, "assets", "icon.ico")):
        icon = os.path.join("assets", "icon.ico")
    if icon:
        args += ["--icon", icon]
        print(f"+ 使用图标: {icon}", flush=True)
    else:
        print("! 没找到图标素材，产物将使用 PyInstaller 默认图标", flush=True)

    args.append("app.py")
    run(args)

    # 校验图标真的进了包（不然等于没设）
    if sys.platform == "darwin" and icon:
        res_dir = os.path.join(ROOT, "dist", "cogcraft.app", "Contents", "Resources")
        icns = [f for f in os.listdir(res_dir) if f.endswith(".icns")] if os.path.isdir(res_dir) else []
        assert icns, "macOS 包内没有 .icns，图标没打进去"
        print(f"+ 包内图标: {icns}", flush=True)

    # 打包：mac 必须是 .app，用 ditto 才保得住符号链接
    out_name = f"cogcraft-{sysname}-{arch}.zip"
    out_path = os.path.join(ROOT, "dist", out_name)
    if sys.platform == "darwin":
        run(["ditto", "-c", "-k", "--sequesterRsrc", "--keepParent",
             os.path.join("dist", "cogcraft.app"), out_path])
    else:
        target = os.path.join("dist", "cogcraft")
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
            for dirpath, _dirs, files in os.walk(target):
                for f in files:
                    full = os.path.join(dirpath, f)
                    z.write(full, os.path.relpath(full, os.path.join(ROOT, "dist")))
    size = os.path.getsize(out_path)
    print(f"\n产物: {out_path}  {size / 1048576:.1f} MB", flush=True)
    assert size > 20 * 1048576, "产物太小，说明 JRE 或引擎没打进去"


if __name__ == "__main__":
    main()
