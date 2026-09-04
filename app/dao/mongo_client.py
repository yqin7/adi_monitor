"""MongoDB 连接管理"""
from __future__ import annotations

import os


class MongoConnection:
    """封装 MongoClient 连接，供各 DAO 共享同一个数据库句柄"""

    def __init__(self, uri: str, database: str = "adidas_monitor"):
        try:
            from pymongo import MongoClient
        except ImportError as exc:
            raise RuntimeError("MongoDB support requires pymongo: pip install pymongo") from exc

        self.client = MongoClient(uri, serverSelectionTimeoutMS=10000)
        self.db = self.client[database]
        self.client.admin.command("ping")

    @classmethod
    def from_environment(cls, required: bool = False) -> "MongoConnection | None":
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except ImportError:
            pass

        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            if required:
                raise RuntimeError("MONGODB_URI is not configured")
            return None
        return cls(uri, database=os.getenv("MONGODB_DATABASE", "adidas_monitor"))

    def close(self) -> None:
        self.client.close()
