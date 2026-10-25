#!/usr/bin/env python3
"""把 ui/index.html 的各个状态渲染成 PNG（无头 chromium）。

用途：改完界面必须出图核对，不能靠猜。
HTML 自带 mockApi()，所以不需要真起 pywebview。
"""
import os
import re
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "ui", "index.html")
STATE_JSON = os.path.join(ROOT, "ui", "dev-state.json")   # 由真实 app.state() 导出
OUT = os.path.join(ROOT, "ui-shots")
def _find_chrome() -> str:
    """找无头 chromium：优先环境变量 CHROME，其次 playwright 缓存，最后系统 chrome。"""
    env = os.environ.get("CHROME")
    if env and os.path.isfile(env):
        return env
    import glob
    for pat in ("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome",
                "~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium",
                "~/AppData/Local/ms-playwright/chromium-*/chrome-win*/chrome.exe"):
        hits = sorted(glob.glob(os.path.expanduser(pat)))
        if hits:
            return hits[-1]
    for name in ("chromium", "chromium-browser", "google-chrome"):
        import shutil as _sh
        f = _sh.which(name)
        if f:
            return f
    raise SystemExit("找不到 chromium，请设置环境变量 CHROME=/path/to/chrome")


CHROME = _find_chrome()
W, H, SCALE = 1000, 640, 2

# 名字 → (注入 JS, URL query)
STATES = {
    "01-主界面-就绪": ("", ""),
    "02-主界面-下载中": ("""
      setProgress(0.437, '正在下载模组 99/226 · 214 MB', '下载客户端文件');""", ""),
    "03-安装完成": ("""
      setProgress(1.0, '全部就绪，可以进入游戏了', '就绪');
      setBar('done');
      document.getElementById('btnMain').disabled = false;
      document.getElementById('btnMain').textContent = '进入游戏';
      document.getElementById('pill').className = 'pill ok';
      document.getElementById('pill').textContent = '● 已就绪';""", ""),
    "04-出错态": ("""
      window.__onEvent('error', {text: '3 个文件下载失败，可以点重试继续（已下载的不会重下）'});""", ""),
    "05-游戏运行中": ("""
      window.__onEvent('phase', {phase: 'running'});""", ""),
    "06-设置面板": ("""
      document.getElementById('btnSettings').click();""", ""),
    "07-内存不足提示": ("""
      window.__onEvent('phase', {phase: 'exited',
        text: '游戏已退出', oom: 'heap', suggest_mb: 7168});""", ""),

    # ── 首次引导 ──
    "08-引导-欢迎":   ("", "?onboard=1"),
    "09-引导-硬件检测": ("window.__wiz(1);", "?onboard=1"),
    "10-引导-选版本":  ("window.__wiz(2);", "?onboard=1"),
    "11-引导-确认":    ("window.__wiz(3);", "?onboard=1"),

    # ── 下载详情 ──
    "12-下载详情": ("""
      window.__busy();
      window.__steps('disk', 1, '空间充足（剩 128.0 GB）');
      window.__steps('loader', 1, '安装完成');
      window.__steps('libraries', 1, '运行库齐全');
      window.__steps('assets', 0.62, '2410 / 3888 个 · 486 MB');""", ""),
    "13-下载详情-同步模组": ("""
      window.__busy();
      window.__steps('disk', 1, '空间充足（剩 128.0 GB）');
      window.__steps('loader', 1, '安装完成');
      window.__steps('libraries', 1, '运行库齐全');
      window.__steps('assets', 1, '资源就绪');
      window.__steps('files', 0.44, '99 / 226 · 214 MB');""", ""),
}


def render(name: str, spec) -> str:
    js, query = spec if isinstance(spec, tuple) else (spec, "")
    src = open(HTML, encoding="utf-8").read()
    if os.path.exists(STATE_JSON):
        real = open(STATE_JSON, encoding="utf-8").read()
        src = src.replace("<body>", "<body>\n<script>window.__REAL_STATE=" +
                          real + ";</script>", 1)
    if js:
        src = src.replace("</body>", f"<script>setTimeout(function(){{{js}}},260);</script></body>")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    tmp.write(src)
    tmp.close()
    # chromium 写不出中文文件名的路径（会静默失败、产出 0 字节），
    # 所以先落到 ASCII 临时名，再用 Python 改名。
    # 临时文件必须和目标同盘，否则 os.replace 会 EXDEV；名字保持 ASCII
    raw = os.path.join(OUT, "_raw_%d.png" % abs(hash(name)))
    r = subprocess.run([
        CHROME, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--hide-scrollbars", f"--force-device-scale-factor={SCALE}",
        f"--window-size={W},{H}", "--virtual-time-budget=2500",
        f"--screenshot={raw}", "file://" + tmp.name + query,
    ], capture_output=True, text=True)
    os.unlink(tmp.name)
    png = os.path.join(OUT, name + ".png")
    if os.path.exists(raw) and os.path.getsize(raw) > 3000:
        os.replace(raw, png)
        return png
    print("    chromium:", (r.stdout + r.stderr).strip()[:200])
    return png


def main():
    os.makedirs(OUT, exist_ok=True)
    names = sys.argv[1:] or list(STATES)
    for n in names:
        if n not in STATES:
            print(f"  未知状态: {n}")
            continue
        p = render(n, STATES[n])
        sz = os.path.getsize(p) if os.path.exists(p) else 0
        print(f"  {'✅' if sz > 3000 else '❌'} {n:<18} {sz/1024:6.0f} KB  {p}")


def _warn_identical(paths):
    """多个状态渲染出完全相同的字节数 → 说明它们其实是同一张图（多半被覆盖层挡住）。"""
    from collections import defaultdict
    by = defaultdict(list)
    for p in paths:
        if os.path.exists(p):
            by[os.path.getsize(p)].append(os.path.basename(p))
    bad = {k: v for k, v in by.items() if len(v) > 2}
    if bad:
        print("\n  ⚠️ 大小雷同告警：以下状态可能是同一张图")
        for size, names in bad.items():
            print(f"     {size} 字节 × {len(names)}: {', '.join(names[:6])}")


if __name__ == "__main__":
    main()
    _warn_identical([os.path.join(OUT, n + '.png') for n in STATES])
