"""安装流水线 —— 把「清单」变成「能玩的客户端」。

流程（每一步都可中断、可重入，重复执行不会重复下载）：
  1. 磁盘空间检查
  2. 下载并改写 NeoForge 安装器 → 静默安装（装过了就跳过）
  3. 补齐 vanilla 依赖库（NeoForge 安装器不管这部分）
  4. 下载 assets 资源（约 800 MB，一次装好永久复用）
  5. 按清单同步 mods / config（带 sha1 校验）
  6. 可以启动了

进度通过 progress(step, pct, message) 回调给 UI。
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import manifest as manifest_mod  # noqa: E402

# 国内可用的镜像（实测：BMCLAPI 的 client.jar 9.5 MB/s）
BMCLAPI = "https://bmclapi2.bangbang93.com"


class Step:
    DISK = "disk"
    LOADER = "loader"
    LIBRARIES = "libraries"
    ASSETS = "assets"
    FILES = "files"
    DONE = "done"


STEP_LABELS = {
    Step.DISK: "检查磁盘空间",
    Step.LOADER: "安装模组加载器",
    Step.LIBRARIES: "补齐运行库",
    Step.ASSETS: "下载游戏资源",
    Step.FILES: "同步模组与配置",
    Step.DONE: "准备完成",
}

# 一次完整安装（不含 mods）大约占这么多
BASE_INSTALL_BYTES = 1200 * 1024 * 1024


class PipelineError(Exception):
    pass


def _download(url: str, dest: str, timeout: float = 120.0, retries: int = 3,
              allow_empty: bool = False) -> int:
    """下载到 dest。实现收敛在 net.py（IPv4 优先 + 原子写 + 退避重试）。"""
    import net
    return net.download_to(url, dest, timeout=timeout, retries=retries,
                           allow_empty=allow_empty)


class Pipeline:
    def __init__(self, game_dir: str, mf: dict, variant_id: str | None,
                 java_exe: str, progress=None):
        self.game_dir = game_dir
        self.mf = mf
        self.variant = manifest_mod.pick_variant(mf, variant_id)
        self.java_exe = java_exe
        self.progress = progress or (lambda *a, **k: None)
        self.mc_version = mf["minecraft"]["version"]
        self.loader_type = mf["modloader"].get("type", "neoforge")
        self.loader_version = mf["modloader"]["version"]
        self.loader_id = f"{self.loader_type}-{self.loader_version}"
        self._cancel = threading.Event()

    # ────────── 工具 ──────────

    def cancel(self):
        self._cancel.set()

    def _emit(self, step: str, pct: float, msg: str = ""):
        self.progress(step, max(0.0, min(1.0, pct)), msg)

    def _check_cancel(self):
        if self._cancel.is_set():
            raise PipelineError("已取消")

    # ────────── 1. 磁盘 ──────────

    def step_disk(self) -> None:
        self._emit(Step.DISK, 0.0, "检查磁盘空间…")
        need = BASE_INSTALL_BYTES + int(
            self.variant.get("total_bytes") or
            sum(int(f.get("size") or 0) for f in self.variant.get("files", [])))
        try:
            free = shutil.disk_usage(self.game_dir).free
        except Exception:
            return
        if free < need:
            raise PipelineError(
                f"磁盘空间不足：需要约 {need // 1048576} MB，"
                f"当前只剩 {free // 1048576} MB")
        self._emit(Step.DISK, 1.0, f"空间充足（剩 {free // 1073741824:.1f} GB）")

    # ────────── 2. 模组加载器 ──────────

    def _loader_installed(self) -> bool:
        p = os.path.join(self.game_dir, "versions", self.loader_id,
                         f"{self.loader_id}.json")
        return os.path.exists(p)

    def step_loader(self) -> None:
        if self._loader_installed():
            self._emit(Step.LOADER, 1.0, "已安装，跳过")
            return

        self._emit(Step.LOADER, 0.02, "获取安装器…")
        cache = os.path.join(self.game_dir, ".cogcraft", "cache")
        os.makedirs(cache, exist_ok=True)
        raw = os.path.join(cache, f"{self.loader_id}-installer.jar")
        patched = os.path.join(cache, f"{self.loader_id}-installer-cn.jar")

        url = (self.mf["modloader"].get("installer_url")
               or f"{BMCLAPI}/neoforge/version/{self.loader_version}/installer")
        if not os.path.exists(raw):
            _download(url, raw)
        self._check_cancel()

        self._emit(Step.LOADER, 0.15, "改写下载源…")
        if not os.path.exists(patched):
            from patch_installer import patch_installer
            patch_installer(raw, patched)

        # 安装器要求目标目录里有 launcher_profiles.json
        lp = os.path.join(self.game_dir, "launcher_profiles.json")
        if not os.path.exists(lp):
            with open(lp, "w", encoding="utf-8") as f:
                json.dump({"profiles": {}, "settings": {}, "version": 3}, f)

        self._emit(Step.LOADER, 0.22, "正在安装（约 1~3 分钟）…")
        cmd = [self.java_exe, "-Xmx2G",
               "-Djava.net.preferIPv4Stack=true",
               "-jar", patched, "--installClient", self.game_dir]

        si = None
        if sys.platform == "win32":
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                startupinfo=si)
        lines = []
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.append(line.rstrip())
            self._check_cancel()
            # 安装器不打印百分比，只能按输出行数给一个渐进的假进度
            self._emit(Step.LOADER, min(0.95, 0.22 + len(lines) * 0.004),
                       line.strip()[:80] or "安装中…")
        code = proc.wait()
        if code != 0 or not self._loader_installed():
            raise PipelineError(
                "模组加载器安装失败（退出码 %s）\n%s" % (code, "\n".join(lines[-15:])))
        self._emit(Step.LOADER, 1.0, "安装完成")

    # ────────── 3. 依赖库 ──────────

    def step_libraries(self) -> None:
        self._emit(Step.LIBRARIES, 0.05, "核对运行库…")
        from launch import merge_versions, os_name, rule_allows, library_path
        from libraries import ensure_libraries

        merged = merge_versions(self.game_dir, self.loader_id)
        self._emit(Step.LIBRARIES, 0.3, "下载缺失的运行库…")
        st = ensure_libraries(merged, self.game_dir, os_name(),
                              rule_allows, library_path)
        if st.get("missing_after"):
            raise PipelineError(f"仍有 {st['missing_after']} 个运行库缺失，请重试")
        self._emit(Step.LIBRARIES, 1.0,
                   f"运行库齐全" + (f"（补了 {st.get('downloaded', 0)} 个）"
                                    if st.get("downloaded") else ""))

    # ────────── 4. 资源 ──────────

    def _assets_index_path(self) -> str | None:
        """找到真实的资源索引文件。

        坑: 索引文件名是 assetIndex.id（1.21.1 对应的是 "17.json"），
        不是 Minecraft 版本号。按版本号去找永远找不到。
        """
        idx_dir = os.path.join(self.game_dir, "assets", "indexes")
        if not os.path.isdir(idx_dir):
            return None
        # 优先从版本 json 里读 assetIndex.id
        vj = os.path.join(self.game_dir, "versions", self.mc_version,
                          f"{self.mc_version}.json")
        if os.path.exists(vj):
            try:
                with open(vj, encoding="utf-8") as f:
                    aid = json.load(f).get("assetIndex", {}).get("id")
                if aid:
                    p = os.path.join(idx_dir, f"{aid}.json")
                    if os.path.exists(p):
                        return p
            except Exception:
                pass
        # 回退：取目录里最大的那个 json
        cands = [os.path.join(idx_dir, n) for n in os.listdir(idx_dir)
                 if n.endswith(".json")]
        return max(cands, key=os.path.getsize) if cands else None

    def _assets_done(self) -> bool:
        idx = self._assets_index_path()
        obj = os.path.join(self.game_dir, "assets", "objects")
        if not idx or not os.path.isdir(obj):
            return False
        try:
            with open(idx, encoding="utf-8") as f:
                objs = json.load(f).get("objects", {})
            # 必须按唯一 hash 比：3911 条索引里只有 3888 个唯一文件
            want = {o["hash"] for o in objs.values() if o.get("hash")}
            if not want:
                return False
            have = set()
            for _root, _dirs, files in os.walk(obj):
                have.update(n for n in files if not n.endswith(".part"))
            return len(want - have) == 0
        except Exception:
            return False

    def step_assets(self) -> None:
        if self._assets_done():
            self._emit(Step.ASSETS, 1.0, "已就绪，跳过")
            return
        from assets import ensure_asset_index, download_objects

        self._emit(Step.ASSETS, 0.02, "获取资源索引…")
        index_id, index = ensure_asset_index(self.mc_version, self.game_dir)
        objects = index.get("objects") or {}
        if not objects:
            raise PipelineError("资源索引为空，服务器可能返回了错误内容")
        self._emit(Step.ASSETS, 0.05, f"共 {len(objects)} 个资源文件")

        def cb(done, total, nbytes, tbytes):
            self._check_cancel()
            pct = done / total if total else 0
            self._emit(Step.ASSETS, 0.05 + pct * 0.95,
                       f"{done}/{total} 个 · {nbytes / 1048576:.0f} MB")

        r = download_objects(objects, self.game_dir, workers=16, progress=cb)
        if r.get("error_count"):
            raise PipelineError(
                f"{r['error_count']} 个资源下载失败，点重试可续传（已下载的会跳过）\n"
                + "\n".join(r.get("errors") or []))
        self._emit(Step.ASSETS, 1.0, "资源就绪")

    # ────────── 5. mods / config ──────────

    def step_files(self) -> None:
        self._emit(Step.FILES, 0.01, "核对客户端文件…")

        # 清空上一轮的移除暂存区
        trash = os.path.join(self.game_dir, ".cogcraft", "removed")
        if os.path.isdir(trash):
            shutil.rmtree(trash, ignore_errors=True)

        plan = manifest_mod.plan_sync(self.variant, self.game_dir)
        total = len(plan["download"])
        nbytes = plan["bytes_needed"]

        for rel in plan["delete"]:
            src = os.path.join(self.game_dir, rel.replace("/", os.sep))
            dst = os.path.join(trash, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            try:
                shutil.move(src, dst)
            except Exception:
                pass

        if not total:
            self._emit(Step.FILES, 1.0,
                       f"全部就绪（{len(plan['ready'])} 个文件已校验）")
            return

        self._emit(Step.FILES, 0.03,
                   f"需下载 {total} 个文件 · {nbytes / 1048576:.0f} MB")
        done_n = [0]
        got_b = [0]
        lock = threading.Lock()
        errors: list[str] = []

        def one(f):
            self._check_cancel()
            rel = f["path"].lstrip("/").replace("/", os.sep)
            dest = os.path.join(self.game_dir, rel)
            # 主地址失败就试备用地址（mirror）。
            # 现实场景：cdn.modrinth.com 在国内经常连不上或极慢，
            # 备用地址可以是挂在香港服务器上的镜像，只在必要时吃我们的带宽。
            urls = [f["url"]]
            if f.get("mirror"):
                urls.append(f["mirror"])
            last_err = None
            expect_zero = int(f.get("size") or 0) == 0
            for u in urls:
                try:
                    _download(u, dest, allow_empty=expect_zero)
                    with lock:
                        done_n[0] += 1
                        got_b[0] += int(f.get("size") or 0)
                        self._emit(Step.FILES, 0.03 + done_n[0] / total * 0.97,
                                   f"{done_n[0]}/{total} · "
                                   f"{got_b[0] / 1048576:.0f} MB")
                    return
                except Exception as e:
                    last_err = e
                    continue
            with lock:
                errors.append(f"{f['path']}: {last_err}")

        # 并发上限 5：HK2 服务端 limit_conn perip 是 8，
        # 留 3 条余量给「重试/换备用地址」用，避免自己把自己顶到 429。
        # 226 个文件里 212 个是小配置文件，并发主要是省往返延迟，不是抢带宽。
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            list(ex.map(one, plan["download"]))

        if errors:
            raise PipelineError(
                f"{len(errors)} 个文件下载失败，可以点重试继续（已下载的不会重下）\n"
                + "\n".join(errors[:6]))
        self._emit(Step.FILES, 1.0, f"同步完成（{total} 个文件）")

    # ────────── 编排 ──────────

    def ensure_all(self) -> dict:
        os.makedirs(self.game_dir, exist_ok=True)
        t0 = time.time()
        report = {}
        for name, fn in (
            (Step.DISK, self.step_disk),
            (Step.LOADER, self.step_loader),
            (Step.LIBRARIES, self.step_libraries),
            (Step.ASSETS, self.step_assets),
            (Step.FILES, self.step_files),
        ):
            self._check_cancel()
            t = time.time()
            fn()
            report[name] = round(time.time() - t, 1)
        report["total"] = round(time.time() - t0, 1)
        self._emit(Step.DONE, 1.0, "可以进入游戏了")
        return report
