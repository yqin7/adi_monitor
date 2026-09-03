#!/usr/bin/env python3
"""得物开放平台 OAuth2 授权码流程

这套流程针对第三方软件服务商（ISV）—— ISV 需要商家授权后才能代其调用。
自研企业商家在入驻时绑定授权，不需要 token。

流程：

1. 把商家引导到授权页（参数取自开放平台前端源码）：
       https://open.dewu.com/#/authorize?appKey=<app_key>&redirect_uri=<encoded>
           &state=<state>&scope=all&response_type=code
2. 商家同意后浏览器跳回 redirect_uri：
       <redirect_uri>?code=<urlencoded_code>&state=<urlencoded_state>
3. 用 code 换 access_token。
4. access_token 过期前用 refresh_token 续期。

第 3、4 步的接口取自官方 Java SDK（com.dewu.sdk.oauth.Client）：

    POST https://openapi.dewu.com/api/v1/h5/passport/v1/oauth2/token
         {client_id, client_secret, authorization_code}
    POST https://openapi.dewu.com/api/v1/h5/passport/v1/oauth2/refresh_token
         {client_id, client_secret, refresh_token, grant_type}

注意这两个接口**不走网关签名**：client_secret 直接放在 body 里，
没有 app_key/timestamp/sign，与其他业务接口完全不同。
返回 open_id、access_token、refresh_token、access_token_expires_in、
refresh_token_expires_in、scope。
"""

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests

from dewu_client import DewuClient

AUTHORIZE_BASE = "https://open.dewu.com/#/authorize"
# 沙箱没有独立的授权页域名（open-sandbox.dewu.com 不解析），
# 同样走 open.dewu.com，用沙箱 appKey 区分环境。
SANDBOX_AUTHORIZE_BASE = AUTHORIZE_BASE

TOKEN_PATH = "api/v1/h5/passport/v1/oauth2/token"
REFRESH_TOKEN_PATH = "api/v1/h5/passport/v1/oauth2/refresh_token"

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

    def _post(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        """这两个接口不走网关签名，client_secret 直接放在 body 里"""
        url = f"{self.client.gateway}/{path}"
        payload = dict(body)
        payload["client_id"] = self.client.app_key
        payload["client_secret"] = self.client.app_secret
        resp = requests.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=self.client.timeout,
        )
        try:
            return resp.json()
        except ValueError:
            return {"http_status": resp.status_code, "text": resp.text[:2000]}

    def exchange_code(self, code: str) -> Dict[str, Any]:
        """用回调拿到的 code 换 access_token，并写入本地缓存"""
        return self._store_token(
            self._post(TOKEN_PATH, {"authorization_code": code})
        )

    def refresh(self, refresh_token: str) -> Dict[str, Any]:
        """用 refresh_token 续期"""
        return self._store_token(self._post(REFRESH_TOKEN_PATH, {
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }))

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
        """从响应中取出 token 字段并落盘

        SDK 里 AuthTokenRes 的字段：open_id、access_token、refresh_token、
        access_token_expires_in、refresh_token_expires_in、scope。
        """
        data = resp.get("data") if isinstance(resp.get("data"), dict) else resp
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError(
                f"换取 access_token 失败: {json.dumps(resp, ensure_ascii=False)}"
            )

        expires_in = int(data.get("access_token_expires_in") or 0)
        refresh_expires_in = int(data.get("refresh_token_expires_in") or 0)
        now = time.time()
        token = {
            "open_id": data.get("open_id", ""),
            "access_token": access_token,
            "refresh_token": data.get("refresh_token", ""),
            "scope": data.get("scope"),
            "expires_in": expires_in,
            "expires_at": now + expires_in if expires_in else 0,
            "refresh_expires_at": now + refresh_expires_in if refresh_expires_in else 0,
            "obtained_at": now,
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
