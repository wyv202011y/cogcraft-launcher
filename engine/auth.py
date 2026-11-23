"""认证客户端 —— 注册 / 登录 / 校验 / 刷新。

设计原则（已和服主敲定）：
  · 只有一套账号：所有玩家（含正版玩家）都在我们自己的认证服注册，
    UUID 由服务端按离线算法 MD5("OfflinePlayer:"+小写名) 生成，与老存档一致。
  · 启动器只保存 token，绝不保存密码。切换账号 = 清掉 token 回登录页重输。
  · 认证服地址从清单下发，改地址不用发新版启动器。

服务端契约（见 auth-server 项目）：
  POST /authserver/register   {username, password} -> {ok, name, uuid}
  POST /authserver/authenticate {username, password} -> Yggdrasil 标准响应
  POST /authserver/validate   {accessToken, clientToken} -> 204 / 403
  POST /authserver/refresh    {accessToken, clientToken} -> 同 authenticate
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

# 没有下发清单时的兜底地址（本地开发 / 内网调试用）
DEFAULT_API = "http://127.0.0.1:8081"

TIMEOUT = 15


class AuthError(Exception):
    """带用户可读文案的认证失败。code 是认证服返回的 error 字段。"""

    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        self.message = message
        self.code = code
        self.status = status
        super().__init__(message)


def api_base(manifest: dict | None) -> str:
    """认证服根地址。清单里给 auth.api，没给就回落到默认值。"""
    if manifest:
        auth = manifest.get("auth") or {}
        url = (auth.get("api") or "").strip()
        if url:
            return url.rstrip("/")
    return DEFAULT_API.rstrip("/")


def _post(url: str, payload: dict | None, token: str | None = None) -> tuple[int, dict | str]:
    """统一 POST。返回 (status, 解析后的 body)。"""
    data = json.dumps(payload).encode("utf-8") if payload is not None else b""
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else "")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, (json.loads(raw) if raw.strip() else "")
        except json.JSONDecodeError:
            return e.code, raw
    except urllib.error.URLError as e:
        raise AuthError(f"连不上服务器（{e.reason}）。检查一下网络，或者稍后再试。") from e
    except TimeoutError as e:
        raise AuthError("服务器没响应，可能网络不太行，稍后再试。") from e


def _raise_for(status: int, body) -> None:
    """把认证服的标准错误体转成可读文案。"""
    if isinstance(body, dict):
        msg = body.get("errorMessage") or body.get("detail") or ""
        code = body.get("error")
    else:
        msg, code = str(body or ""), None

    if status in (400, 409):
        raise AuthError(msg or "请求无效。", code, status)
    if status == 403:
        raise AuthError(msg or "账号或密码不对。", code, status)
    raise AuthError(msg or f"服务器返回 {status}。", code, status)


# ── 对外接口 ────────────────────────────────────

def register(base: str, username: str, password: str) -> dict:
    status, body = _post(f"{base}/authserver/register",
                         {"username": username, "password": password})
    if status != 200:
        _raise_for(status, body)
    return body if isinstance(body, dict) else {}


def login(base: str, username: str, password: str) -> dict:
    """登录，返回认证服的 Yggdrasil 响应（含 accessToken / selectedProfile）。"""
    status, body = _post(f"{base}/authserver/authenticate", {
        "agent": {"name": "Minecraft", "version": 1},
        "username": username,
        "password": password,
        "requestUser": True,
    })
    if status != 200:
        _raise_for(status, body)
    if not isinstance(body, dict) or not body.get("accessToken"):
        raise AuthError("服务器没有返回登录凭证。")
    return body


def validate(base: str, access_token: str, client_token: str = "") -> bool:
    status, _ = _post(f"{base}/authserver/validate",
                      {"accessToken": access_token, "clientToken": client_token})
    return status == 204


def refresh(base: str, access_token: str, client_token: str = "") -> dict | None:
    """刷新 token。旧 token 会在服务端立即失效，所以失败就是真失败。"""
    status, body = _post(f"{base}/authserver/refresh",
                         {"accessToken": access_token, "clientToken": client_token})
    if status != 200 or not isinstance(body, dict):
        return None
    return body


# ── 本地凭证存储（只存 token，不存密码）──────────

def token_path(appdata_dir: str) -> str:
    return os.path.join(appdata_dir, "auth.json")


def load(appdata_dir: str) -> dict | None:
    p = token_path(appdata_dir)
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        if not d.get("access_token"):
            return None
        return d
    except Exception:
        return None


def save(appdata_dir: str, base: str, resp: dict) -> dict:
    """把登录响应落盘。expiresAt 是毫秒时间戳（Yggdrasil 规范）。"""
    prof = resp.get("selectedProfile") or {}
    rec = {
        "api": base,
        "access_token": resp.get("accessToken", ""),
        "client_token": resp.get("clientToken", ""),
        "expires_at": int(resp.get("expiresAt") or 0),
        "player_name": prof.get("name", ""),
        "uuid": prof.get("id", ""),
        "saved_at": int(time.time()),
    }
    p = token_path(appdata_dir)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(rec, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(tmp, 0o600)          # 只有本人可读
    except OSError:
        pass                          # Windows 上不生效，忽略
    os.replace(tmp, p)
    return rec


def clear(appdata_dir: str) -> None:
    p = token_path(appdata_dir)
    try:
        if os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def ensure_valid(appdata_dir: str, manifest: dict | None) -> dict | None:
    """启动游戏前调用：凭证还有效就原样返回，过期就尝试刷新，失败返回 None。

    返回的 dict 里一定有 player_name / uuid / access_token 三个键，
    直接喂给 engine/launch.py 的参数模板。
    """
    rec = load(appdata_dir)
    if not rec:
        return None

    base = rec.get("api") or api_base(manifest)
    token = rec.get("access_token", "")
    ctok = rec.get("client_token", "")

    if validate(base, token, ctok):
        return rec

    fresh = refresh(base, token, ctok)
    if not fresh:
        clear(appdata_dir)            # 刷新不了就清掉，让玩家重新登录
        return None

    return save(appdata_dir, base, fresh)
