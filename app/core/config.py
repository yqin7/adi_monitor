"""Configuration loader for Adidas Monitor"""
from __future__ import annotations

import os
import re
from typing import Any, Dict

try:
    import yaml
except ImportError:
    print("请先安装依赖: pip install pyyaml")
    exit(1)

from app.core.paths import PROJECT_ROOT


def _resolve_env_vars(value: Any) -> Any:
    """递归解析环境变量引用 (${VAR_NAME})"""
    if isinstance(value, str):
        # 替换 ${VAR_NAME} 格式
        def replace_env(match):
            var_name = match.group(1)
            return os.getenv(var_name, "")
        return re.sub(r'\$\{([^}]+)\}', replace_env, value)
    elif isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [_resolve_env_vars(v) for v in value]
    return value


def load_config() -> Dict[str, Any]:
    """加载配置文件 config.yaml

    Returns:
        配置字典
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass

    config_file = PROJECT_ROOT / "config.yaml"
    if not config_file.exists():
        raise FileNotFoundError(f"Config file not found: {config_file}")

    with open(config_file, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)

    # 解析环境变量
    config = _resolve_env_vars(config)

    return config


def get_mongodb_config(config: Dict[str, Any]) -> tuple:
    """提取 MongoDB 配置

    Returns:
        (uri, database, collection, history_collection)
    """
    mongo = config["mongodb"]
    return (
        mongo["uri"],
        mongo["database"],
        mongo["collection"],
        mongo["history_collection"],
    )


def get_scraper_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """获取爬虫配置"""
    return config["scraper"]


def get_app_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """获取应用配置"""
    return config.get("app", {})


def get_proxy_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """获取代理配置"""
    return config.get("proxy", {})


def build_proxy_url(proxy_cfg: Dict[str, Any]) -> str | None:
    """根据代理配置构造代理 URL，未启用或缺少 host/port 时返回 None"""
    if not proxy_cfg or not proxy_cfg.get("enabled"):
        return None

    host = proxy_cfg.get("host")
    port = proxy_cfg.get("port")
    if not host or not port:
        return None

    protocol = proxy_cfg.get("protocol") or "http"
    username = proxy_cfg.get("username")
    password = proxy_cfg.get("password")

    auth = ""
    if username:
        auth = f"{username}:{password}@" if password else f"{username}@"

    return f"{protocol}://{auth}{host}:{port}"


def get_requests_proxies(config: Dict[str, Any]) -> Dict[str, str] | None:
    """构造 curl_cffi / requests 风格的 proxies 字典，未启用时返回 None"""
    url = build_proxy_url(get_proxy_config(config))
    if not url:
        return None
    return {"http": url, "https": url}


def proxy_enabled(config: Dict[str, Any]) -> bool:
    """代理总开关是否打开（且配置完整可用）"""
    return build_proxy_url(get_proxy_config(config)) is not None


def use_env_proxy(config: Dict[str, Any]) -> bool:
    """代理关闭时是否仍允许读取系统环境变量（HTTP_PROXY 等）。

    默认 False —— 关掉开关就是真的直连，不会被系统代理悄悄接管。
    需要沿用系统代理时在 config.yaml 里设 proxy.use_env_proxy: true。
    """
    return bool(get_proxy_config(config).get("use_env_proxy", False))


def get_playwright_proxy(config: Dict[str, Any]) -> Dict[str, str] | None:
    """构造 Playwright 风格的 proxy 参数，未启用时返回 None"""
    proxy_cfg = get_proxy_config(config)
    if not proxy_cfg or not proxy_cfg.get("enabled"):
        return None

    host = proxy_cfg.get("host")
    port = proxy_cfg.get("port")
    if not host or not port:
        return None

    protocol = proxy_cfg.get("protocol") or "http"
    result = {"server": f"{protocol}://{host}:{port}"}
    if proxy_cfg.get("username"):
        result["username"] = proxy_cfg["username"]
    if proxy_cfg.get("password"):
        result["password"] = proxy_cfg["password"]
    return result


if __name__ == "__main__":
    # 测试配置加载
    import json

    try:
        cfg = load_config()
        print("Config loaded successfully")
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error loading config: {e}")
