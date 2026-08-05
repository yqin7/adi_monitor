"""Configuration loader for Adidas Monitor"""
import os
import re
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError:
    print("请先安装依赖: pip install pyyaml")
    exit(1)


def _resolve_env_vars(value: Any) -> Any:
    """递归解析环境变量引用 (${VAR_NAME})"""
    if isinstance(value, str):
        # 替换 ${VAR_NAME} 格式
        def replace_env(match):
            var_name = match.group(1)
            return os.getenv(var_name, match.group(0))
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
    config_file = Path(__file__).parent / "config.yaml"
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


if __name__ == "__main__":
    # 测试配置加载
    import json

    try:
        cfg = load_config()
        print("Config loaded successfully")
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    except Exception as e:
        print(f"Error loading config: {e}")
