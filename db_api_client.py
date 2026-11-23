"""汐汐工具箱 · SQLite 查询服务客户端。

xixitools 主程序内置了这个查询服务（同一个 Node 进程，监听 8801），
直接查 history.sqlite，能拿到**完整数据**（不受 /history 接口 5000 条上限影响）。

    /bililive/api/history（8800）    SQLite 直查（8801）
    ────────────────────────────    ───────────────────
    弹幕  20,000（截断）             完整
    进场  19,990                     完整
    分享  5                          完整
    营收  需逐条累加                  SQL SUM 直出

接口一览（8801）
────────────────
    GET /health                         健康检查 + 接口清单
    GET /rooms                          所有直播间
    GET /dates?room=&days=              有记录的日期
    GET /stats?room=&days=              每日统计
    GET /revenue?room=&days=            每日营收（含合计）
    GET /payers?room=&days=&top=        付费排行（礼物+SC+舰长）
    GET /danmaku-rank?room=&days=&top=  弹幕排行
    GET /guard-list?room=&days=         大航海名单（含实付/贡献值）
    GET /live-log?room=&limit=          开播下播记录
    GET /gifts?room=&days=&top=         礼物统计
    GET /hourly?room=&date=             按小时热度
    GET /user?room=&uid=&days=          单用户画像
    GET /search?room=&date=&q=&limit=   搜索历史消息
    GET /export?room=&date=&format=     导出（json/csv）
"""

from __future__ import annotations

import asyncio
from urllib.parse import quote
from typing import Any

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]

from astrbot.api import logger


class DbApiClient:
    """汐汐 SQLite 查询服务客户端（8801）。"""

    def __init__(self, base_url: str, timeout: float = 15.0) -> None:
        self.base = (base_url or "").rstrip("/")
        self.timeout = timeout

    def _url(self, path: str, **params) -> str:
        p = path if path.startswith("/") else "/" + path
        qs = "&".join(
            f"{k}={quote(str(v), safe='')}"
            for k, v in params.items()
            if v is not None and v != ""
        )
        return f"{self.base}{p}" + (f"?{qs}" if qs else "")

    async def _get(self, path: str, **params) -> dict | None:
        if not self.base or aiohttp is None:
            return None
        url = self._url(path, **params)
        try:
            tmo = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(url) as resp:
                    if resp.status == 503:
                        logger.debug(f"[db_api] {path} -> 503 数据库未就绪")
                        return None
                    if resp.status != 200:
                        logger.debug(f"[db_api] {path} -> HTTP {resp.status}")
                        return None
                    j = await resp.json(content_type=None)
            return j if isinstance(j, dict) and j.get("ok") else None
        except asyncio.TimeoutError:
            logger.debug(f"[db_api] {path} 超时")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[db_api] {path} 失败: {exc}")
            return None

    # ── 基础 ──

    async def health(self) -> bool:
        """服务是否可用。"""
        j = await self._get("/health")
        return bool(j and j.get("ok"))

    async def info(self) -> dict | None:
        """健康信息（含记录总数、接口清单）。"""
        return await self._get("/health")

    async def rooms(self) -> list[dict]:
        """所有被监听的直播间。"""
        j = await self._get("/rooms")
        return (j or {}).get("list") or []

    # ── 统计 ──

    async def get_stats(self, room_id: int, days: int = 5) -> list[dict]:
        """每日统计（完整数据）。

        Returns:
            [{"date","danmaku","gift","sc","guard","watched","enter",
              "share","follow","like","giftYuan","scYuan","guardYuan","revenue"}, ...]
        """
        j = await self._get("/stats", room=int(room_id), days=int(days))
        return (j or {}).get("list") or []

    async def get_revenue(self, room_id: int, days: int = 5) -> dict:
        """每日营收（含 total 合计）。"""
        j = await self._get("/revenue", room=int(room_id), days=int(days))
        if not j:
            return {"list": [], "total": 0}
        return {"list": j.get("list") or [], "total": j.get("total") or 0}

    async def get_payers(self, room_id: int, days: int = 5, top: int = 20) -> list[dict]:
        """付费排行（礼物 + SC + 舰长）。"""
        j = await self._get("/payers", room=int(room_id), days=int(days), top=int(top))
        return (j or {}).get("list") or []

    async def get_danmaku_rank(self, room_id: int, days: int = 5, top: int = 20) -> list[dict]:
        """弹幕排行。"""
        j = await self._get("/danmaku-rank", room=int(room_id), days=int(days), top=int(top))
        return (j or {}).get("list") or []

    async def get_gifts(self, room_id: int, days: int = 7, top: int = 20) -> list[dict]:
        """礼物统计（按礼物名聚合）。"""
        j = await self._get("/gifts", room=int(room_id), days=int(days), top=int(top))
        return (j or {}).get("list") or []

    async def get_hourly(self, room_id: int, date: str) -> list[dict]:
        """按小时的热度分布。"""
        j = await self._get("/hourly", room=int(room_id), date=date)
        return (j or {}).get("list") or []

    async def get_dates(self, room_id: int, days: int = 30) -> list[str]:
        """有记录的日期（倒序）。"""
        j = await self._get("/dates", room=int(room_id), days=int(days))
        return (j or {}).get("dates") or []

    async def get_followers(self, room_id: int, days: int = 5) -> list[dict]:
        """粉丝数趋势（汐汐每 10 分钟抓一次的快照）。

        Returns:
            [{"date": "2026-11-19", "count": 1748125}, ...]  按日期升序
        """
        j = await self._get("/followers", room=int(room_id), days=int(days))
        return (j or {}).get("list") or []

    # ── 大航海 / 开播 ──

    async def get_guard_list(self, room_id: int, days: int = 30) -> list[dict]:
        """大航海名单（含实付金额、贡献值前后对比）。

        Returns:
            [{"uid","uname","level","levelName","price","paidSource",
              "contribBefore","contribAfter","isRenew","expire","lastAt","count"}, ...]
        """
        j = await self._get("/guard-list", room=int(room_id), days=int(days))
        return (j or {}).get("list") or []

    async def get_live_log(self, room_id: int, limit: int = 10) -> list[dict]:
        """开播 / 下播记录。

        Returns:
            [{"start","startText","end","endText","duration","durationText",
              "title","uname","live"}, ...]
        """
        j = await self._get("/live-log", room=int(room_id), limit=int(limit))
        return (j or {}).get("list") or []

    # ── 用户 / 搜索 ──

    async def get_user(self, room_id: int, uid: int, days: int = 30) -> dict | None:
        """单用户画像（消费 + 活跃度）。"""
        j = await self._get("/user", room=int(room_id), uid=int(uid), days=int(days))
        return (j or {}).get("user")

    async def search(self, room_id: int, date: str, q: str, limit: int = 200) -> list[dict]:
        """搜索历史消息。"""
        j = await self._get("/search", room=int(room_id), date=date, q=q, limit=int(limit))
        return (j or {}).get("list") or []
