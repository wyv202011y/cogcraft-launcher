"""cogcraft 启动器 —— 应用外壳（pywebview + Python 引擎桥）。

架构：
    界面 (HTML/CSS/JS)  ←→  本文件 (Api)  ←→  engine/ (拼装引擎)
    pywebview 把 Api 的方法暴露成 window.pywebview.api.xxx()

所有耗时操作都在后台线程跑，通过 window.evaluate_js 把进度推回界面，
绝不在 UI 线程里做 IO —— 否则窗口会假死。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENGINE = os.path.join(_HERE, "engine")
if _ENGINE not in sys.path:
    sys.path.insert(0, _ENGINE)

import hardware  # noqa: E402
import jvm as jvm_mod  # noqa: E402
import manifest as manifest_mod  # noqa: E402
import pipeline as pipeline_mod  # noqa: E402
import bench as bench_mod  # noqa: E402

APP_NAME = "cogcraft"
MANIFEST_URL = "https://www.cogcraft.top/client/manifest.json"
MANIFEST_FALLBACK = os.path.join(_HERE, "manifest.example.json")


# ────────────────────────── 路径 ──────────────────────────

def appdata_dir() -> str:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def bundled_java() -> str | None:
    """优先用随包携带的 64 位 Java，彻底消灭玩家装错 Java 的可能。"""
    exe = "java.exe" if sys.platform == "win32" else "java"
    for rel in (os.path.join("runtime", "bin", exe),
                os.path.join("jre", "bin", exe)):
        p = os.path.join(_HERE, rel)
        if os.path.exists(p):
            return p
    return None


def find_java(settings: dict) -> str:
    """按优先级找 Java：用户指定 → 随包内置 → JAVA_HOME → PATH。"""
    cands = [settings.get("java_path"), bundled_java()]
    jh = os.environ.get("JAVA_HOME", "").strip()
    if jh:   # 空 JAVA_HOME 会拼出 /bin/java 这种假路径，必须先判空
        cands.append(os.path.join(jh, "bin", "java.exe" if sys.platform == "win32" else "java"))
    for cand in cands:
        if cand and os.path.exists(cand):
            return cand
    import shutil
    found = shutil.which("java")
    if found:
        return found
    raise RuntimeError("未找到 Java 运行时，请重新安装启动器")


# ────────────────────────── 设置 ──────────────────────────

DEFAULT_SETTINGS = {
    "variant": None,          # None = 按硬件自动推荐
    "memory_mb": None,        # None = 按硬件自动算
    "player_name": "",
    "java_path": None,
    "game_dir": None,
    "auto_join": True,
    "prefer_zgc": False,
    "onboarded": False,       # 首次引导是否走完
}


def load_settings() -> dict:
    p = os.path.join(appdata_dir(), "settings.json")
    st = dict(DEFAULT_SETTINGS)
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                st.update(json.load(f) or {})
        except Exception:
            pass
    if not st.get("game_dir"):
        st["game_dir"] = os.path.join(appdata_dir(), "client")
    if not st.get("player_name"):
        st["player_name"] = os.environ.get("USERNAME") or os.environ.get("USER") or "Player"
    return st


def save_settings(st: dict) -> None:
    p = os.path.join(appdata_dir(), "settings.json")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


# ────────────────────────── 桥 ──────────────────────────

class Api:
    def __init__(self):
        self.win = None
        self.settings = load_settings()
        self.hw: dict = {}
        self.manifest: dict = {}
        self.manifest_error: str | None = None
        self.pipeline: pipeline_mod.Pipeline | None = None
        self.proc: subprocess.Popen | None = None
        self._busy = threading.Lock()
        self._bench_thread: threading.Thread | None = None

    # ── 向后端推事件 ──
    def emit(self, kind: str, payload: dict):
        if not self.win:
            return
        try:
            data = json.dumps(payload, ensure_ascii=False)
            self.win.evaluate_js(
                f"window.__onEvent && window.__onEvent({json.dumps(kind)}, {data})")
        except Exception:
            pass

    def _run_bg(self, fn, *a, **kw):
        t = threading.Thread(target=fn, args=a, kwargs=kw, daemon=True)
        t.start()
        return t

    # ── 初始化 ──
    def bootstrap(self) -> dict:
        """界面加载后第一件事：探测硬件 + 拉清单 + 算内存方案。"""
        try:
            java = find_java(self.settings)
        except Exception:
            java = None
        self.hw = hardware.detect(java)
        self._refresh_manifest_sync()
        return self.state()

    def _refresh_manifest_sync(self):
        try:
            mf = manifest_mod.fetch(MANIFEST_URL, cache_dir=appdata_dir())
            self.manifest = manifest_mod.validate(mf)
            self.manifest_error = None
        except Exception as e:
            self.manifest_error = str(e)
            # 服务器拿不到就退回本地清单，至少别让界面空白
            try:
                with open(MANIFEST_FALLBACK, encoding="utf-8") as f:
                    self.manifest = manifest_mod.validate(json.load(f))
                self.manifest["_offline"] = True
            except Exception:
                self.manifest = {}

    def state(self) -> dict:
        """界面渲染需要的全部数据。"""
        mf = self.manifest or {}
        hw = self.hw or {}

        cached_bench = self.settings.get("bench") or None
        bench = cached_bench if self._bench_fresh(cached_bench) else None
        auto_variant, reason = (jvm_mod.recommend_variant(hw, bench) if hw
                                else ("full", ""))
        variant_id = self.settings.get("variant") or auto_variant
        try:
            rec = jvm_mod.recommend(hw, variant_id,
                                    self.settings.get("memory_mb"),
                                    bool(self.settings.get("prefer_zgc")))
        except Exception:
            rec = {}

        variants = manifest_mod.variant_summary(mf) if mf else []
        plan = {}
        if mf:
            try:
                v = manifest_mod.pick_variant(mf, variant_id)
                plan = manifest_mod.plan_sync(v, self.settings["game_dir"])
            except Exception:
                plan = {}

        try:
            java = find_java(self.settings)
            using_bundled = java == bundled_java()
        except Exception:
            java, using_bundled = None, False

        return {
            "hardware": self.hw,
            "hardware_text": jvm_mod.describe(hw) if hw else "",
            "variants": variants,
            "variant": variant_id,
            "variant_auto": auto_variant,
            "variant_reason": reason,
            "bench": bench or {},
            "memory": rec,
            "pending": {
                "files": len(plan.get("download") or []),
                "bytes": plan.get("bytes_needed", 0),
            },
            "server": (mf.get("server") or {}),
            "manifest_error": self.manifest_error,
            "offline": bool(mf.get("_offline")),
            "java": {"path": java, "bundled": using_bundled},
            "settings": self.settings,
            "modloader": (mf.get("modloader") or {}),
            "minecraft": (mf.get("minecraft") or {}),
        }

    # ── 设置修改 ──
    def benchmark(self, force: bool = False) -> dict:
        """跑性能微基准。已测过直接返回缓存，没测过就丢到后台线程。

        为什么不在调用里直接跑：pywebview 的 JS→Python 桥是串行的，
        在前台跑 3.4 秒会让界面这 3.4 秒点什么都没反应。所以返回 pending，
        测完用 emit("bench", ...) 把结果推回界面。

        为什么用 JRE 跑真 Java 代码：测的是「这台机器跑 MC 有多快」，
        含 JIT 预热和 G1 回收，比第三方跑分贴近得多。
        """
        cached = self.settings.get("bench")
        if cached and cached.get("ok") and not force and self._bench_fresh(cached):
            return cached
        if self._bench_thread and self._bench_thread.is_alive():
            return {"ok": False, "pending": True}
        self._bench_thread = self._run_bg(self._bench_worker)
        return {"ok": False, "pending": True}

    def _bench_fresh(self, cached: dict | None) -> bool:
        """缓存是否还有效：硬件指纹一致才算数（加内存/换显卡后要重测）。"""
        if not cached or not cached.get("ok"):
            return False
        want = bench_mod.hw_fingerprint(self.hw)
        got = cached.get("hw_fp")
        if not got:
            return False          # 老版本存的数据没有指纹 → 当作过期
        return got == want

    def _bench_worker(self):
        try:
            java = find_java(self.settings)
        except Exception:
            java = None
        try:
            res = bench_mod.measure(java, self.hw)
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        if res.get("ok"):
            self.settings["bench"] = res
            try:
                save_settings(self.settings)
            except Exception:
                pass
        self.emit("bench", res)

    def set_variant(self, variant_id: str) -> dict:
        self.settings["variant"] = variant_id
        save_settings(self.settings)
        return self.state()

    def set_memory(self, mb: int) -> dict:
        self.settings["memory_mb"] = int(mb) if mb else None
        save_settings(self.settings)
        return self.state()

    def set_player(self, name: str) -> dict:
        self.settings["player_name"] = (name or "").strip()[:16]
        save_settings(self.settings)
        return self.state()

    def set_auto_join(self, on: bool) -> dict:
        self.settings["auto_join"] = bool(on)
        save_settings(self.settings)
        return self.state()

    def reset_settings(self) -> dict:
        """恢复推荐：清掉手调的版本与内存，交还给硬件自动判断。"""
        self.settings["variant"] = None
        self.settings["memory_mb"] = None
        self.settings["prefer_zgc"] = False
        save_settings(self.settings)
        return self.state()

    def finish_onboard(self) -> dict:
        """首次引导走完后调用：记住别再弹了。"""
        self.settings["onboarded"] = True
        save_settings(self.settings)
        return self.state()

    def launch(self) -> dict:
        """不检查文件，直接启动。供界面在「已就绪」状态下调用。"""
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "任务已在进行中"}
        try:
            self._run_bg(self._launch_wrapper)
            return {"ok": True}
        except Exception as e:
            self._busy.release()
            return {"ok": False, "error": str(e)}

    def _launch_wrapper(self):
        try:
            java = find_java(self.settings)
            self._launch_now(java)
        except Exception as e:
            self.emit("error", {"text": str(e), "detail": traceback.format_exc()[-1500:]})
        finally:
            try:
                self._busy.release()
            except Exception:
                pass

    # ── 主流程 ──
    def start(self) -> dict:
        """下载/校验 + 启动。整体在后台线程里跑。"""
        if not self._busy.acquire(blocking=False):
            return {"ok": False, "error": "任务已在进行中"}
        try:
            self.emit("phase", {"phase": "prepare", "text": "开始检查"})
            self._run_bg(self._worker)
            return {"ok": True}
        finally:
            pass

    def _worker(self):
        try:
            java = find_java(self.settings)
            pipe = pipeline_mod.Pipeline(
                self.settings["game_dir"], self.manifest,
                self.settings.get("variant"), java,
                progress=lambda step, pct, msg: self.emit(
                    "progress", {"step": step, "pct": pct, "text": msg,
                                 "label": pipeline_mod.STEP_LABELS.get(step, step)}),
            )
            self.pipeline = pipe
            report = pipe.ensure_all()
            self.emit("progress", {"step": "done", "pct": 1.0,
                                   "text": f"准备完成（{report['total']} 秒）",
                                   "label": "准备完成"})
            self._launch_now(java)
        except Exception as e:
            self.emit("error", {"text": str(e), "detail": traceback.format_exc()[-1500:]})
        finally:
            self.pipeline = None
            try:
                self._busy.release()
            except Exception:
                pass

    def cancel(self) -> dict:
        if self.pipeline:
            self.pipeline.cancel()
        return {"ok": True}

    # ── 启动游戏 ──
    def _launch_now(self, java: str):
        from launch import build_command

        variant_id = self.settings.get("variant") or jvm_mod.recommend_variant(self.hw, self.settings.get("bench"))[0]
        rec = jvm_mod.recommend(self.hw, variant_id,
                                self.settings.get("memory_mb"),
                                bool(self.settings.get("prefer_zgc")))

        srv = self.manifest.get("server") or {}
        addr = srv.get("address") if (srv.get("auto_join") and
                                      self.settings.get("auto_join", True)) else ""

        mc_ver = self.manifest["minecraft"]["version"]
        loader_id = f"{self.manifest['modloader'].get('type', 'neoforge')}-" \
                    f"{self.manifest['modloader']['version']}"

        cmd, _merged = build_command(
            self.settings["game_dir"], loader_id, java,
            {"player_name": self.settings["player_name"],
             "max_memory_mb": rec["xmx_mb"],
             "server_address": addr or "",
             "extra_jvm": rec["jvm_args"] + (self.manifest.get("jvm", {}).get("extra_args") or [])})

        self.emit("phase", {"phase": "launching", "text": "正在启动游戏…"})
        si = None
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        self.proc = subprocess.Popen(cmd, cwd=self.settings["game_dir"],
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL, startupinfo=si)
        self.emit("phase", {"phase": "running", "text": "游戏已启动"})

        code = self.proc.wait()
        oom = None
        log = os.path.join(self.settings["game_dir"], "logs", "latest.log")
        try:
            oom = jvm_mod.scan_log_for_oom(log)
        except Exception:
            pass
        payload = {"phase": "exited", "code": code,
                   "text": f"游戏已退出（代码 {code}）"}
        if oom:
            if oom["kind"] == "heap":
                nxt, msg = jvm_mod.next_xmx_try(rec["xmx_mb"], self.hw)
                payload.update({"oom": "heap", "suggest_mb": nxt, "text": msg})
            else:
                payload.update({"oom": "system",
                                "text": "系统可用内存不足，建议关闭其它程序或改用流畅版"})
        self.emit("phase", payload)


# ────────────────────────── 入口 ──────────────────────────

def main():
    try:
        import webview
    except ImportError:
        print("缺少 pywebview，开发环境请先 pip install pywebview", file=sys.stderr)
        raise

    api = Api()
    ui = os.path.join(_HERE, "ui", "index.html")
    win = webview.create_window(
        "cogcraft", f"file://{ui}",
        width=1000, height=640, min_size=(900, 600),
        background_color="#0f1115",
        js_api=api,
    )
    api.win = win
    webview.start(debug=bool(os.environ.get("COGCRAFT_DEBUG")))


if __name__ == "__main__":
    main()
