#!/usr/bin/env python3
"""
cogcraft 拼装引擎 —— 依赖库下载器

为什么需要: NeoForge 官方安装器只负责 NeoForge 自己的 47 个库 + 打补丁，
           vanilla 1.21.1 的 97 个库它一个都不下（实测 classpath 93 条里缺 44 条，
           lwjgl 都缺，导致启动报 NoClassDefFoundError）。
           这一步是启动器的职责。

下载源: 优先 BMCLAPI 镜像（国内 ~10MB/s），失败回退原始地址。
"""

import hashlib, json, os, sys, time, urllib.error, urllib.request, ssl, concurrent.futures

BMCLAPI = "https://bmclapi.bangbang93.com"      # 老域名，偶尔比 bmclapi2 更稳
UA = "cogcraft-launcher/1.0"
CTX_FALLBACK = ""
TLS_HINT = [False]        # True = 本轮出现过证书校验失败（八成是本机有 TLS 拦截软件）      # 非空 = 回落到系统根证书了（macOS 冻结包里这几乎必然全线证书错误）


def _make_ctx():
    """根证书必须跟 net.py 一致（certifi），否则 macOS 冻结包里一条都下不动。

    回落不再静默：以前 `except: ssl.create_default_context()` 一声不吭，
    结果就是「清单能拉、下载全崩」，排查绕了一大圈。现在回落会在日志和
    失败信息里明确写出来。
    """
    global CTX_FALLBACK
    try:
        import net as _n
        return _n._tls_ctx()
    except Exception as e:
        CTX_FALLBACK = f"{type(e).__name__}: {e}"
        return ssl.create_default_context()


CTX = _make_ctx()


BMCLAPI2 = "https://bmclapi2.bangbang93.com"
ALIYUN = "https://maven.aliyun.com/repository/public"


def rewrite_url(url: str) -> list:
    """候选 URL 列表：镜像 → 另一镜像 → 原始地址。

    实测（2026-11-02）：macOS/arm64 需要的 99 个构件，bmclapi2 镜像全部能拿到且
    sha1 一致（22 秒下完）。所以镜像本身没问题 —— 用户机上失败多半是「某个源在他那条
    线路上出问题」或本地状态，因此这里把候选拉长，任何一源能用就能补上。
    """
    out = []
    if "libraries.minecraft.net/" in url:
        path = url.split("libraries.minecraft.net/", 1)[1]
        out.append(f"{BMCLAPI2}/libraries/{path}")
        out.append(f"{BMCLAPI}/libraries/{path}")
        out.append(url)
        # 阿里云公共仓库兜底：libraries.minecraft.net 的构件绝大多数是 Maven Central 坐标，
        # 镜像全挂时它常常还能下（Mojang 独有的会 404，无害）
        out.append(f"{ALIYUN}/{path}")
    elif "maven.neoforged.net/releases/" in url:
        path = url.split("maven.neoforged.net/releases/", 1)[1]
        out.append(f"{BMCLAPI2}/maven/{path}")
        out.append(f"{BMCLAPI}/maven/{path}")
        out.append(url)
    else:
        out.append(url)
    # 去重（老域名和 bmclapi2 可能指向同一节点）
    return list(dict.fromkeys(out))


class _Permanent(Exception):
    """这个源没救（404/403 之类），别在它身上浪费时间，直接换下一个源。"""


def _host(url: str) -> str:
    try:
        return url.split("//", 1)[1].split("/", 1)[0]
    except IndexError:
        return url[:40]


_NO_VERIFY = None


def _no_verify_ctx():
    """免验证上下文 —— **只在 sha1+size 都在手里时**才允许用。

    道理和 net._tls_ctx(insecure=True) 一样：TLS 被本地软件拆开时，链验证必然失败，
    但内容对不对仍能靠清单里的 sha1 离线断定。牺牲的是「传输机密性」，
    保住的是「拿到的东西是对的」。
    """
    global _NO_VERIFY
    if _NO_VERIFY is None:
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        _NO_VERIFY = c
    return _NO_VERIFY


def _fetch(url, timeout=60, retries=3, insecure=False):
    """单源下载。返回 bytes；该源没救时抛 _Permanent。

    为什么要区分：以前一律重试 3 次，遇到 404 也重试，白白多花 2.4 秒/个；
    而 429/503（限流、节点过载）恰恰最该多试几次 —— 现在按状态码分流。
    """
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            ctx = _no_verify_ctx() if insecure else CTX
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (404, 410, 401):
                raise _Permanent(f"HTTP {e.code}") from e
            if e.code in (403, 429, 503, 502, 500):
                # 限流/过载：按对方给的窗口等，别硬撞
                wait = 0
                for h in ("Retry-After", "x-ratelimit-reset-short", "x-ratelimit-reset-long"):
                    v = e.headers.get(h)
                    if v and str(v).strip().isdigit():
                        wait = max(wait, min(int(str(v).strip()), 20))
                time.sleep(wait or (1.5 * (i + 1)))
                continue
            time.sleep(0.8 * (i + 1))
        except Exception as e:
            last = e
            time.sleep(0.8 * (i + 1))
    raise last if last else IOError("未知错误")



def probe_sources(sample_url: str, timeout=8) -> list:
    """逐个源试探「这条线路到底通不通」，只取一点头部就断开。

    排障价值极大：下载失败时先看这一行，就能分清是「镜像挂了 / 你的网络出不去 /
    文件真的不存在」，不用再猜。返回 [{host, ok, ms, err}]。
    """
    out = []
    for u in rewrite_url(sample_url):
        t0 = time.time()
        try:
            req = urllib.request.Request(u, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout, context=CTX) as r:
                r.read(1)          # 只读 1 字节就能确认通不通
            out.append({"host": _host(u), "ok": True, "ms": int((time.time() - t0) * 1000)})
        except _Permanent as e:
            out.append({"host": _host(u), "ok": False, "err": str(e),
                        "ms": int((time.time() - t0) * 1000)})
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:70]}"
            if "CERTIFICATE_VERIFY_FAILED" in str(e):
                msg = "证书校验失败（本机有软件在解密 HTTPS）"
                TLS_HINT[0] = True
            out.append({"host": _host(u), "ok": False, "err": msg,
                        "ms": int((time.time() - t0) * 1000)})
    return out


def ensure_libraries(merged: dict, game_dir: str, os_name: str,
                     rule_allows, library_path, workers=8):
    """把 merged['libraries'] 里所有缺失的构件下载到 game_dir/libraries/"""
    lib_root = os.path.join(game_dir, "libraries")
    tasks = []
    for lib in merged["libraries"]:
        if not rule_allows(lib.get("rules"), os_name, {}):
            continue
        rel = library_path(lib)
        dst = os.path.join(lib_root, rel.replace("/", os.sep))
        if os.path.exists(dst) and os.path.getsize(dst) > 0:
            continue
        art = (lib.get("downloads") or {}).get("artifact") or {}
        url = art.get("url")
        if not url:
            continue
        tasks.append((rel, dst, url, art.get("size"), art.get("sha1")))

    if not tasks:
        return {"needed": 0, "downloaded": 0, "errors": 0, "missing_after": 0}

    TLS_HINT[0] = False
    sources = probe_sources(tasks[0][2]) if tasks else []
    if CTX_FALLBACK:
        sources.append({"host": "(根证书)", "ok": False,
                        "err": "已回落到系统根证书：" + CTX_FALLBACK})
    done = {"n": 0, "bytes": 0}
    errors = []

    def work(t):
        rel, dst, url, size, sha1 = t
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        tried = []
        cert_blocked = False
        for u in rewrite_url(url):
            try:
                data = _fetch(u)
                if size and len(data) != size:
                    raise IOError(f"大小不符 期望{size} 实得{len(data)}")
                if sha1:
                    got = hashlib.sha1(data).hexdigest()
                    if got != sha1:
                        raise IOError(f"sha1 不符 {got[:8]} != {sha1[:8]}")
                tmp = dst + ".part"
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, dst)
                done["n"] += 1
                done["bytes"] += len(data)
                return None
            except _Permanent as e:
                tried.append(f"{_host(u)}: {e}")
            except ssl.SSLCertVerificationError as e:
                tried.append(f"{_host(u)}: 证书校验失败（{str(e)[:40]}）")
                TLS_HINT[0] = True
                cert_blocked = True
            except Exception as e:
                if "CERTIFICATE_VERIFY_FAILED" in str(e) or "证书" in str(e):
                    TLS_HINT[0] = True
                    cert_blocked = True
                tried.append(f"{_host(u)}: {type(e).__name__}: {str(e)[:80]}")
        # 全源都被证书卡住，且清单给了 sha1+size → 免验证再来一轮，靠哈希断定内容
        if cert_blocked and sha1 and size:
            for u in rewrite_url(url):
                try:
                    data = _fetch(u, insecure=True)
                    if len(data) != size:
                        raise IOError(f"大小不符 期望{size} 实得{len(data)}")
                    got = hashlib.sha1(data).hexdigest()
                    if got != sha1:
                        raise IOError(f"sha1 不符 {got[:8]} != {sha1[:8]}")
                    tmp = dst + ".part"
                    with open(tmp, "wb") as f:
                        f.write(data)
                    os.replace(tmp, dst)
                    done["n"] += 1
                    done["bytes"] += len(data)
                    done["insecure"] = done.get("insecure", 0) + 1
                    return None
                except Exception as e:
                    tried.append(f"{_host(u)}(免验证): {type(e).__name__}: {str(e)[:60]}")
        # 排障友好：写清「哪个文件、试过哪几个源、各自为什么失败」
        errors.append({"path": rel, "url": url, "tried": tried})
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        list(ex.map(work, tasks))

    # 复核
    missing_after = 0
    for lib in merged["libraries"]:
        if not rule_allows(lib.get("rules"), os_name, {}):
            continue
        rel = library_path(lib)
        dst = os.path.join(lib_root, rel.replace("/", os.sep))
        if not os.path.exists(dst):
            missing_after += 1

    failed = []
    if errors:
        list_path = os.path.join(lib_root, "libraries_failed.txt")
        try:
            os.makedirs(lib_root, exist_ok=True)
            with open(list_path, "w", encoding="utf-8") as f:
                f.write(time.strftime("生成时间 %Y-%m-%d %H:%M:%S\n"))
                for e in errors:
                    f.write(f"{e['path']}\n    原始地址 {e['url']}\n")
                    for line in e["tried"]:
                        f.write(f"    试过 {line}\n")
        except OSError:
            pass
        failed = [{"path": e["path"], "tried": e["tried"]} for e in errors]

    return {"needed": len(tasks), "downloaded": done["n"],
            "bytes": done["bytes"], "errors": len(errors),
            "insecure": done.get("insecure", 0),
            "sources": sources,
            "tls_intercepted": TLS_HINT[0],
            "error_samples": errors[:5], "failed": failed[:20],
            "failed_file": os.path.join(lib_root, "libraries_failed.txt") if errors else None,
            "missing_after": missing_after}


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from launch import merge_versions, rule_allows, library_path, os_name
    gd = sys.argv[1] if len(sys.argv) > 1 else "."
    nf = sys.argv[2] if len(sys.argv) > 2 else "neoforge-21.1.264"
    merged = merge_versions(gd, nf)
    t0 = time.time()
    r = ensure_libraries(merged, gd, os_name(), rule_allows, library_path)
    print(json.dumps(r, ensure_ascii=False, indent=2))
    print(f"用时 {time.time()-t0:.1f}s")
