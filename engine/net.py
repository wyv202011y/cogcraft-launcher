"""统一网络层 —— 所有下载都走这里。

为什么需要它：
1. **IPv6 优先导致下载直接失败**
   本机实测：cdn.modrinth.com 同时返回 A(146.75.113.242) 和 AAAA(2a04:4e42::498)，
   Python 的 urllib 默认优先 IPv6，而机器没有 IPv6 路由时直接抛
   `URLError: Network is unreachable`，**不会自动回退到 IPv4**。
   强制 IPv4 后同一个请求就能发出去了。所以全局把 getaddrinfo 的
   结果按 IPv4 优先排序；如果某个域名只有 IPv6，则退回原始结果。

2. **下载必须原子**
   先写 `.part` 再 os.replace，避免半截文件被当成完整文件（sha1 校验
   虽然能查出来，但会导致每次启动都重下同一个文件）。

3. **重试要带退避**
   CDN 偶发 5xx / 连接重置很常见，固定间隔重试会撞在同一个坏节点上。
"""

from __future__ import annotations

import http.client
import os
import random
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

UA = "cogcraft-launcher/1.0"
TLS_INFO = {"certifi": "", "extras": [], "anchors_note": ""}


def _data_dir_hint() -> str:
    """数据目录由 app.py 通过 COGCRAFT_DATA_DIR 告诉我们（net 不该自己去猜路径）。"""
    return os.environ.get("COGCRAFT_DATA_DIR", "").strip()


def _macos_keychain_pem(data_dir: str) -> str | None:
    """把 macOS 钥匙串里的根证书导成一份 PEM，缓存 7 天。

    为什么需要：玩家机器上有 TLS 拦截软件（网络加速器 / 代理 / 杀毒的 HTTPS 扫描 /
    校园网门户）时，它用自己的根证书做中间人。这类根证书**装在系统钥匙串里**，
    浏览器认，但 certifi 包里的那份 Mozilla 根证书列表**没有它** ——
    于是清单能拉（走别的路径），一到下载就全线 `CERTIFICATE_VERIFY_FAILED:
    self-signed certificate in certificate chain`。实测玩家机就是这个。

    系统默认上下文在 macOS 上读不到钥匙串（CPython 用 OpenSSL 的默认路径），
    所以只能自己用 `security find-certificate -a -p` 导出来再叠加。
    """
    if sys.platform != "darwin":
        return None
    base = data_dir or os.path.join(tempfile.gettempdir(), "cogcraft")
    cache_dir = os.path.join(base, "certs")
    out = os.path.join(cache_dir, ".keychain-ca.pem")
    try:
        if os.path.isfile(out) and os.path.getsize(out) > 0 and \
                (time.time() - os.path.getmtime(out)) < 7 * 86400:
            return out
    except OSError:
        pass
    keychains = [
        "/System/Library/Keychains/SystemRootCertificates.keychain",
        "/Library/Keychains/System.keychain",
        os.path.expanduser("~/Library/Keychains/login.keychain-db"),
    ]
    parts = []
    for kc in keychains:
        if not os.path.exists(kc) and "login" not in kc:
            continue
        try:
            r = subprocess.run(["security", "find-certificate", "-a", "-p", kc],
                               capture_output=True, timeout=25)
            if r.returncode == 0 and r.stdout:
                parts.append(r.stdout.decode("utf-8", "replace"))
        except Exception:
            continue
    if not parts:
        return None
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
        return out if os.path.getsize(out) > 0 else None
    except OSError:
        return None


def _extra_ca_files() -> list[str]:
    """附加信任源：<数据目录>/certs/*.pem|crt|cer（玩家自己放的）+ macOS 钥匙串导出。"""
    out = []
    d = _data_dir_hint()
    if d:
        cdir = os.path.join(d, "certs")
        if os.path.isdir(cdir):
            for n in sorted(os.listdir(cdir)):
                if n.lower().endswith((".pem", ".crt", ".cer")):
                    out.append(os.path.join(cdir, n))
    kc = _macos_keychain_pem(d)
    if kc:
        out.append(kc)
    return out


def _make_tls() -> ssl.SSLContext:
    """TLS 上下文必须和 requests 用同一套根证书。

    坑（macOS 实测踩中）：清单走 requests（自带 certifi 根证书）拉得好好的，
    但下载走 urllib + create_default_context()，靠的是**系统**根证书。
    macOS 上官方 Python 系统根证书路径是空的、冻结包更没有，于是清单能拿到、
    下载必然 SSL: CERTIFICATE_VERIFY_FAILED —— 界面表现就是「检查磁盘空间 ✅
    安装模组加载器 ❌ self-signed certificate in certificate chain」。

    修法：先建系统默认上下文，再把 certifi 的 cacert.pem 叠加进去，
    这样下载和清单的信任锚完全一致。
    """
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
        TLS_INFO["certifi"] = certifi.where()
    except Exception:
        pass
    # 系统钥匙串 / 玩家自备的根证书：拦截型网络下这是唯一出路
    loaded = []
    for f in _extra_ca_files():
        try:
            ctx.load_verify_locations(cafile=f)
            loaded.append(os.path.basename(f))
        except Exception:
            continue
    TLS_INFO["extras"] = loaded
    TLS_INFO["anchors_note"] = ("信任源: certifi" + (f" + {len(loaded)} 个附加" if loaded else ""))
    return ctx


_TLS = _make_tls()
_INSECURE_TLS = None
_POOL = threading.local()


def _tls_ctx(insecure: bool = False) -> ssl.SSLContext:
    """insecure=True 只在「已经用清单里的 sha1 钉死内容」时才允许使用。

    为什么需要它：国内运营商/校园网会插入自签证书做劫持，链验证必然失败。
    这时候 TLS 挡攻击的价值已经没了，但**内容哈希**还有 —— 只要清单里
    写了 installer_sha1，拿到的东西对不对能离线判定，比死等一个连不上的
    官方源强。绝不静默降级：调用方必须先有 hash 才允许传 insecure。
    """
    global _INSECURE_TLS
    if not insecure:
        return _TLS
    if _INSECURE_TLS is None:
        c = ssl.create_default_context()
        c.check_hostname = False
        c.verify_mode = ssl.CERT_NONE
        _INSECURE_TLS = c
    return _INSECURE_TLS


# nginx 的 keepalive_timeout 是 60s，到点它悄悄关掉空闲连接、我们这边毫无察觉，
# 下一个请求直接 SSLEOFError。空闲超过这个秒数就主动换新连接，不赌它还活着。
_IDLE_RECONNECT_SEC = 45.0


def _touch_conn(scheme: str, host: str, port: int, insecure: bool = False) -> None:
    """记一下这个连接最后一次干完活的时间，用来判断空闲了多久。"""
    used = getattr(_POOL, "used", None)
    if used is None:
        used = _POOL.used = {}
    used[(scheme, host, port, insecure)] = time.time()


def _pooled_conn(scheme: str, host: str, port: int, timeout: float,
                 insecure: bool = False):
    """取本线程到该主机的可复用连接。

    坑：226 个客户端文件里 212 个是小配置文件，每个几 KB。
    如果每个文件都新建 TLS 连接，59ms RTT 下光握手就要
    2×RTT×226 ≈ 27 秒——实测正是这个数。复用连接后只剩 1 个 RTT。
    """
    conns = getattr(_POOL, "conns", None)
    if conns is None:
        conns = _POOL.conns = {}
    used = getattr(_POOL, "used", None)
    if used is None:
        used = _POOL.used = {}
    key = (scheme, host, port, insecure)
    c = conns.get(key)
    if c is not None:
        idle = time.time() - used.get(key, 0.0)
        if getattr(c, "sock", None) is None or idle > _IDLE_RECONNECT_SEC:
            try:
                c.close()
            except Exception:
                pass
            conns.pop(key, None)
            c = None
    if c is None:
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        kw = {"context": _tls_ctx(insecure)} if scheme == "https" else {}
        c = cls(host, port, timeout=timeout, **kw)
        conns[key] = c
        used[key] = time.time()   # 新连接从此刻开始算空闲
    return c


def _drop_conn(scheme: str, host: str, port: int) -> None:
    conns = getattr(_POOL, "conns", None) or {}
    for k in [k for k in conns if k[:3] == (scheme, host, port)]:
        c = conns.pop(k, None)
        try:
            if c:
                c.close()
        except Exception:
            pass


def _http_get_stream(url: str, timeout: float, chunk: int, sink,
                     redirects: int = 4, insecure: bool = False) -> int:
    """用可复用连接流式 GET，把数据喂给 sink(bytes)。返回字节数。

    只做最朴素的实现：不走代理、不自动解压，够用且可预测。
    出任何问题都由调用方丢弃连接后重试。
    """
    u = urlsplit(normalize_url(url))
    scheme = u.scheme or "https"
    host = u.hostname or ""
    port = u.port or (443 if scheme == "https" else 80)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query

    conn = _pooled_conn(scheme, host, port, timeout, insecure)
    try:
        conn.request("GET", path, headers={
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive",
        })
        r = conn.getresponse()
        if r.status in (301, 302, 303, 307, 308) and redirects > 0:
            loc = r.getheader("Location")
            r.read()
            if loc:
                if loc.startswith("//"):
                    loc = scheme + ":" + loc
                elif loc.startswith("/"):
                    loc = f"{scheme}://{host}{loc}"
                return _http_get_stream(loc, timeout, chunk, sink,
                                        redirects - 1, insecure)
        if r.status not in (200, 206):
            body = r.read(512)
            # 连接上的错误响应读完后连接仍可用，但 4xx/5xx 后保险起见丢弃
            _drop_conn(scheme, host, port)
            raise urllib.error.HTTPError(url, r.status, r.reason, r.headers,
                                         None if not body else _Wrap(body))
        n = 0
        while True:
            b = r.read(chunk)
            if not b:
                break
            sink(b)
            n += len(b)
        _touch_conn(scheme, host, port, insecure)
        return n
    except Exception:
        _drop_conn(scheme, host, port)
        raise


class _Wrap:
    def __init__(self, b):
        self._b = b

    def read(self, *_a):
        return self._b
_ORIG_GETADDRINFO = socket.getaddrinfo
_IPV4_INSTALLED = False


def prefer_ipv4() -> None:
    """全局让 DNS 结果里 IPv4 优先。幂等，可重复调用。"""
    global _IPV4_INSTALLED
    if _IPV4_INSTALLED:
        return

    def _sorted(host, port, family=0, type=0, proto=0, flags=0):
        try:
            infos = _ORIG_GETADDRINFO(host, port, family, type, proto, flags)
        except Exception:
            raise
        if family in (0, socket.AF_UNSPEC):
            v4 = [i for i in infos if i[0] == socket.AF_INET]
            v6 = [i for i in infos if i[0] == socket.AF_INET6]
            # 有 IPv4 就把它排前面；只有 IPv6 的站点保持原样
            if v4:
                return v4 + v6
        return infos

    socket.getaddrinfo = _sorted  # type: ignore[assignment]
    _IPV4_INSTALLED = True


def normalize_url(url: str) -> str:
    """把 URL 里的非 ASCII 字符（中文、全角符号）转成百分号编码。

    urllib 只接受 ASCII URL，整合包里的模组文件名普遍带中文，
    直接用会抛 `'ascii' codec can't encode characters`。
    对已经是 %XX 的部分不重复编码。
    """
    from urllib.parse import quote, urlsplit, urlunsplit
    try:
        p = urlsplit(url)
    except Exception:
        return url
    path = quote(p.path, safe="/%")
    query = quote(p.query, safe="=&%+")
    return urlunsplit((p.scheme, p.netloc, path, query, p.fragment))


# 连接层面的偶发中断：对端悄悄关掉 keep-alive、中间设备收尾、响应没读完就断了。
# 这些重连一次基本就好 —— 重试前 _drop_conn 会把池里的死连接清掉，下一次一定是新连接。
_TRANSIENT = (
    ssl.SSLEOFError,          # _ssl.c:2427 —— nginx keepalive_timeout 到点关连接，最常见
    ssl.SSLZeroReturnError,
    ssl.SSLSyscallError,
    ssl.SSLWantReadError,
    ssl.SSLWantWriteError,
    ConnectionResetError,
    ConnectionAbortedError,
    BrokenPipeError,
    TimeoutError,
    socket.timeout,
    http.client.RemoteDisconnected,
    http.client.IncompleteRead,
    http.client.BadStatusLine,
)


def _permanent(err: Exception) -> bool:
    """判断错误是否值得重试。

    4xx 里除了 408/429 都是「这个文件本来就不该能下」，
    重试三次只会让界面卡住——实测过：URL 层级写错导致 404 时，
    每个文件要磨掉 21 秒才报错。

    证书错误同理：链验证失败重试一百次也是失败，只会白等。
    macOS 上就是这一条把「换个源」拖成了好几分钟。

    但注意：ssl.SSLError 是个大类，SSLEOFError（连接被对端关掉）也在里面。
    以前一刀切把整个 SSLError 当永久错误，结果是每装一次就有几个文件
    撞上 keep-alive 死连接直接判死、一次都不重试。必须按具体类型分开判。
    """
    # HTTPError 是 URLError 的子类，必须最先判 —— 它的 .reason 是字符串不是异常，
    # 若先走下面那个拆包逻辑，404 会被当成字符串里的错误而变成「可重试」，
    # 又回到「URL 写错时每个文件磨 21 秒」的老坑。
    if isinstance(err, urllib.error.HTTPError):
        return err.code not in (408, 429) and 400 <= err.code < 500
    inner = err
    if isinstance(err, urllib.error.URLError):
        reason = getattr(err, "reason", None)
        if isinstance(reason, BaseException):
            inner = reason
    if isinstance(inner, ssl.SSLCertVerificationError):
        return True
    if isinstance(inner, _TRANSIENT):
        return False
    if isinstance(inner, ssl.SSLError):
        return True
    return False


def _retry_after(err: Exception) -> float | None:
    """读 429/503 响应里的 Retry-After（秒）。服务器主动让我们等，就老实等。"""
    h = getattr(err, "headers", None)
    if not h:
        return None
    try:
        v = h.get("Retry-After")
        return float(v) if v else None
    except Exception:
        return None


def _sleep_before_retry(i: int, err: Exception, base: float) -> None:
    """退避 + 抖动。

    抖动很重要：一批客户端同时被限流后如果都按同一间隔重试，
    会形成尖峰一波波撞服务器，等于自己制造 DDoS。
    """
    wait = _retry_after(err)
    if wait is None:
        wait = base * (i + 1) * (1 + random.random() * 0.6)
    time.sleep(min(wait, 30.0))


def _req(url: str, timeout: float, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(normalize_url(url), method=method,
                                  headers={"User-Agent": UA})


def get_bytes(url: str, timeout: float = 30.0, retries: int = 3,
              ok_codes: tuple[int, ...] = (200, 206)) -> bytes:
    """取一个 URL 的内容（小文件/JSON 用）。"""
    prefer_ipv4()
    last: Exception | None = None
    for i in range(retries):
        try:
            with urllib.request.urlopen(_req(url, timeout), timeout=timeout,
                                       context=_tls_ctx()) as r:
                if r.status not in ok_codes:
                    raise IOError(f"HTTP {r.status}")
                return r.read()
        except Exception as e:
            last = e
            if _permanent(e):
                break
            if i < retries - 1:
                _sleep_before_retry(i, e, 1.0)
    raise IOError(f"请求失败 {url}: {last}")


def get_conditional(url: str, timeout: float = 20.0, etag: str = "",
                    last_modified: str = "", retries: int = 2) -> dict:
    """带 If-None-Match / If-Modified-Since 的 GET，用来「安静地」轮询。

    服务端没改就回 304（body 为空，几十字节），不会白下整份清单：
    清单 227 KB，五分钟一查不加条件的话，一天要给玩家多灌 60 MB。

    ⚠️ 第一次请求（还没有 ETag 时）也要走这里，别图省事去调 get_bytes ——
    那样拿不到响应头，缓存里永远没有 ETag，之后就再也发不出条件请求，
    「304」这条路径等于从来没生效过。

    返回 `{"status": 304}`，或
        `{"status": 200, "body": bytes, "etag": str, "last_modified": str}`。
    """
    prefer_ipv4()
    last: Exception | None = None
    for i in range(max(1, retries)):
        try:
            req = _req(url, timeout)
            if etag:
                req.add_header("If-None-Match", etag)
            if last_modified:
                req.add_header("If-Modified-Since", last_modified)
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=_tls_ctx()) as r:
                return {"status": 200, "body": r.read(),
                        "etag": (r.headers.get("ETag") or "").strip(),
                        "last_modified": (r.headers.get("Last-Modified") or "").strip()}
        except urllib.error.HTTPError as e:
            if e.code == 304:                      # 条件命中，内容没变
                return {"status": 304}
            last = e
            if _permanent(e):
                break
            if i < retries - 1:
                _sleep_before_retry(i, e, 1.0)
        except Exception as e:
            last = e
            if _permanent(e):
                break
            if i < retries - 1:
                _sleep_before_retry(i, e, 1.0)
    raise IOError(f"请求失败 {url}: {last}")


def download_to(url: str, dest: str, timeout: float = 120.0, retries: int = 3,
                chunk: int = 1 << 18, on_chunk=None,
                allow_empty: bool = False, insecure: bool = False) -> int:
    """下载到 dest（原子替换）。返回字节数。

    allow_empty: 整合包里确实存在 0 字节文件（如 ModernUI/bootstrap.properties），
    默认把 0 字节当失败会把这类文件误报成下载失败。
    insecure: 跳过证书链校验（只在调用方持有内容哈希时用，见 _tls_ctx）。
    """
    prefer_ipv4()
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    last: Exception | None = None

    for i in range(retries):
        try:
            n = 0
            with open(tmp, "wb") as f:
                def sink(b):
                    nonlocal n
                    f.write(b)
                    n += len(b)
                    if on_chunk:
                        on_chunk(len(b))
                try:
                    total = _http_get_stream(url, timeout, chunk, sink,
                                             insecure=insecure)
                except (http.client.HTTPException, OSError) as e:
                    # 长连接可能已被对端关掉（keep-alive 超时），重试时自然重连
                    if isinstance(e, urllib.error.HTTPError):
                        raise
                    raise
            if n == 0 and not allow_empty:
                raise IOError("收到 0 字节")
            os.replace(tmp, dest)
            return n
        except Exception as e:
            last = e
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            if _permanent(e):
                break
            if i < retries - 1:
                # 429 是服务器明确要求放慢，多给几次机会（默认 3 次太急）
                if isinstance(e, urllib.error.HTTPError) and e.code == 429:
                    retries = max(retries, i + 4)
                _sleep_before_retry(i, e, 1.2)
    raise IOError(f"下载失败 {url}: {last}")


def head_ok(url: str, timeout: float = 20.0) -> tuple[bool, str]:
    """检查 URL 是否可下载（服务器/CDN 支持 HEAD 就用 HEAD，否则退 GET 首字节）。"""
    prefer_ipv4()
    try:
        with urllib.request.urlopen(_req(url, timeout, "HEAD"), timeout=timeout,
                                       context=_tls_ctx()) as r:
            return True, f"HTTP {r.status} · {r.headers.get('Content-Length','?')} 字节"
    except Exception as e_head:
        try:
            req = _req(url, timeout)
            req.add_header("Range", "bytes=0-0")
            with urllib.request.urlopen(req, timeout=timeout,
                                       context=_tls_ctx()) as r:
                return True, f"HTTP {r.status} · 支持 Range"
        except Exception:
            return False, f"{type(e_head).__name__}: {e_head}"


def sha1_of_file(path: str, chunk: int = 1 << 20) -> str:
    """算文件的 sha1（清单里钉内容用的就是 sha1）。"""
    import hashlib
    h = hashlib.sha1()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()
