#!/usr/bin/env python3
"""内网直连兜底（"小后门"）的回归测试。

需求：公网域名连不上时，客户端自动改走 192.168.3.40 再试**一次**。
守住的边界：
  · 只有「压根没连上」才换源 —— 4xx/5xx 是认证服正常回话，绝不能改源；
  · 只试一次，不叠加重试；
  · 站点地址（清单/元数据/客户端文件）→ 内网 25585，认证服 → 内网 8081，
    且 `/auth` 反代前缀要去掉，否则内网会 404。

测试不连任何外网、不动生产服务：起一个 127.0.0.1 临时 HTTP 服务当「内网服务器」，
把「公网地址」指向一个必定拒绝连接的端口，验证程序真的改道了。

跑法（用生产解释器，别用 Hermes 的 venv）：
    env -u PYTHONPATH -u VIRTUAL_ENV -u PYTHONHOME python3 ci/test_lan_fallback.py
"""
import json
import os
import socket
import ssl
import sys
import tempfile
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "engine"))
import net    # noqa: E402  # pyright: ignore[reportMissingImports]
import auth   # noqa: E402  # pyright: ignore[reportMissingImports]

FAIL = []


def check(name, cond, extra=None):
    show = "" if (cond or extra is None) else ("  → " + str(extra))
    print(("  ✓ " if cond else "  ✗ ") + name + show)
    if not cond:
        FAIL.append(name)


# ── 临时「内网服务器」 ─────────────────────────────
HITS = []


class H(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        HITS.append(("GET", self.path))
        if "/client/manifest.json" in self.path:
            self._send(200, json.dumps({"client_id": "cogcraft", "schema": 1,
                                        "_from": "lan"}).encode())
        elif "/client/tier" in self.path:
            self._send(200, json.dumps({"tier": "beta"}).encode())
        elif "/forbidden" in self.path:
            self._send(403, b'{"error":"ForbiddenOperationException"}')
        else:
            self._send(404, b"{}")

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n)
        HITS.append(("POST", self.path))
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}
        if "/authserver/authenticate" in self.path:
            # 密码写错 → 认证服正常回话 403，用来验证「绝不偷偷改道」
            if payload.get("password") == "wrongpass":
                self._send(403, json.dumps({"error": "ForbiddenOperationException",
                                            "errorMessage": "账号或密码不对。"}).encode())
                return
            self._send(200, json.dumps({
                "accessToken": "tok-lan", "clientToken": "ctok",
                "selectedProfile": {"name": "cryolite", "id": "0" * 32},
            }).encode())
        elif "/authserver/validate" in self.path:
            self._send(204)
        else:
            self._send(404, b"{}")


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
PORT = srv.server_address[1]
threading.Thread(target=srv.serve_forever, daemon=True).start()

# 找一个必定没人监听的端口当「公网地址」
_s = socket.socket()
_s.bind(("127.0.0.1", 0))
DEAD = _s.getsockname()[1]
_s.close()

print("临时内网服务器 127.0.0.1:%d ；公网(不通)地址 127.0.0.1:%d" % (PORT, DEAD))

# ── ① 地址映射 ────────────────────────────────────
print("\n=== ① 地址映射（公网 → 内网）===")
net.LAN_HOST = "192.168.3.40"
net.LAN_SITE_PORT = 25585
net.LAN_AUTH_PORT = 8899
check("站点：清单地址改成内网 25585",
      net.lan_site_url("https://www.cogcraft.top/client/manifest.json")
      == "http://192.168.3.40:25585/client/manifest.json",
      net.lan_site_url("https://www.cogcraft.top/client/manifest.json"))
check("站点：别的域名不管",
      net.lan_site_url("https://ghfast.top/x/cogcraft-mac-arm64.zip") is None)
check("站点：带 query 也保留",
      net.lan_site_url("https://cogcraft.top/client/manifest.json?v=2")
      == "http://192.168.3.40:25585/client/manifest.json?v=2")
check("认证：/auth 前缀必须保留（实测去掉就 404）",
      net.lan_auth_url("https://cogcraft.top/auth/authserver/authenticate")
      == "http://192.168.3.40:8899/auth/authserver/authenticate",
      net.lan_auth_url("https://cogcraft.top/auth/authserver/authenticate"))
check("认证：兜底的 127.0.0.1:8081 也要改道并补上 /auth",
      net.lan_auth_url("http://127.0.0.1:8081/authserver/validate")
      == "http://192.168.3.40:8899/auth/authserver/validate",
      net.lan_auth_url("http://127.0.0.1:8081/authserver/validate"))
check("认证：非认证接口不乱改",
      net.lan_auth_url("https://www.cogcraft.top/client/manifest.json") is None)
check("内网超时给了个短值", net.LAN_TIMEOUT <= 5.0, net.LAN_TIMEOUT)

# ── ② 错误分类 ────────────────────────────────────
print("\n=== ② 只有「没连上」才换源 ===")
check("ConnectionRefusedError → 换源",
      net._conn_down(ConnectionRefusedError(111, "Connection refused")))
check("URLError 包着的 refused → 换源",
      net._conn_down(urllib.error.URLError(ConnectionRefusedError(111, "refused"))))
check("macOS 的 errno 61 → 换源",
      net._conn_down(OSError(61, "Connection refused")))
check("DNS 失败 → 换源", net._conn_down(socket.gaierror(-2, "Name or service not known")))
check("HTTP 404 → 不换源（对面明明通着）",
      not net._conn_down(urllib.error.HTTPError("u", 404, "Not Found", None, None)))
check("连接被重置 → 不换源（该重试，不该改道）",
      not net._conn_down(ConnectionResetError(104, "reset")))
check("证书错误 → 不换源",
      not net._conn_down(ssl.SSLCertVerificationError(1, "verify failed")))


def _wrapped():
    try:
        raise ConnectionRefusedError(111, "Connection refused")
    except ConnectionRefusedError as inner:
        raise IOError("请求失败 http://x: refused") from inner


try:
    _wrapped()
except IOError as w:
    check("被 IOError 包一层（带 from）后仍能识别 —— net.py 就是这么包的",
          net._conn_down(w), w.__cause__)

# ── ③ 端到端：站点（清单/下载）改道 ────────────────
print("\n=== ③ 清单/下载：公网不通 → 内网拿到内容 ===")
net.LAN_HOST = "127.0.0.1"
net.LAN_SITE_PORT = PORT
net.LAN_AUTH_PORT = PORT
net._SITE_HOSTS = {"www.cogcraft.top", "cogcraft.top", "127.0.0.1"}

HITS.clear()
got = net.get_conditional("http://127.0.0.1:%d/client/manifest.json" % DEAD, timeout=3)
body = json.loads((got.get("body") or b"{}").decode())
check("清单从内网取到了", got.get("status") == 200 and body.get("_from") == "lan", got)
check("内网服务器确实收到了这次请求",
      ("GET", "/client/manifest.json") in HITS, HITS)

dest = os.path.join(tempfile.mkdtemp(), "manifest.json")
n = net.download_to("http://127.0.0.1:%d/client/manifest.json" % DEAD, dest, timeout=3)
check("download_to 也会改道", n > 0 and open(dest, "rb").read().startswith(b"{"), n)

ok, note = net.head_ok("http://127.0.0.1:%d/client/manifest.json" % DEAD, timeout=3)
check("head_ok 也会改道", ok and "内网" in note, note)

# ── ④ 端到端：认证服改道 ──────────────────────────
print("\n=== ④ 登录：公网认证服不通 → 内网认证服 ===")
# ③ 段为了拿 loopback 当站点，临时把 127.0.0.1 塞进了 _SITE_HOSTS，这里复原，
# 否则 loopback 会先命中站点分支、被当成「不是认证接口」放掉。
net._SITE_HOSTS = {"www.cogcraft.top", "cogcraft.top"}
HITS.clear()
status, resp = auth._post("http://127.0.0.1:%d/authserver/authenticate" % DEAD,
                          {"username": "cryolite", "password": "x"})
check("登录请求改道并拿到 token",
      status == 200 and isinstance(resp, dict) and resp.get("accessToken") == "tok-lan",
      (status, resp))
check("内网认证服收到了 POST（带 /auth 前缀）",
      ("POST", "/auth/authserver/authenticate") in HITS, HITS)

check("公网 /auth 前缀地址改道后前缀仍在",
      auth._lan_auth_url("https://cogcraft.top/auth/authserver/authenticate")
      == "http://127.0.0.1:%d/auth/authserver/authenticate" % PORT,
      auth._lan_auth_url("https://cogcraft.top/auth/authserver/authenticate"))

# ── ⑤ 反例：认证服回 403 时绝不能偷偷换源 ──────────
print("\n=== ⑤ 反例：403 不许改道 ===")
HITS.clear()
try:
    auth.login("http://127.0.0.1:%d" % PORT, "cryolite", "wrongpass")
    check("密码错应当抛 AuthError", False)
except auth.AuthError as e:
    check("密码错抛 AuthError 且带 403", e.status == 403, e)
check("只打了一次，没有偷偷换源重试", len(HITS) == 1, HITS)

srv.shutdown()

print()
if FAIL:
    print("✗ 失败 %d 项：%s" % (len(FAIL), "；".join(FAIL)))
    sys.exit(1)
print("✓ 全部通过")
