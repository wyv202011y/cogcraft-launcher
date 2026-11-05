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
import webbrowser
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
import updater as updater_mod  # noqa: E402

APP_NAME = "cogcraft"
# 版本号唯一来源：CI 打包时从这里读，发版脚本也从这里读。
# 改版本 = 改这一行，别在别处再写一份。
VERSION = "1.0.4"
MANIFEST_URL = "https://www.cogcraft.top/client/manifest.json"
# 启动器自身的更新元数据（几十字节的 JSON），与清单分开：
# 清单管「游戏文件」，这里管「启动器本体」，两者的发布节奏不同。
LAUNCHER_META_URL = "https://www.cogcraft.top/client/launcher/latest.json"
MANIFEST_FALLBACK = os.path.join(_HERE, "manifest.example.json")


# ────────────────────────── 路径 ──────────────────────────

def userdata_dir() -> str:
    """用户目录下的数据位置（老位置，macOS 上是 ~/Library/Application Support/cogcraft）。

    保留它只为了①降级兜底②把老数据搬进 .app 时找得到源头。
    """
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, APP_NAME)


def bundle_data_dir() -> str | None:
    """macOS：数据放进 .app 自己里面，访达里把 app 拖进废纸篓就一并清干净。

    这是 App Store 应用的默认心算模型（一个 app = 一个可整体丢弃的包），
    而 ~/Library/Application Support/<app> 删了 app 之后会永久留在盘上。
    只有冻结后的 macOS 包才这么做：
      · Windows 玩家没有「拖到回收站卸载」的直觉，维持 %APPDATA%
      · 源码运行时 _HERE 是仓库目录，往那儿写数据会污染工作区
    """
    if sys.platform != "darwin" or not getattr(sys, "frozen", False):
        return None
    return os.path.join(_HERE, "data")


def _writable(p: str) -> bool:
    """真写一个探针文件再删掉 —— os.access 在只读挂载/沙箱下会骗人。"""
    try:
        os.makedirs(p, exist_ok=True)
        probe = os.path.join(p, ".write_probe")
        with open(probe, "w") as f:
            f.write("1")
        os.remove(probe)
        return True
    except Exception:
        return False


_DATA_DIR: str | None = None


def appdata_dir() -> str:
    """数据目录：优先 .app 内部；不可写就回落用户目录。

    不可写的真实场景：① 首次打开被 Gatekeeper 转译（App Translocation）到
    只读的随机路径 ② app 装在 /Applications 但当前用户不是拥有者。
    这两种情况都必须能继续用，不能因为「想放 app 里」就把启动器整个搞挂。
    """
    global _DATA_DIR
    if _DATA_DIR:
        return _DATA_DIR
    # 排障/测试用：COGCRAFT_DATA_DIR 强制指定数据目录
    forced = os.environ.get("COGCRAFT_DATA_DIR", "").strip()
    if forced:
        _DATA_DIR = forced
        return _DATA_DIR
    cand = bundle_data_dir()
    if cand and _writable(cand):
        _DATA_DIR = cand
    else:
        _DATA_DIR = userdata_dir()
    # 告诉 net.py 数据目录在哪：它要在 <data>/certs/ 里找玩家自备的根证书，
    # 也要往那儿缓存 macOS 钥匙串导出的 CA（拦截型网络下这是唯一能下动的前提）。
    os.environ["COGCRAFT_DATA_DIR"] = _DATA_DIR
    return _DATA_DIR


def in_bundle() -> bool:
    """当前数据是否真的落在 .app 内部（界面据此显示「删掉即清空」提示）。"""
    d = bundle_data_dir()
    return bool(d) and os.path.abspath(appdata_dir()) == os.path.abspath(d)


def migrate_legacy_data() -> dict:
    """把老位置（用户目录）的数据搬进 .app，只做一次，失败不影响启动。

    搬的是 settings.json + client/（游戏文件）。日志与清单缓存不搬 —— 留着无害，
    而且清单缓存下次自动重建。
    """
    new, old = appdata_dir(), userdata_dir()
    if os.path.abspath(new) == os.path.abspath(old) or not os.path.isdir(old):
        return {"状态": "无需迁移"}

    import shutil
    report: dict = {}
    # 1) 设置（迁过来才有引导完成状态、服务器地址这些）
    o_st, n_st = os.path.join(old, "settings.json"), os.path.join(new, "settings.json")
    if os.path.exists(o_st) and not os.path.exists(n_st):
        try:
            shutil.copy2(o_st, n_st)
            report["设置"] = "已迁入"
        except Exception as e:
            report["设置"] = f"失败({e})"

    # 2) 游戏文件（同卷时是一次 rename，几乎零耗时）
    o_c, n_c = os.path.join(old, "client"), os.path.join(new, "client")
    if os.path.isdir(o_c) and not os.path.exists(n_c):
        try:
            shutil.move(o_c, n_c)
            report["游戏文件"] = "已迁入"
        except Exception as e:
            report["游戏文件"] = f"失败({e})"
    return report


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
    "player_name_set": False,     # 玩家自己填过没有：没填过就别把系统用户名当昵称塞进引导页
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
    legacy_default = os.path.join(userdata_dir(), "client")
    new_default = os.path.join(appdata_dir(), "client")
    cur = st.get("game_dir")
    if not cur:
        st["game_dir"] = new_default
    elif (new_default != legacy_default
          and os.path.abspath(cur) == os.path.abspath(legacy_default)
          and not os.path.isdir(legacy_default)):
        # 老目录已经成功搬进 .app 了，持久化的旧路径要跟着改，否则下次还指回老地方
        st["game_dir"] = new_default
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
        # 文件指纹缓存：size+mtime 未变就不重算 sha1（465MB 每算一遍纯浪费）
        self._filecache_path = os.path.join(appdata_dir(), "filecache.json")
        self._filecache = manifest_mod.load_cache(self._filecache_path)
        # 启动器自更新检查结果（None = 还没查过）
        self.update: dict | None = None
        self.update_checking = False

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
                plan = manifest_mod.plan_sync(v, self.settings["game_dir"],
                                              cache=self._filecache)
                if plan.get("hashed"):
                    # 只有真算过哈希才落盘，避免每次界面刷新都写文件
                    manifest_mod.save_cache(self._filecache_path,
                                            str(mf.get("updated") or ""),
                                            self._filecache)
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
                "removed": len(plan.get("delete") or []),
                "verified": len(plan.get("ready") or []),
                "trusted": plan.get("trusted", 0),
            },
            "app_version": VERSION,
            "update": self.update,
            "update_checking": self.update_checking,
            "server": self.server_choice(),
            "manifest_error": self.manifest_error,
            "offline": bool(mf.get("_offline")),
            "java": {"path": java, "bundled": using_bundled},
            "settings": self.settings,
            # 数据是否在 .app 里：界面据此告诉用户「删掉 app 就清空」
            "data_dir": appdata_dir(),
            "data_in_app": in_bundle(),
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

    # ── 更新检查 ──
    def check_updates(self, force: bool = False) -> dict:
        """检查「启动器本体」和「游戏文件」两侧是否有更新。

        绝不抛异常、绝不阻塞启动：拉不到元数据就是「这次没查到」，
        界面照常能进游戏。游戏文件那侧的结论直接来自 state() 里的 plan，
        这里只额外算一份给界面显示的摘要，避免前端自己拼。
        """
        if self.update and not force:
            return self.update
        res = {"launcher": None, "files": None, "checked_at": int(time.time())}
        try:
            self.update_checking = True
            meta = updater_mod.fetch_meta(LAUNCHER_META_URL)
            res["launcher"] = updater_mod.check(
                meta, VERSION, channel=str((self.manifest or {}).get("channel") or "stable"))
        except Exception as e:
            res["launcher"] = {"available": False, "current": VERSION,
                               "error": f"暂时查不到更新（{e}）"}
            self._log("[update] 检查启动器更新失败: %s" % e)
        try:
            mf = self.manifest or {}
            if mf:
                v = manifest_mod.pick_variant(mf, self.settings.get("variant"))
                plan = manifest_mod.plan_sync(v, self.settings["game_dir"],
                                              cache=self._filecache)
                res["files"] = {
                    "changed": len(plan["download"]),
                    "removed": len(plan["delete"]),
                    "bytes": plan["bytes_needed"],
                    "ready": len(plan["ready"]),
                    "manifest_updated": mf.get("updated") or "",
                }
                if plan.get("hashed"):
                    manifest_mod.save_cache(self._filecache_path,
                                            str(mf.get("updated") or ""),
                                            self._filecache)
        except Exception as e:
            res["files"] = {"error": str(e)}
            self._log("[update] 检查游戏文件失败: %s" % e)
        self.update_checking = False
        self.update = res
        return res

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

    def open_url(self, url: str) -> dict:
        """用系统浏览器打开外部链接（加群页等）。

        为什么不让页面直接用 <a target="_blank">：pywebview 的 WebView 里那会
        开一个没有地址栏的空白窗口，或者干脆什么都不发生。交给系统浏览器最稳。
        只放行 http/https —— 这个方法是 JS 能调的，别给它留 file:// 或自定义协议。
        """
        u = (url or "").strip()
        if not u.lower().startswith(("http://", "https://")) or len(u) > 2000:
            return {"ok": False, "error": "只支持 http/https 链接"}
        try:
            webbrowser.open(u)
            return {"ok": True, "url": u}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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
        self.settings["player_name_set"] = True
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

    # 老版本把数据放在用户目录，新版本搬进 .app：只做一次，失败也只是继续用老位置
    mig = migrate_legacy_data()
    api = Api()
    if mig:
        api._log(f"[migrate] {mig} → 数据目录 {appdata_dir()}")
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
