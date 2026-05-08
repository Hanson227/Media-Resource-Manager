import httpx


class ApiClient:
    """HTTP 客户端封装，与桌面端 API 通信。"""

    def __init__(self, server_url: str):
        self.base = server_url.rstrip("/")
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))

    async def close(self):
        await self._client.aclose()

    async def health(self) -> bool:
        """GET /api/health 连接检查。"""
        try:
            r = await self._client.get(f"{self.base}/api/health", timeout=3.0)
            return r.status_code == 200
        except Exception:
            return False

    async def get_units(self) -> list[dict]:
        """GET /api/units 获取资源单元列表。"""
        r = await self._client.get(f"{self.base}/api/units", timeout=5)
        r.raise_for_status()
        return r.json()["units"]

    async def get_unit_files(self, unit_id: int) -> list[dict]:
        """GET /api/units/{unit_id}/files 获取单元内文件列表。"""
        r = await self._client.get(f"{self.base}/api/units/{unit_id}/files", timeout=10)
        r.raise_for_status()
        return r.json()

    async def get_file_detail(self, file_id: int) -> dict:
        """GET /api/files/{file_id} 获取文件详情。"""
        r = await self._client.get(f"{self.base}/api/files/{file_id}", timeout=10)
        r.raise_for_status()
        return r.json()

    async def get_unread_events(self) -> dict:
        """GET /api/events/unread 获取未读事件简报。"""
        r = await self._client.get(f"{self.base}/api/events/unread", timeout=5)
        r.raise_for_status()
        return r.json()

    async def run_dedup(self, unit_ids: list[int], threshold: float = 0.8) -> dict:
        """POST /api/dedup/run 触发查重任务。"""
        r = await self._client.post(
            f"{self.base}/api/dedup/run",
            json={"unit_ids": unit_ids, "threshold": threshold},
            timeout=300,
        )
        r.raise_for_status()
        return r.json()

    def thumbnail_url(self, file_id: int) -> str:
        """构造缩略图 URL。"""
        return f"{self.base}/api/files/{file_id}/thumbnail"

    def stream_url(self, file_id: int) -> str:
        """构造流媒体 URL。"""
        return f"{self.base}/api/files/{file_id}/stream"
