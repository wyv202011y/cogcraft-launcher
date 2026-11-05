#!/usr/bin/env python3
"""net.py 重试判定的回归测试。

守着一个真实事故：nginx 的 keepalive_timeout 到点会悄悄关掉空闲连接，
客户端毫不知情地复用那条死连接，抛 ssl.SSLEOFError（_ssl.c:2427）。
旧逻辑把所有 ssl.SSLError 一律当「永久错误」→ 每装一次就有 3~4 个
文件直接判死、一次都不重试。这里把这套判定钉死，别再退回去。

跑法（用生产解释器，别用 Hermes 的 venv）：
    env -u PYTHONPATH -u VIRTUAL_ENV -u PYTHONHOME python3 ci/test_net_retry.py
"""
import http.client
import os
import socket
import ssl
import sys
import time
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from engine import net  # noqa: E402

FAIL = []


def check(name, cond, extra=None):
    show = "" if (cond or extra is None) else ("  → " + str(extra))
    print(("  ✓ " if cond else "  ✗ ") + name + show)
    if not cond:
        FAIL.append(name)


EOF = ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:2427)")

print("=== ① 偶发断连必须重试（这次事故的根因）===")
check("裸 SSLEOFError → 可重试", net._permanent(EOF) is False)
check("URLError 包着的 SSLEOFError → 可重试",
      net._permanent(urllib.error.URLError(EOF)) is False)
for exc in (ConnectionResetError(), BrokenPipeError(), ConnectionAbortedError(),
            TimeoutError(), socket.timeout(),
            http.client.RemoteDisconnected(),
            http.client.IncompleteRead(b"abc"),
            http.client.BadStatusLine("garbage")):
    check(f"{type(exc).__name__} → 可重试", net._permanent(exc) is False, net._permanent(exc))

print("\n=== ② 真·永久错误仍然不重试（别把老优化改坏）===")
check("证书链验证失败 → 永久",
      net._permanent(ssl.SSLCertVerificationError(1, "verify failed")) is True)
check("URLError 包着的证书错误 → 永久",
      net._permanent(urllib.error.URLError(ssl.SSLCertVerificationError(1, "x"))) is True)
check("404 → 永久", net._permanent(urllib.error.HTTPError("u", 404, "NF", {}, None)) is True)
check("400 → 永久", net._permanent(urllib.error.HTTPError("u", 400, "BR", {}, None)) is True)
check("408 → 可重试", net._permanent(urllib.error.HTTPError("u", 408, "TO", {}, None)) is False)
check("429 → 可重试", net._permanent(urllib.error.HTTPError("u", 429, "TL", {}, None)) is False)
check("503 → 可重试", net._permanent(urllib.error.HTTPError("u", 503, "UA", {}, None)) is False)

print("\n=== ③ HTTPError.reason 是字符串，不能干扰判定 ===")
# 这是修这个 bug 时差点引入的新坑：HTTPError 也是 URLError，.reason 是 str
h404 = urllib.error.HTTPError("u", 404, "Not Found", {}, None)
check("HTTPError.reason 确实是字符串", isinstance(h404.reason, str))
check("404 仍被判永久（没被 reason 字符串带偏）", net._permanent(h404) is True)

print("\n=== ④ 端到端：第一次断连，重试后必须拿到完整内容 ===")
_tmpdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_net_retry_tmp")
os.makedirs(_tmpdir, exist_ok=True)
dest = os.path.join(_tmpdir, "ponder.jar")
for f in (dest, dest + ".part"):
    if os.path.exists(f):
        os.remove(f)

calls = []
_orig_stream = net._http_get_stream
_orig_sleep = net._sleep_before_retry


def fake_stream(url, timeout, chunk, sink, **kw):
    calls.append(url)
    if len(calls) == 1:
        raise EOF                      # 模拟复用了一条被 nginx 关掉的连接
    sink(b"x" * 820109)
    return 820109


net._http_get_stream = fake_stream
net._sleep_before_retry = lambda *a, **k: None
try:
    n = net.download_to("https://www.cogcraft.top/client/files/full/mods/ponder.jar",
                        dest, retries=3)
    check("断连后重试成功", n == 820109, n)
    check("确实重试了 2 次", len(calls) == 2, calls)
    check("文件落盘且大小正确",
          os.path.exists(dest) and os.path.getsize(dest) == 820109,
          os.path.getsize(dest) if os.path.exists(dest) else "缺失")
    check("没有残留 .part", not os.path.exists(dest + ".part"))
finally:
    net._http_get_stream = _orig_stream
    net._sleep_before_retry = _orig_sleep

print("\n=== ⑤ 永久错误不该重试（浪费时间的路径）===")
calls2 = []


def bad_stream(url, timeout, chunk, sink, **kw):
    calls2.append(url)
    raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)


net._http_get_stream = bad_stream
net._sleep_before_retry = lambda *a, **k: None
try:
    try:
        net.download_to("https://www.cogcraft.top/client/files/nope.jar",
                        os.path.join(_tmpdir, "nope.jar"), retries=3)
        check("404 应该抛错", False, "居然没抛")
    except IOError:
        check("404 抛 IOError", True)
    check("404 只试了 1 次就放弃", len(calls2) == 1, len(calls2))
finally:
    net._http_get_stream = _orig_stream
    net._sleep_before_retry = _orig_sleep

print("\n=== ⑥ 空闲超过 45 秒要主动换连接（预防那一半）===")
key = ("https", "www.cogcraft.top", 443, False)


class FakeConn:
    sock = object()          # 假装是活的
    closed = False

    def close(self):
        self.closed = True


# threading.local 的属性要自己初始化：走到这里时没真发过请求
net._POOL.conns = {}
net._POOL.used = {}
fake = FakeConn()
net._POOL.conns[key] = fake
net._POOL.used[key] = time.time() - 100      # 空闲 100 秒 > 45
c = net._pooled_conn("https", "www.cogcraft.top", 443, 10.0)
check("空闲 100s → 换掉旧连接", c is not fake, type(c).__name__)
check("旧连接被 close 了", fake.closed is True)

fake2 = FakeConn()
fake2.sock = object()
net._POOL.conns[key] = fake2
net._POOL.used[key] = time.time()            # 刚用过
c2 = net._pooled_conn("https", "www.cogcraft.top", 443, 10.0)
check("刚用过 → 继续复用", c2 is fake2, type(c2).__name__)
check("复用时不会 close 它", fake2.closed is False)

# 清理
for f in (dest, dest + ".part"):
    if os.path.exists(f):
        os.remove(f)
try:
    os.rmdir(_tmpdir)
except OSError:
    pass
net._POOL.conns.clear()
net._POOL.used.clear()

print()
if FAIL:
    print(f"✗ {len(FAIL)} 项失败：" + "、".join(FAIL))
    sys.exit(1)
print(f"✓ 全部通过（{len(FAIL)} 失败）")
