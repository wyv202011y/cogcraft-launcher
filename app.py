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
import socket
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


def _ensure_exec(path: str) -> None:
    """补回可执行位（只动随包内置的 runtime）。

    macOS 上从 zip/隔空投送过来的 .app 偶尔会丢可执行位，
    表现是 java 存在但一跑就 Permission denied，报错还很难看懂。
    """
    if sys.platform == "win32" or not path:
        return
    try:
        if not os.access(path, os.X_OK):
            os.chmod(path, os.stat(path).st_mode | 0o111)
    except Exception:
        pass


def find_java(settings: dict) -> str:
    """按优先级找 Java：用户指定 → 随包内置 → JAVA_HOME → PATH。"""
    _ensure_exec(bundled_java())
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
    "server_address": "",     # "" = 自动（内网可达用内网，否则用清单里的公网地址）
}

# 探活结果按地址缓存，避免每次 state() 都白等超时
_PROBE_CACHE: dict[str, tuple[float, bool]] = {}
_PROBE_TTL = 60.0


def probe_addr(addr: str, timeout: float = 1.2) -> bool:
    """TCP 探一下 host:port 通不通。内网/公网二选一时用，纯读取，不改任何状态。"""
    if not addr:
        return False
    now = time.time()
    hit = _PROBE_CACHE.get(addr)
    if hit and now - hit[0] < _PROBE_TTL:
        return hit[1]
    host, sep, port = addr.rpartition(":")
    if not sep:
        host, port = addr, "25565"
    ok = False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            ok = True
    except Exception:
        ok = False
    _PROBE_CACHE[addr] = (now, ok)
    return ok


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
        # 事件状态：macOS（pywebview/cocoa）下从后台线程调 evaluate_js 会被静默吞掉，
        # 界面改为靠轮询 poll() 拿进度，evaluate_js 只作为 Windows 上的即时锦上添花。
        self.progress: dict = {"seq": 0, "kind": None, "data": {}}
        self._log_path = os.path.join(appdata_dir(), "launcher.log")

    # ── 日志（用户机器上排障用；>512KB 自动轮转）──
    def _log(self, msg: str):
        try:
            if os.path.exists(self._log_path) and os.path.getsize(self._log_path) > 512 * 1024:
                os.replace(self._log_path, self._log_path + ".1")
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
        except Exception:
            pass

    # ── 事件：先落进可轮询的状态，再尝试即时推送 ──
    def emit(self, kind: str, payload: dict):
        try:
            self.progress = {"seq": int(self.progress.get("seq") or 0) + 1,
                             "kind": kind, "data": payload or {}}
        except Exception:
            pass
        if kind == "error":
            self._log("error: %s" % ((payload or {}).get("text") or ""))
        if not self.win:
            return
        try:
            data = json.dumps(payload, ensure_ascii=False)
            self.win.evaluate_js(
                f"window.__onEvent && window.__onEvent({json.dumps(kind)}, {data})")
        except Exception as e:
            # macOS 上后台线程调用 evaluate_js 会抛错（界面已改走轮询，功能不受影响）
            self._log("evaluate_js 推送失败（轮询兜底）: %s" % e)

    # ── 前端轮询入口：返回最后一次事件（seq 递增，前端据此判断是否新事件）──
    def poll(self) -> dict:
        return dict(self.progress)

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
            "hardware_text": self._describe_safe(hw),
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
            "server": self.server_choice(),
            "manifest_error": self.manifest_error,
            "offline": bool(mf.get("_offline")),
            "java": {"path": java, "bundled": using_bundled},
            "settings": self.settings,
            "modloader": (mf.get("modloader") or {}),
            "minecraft": (mf.get("minecraft") or {}),
        }

    def _describe_safe(self, hw: dict) -> str:
        """硬件摘要绝不允许抛异常 —— 它崩了整个 bootstrap 就跟着崩。

        macOS 上就踩过：avail_mb 是 None，describe() 里 None // 1024 抛
        TypeError，界面直接停在「初始化失败」，一个纯展示字段毁掉整个启动。
        """
        if not hw:
            return ""
        try:
            return jvm_mod.describe(hw)
        except Exception:
            self._log("describe() 失败，已降级：\n" + traceback.format_exc())
            cpu = (hw.get("cpu") or {}).get("name") or "未知"
            total = (hw.get("ram") or {}).get("total_mb")
            return f"CPU  {cpu}\n内存 {total // 1024 if total else '?'} GB"

    def client_error(self, msg: str) -> dict:
        """界面侧捕获到的错误写到 launcher.log —— 用户机器上没有控制台。"""
        self._log("[UI] " + str(msg)[:2000])
        return {"ok": True}

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

    def server_choice(self) -> dict:
        """这次到底连哪个地址。

        优先级：设置里手填的 > 清单的 address_lan（探活通了才用）> 清单的 address。

        为什么要内网这一层：家里内网连自己的公网域名走 hairpin NAT 是**不通**的
        （实测内网 telnet cogcraft.top:25565 直接失败），而内网地址直连是通的，
        所以本地测试必须能落到内网地址上。做成自动判定而不是让用户每次手改，
        外地玩家探内网 1.2 秒不通就回落公网，无感。
        """
        srv = dict(self.manifest.get("server") or {})
        pub = srv.get("address") or ""
        lan = srv.get("address_lan") or ""
        manual = (self.settings.get("server_address") or "").strip()
        if manual:
            return {**srv, "address": manual, "address_public": pub,
                    "address_lan": lan, "source": "manual"}
        if lan and lan != pub and probe_addr(lan):
            return {**srv, "address": lan, "address_public": pub,
                    "address_lan": lan, "source": "lan"}
        return {**srv, "address": pub, "address_public": pub,
                "address_lan": lan, "source": "public"}

    def set_server_address(self, addr: str) -> dict:
        """手填服务器地址；传空串表示回到自动（内网优先）。"""
        self.settings["server_address"] = (addr or "").strip()
        _PROBE_CACHE.pop(self.settings["server_address"], None)
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
                log=lambda m: self._log(str(m)),
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

        srv = self.server_choice()
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
