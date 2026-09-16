"""抓取闸门：只有云服务器允许对 Adidas / 得物 发请求。

本地（开发机、本地起的服务、CLI 脚本）没有 SCRAPE_ALLOWED，所有抓取入口在发出第一个
请求之前就会拒绝——哪怕只查一个货号。抓取必须走服务器的固定住宅代理和白名单 IP。
服务器的 docker-compose.prod.yml 里设 SCRAPE_ALLOWED=true，本地 .env 写 false。
"""
from __future__ import annotations

import os


class ScrapeNotAllowed(RuntimeError):
    pass


def scrape_allowed() -> bool:
    return os.getenv("SCRAPE_ALLOWED", "").strip().lower() in ("1", "true", "yes", "on")


def assert_scrape_allowed(what: str) -> None:
    if not scrape_allowed():
        raise ScrapeNotAllowed(
            f"拒绝执行「{what}」：抓取只允许在云服务器上运行（SCRAPE_ALLOWED 不是 true）。"
            "本地不要抓取，请通过 ssh 在服务器上触发任务，见 CLAUDE.md。")
