#!/usr/bin/env python3
"""得物开放平台 OAuth2 授权码流程

流程（授权页参数与回调格式已从开放平台前端源码确认）：

1. 把商家引导到授权页：
       https://open.dewu.com/#/authorize?appKey=<app_key>&redirect_uri=<encoded>
           &state=<state>&scope=all&response_type=code
2. 商家同意后浏览器跳回 redirect_uri：
       <redirect_uri>?code=<urlencoded_code>&state=<urlencoded_state>
3. 用 code 到网关换 access_token（签名同普通接口）。
4. access_token 过期前用 refresh_token 续期。

第 3、4 步的网关路径与出入参字段名以官方文档为准，可用环境变量覆盖：
    DEWU_TOKEN_PATH          默认 dop/api/v1/oauth/token
    DEWU_REFRESH_TOKEN_PATH  默认 dop/api/v1/oauth/refresh_token
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

from dewu_client import DewuClient

AUTHORIZE_BASE = "https://open.dewu.com/#/authorize"
SANDBOX_AUTHORIZE_BASE = "https://open-sandbox.dewu.com/#/authorize"

TOKEN_PATH = os.getenv("DEWU_TOKEN_PATH", "dop/api/v1/oauth/token")
REFRESH_TOKEN_PATH = os.getenv("DEWU_REFRESH_TOKEN_PATH", "dop/api/v1/oauth/refresh_token")

# access_token 剩余有效期低于该秒数时提前刷新
REFRESH_MARGIN_SECONDS = 300


def build_authorize_url(app_key: str, redirect_uri: str, state: str = "",
                        scope: str = "all", sandbox: bool = False) -> str:
    """拼出让商家点击的授权页地址"""
    base = SANDBOX_AUTHORIZE_BASE if sandbox else AUTHORIZE_BASE
    url = (
        f"{base}?appKey={quote(app_key, safe='')}"
        f"&redirect_uri={quote(redirect_uri, safe='')}"
        f"&scope={quote(scope, safe='')}"
        f"&response_type=code"
    )
    if state:
        url += f"&state={quote(state, safe='')}"
    return url


class TokenStore:
    """access_token / refresh_token 本地持久化

    生产环境建议换成 MongoDB 或密钥管理服务，这里用本地文件方便单机部署。
    """

    def __init__(self, path: str = None):
        self.path = Path(path or os.getenv("DEWU_TOKEN_FILE", ".dewu_token.json"))

    def load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}

    def save(self, data: Dict[str, Any]) -> None:
        self.path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.chmod(self.path, 0o600)


class DewuOAuth:
    """授权码换取 / 刷新 / 缓存 access_token"""

    def __init__(self, client: DewuClient, redirect_uri: str = None,
                 store: TokenStore = None):
        self.client = client
        self.redirect_uri = redirect_uri or os.getenv("DEWU_REDIRECT_URI", "")
        self.store = store or TokenStore()

    def authorize_url(self, state: str = "") -> str:
        return build_authorize_url(
            self.client.app_key,
            self.redirect_uri,
            state=state,
            sandbox=self.client.gateway.endswith("-sandbox.dewu.com"),
        )

    def exchange_code(self, code: str) -> Dict[str, Any]:
        """用回调拿到的 code 换 access_token，并写入本地缓存"""
        resp = self.client.call(TOKEN_PATH, {
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        })
        return self._store_token(resp)

    def refresh(self, refresh_token: str) -> Dict[str, Any]:
        """用 refresh_token 续期"""
        resp = self.client.call(REFRESH_TOKEN_PATH, {
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        })
        return self._store_token(resp)

    def get_access_token(self) -> Optional[str]:
        """取一个可用的 access_token，快过期时自动刷新

        Returns:
            access_token，尚未授权时返回 None
        """
        token = self.store.load()
        if not token.get("access_token"):
            return None

        expires_at = token.get("expires_at", 0)
        if expires_at and expires_at - time.time() < REFRESH_MARGIN_SECONDS:
            if token.get("refresh_token"):
                token = self.refresh(token["refresh_token"])
            else:
                return None

        return token.get("access_token")

    def _store_token(self, resp: Dict[str, Any]) -> Dict[str, Any]:
        """从网关响应中取出 token 字段并落盘"""
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        access_token = data.get("access_token") or data.get("accessToken")
        if not access_token:
            raise RuntimeError(f"换取 access_token 失败: {json.dumps(resp, ensure_ascii=False)}")

        expires_in = int(data.get("expires_in") or data.get("expiresIn") or 0)
        token = {
            "access_token": access_token,
            "refresh_token": data.get("refresh_token") or data.get("refreshToken", ""),
            "expires_in": expires_in,
            "expires_at": time.time() + expires_in if expires_in else 0,
            "obtained_at": time.time(),
        }
        self.store.save(token)
        return token


def _cli():
    import argparse

    parser = argparse.ArgumentParser(description="得物 OAuth2 授权工具")
    parser.add_argument("action", choices=["url", "exchange", "refresh", "show"],
                        help="url=打印授权链接 exchange=用 code 换 token "
                             "refresh=刷新 show=查看本地 token")
    parser.add_argument("--code", help="授权回调返回的 code（exchange 时必填）")
    parser.add_argument("--state", default="", help="防 CSRF 的 state")
    parser.add_argument("--sandbox", action="store_true", help="使用沙箱环境")
    args = parser.parse_args()

    prefix = "DEWU_SANDBOX_" if args.sandbox else "DEWU_"
    client = DewuClient(
        os.getenv(f"{prefix}APP_KEY", ""),
        os.getenv(f"{prefix}APP_SECRET", ""),
        sandbox=args.sandbox,
    )
    oauth = DewuOAuth(client)

    if args.action == "url":
        print(oauth.authorize_url(state=args.state))
    elif args.action == "exchange":
        if not args.code:
            parser.error("exchange 需要 --code")
        print(json.dumps(oauth.exchange_code(args.code), ensure_ascii=False, indent=2))
    elif args.action == "refresh":
        token = oauth.store.load()
        if not token.get("refresh_token"):
            parser.error("本地没有 refresh_token，请先完成一次授权")
        print(json.dumps(oauth.refresh(token["refresh_token"]), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(oauth.store.load(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
