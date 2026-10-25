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
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit

UA = "cogcraft-launcher/1.0"
_TLS = ssl.create_default_context()
_POOL = threading.local()


def _pooled_conn(scheme: str, host: str, port: int, timeout: float):
    """取本线程到该主机的可复用连接。

    坑：226 个客户端文件里 212 个是小配置文件，每个几 KB。
    如果每个文件都新建 TLS 连接，59ms RTT 下光握手就要
    2×RTT×226 ≈ 27 秒——实测正是这个数。复用连接后只剩 1 个 RTT。
    """
    conns = getattr(_POOL, "conns", None)
    if conns is None:
        conns = _POOL.conns = {}
    key = (scheme, host, port)
    c = conns.get(key)
    if c is None or getattr(c, "sock", None) is None:
        cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
        kw = {"context": _TLS} if scheme == "https" else {}
        c = cls(host, port, timeout=timeout, **kw)
        conns[key] = c
    return c


def _drop_conn(scheme: str, host: str, port: int) -> None:
    conns = getattr(_POOL, "conns", None) or {}
    c = conns.pop((scheme, host, port), None)
    try:
        if c:
            c.close()
    except Exception:
        pass


def _http_get_stream(url: str, timeout: float, chunk: int, sink, redirects: int = 4) -> int:
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

    conn = _pooled_conn(scheme, host, port, timeout)
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
                return _http_get_stream(loc, timeout, chunk, sink, redirects - 1)
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


def _permanent(err: Exception) -> bool:
    """判断错误是否值得重试。

    4xx 里除了 408/429 都是「这个文件本来就不该能下」，
    重试三次只会让界面卡住——实测过：URL 层级写错导致 404 时，
    每个文件要磨掉 21 秒才报错。
    """
    if isinstance(err, urllib.error.HTTPError):
        return err.code not in (408, 429) and 400 <= err.code < 500
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
            with urllib.request.urlopen(_req(url, timeout), timeout=timeout) as r:
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


def download_to(url: str, dest: str, timeout: float = 120.0, retries: int = 3,
                chunk: int = 1 << 18, on_chunk=None,
                allow_empty: bool = False) -> int:
    """下载到 dest（原子替换）。返回字节数。

    allow_empty: 整合包里确实存在 0 字节文件（如 ModernUI/bootstrap.properties），
    默认把 0 字节当失败会把这类文件误报成下载失败。
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
                    total = _http_get_stream(url, timeout, chunk, sink)
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
        with urllib.request.urlopen(_req(url, timeout, "HEAD"), timeout=timeout) as r:
            return True, f"HTTP {r.status} · {r.headers.get('Content-Length','?')} 字节"
    except Exception as e_head:
        try:
            req = _req(url, timeout)
            req.add_header("Range", "bytes=0-0")
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return True, f"HTTP {r.status} · 支持 Range"
        except Exception:
            return False, f"{type(e_head).__name__}: {e_head}"
