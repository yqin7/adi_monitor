"""MongoDB 连接管理"""
from __future__ import annotations

import os
import threading

# 进程内共享的连接。
# 此前 from_environment() 每次调用都新建一个 MongoClient：读 .env、TLS 握手、
# SRV 解析、拓扑发现，外加一次 ping 往返 —— 每个接口每次请求都要走一遍，
# 而且 /raw 拿完从不 close，泄漏的 client 还各自带着心跳线程继续轮询 Atlas。
# MongoClient 本身线程安全且自带连接池，正确用法就是全进程一个。
_shared: "MongoConnection | None" = None
_lock = threading.Lock()


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
        """取进程内共享连接，没有就建一个。"""
        global _shared
        if _shared is not None:
            return _shared
        with _lock:
            if _shared is not None:
                return _shared
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
            _shared = cls(uri, database=os.getenv("MONGODB_DATABASE", "adidas_monitor"))
            return _shared

    def close(self) -> None:
        """共享连接不真关。

        调用方遍布各处（大多写在 finally 里），真关掉会让后续请求全部失败；
        而进程退出时操作系统会回收，本来也不需要显式关闭。
        """
        if self is _shared:
            return
        self.client.close()

    @classmethod
    def shutdown(cls) -> None:
        """进程退出时收尾用。"""
        global _shared
        if _shared is not None:
            _shared.client.close()
            _shared = None
