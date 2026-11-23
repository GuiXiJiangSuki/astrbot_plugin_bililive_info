"""汐汐工具箱 API 客户端。

数据来源
────────
汐汐工具箱（xixi-toolbox）是个本地直播助手，同时提供 HTTP API。
默认端口 8800，所有接口挂在 `/bililive/api/` 前缀下。

关键接口
────────
    GET /bililive/api/stats          实时统计 + 主播信息 + 房间状态
    GET /bililive/api/messages       消息流（弹幕/进场/礼物/SC/舰长）
    GET /bililive/api/superchats     SC 完整列表 + 分档统计
    GET /bililive/api/guards         舰长列表
    GET /bililive/api/users          观众列表（含贡献度）
    GET /bililive/api/rooms          已配置的房间列表
    GET /bililive/api/blindbox       盲盒盈亏
    GET /bililive/api/pk             PK 状态
    GET /bililive/api/gift/summary   礼物汇总
    GET /bililive/api/contrib-rank   贡献榜

重要限制
────────
汐汐只对**已添加到它列表里的房间**维护实时数据（WebSocket 长连累计）。
查询其他房间时它没有弹幕/礼物等历史，只能拿到 B 站官方的基础信息。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore[assignment]

from astrbot.api import logger

# 图表显示最近多少天（柱形图数量）
DAILY_DAYS = 7


@dataclass
class LiveSnapshot:
    """一个直播间的完整数据快照。"""

    room_id: int = 0
    # 主播
    uid: int = 0
    uname: str = ""
    face: str = ""
    title: str = ""
    live_status: int = 0        # 0未开播 1直播中 2轮播
    online: int = 0             # 人气值
    # 统计
    danmaku: int = 0
    gift: int = 0
    sc: int = 0
    guard: int = 0
    enter: int = 0
    like: int = 0
    # 人数
    users: int = 0              # 独立观众数
    guards_count: int = 0       # 舰长数
    messages: int = 0
    # SC 详情
    sc_total_yuan: float = 0.0
    sc_avg_yuan: float = 0.0
    sc_tiers: dict = field(default_factory=dict)
    sc_list: list = field(default_factory=list)
    # 舰长详情
    guard_list: list = field(default_factory=list)
    # 观众排行
    user_list: list = field(default_factory=list)
    # 按付费金额排行的观众
    payer_list: list = field(default_factory=list)
    # 弹幕采样（用于词云）
    danmaku_samples: list[str] = field(default_factory=list)
    # 房间状态
    state: str = ""
    is_tracked: bool = False    # 是否在汐汐的实时跟踪列表里

    # ── 场次信息 ──
    live_start_ts: int = 0      # 本场开播时间戳（毫秒）
    live_start_text: str = ""   # "2026-11-18 19:54"
    live_end_ts: int = 0
    live_end_text: str = ""
    session_duration: str = ""  # "直播中" 或 "3小时25分"
    live_sessions: list = field(default_factory=list)  # 最近几场（用于多场对比）
    follower_daily: list = field(default_factory=list)  # 粉丝数每日快照
    gift_list: list = field(default_factory=list)       # 礼物统计（按礼物名聚合，金额降序）
    hourly_list: list = field(default_factory=list)     # 按小时的真实分布（弹幕/礼物/进场）
    message_total: int = 0                              # 真实消息总数（不受 5000 上限）
    payer_total: int = 0                                # 真实付费人数
    blind_daily: list = field(default_factory=list)     # 盲盒每日盈亏
    is_live_now: bool = False

    # ── 营收 ──
    revenue_today: float = 0.0  # 本次直播期营收
    revenue_all: float = 0.0    # 累计（上次直播期）
    revenue_range_label: str = ""  # "本次直播" / "上次直播"
    gift_count: int = 0

    # ── 主播粉丝数（B 站官方 Master/info）──
    followers: int = 0

    # ── 时间序列（含时间戳，用于时长/流量曲线）──
    timeline: list = field(default_factory=list)

    # ── 盲盒 ──
    blindbox_total: int = 0
    blindbox_cost: float = 0.0
    blindbox_profit: float = 0.0

    # ── 近 N 天每日统计（来自 /history?date=）──
    daily: list = field(default_factory=list)

    # ── 每日营收金额（来自 /gift/summary byDay），升序 ──
    daily_revenue: list = field(default_factory=list)

    # ── 精确统计（按 kind 逐类拉取，不受 5000 上限影响）──
    exact: dict = field(default_factory=dict)

    raw: dict = field(default_factory=dict)


class XixiClient:
    """汐汐工具箱 HTTP 客户端。"""

    def __init__(
        self, base_url: str, timeout: float = 12.0, db_api_url: str = ""
    ) -> None:
        # base_url 形如 http://192.168.1.5:8800
        self.base = (base_url or "").rstrip("/")
        self.timeout = timeout
        # SQLite 查询服务（可选，能拿到比 /history 更完整的数据）
        self.db_api_url = (db_api_url or "").rstrip("/")

    def _url(self, path: str) -> str:
        p = path if path.startswith("/") else "/" + path
        return f"{self.base}/bililive/api{p}"

    async def _get(self, path: str) -> Any:
        if not self.base:
            return None
        if aiohttp is None:
            logger.error("[bili_live] 缺少 aiohttp 依赖")
            return None
        url = self._url(path)
        try:
            tmo = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        logger.warning(f"[bili_live] {path} -> HTTP {resp.status}")
                        return None
                    return await resp.json(content_type=None)
        except asyncio.TimeoutError:
            logger.warning(f"[bili_live] {path} 请求超时")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] {path} 请求失败: {exc}")
            return None

    @staticmethod
    def _norm_room(r: dict) -> dict:
        """把两种房间结构统一成同时带「扁平字段」和「anchor 嵌套」。

        两个数据源结构不同：
          · db-api（8801）  → {"roomId","uid","uname","face","live","online",...}
          · 主程序（8800）  → {"roomId","anchor":{"uid","uname","face","liveStatus"},...}

        调用方大多按 r["anchor"]["uid"] 取值，直接返回 db-api 的结构会
        导致 uid=0、头像/粉丝数都拿不到。这里统一补齐。
        """
        if not isinstance(r, dict):
            return {}
        a = r.get("anchor")
        if not isinstance(a, dict):
            a = {}
        # 扁平字段优先（db-api 的更全），否则用 anchor 里的
        uid = int(r.get("uid") or a.get("uid") or 0)
        uname = str(r.get("uname") or a.get("uname") or r.get("alias") or "")
        face = str(r.get("face") or a.get("face") or "")
        title = str(r.get("title") or a.get("title") or "")
        live = r.get("live")
        if live is None:
            live = int(a.get("liveStatus") or 0) == 1
        online = int(r.get("online") or a.get("online") or 0)

        out = dict(r)
        out["roomId"] = int(r.get("roomId") or 0)
        out["uid"] = uid
        out["uname"] = uname
        out["face"] = face
        out["title"] = title
        out["live"] = bool(live)
        out["online"] = online
        # anchor 嵌套（兼容旧调用方）
        out["anchor"] = {
            **a,
            "uid": uid,
            "uname": uname,
            "face": face,
            "title": title,
            "liveStatus": 1 if live else 0,
            "online": online,
        }
        return out

    async def get_rooms(self) -> list[dict]:
        """已配置的房间列表（统一结构，同时带扁平字段和 anchor 嵌套）。

        优先走 db-api（8801，/rooms 返回 list，带 face/online/lastStartText），
        失败再退回主程序（8800，返回 rooms）。
        """
        # ① db-api
        if self.db_api_url:
            j = await self._db_get("/rooms")
            if j and j.get("list"):
                return [self._norm_room(r) for r in j["list"] if isinstance(r, dict)]
        # ② 主程序
        j2 = await self._get("/rooms")
        if isinstance(j2, dict):
            return [self._norm_room(r) for r in (j2.get("rooms") or []) if isinstance(r, dict)]
        return []

    async def resolve_uid_to_room(self, uid: int) -> int:
        """把主播 UID 转成直播间号。

        两条路：
          1. **本地匹配**（快，无需外网）：汐汐 /rooms 里每个房间都带 anchor.uid
          2. **B 站反查**（兜底）：room/v1/Room/getRoomInfoOld?mid=
        """
        # 1) 本地匹配
        for r in await self.get_rooms():
            if not isinstance(r, dict):
                continue
            a = r.get("anchor") or {}
            try:
                if int(a.get("uid") or 0) == uid:
                    rid = int(r.get("roomId") or 0)
                    if rid:
                        logger.info(f"[bili_live] UID {uid} 在汐汐列表里 -> 房间 {rid}")
                        return rid
            except (TypeError, ValueError):
                continue

        # 2) B 站反查
        if aiohttp is None:
            return 0
        url = (
            "https://api.live.bilibili.com/room/v1/Room/getRoomInfoOld"
            f"?mid={int(uid)}"
        )
        try:
            tmo = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                        ),
                        "Referer": "https://live.bilibili.com/",
                    },
                ) as resp:
                    if resp.status != 200:
                        return 0
                    j = await resp.json(content_type=None)
            d = (j or {}).get("data") or {}
            rid = int(d.get("roomid") or 0)
            if rid:
                logger.info(f"[bili_live] UID {uid} 经 B站反查 -> 房间 {rid}")
            return rid
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili_live] UID {uid} 反查失败: {exc}")
            return 0

    async def find_local_by_name(self, name: str) -> tuple[int, str]:
        """在汐汐已跟踪的房间里按主播名匹配（支持模糊）。

        Returns:
            (room_id, uname)；没找到返回 (0, "")。
        """
        key = str(name or "").strip().lower()
        if not key:
            return 0, ""

        rooms = await self.get_rooms()
        exact: tuple[int, str] | None = None
        fuzzy: tuple[int, str] | None = None

        for r in rooms:
            if not isinstance(r, dict):
                continue
            a = r.get("anchor") or {}
            uname = str(a.get("uname") or "").strip()
            if not uname:
                continue
            try:
                rid = int(r.get("roomId") or 0)
            except (TypeError, ValueError):
                continue
            if not rid:
                continue

            low = uname.lower()
            if low == key:
                exact = (rid, uname)
                break
            if fuzzy is None and (key in low or low in key):
                fuzzy = (rid, uname)

        if exact:
            logger.info(f"[bili_live] 主播名「{name}」本地精确匹配 -> {exact[1]} ({exact[0]})")
            return exact
        if fuzzy:
            logger.info(f"[bili_live] 主播名「{name}」本地模糊匹配 -> {fuzzy[1]} ({fuzzy[0]})")
            return fuzzy
        return 0, ""

    async def search_anchor_by_name(self, name: str) -> tuple[int, str, int]:
        """用 B 站搜索按名字找主播。

        B 站搜索有风控（-412 request was banned）：
        空 session 直接搜会被拒，必须先访问一次首页拿到 buvid3 等 cookie。

        Returns:
            (room_id, uname, uid)；没找到返回 (0, "", 0)。
        """
        import urllib.parse

        if aiohttp is None:
            return 0, "", 0

        name = str(name or "").strip()
        if not name:
            return 0, "", 0

        url = (
            "https://api.bilibili.com/x/web-interface/search/type"
            f"?search_type=bili_user&keyword={urllib.parse.quote(name)}&page=1"
        )
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )

        async def _once(warm: bool) -> dict | None:
            """一次搜索尝试。warm=True 时先访问首页养 cookie。"""
            try:
                tmo = aiohttp.ClientTimeout(total=self.timeout)
                async with aiohttp.ClientSession(
                    timeout=tmo, cookies=aiohttp.CookieJar(unsafe=True)
                ) as sess:
                    if warm:
                        try:
                            async with sess.get(
                                "https://www.bilibili.com/",
                                headers={
                                    "User-Agent": ua,
                                    "Accept": (
                                        "text/html,application/xhtml+xml,"
                                        "application/xml;q=0.9,*/*;q=0.8"
                                    ),
                                },
                            ) as wr:
                                await wr.read()
                        except Exception:  # noqa: BLE001
                            pass
                        await asyncio.sleep(0.6)

                    async with sess.get(
                        url,
                        headers={
                            "User-Agent": ua,
                            "Referer": "https://search.bilibili.com/",
                            "Accept": "application/json, text/plain, */*",
                            "Accept-Language": "zh-CN,zh;q=0.9",
                        },
                    ) as resp:
                        if resp.status != 200:
                            logger.debug(
                                f"[bili_live] 搜索「{name}」HTTP {resp.status}"
                            )
                            return None
                        return await resp.json(content_type=None)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[bili_live] 搜索「{name}」失败: {exc}")
                return None

        j = await _once(warm=False)
        # 被风控就养 cookie 重试一次
        if j is None or (j or {}).get("code") == -412:
            logger.info(f"[bili_live] 搜索「{name}」被风控，预热 cookie 后重试")
            await asyncio.sleep(1.2)
            j = await _once(warm=True)

        if not j or j.get("code") != 0:
            if j and j.get("code") == -412:
                logger.warning(f"[bili_live] 搜索「{name}」仍被 B站风控限制")
            return 0, "", 0

        results = (j.get("data") or {}).get("result") or []
        if not isinstance(results, list):
            return 0, "", 0

        key = name.lower()
        best: tuple[int, str, int] | None = None
        best_score = 0.0
        for r in results:
            if not isinstance(r, dict):
                continue
            uname = str(r.get("uname") or "").strip()
            try:
                rid = int(r.get("room_id") or 0)
                uid = int(r.get("mid") or 0)
                fans = int(r.get("fans") or 0)
            except (TypeError, ValueError):
                continue
            if not uname or not rid:
                continue

            low = uname.lower()
            if low == key:
                score = 100.0
            elif key in low or low in key:
                score = 50.0
            else:
                score = 0.0
            # 同分时粉丝多的优先
            score += min(30.0, fans / 1e6)
            if score > best_score:
                best_score = score
                best = (rid, uname, uid)

        if best:
            logger.info(
                f"[bili_live] 搜索「{name}」命中 -> {best[1]} "
                f"(uid={best[2]} room={best[0]} score={best_score:.1f})"
            )
        return best or (0, "", 0)

    async def get_stats(self) -> dict | None:
        j = await self._get("/stats")
        return j if isinstance(j, dict) and j.get("ok") else None

    async def get_messages(self, limit: int = 200) -> list[dict]:
        j = await self._get(f"/messages?limit={int(limit)}")
        if not isinstance(j, dict):
            return []
        return j.get("list") or []

    async def get_superchats(self) -> dict | None:
        j = await self._get("/superchats")
        return j if isinstance(j, dict) and j.get("ok") else None

    async def get_guards(self) -> dict | None:
        j = await self._get("/guards")
        return j if isinstance(j, dict) and j.get("ok") else None

    async def get_users(self, limit: int = 20, sort: str = "danmaku") -> dict | None:
        """观众列表。

        Args:
            limit: 数量
            sort: 排序方式，可选 danmaku（弹幕数）/ giftValue（付费金额）/
                  scValue（SC 金额）/ like
        """
        j = await self._get(f"/users?limit={int(limit)}&sort={sort}")
        return j if isinstance(j, dict) and j.get("ok") else None

    async def get_live_log(self, room_id: int, limit: int = 5) -> dict | None:
        """场次记录（开播/关播时间）。"""
        j = await self._get(f"/live-log?roomId={int(room_id)}&limit={int(limit)}")
        return j if isinstance(j, dict) and j.get("ok") else None

    async def get_room_info(self, room_id: int) -> dict | None:
        """B 站官方房间信息（含 uid / 标题 / 开播状态）。"""
        if aiohttp is None:
            return None
        url = f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={int(room_id)}"
        try:
            tmo = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                        ),
                        "Referer": "https://live.bilibili.com/",
                    },
                ) as resp:
                    if resp.status != 200:
                        return None
                    j = await resp.json(content_type=None)
            if (j or {}).get("code") != 0:
                return None
            return j.get("data")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili_live] 房间信息 {room_id} 失败: {exc}")
            return None

    async def _db_get(self, path: str, **params) -> dict | None:
        """通用的 db-api 请求（8801）。"""
        if not self.db_api_url or aiohttp is None:
            return None
        from urllib.parse import quote as _q
        qs = "&".join(
            f"{k}={_q(str(v), safe='')}"
            for k, v in params.items() if v is not None
        )
        url = f"{self.db_api_url}{path}" + (f"?{qs}" if qs else "")
        try:
            tmo = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        return None
                    j = await resp.json(content_type=None)
            if not isinstance(j, dict) or not j.get("ok"):
                return None
            return j
        except Exception:  # noqa: BLE001
            return None

    async def get_db_stats(self, room_id: int, days: int = 5) -> list[dict]:
        """从 SQLite 查询服务拿每日统计（完整数据，不截断）。

        Returns:
            [{"date","danmaku","gift","sc","guard","watched",
              "enter","share","follow","like","giftYuan","scYuan","revenue"}, ...]
        """
        if not self.db_api_url or aiohttp is None:
            return []
        url = f"{self.db_api_url}/stats?room={int(room_id)}&days={int(days)}"
        try:
            tmo = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        return []
                    j = await resp.json(content_type=None)
            if not isinstance(j, dict) or not j.get("ok"):
                return []
            return j.get("list") or []
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili_live] db_api /stats 失败: {exc}")
            return []

    async def get_db_payers(self, room_id: int, days: int = 5, top: int = 20) -> list[dict]:
        """从 SQLite 拿付费排行。"""
        if not self.db_api_url or aiohttp is None:
            return []
        url = f"{self.db_api_url}/payers?room={int(room_id)}&days={int(days)}&top={int(top)}"
        try:
            tmo = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        return []
                    j = await resp.json(content_type=None)
            if not isinstance(j, dict) or not j.get("ok"):
                return []
            return j.get("list") or []
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili_live] db_api /payers 失败: {exc}")
            return []

    async def get_db_hourly(self, room_id: int, date: str) -> list[dict]:
        """按小时的真实热度分布（弹幕/礼物/进场）。

        数据来自 SQLite 的 strftime 聚合，覆盖全天所有消息，
        不像 timeline 只有 300 条采样。

        Returns:
            [{"hour":13,"danmaku":933,"gift":133,"enter":888,"total":2111}, ...]
        """
        j = await self._db_get("/hourly", room=int(room_id), date=date)
        return (j or {}).get("list") or []

    async def get_db_gifts(self, room_id: int, days: int = 5, top: int = 20) -> list[dict]:
        """礼物统计（按礼物名聚合，金额降序）。

        Returns:
            [{"name","cnt","num","yuan"}, ...]   yuan 降序
        """
        j = await self._db_get("/gifts", room=int(room_id), days=int(days), top=int(top))
        return (j or {}).get("list") or []

    async def get_db_danmaku_rank(self, room_id: int, days: int = 5, top: int = 20) -> list[dict]:
        """从 SQLite 拿弹幕排行。"""
        if not self.db_api_url or aiohttp is None:
            return []
        url = (
            f"{self.db_api_url}/danmaku-rank"
            f"?room={int(room_id)}&days={int(days)}&top={int(top)}"
        )
        try:
            tmo = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(url) as resp:
                    if resp.status != 200:
                        return []
                    j = await resp.json(content_type=None)
            if not isinstance(j, dict) or not j.get("ok"):
                return []
            return j.get("list") or []
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili_live] db_api /danmaku-rank 失败: {exc}")
            return []


    async def get_exact_stats(self, room_id: int, date: str = "") -> dict:
        """按 kind 精确统计各类消息数量（绕过 5000 条上限）。

        汐汐的 /history 不带 kind 时最多返回 5000 条，
        但带 kind 时会下推到 SQLite 索引，能拿到全量。
        注意：弹幕仍会被截断在 20000，要完整数据请用 db_api_url。

        Returns:
            {"danmaku": N, "gift": N, "sc": N, "guard": N,
             "like": N, "likeTotal": N, "enter": N, "share": N,
             "follow": N, "watched": N}
        """
        out = {
            "danmaku": 0, "gift": 0, "sc": 0, "guard": 0,
            "like": 0, "likeTotal": 0, "enter": 0, "share": 0,
            "follow": 0, "watched": 0,
        }

        # 1) 独立 kind 直接 count
        for k in ("danmaku", "gift", "sc", "guard", "like", "likeTotal", "watched"):
            msgs = await self.get_history(room_id, limit=50000, date=date, kind=k)
            out[k] = len(msgs)
            # likeTotal 是累计值，取最大 count
            if k == "likeTotal" and msgs:
                best = 0
                for m in msgs:
                    if isinstance(m, dict):
                        try:
                            best = max(best, int(m.get("count") or 0))
                        except (TypeError, ValueError):
                            continue
                out["likeTotalMax"] = best

        # 2) interact 需要按 sub 细分
        inter = await self.get_history(room_id, limit=50000, date=date, kind="interact")
        for m in inter:
            if not isinstance(m, dict):
                continue
            sub = str(m.get("sub") or "")
            if sub == "enter":
                out["enter"] += 1
            elif sub == "share":
                out["share"] += 1
            elif sub == "follow" or sub in ("special", "mutual"):
                out["follow"] += 1

        return out

    async def get_gift_summary(self) -> dict | None:
        """礼物/营收汇总（all = 累计，today = 今日，byDay = 每日金额）。"""
        j = await self._get("/gift/summary")
        return j if isinstance(j, dict) and j.get("ok") else None

    async def get_daily_revenue(self, room_id: int = 0) -> list[dict]:
        """每日营收金额（真实元），按日期升序。

        数据来自 /gift/summary 的 byDay（全局汇总）。
        """
        j = await self.get_gift_summary()
        if not j:
            return []
        by_day = j.get("byDay") or []
        out = []
        for it in by_day:
            if not isinstance(it, dict):
                continue
            dt = str(it.get("date") or "")
            try:
                yuan = float(it.get("yuan") or 0)
            except (TypeError, ValueError):
                yuan = 0.0
            if dt:
                out.append({"date": dt, "yuan": yuan})
        # 接口返回是倒序（最新在前），反转成升序（从左到右）
        out.sort(key=lambda x: x["date"])
        return out

    async def get_payers_full(self, room_id: int, days: int = 5) -> list[dict]:
        """完整付费排行：按 kind=gift,sc 逐天拉取（不受 5000 条上限截断）。

        Returns:
            [{"uid","uname","giftValue","scValue","giftCount"}, ...] 金额降序
        """
        dates = await self.get_history_dates(room_id)
        dates = sorted(dates)[-days:] if dates else []

        agg: dict = {}
        for d in dates:
            # 礼物（带 price 字段）
            for m in await self.get_history(room_id, limit=50000, date=d, kind="gift"):
                if not isinstance(m, dict):
                    continue
                uid = m.get("uid")
                if not uid:
                    continue
                try:
                    uid_i = int(uid)
                    price = float(m.get("price") or 0)
                    num = int(m.get("num") or 1)
                except (TypeError, ValueError):
                    continue
                p = agg.setdefault(
                    uid_i,
                    {"uid": uid_i, "uname": str(m.get("uname") or "?"),
                     "giftValue": 0.0, "scValue": 0.0, "giftCount": 0},
                )
                p["giftValue"] += price * num
                p["giftCount"] += 1

            # SC
            for m in await self.get_history(room_id, limit=50000, date=d, kind="sc"):
                if not isinstance(m, dict):
                    continue
                uid = m.get("uid")
                if not uid:
                    continue
                try:
                    uid_i = int(uid)
                    price = float(m.get("price") or 0)
                except (TypeError, ValueError):
                    continue
                p = agg.setdefault(
                    uid_i,
                    {"uid": uid_i, "uname": str(m.get("uname") or "?"),
                     "giftValue": 0.0, "scValue": 0.0, "giftCount": 0},
                )
                p["scValue"] += price

        return sorted(
            agg.values(),
            key=lambda p: (p.get("giftValue") or 0) + (p.get("scValue") or 0),
            reverse=True,
        )[:20]

    async def get_daily_revenue_by_room(self, room_id: int, days: int = 5) -> list[dict]:
        """按房间统计每日营收金额（元），按日期升序。

        用 kind=gift 逐天拉，自己累加 price —— 比 /gift/summary 的全局值准。
        """
        dates = await self.get_history_dates(room_id)
        dates = sorted(dates)[-days:] if dates else []

        out = []
        for d in dates:
            total = 0.0
            for m in await self.get_history(room_id, limit=50000, date=d, kind="gift"):
                if not isinstance(m, dict):
                    continue
                try:
                    total += float(m.get("price") or 0) * int(m.get("num") or 1)
                except (TypeError, ValueError):
                    continue
            for m in await self.get_history(room_id, limit=50000, date=d, kind="sc"):
                if not isinstance(m, dict):
                    continue
                try:
                    total += float(m.get("price") or 0)
                except (TypeError, ValueError):
                    continue
            out.append({"date": d, "yuan": total})
        return out
        """每日营收金额（真实元），按日期升序。

        Returns:
            [{"date": "2026-11-18", "yuan": 1743.5}, ...]
        """
        j = await self.get_gift_summary()
        if not j:
            return []
        by_day = j.get("byDay") or []
        out = []
        for it in by_day:
            if not isinstance(it, dict):
                continue
            dt = str(it.get("date") or "")
            try:
                yuan = float(it.get("yuan") or 0)
            except (TypeError, ValueError):
                yuan = 0.0
            if dt:
                out.append({"date": dt, "yuan": yuan})
        # 接口返回是倒序（最新在前），反转成升序（从左到右）
        out.sort(key=lambda x: x["date"])
        return out

    async def add_room(self, room_id: int, alias: str = "") -> tuple[bool, str]:
        """添加直播间到汐汐并自动连接。

        Returns:
            (成功?, 错误信息)
        """
        if aiohttp is None:
            return False, "缺少 aiohttp 依赖"
        if not self.base:
            return False, "汐汐地址未配置"

        url = self._url("/rooms/add")
        payload = {"roomId": int(room_id)}
        if alias:
            payload["alias"] = alias
        try:
            tmo = aiohttp.ClientTimeout(total=self.timeout + 10)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.post(
                    url, json=payload, headers={"Content-Type": "application/json"}
                ) as resp:
                    j = await resp.json(content_type=None)
            if not isinstance(j, dict):
                return False, f"HTTP {resp.status}"
            if j.get("ok"):
                return True, ""
            return False, str(j.get("error") or "未知错误")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] 添加房间 {room_id} 失败: {exc}")
            return False, str(exc)

    async def get_blindbox(self) -> dict | None:
        j = await self._get("/blindbox")
        return j if isinstance(j, dict) and j.get("ok") else None

    async def get_history(
        self, room_id: int, limit: int = 200, date: str = "", kind: str = ""
    ) -> list[dict]:
        """历史消息流（带时间戳，用于时间序列图）。

        Args:
            date: 指定日期 "2026-11-18"；留空为今天
            kind: 按类型过滤，如 "gift" / "danmaku" / "sc,guard"
                  **带 kind 时过滤下推到 SQLite 索引，不受 5000 条上限截断**
        """
        q = f"/history?roomId={int(room_id)}&limit={int(limit)}"
        if date:
            q += f"&date={date}"
        if kind:
            q += f"&kind={kind}"
        j = await self._get(q)
        if not isinstance(j, dict):
            return []
        return j.get("list") or []

    async def get_history_dates(self, room_id: int) -> list[str]:
        """有哪些天有历史数据（近 N 天）。"""
        j = await self._get(f"/history/dates?roomId={int(room_id)}")
        if not isinstance(j, dict):
            return []
        return list(j.get("dates") or [])

    async def get_multi_day_stats(self, room_id: int, days: int = 5) -> list[dict]:
        """近 N 天的每日统计（弹幕/礼物/进场/SC/舰长）。

        Returns:
            [{"date": "2026-11-18", "danmaku": 123, "gift": 5, ...}, ...]
            按日期升序，最多 days 条。
        """
        dates = await self.get_history_dates(room_id)
        dates = sorted(dates)[-days:] if dates else []

        out: list[dict] = []
        for d in dates:
            msgs = await self.get_history(room_id, limit=20000, date=d)
            stat = {"date": d, "danmaku": 0, "gift": 0, "enter": 0,
                    "sc": 0, "guard": 0, "like": 0, "total": len(msgs)}
            for m in msgs:
                if not isinstance(m, dict):
                    continue
                k = m.get("kind")
                if k == "danmaku":
                    stat["danmaku"] += 1
                elif k == "gift":
                    stat["gift"] += 1
                elif k == "sc" or k == "superchat":
                    stat["sc"] += 1
                elif k == "guard":
                    stat["guard"] += 1
                elif k == "interact":
                    sub = m.get("sub")
                    if sub == "enter":
                        stat["enter"] += 1
                elif k == "like":
                    stat["like"] += 1
            out.append(stat)
        return out

    async def get_master_info(self, uid: int) -> dict | None:
        """主播详细信息（B 站官方接口，含粉丝数）。"""
        if aiohttp is None:
            return None
        url = f"https://api.live.bilibili.com/live_user/v1/Master/info?uid={int(uid)}"
        try:
            tmo = aiohttp.ClientTimeout(total=self.timeout)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(
                    url,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
                        ),
                        "Referer": "https://live.bilibili.com/",
                    },
                ) as resp:
                    if resp.status != 200:
                        return None
                    j = await resp.json(content_type=None)
            return (j or {}).get("data")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili_live] 主播信息 {uid} 失败: {exc}")
            return None

    async def get_basic_snapshot(self, room_id: int) -> LiveSnapshot | None:
        """未监听房间的降级快照：只用 B 站官方接口取基础信息。

        能拿到：主播名/头像/标题/开播状态/人气/粉丝数/分区/开播时间。
        拿不到：弹幕/礼物/SC/舰长等实时统计（这些需要 WebSocket 长连累计）。
        """
        if aiohttp is None:
            return None

        tmo = aiohttp.ClientTimeout(total=self.timeout)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
            ),
            "Referer": "https://live.bilibili.com/",
        }

        snap = LiveSnapshot(room_id=room_id, is_tracked=False)

        try:
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                # 1) 房间基础信息（支持短号）
                async with sess.get(
                    f"https://api.live.bilibili.com/room/v1/Room/get_info?room_id={int(room_id)}",
                    headers=headers,
                ) as resp:
                    j = await resp.json(content_type=None)
                if (j or {}).get("code") != 0:
                    logger.info(f"[bili_live] 房间 {room_id} 官方查询失败: {j.get('msg')}")
                    return None
                d = j.get("data") or {}
                snap.uid = int(d.get("uid") or 0)
                snap.room_id = int(d.get("room_id") or room_id)
                snap.uname = ""          # get_info 不带主播名，用 Master/info 补
                snap.face = ""
                snap.title = str(d.get("title") or "")
                snap.live_status = int(d.get("live_status") or 0)
                snap.online = int(d.get("online") or 0)
                try:
                    lt = str(d.get("live_time") or "")
                    if lt and lt != "0000-00-00 00:00:00":
                        snap.live_start_text = lt
                        import datetime as _dt

                        t = _dt.strptime(lt, "%Y-%m-%d %H:%M:%S")
                        snap.live_start_ts = int(t.timestamp() * 1000)
                except Exception:  # noqa: BLE001
                    pass

                # 2) 主播信息（名字/头像/粉丝数）
                if snap.uid:
                    mi = await self.get_master_info(snap.uid)
                    if mi:
                        info = mi.get("info") or {}
                        snap.uname = str(info.get("uname") or "")
                        snap.face = str(info.get("face") or "")
                        snap.followers = int(mi.get("follower_num") or 0)
                        # 真实房间号（短号会换成长号）
                        try:
                            snap.room_id = int(mi.get("room_id") or snap.room_id)
                        except (TypeError, ValueError):
                            pass

                snap.is_live_now = snap.live_status == 1
                if snap.is_live_now:
                    snap.revenue_range_label = "本次直播"
                else:
                    snap.revenue_range_label = "未开播"
                return snap
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] 基础快照 {room_id} 失败: {exc}")
            return None

    async def snapshot(self, room_id: int) -> LiveSnapshot | None:
        """拉取一个房间的完整快照。

        关键：汐汐同一时刻只「连接」一个房间，但 `/rooms` 里
        **每个已添加的房间都保留着自己的统计数据**（弹幕/观众/消息），
        所以不需要是当前连接也能拿到数据。

        只有「实时事件流」类接口（/stats /users /messages /guards /
        /superchats /blindbox）才绑定当前连接，那些需要额外判断。
        """
        rooms = await self.get_rooms()
        target = None
        for r in rooms:
            if not isinstance(r, dict):
                continue
            try:
                if int(r.get("roomId") or 0) == int(room_id):
                    target = r
                    break
            except (TypeError, ValueError):
                continue

        if target is None:
            logger.info(f"[bili_live] 房间 {room_id} 不在汐汐跟踪列表（共 {len(rooms)} 个）")
            return None

        anchor = target.get("anchor") or {}
        st = target.get("stats") or {}

        snap = LiveSnapshot(
            room_id=room_id,
            uid=int(anchor.get("uid") or 0),
            uname=str(anchor.get("uname") or ""),
            face=str(anchor.get("face") or ""),
            title=str(anchor.get("title") or ""),
            live_status=int(anchor.get("liveStatus") or 0),
            online=int(anchor.get("online") or 0),
            danmaku=int(st.get("danmaku") or 0),
            gift=int(st.get("gift") or 0),
            sc=int(st.get("sc") or 0),
            guard=int(st.get("guard") or 0),
            enter=int(st.get("enter") or 0),
            like=int(st.get("like") or 0),
            users=int(target.get("users") or 0),
            guards_count=int(target.get("guards") or 0),
            messages=int(target.get("messages") or 0),
            state=str(target.get("state") or ""),
            is_tracked=True,
            raw=target,
        )

        # 是否当前连接（决定后续实时接口能不能用）
        stats = await self.get_stats()
        cur_room = int((stats or {}).get("roomId") or 0)
        is_current = cur_room == int(room_id)
        snap.raw["_is_current"] = is_current

        if is_current and stats:
            # 当前连接：用 /stats 覆盖（更实时）
            a = stats.get("anchor") or {}
            s = stats.get("stats") or {}
            snap.uname = str(a.get("uname") or snap.uname)
            snap.face = str(a.get("face") or snap.face)
            snap.title = str(a.get("title") or snap.title)
            snap.live_status = int(a.get("liveStatus") or snap.live_status)
            snap.online = int(a.get("online") or snap.online)
            snap.danmaku = int(s.get("danmaku") or snap.danmaku)
            snap.gift = int(s.get("gift") or snap.gift)
            snap.sc = int(s.get("sc") or snap.sc)
            snap.enter = int(s.get("enter") or snap.enter)
            snap.like = int(s.get("like") or snap.like)
            snap.users = int(stats.get("users") or snap.users)
            snap.messages = int(stats.get("messages") or snap.messages)

        # ── 场次记录（开播/关播时间）──
        log = await self.get_live_log(room_id, limit=3)
        session_start_ms = 0
        if log:
            items = log.get("list") or []
            cur = None
            for it in items:
                if isinstance(it, dict) and it.get("live"):
                    cur = it
                    break
            if cur is None and items:
                cur = items[0] if isinstance(items[0], dict) else None

            # 保存全部场次（供时长多场对比）
            snap.live_sessions = [it for it in items if isinstance(it, dict)]

            if cur:
                snap.live_start_ts = int(cur.get("start") or 0)
                snap.live_start_text = str(cur.get("startText") or "")
                snap.live_end_ts = int(cur.get("end") or 0)
                snap.live_end_text = str(cur.get("endText") or "")
                snap.session_duration = str(cur.get("durationText") or "")
                snap.is_live_now = bool(cur.get("live"))
                session_start_ms = snap.live_start_ts

        if not snap.live_start_text and snap.live_status == 1:
            snap.is_live_now = True

        # ── 粉丝数（B 站官方，任何房间都能查）──
        if snap.uid:
            mi = await self.get_master_info(snap.uid)
            if mi:
                snap.followers = int(mi.get("follower_num") or 0)
                info = mi.get("info") or {}
                if not snap.uname:
                    snap.uname = str(info.get("uname") or "")
                if not snap.face:
                    snap.face = str(info.get("face") or "")

        # 以下实时接口只对「当前连接」的房间有效
        if is_current:
            # ── 营收（本场 / 累计）──
            gsum = await self.get_gift_summary()
            if gsum:
                all_ = gsum.get("all") or {}
                today = gsum.get("today") or {}
                snap.revenue_all = float(all_.get("totalYuan") or 0)
                snap.revenue_today = float(today.get("totalYuan") or 0)
                snap.gift_count = int(all_.get("count") or 0) or int(
                    today.get("count") or 0
                )
                if snap.is_live_now:
                    snap.revenue_range_label = "本次直播"
                else:
                    snap.revenue_range_label = "上次直播"
                    if snap.revenue_today:
                        snap.revenue_all = max(snap.revenue_all, snap.revenue_today)

            # SC 详情
            sc = await self.get_superchats()
            if sc:
                snap.sc_total_yuan = float(sc.get("totalYuan") or 0)
                snap.sc_avg_yuan = float(sc.get("avgYuan") or 0)
                snap.sc_tiers = sc.get("tiers") or {}
                snap.sc_list = (sc.get("list") or [])[:9]

            # ── 舰长：只保留直播期间买入的 ──
            gd = await self.get_guards()
            if gd:
                raw_guards = [g for g in (gd.get("list") or []) if isinstance(g, dict)]
                if session_start_ms:
                    during = []
                    for g in raw_guards:
                        try:
                            bt = int(g.get("buyTime") or 0)
                        except (TypeError, ValueError):
                            bt = 0
                        if bt and bt >= session_start_ms:
                            during.append(g)
                    snap.guard_list = during[:9]
                    snap.guard = len(during)
                else:
                    snap.guard_list = raw_guards[:9]

            # ── 观众排行 ──
            us = await self.get_users(limit=20, sort="danmaku")
            if us:
                snap.user_list = us.get("list") or []
            pay = await self.get_users(limit=20, sort="giftValue")
            if pay:
                snap.payer_list = pay.get("list") or []

            # 弹幕采样（词云）
            msgs = await self.get_messages(limit=500)
            snap.danmaku_samples = [
                str(m.get("text") or "")
                for m in msgs
                if isinstance(m, dict) and m.get("kind") == "danmaku" and m.get("text")
            ]

            # 时间序列
            snap.timeline = await self.get_history(room_id, limit=20000, date=__import__("datetime").datetime.now().strftime("%Y-%m-%d"))

            # 盲盒
            bb = await self.get_blindbox()
            if bb:
                snap.blindbox_total = int(bb.get("total") or 0)
                snap.blindbox_cost = float(bb.get("cost") or 0)
                snap.blindbox_profit = float(bb.get("profit") or 0)

            # ── 优先用 SQLite 查询服务（完整数据，不截断）──
            db_list = await self.get_db_stats(room_id, days=DAILY_DAYS)
            if db_list:
                snap.daily = db_list
                snap.daily_revenue = [
                    {"date": x.get("date"), "yuan": float(x.get("revenue") or 0)}
                    for x in db_list
                ]
                # 用最近一天的真实值覆盖总量
                last = db_list[-1]
                snap.danmaku = int(last.get("danmaku") or snap.danmaku)
                snap.enter = int(last.get("enter") or snap.enter)
                snap.gift_count = int(last.get("gift") or snap.gift_count)
                snap.sc = int(last.get("sc") or snap.sc)
                snap.guard = int(last.get("guard") or snap.guard)
                snap.like = int(last.get("like") or snap.like)
                snap.sc_total_yuan = float(last.get("scYuan") or snap.sc_total_yuan)

                # ── 营收：用 SQLite 真实值，不用 /gift/summary ──
                # /gift/summary 只算「今天」，跨天场次（如 19:54→次日05:08）会漏一半。
                # SQLite 按天存，把开播日至今的都加起来才对。
                if snap.is_live_now and session_start_ms:
                    import datetime as _dt
                    start_d = _dt.datetime.fromtimestamp(
                        session_start_ms / 1000
                    ).strftime("%Y-%m-%d")
                    sess_rows = [x for x in db_list if (x.get("date") or "") >= start_d]
                    snap.revenue_today = round(
                        sum(float(x.get("revenue") or 0) for x in sess_rows), 2
                    )
                    snap.revenue_range_label = "本场直播"
                else:
                    snap.revenue_today = float(last.get("revenue") or 0)
                    snap.revenue_range_label = "最近一场"
                snap.revenue_all = round(
                    sum(float(x.get("revenue") or 0) for x in db_list), 2
                )

                # 盲盒每日盈亏（就算没数据也要有 0，让图能画出来）
                snap.blind_daily = [
                    {
                        "date": x.get("date"),
                        "profit": float(x.get("blindProfit") or 0),
                        "cost": float(x.get("blindCost") or 0),
                        "count": int(x.get("blindCount") or 0),
                    }
                    for x in db_list
                ]

                # 粉丝数趋势（汐汐每 10 分钟抓一次的快照）
                try:
                    j = await self._db_get("/followers", room=int(room_id), days=DAILY_DAYS)
                    if j and j.get("list"):
                        snap.follower_daily = j["list"]
                except Exception:
                    pass

                # 精确统计也填上（分享等）
                snap.exact = {
                    "danmaku": snap.danmaku,
                    "gift": snap.gift_count,
                    "sc": snap.sc,
                    "guard": snap.guard,
                    "like": snap.like,
                    "enter": snap.enter,
                    "share": int(last.get("share") or 0),
                    "follow": int(last.get("follow") or 0),
                    "watched": int(last.get("watched") or 0),
                    "revenue": float(last.get("revenue") or 0),
                }
                logger.info(
                    f"[bili_live] \u623f\u95f4 {room_id} SQLite\u7edf\u8ba1: "
                    f"\u5f39\u5e55{snap.danmaku} \u8fdb\u573a{snap.enter} "
                    f"\u793c\u7269{snap.gift_count} SC{snap.sc} \u8230\u957f{snap.guard} "
                    f"\u70b9\u8d5e{snap.like} "
                    f"\u8425\u6536\u00a5{snap.revenue_today}"
                    f"\uff08{snap.revenue_range_label}\uff0c"
                    f"\u7d2f\u8ba1\u00a5{snap.revenue_all}\uff09"
                )

                # 付费榜 / 弹幕榜从 SQLite 拿（已按金额降序）
                dbp = await self.get_db_payers(room_id, days=1, top=20)
                if dbp:
                    snap.payer_list = dbp
                    snap.payer_total = len(dbp)
                dbr = await self.get_db_danmaku_rank(room_id, days=1, top=20)
                if dbr:
                    snap.user_list = dbr

                # 礼物统计（按礼物名聚合，金额降序）
                dbg = await self.get_db_gifts(room_id, days=1, top=20)
                if dbg:
                    snap.gift_list = dbg

                # 按小时的真实分布（弹幕频次图用，比 timeline 采样准）
                try:
                    _d = str((db_list[-1] or {}).get("date") or "")
                    if _d:
                        hr = await self.get_db_hourly(room_id, _d)
                        if hr:
                            snap.hourly_list = hr
                except Exception:
                    pass

                # 真实消息总数（不含被 /history 截断的部分）
                snap.message_total = (
                    int(snap.danmaku or 0) + int(snap.gift_count or 0)
                    + int(snap.sc or 0) + int(snap.enter or 0)
                )
            else:
                # ── 降级：走 HTTP /history ──
                snap.daily = await self.get_multi_day_stats(room_id, days=DAILY_DAYS)

                try:
                    snap.exact = await self.get_exact_stats(room_id)
                    if snap.exact.get("danmaku"):
                        snap.danmaku = snap.exact["danmaku"]
                    if snap.exact.get("enter"):
                        snap.enter = snap.exact["enter"]
                    if snap.exact.get("gift"):
                        snap.gift = snap.exact["gift"]
                        snap.gift_count = snap.exact["gift"]
                    if snap.exact.get("sc"):
                        snap.sc = snap.exact["sc"]
                    if snap.exact.get("guard"):
                        snap.guard = snap.exact["guard"]
                    lt = snap.exact.get("likeTotalMax") or snap.exact.get("like") or 0
                    if lt:
                        snap.like = int(lt)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[bili_live] 精确统计失败: {exc}")

                snap.daily_revenue = await self.get_daily_revenue_by_room(room_id, days=DAILY_DAYS)

            # 付费排行：已有 SQLite 结果就用（已排序）；否则回退到逐天拉取
            if not snap.payer_list:
                snap.payer_list = await self.get_payers_full(room_id, days=1)
            if snap.payer_list:
                snap.sc_total_yuan = sum(p.get("scValue") or 0 for p in snap.payer_list)
                if not (snap.exact or {}).get("gift"):
                    snap.gift_count = sum(int(p.get("giftCount") or 0) for p in snap.payer_list)
                logger.info(
                    f"[bili_live] 房间 {room_id} 付费榜 {len(snap.payer_list)} 人，"
                    f"礼物 {snap.gift_count} 个，SC ¥{snap.sc_total_yuan:.2f}"
                )
        else:
            # 非当前连接：从历史消息里统计全部指标
            snap.timeline = await self.get_history(room_id, limit=20000, date=__import__("datetime").datetime.now().strftime("%Y-%m-%d"))

            n_dm = n_gift = n_enter = n_sc = n_guard = n_like = 0
            samples: list[str] = []
            # 付费统计：uid -> {uname, giftValue, scValue}
            payers: dict = {}

            for m in snap.timeline:
                if not isinstance(m, dict):
                    continue
                k = m.get("kind")
                if k == "danmaku":
                    n_dm += 1
                    txt = str(m.get("text") or "")
                    if txt:
                        samples.append(txt)
                elif k == "gift":
                    n_gift += 1
                    # 从 gift 消息统计付费（有 price 字段）
                    uid = m.get("uid")
                    if uid:
                        p = payers.setdefault(
                            int(uid),
                            {
                                "uid": int(uid),
                                "uname": str(m.get("uname") or "?"),
                                "giftValue": 0.0,
                                "scValue": 0.0,
                                "giftCount": 0,
                            },
                        )
                        try:
                            price = float(m.get("price") or 0)
                            num = int(m.get("num") or 1)
                        except (TypeError, ValueError):
                            price, num = 0.0, 1
                        p["giftValue"] += price * num
                        p["giftCount"] += 1
                elif k in ("sc", "superchat"):
                    n_sc += 1
                    uid = m.get("uid")
                    if uid:
                        p = payers.setdefault(
                            int(uid),
                            {
                                "uid": int(uid),
                                "uname": str(m.get("uname") or "?"),
                                "giftValue": 0.0,
                                "scValue": 0.0,
                                "giftCount": 0,
                            },
                        )
                        try:
                            price = float(m.get("price") or 0)
                        except (TypeError, ValueError):
                            price = 0.0
                        p["scValue"] += price
                elif k == "guard":
                    n_guard += 1
                elif k == "like":
                    n_like += 1
                elif k == "interact" and m.get("sub") == "enter":
                    n_enter += 1

            # 历史统计优先（更完整），拿不到才用 /rooms 缓存
            if n_dm:
                snap.danmaku = max(snap.danmaku, n_dm)
            if n_gift:
                snap.gift = max(snap.gift, n_gift)
                snap.gift_count = max(snap.gift_count, n_gift)
            if n_enter:
                snap.enter = max(snap.enter, n_enter)
            if n_sc:
                snap.sc = max(snap.sc, n_sc)
            if n_guard:
                snap.guard = max(snap.guard, n_guard)
            if n_like:
                snap.like = max(snap.like, n_like)

            snap.danmaku_samples = samples
            snap.user_list = _rank_from_timeline(snap.timeline, "danmaku")

            # 付费排行：已有 SQLite 结果就用（已排序）；否则回退到逐天拉取
            if not snap.payer_list:
                snap.payer_list = await self.get_payers_full(room_id, days=1)
            if snap.payer_list:
                snap.sc_total_yuan = sum(p.get("scValue") or 0 for p in snap.payer_list)
                if not (snap.exact or {}).get("gift"):
                    snap.gift_count = sum(int(p.get("giftCount") or 0) for p in snap.payer_list)

            snap.revenue_range_label = "本次直播" if snap.is_live_now else "未开播"
            logger.info(
                f"[bili_live] 房间 {room_id} 非当前连接，"
                f"从历史统计：{len(snap.timeline)} 条消息 "
                f"(弹幕{n_dm} 礼物{n_gift} 进场{n_enter} SC{n_sc} 舰长{n_guard} "
                f"付费{len(payers)}人)"
            )

            # ── 优先用 SQLite 查询服务（完整数据）──
            db_list = await self.get_db_stats(room_id, days=DAILY_DAYS)
            if db_list:
                snap.daily = db_list
                snap.daily_revenue = [
                    {"date": x.get("date"), "yuan": float(x.get("revenue") or 0)}
                    for x in db_list
                ]
                last = db_list[-1]
                snap.danmaku = int(last.get("danmaku") or snap.danmaku)
                snap.enter = int(last.get("enter") or snap.enter)
                snap.gift_count = int(last.get("gift") or snap.gift_count)
                snap.sc = int(last.get("sc") or snap.sc)
                snap.guard = int(last.get("guard") or snap.guard)
                snap.like = int(last.get("like") or snap.like)
                snap.sc_total_yuan = float(last.get("scYuan") or snap.sc_total_yuan)
                snap.exact = {
                    "danmaku": snap.danmaku,
                    "gift": snap.gift_count,
                    "sc": snap.sc,
                    "guard": snap.guard,
                    "like": snap.like,
                    "enter": snap.enter,
                    "share": int(last.get("share") or 0),
                    "follow": int(last.get("follow") or 0),
                    "watched": int(last.get("watched") or 0),
                    "revenue": float(last.get("revenue") or 0),
                }
                # 盲盒每日盈亏（始终有值，没数据就是 0）
                snap.blind_daily = [
                    {
                        "date": x.get("date"),
                        "profit": float(x.get("blindProfit") or 0),
                        "cost": float(x.get("blindCost") or 0),
                        "count": int(x.get("blindCount") or 0),
                    }
                    for x in db_list
                ]

                # 粉丝数趋势（汐汐每 10 分钟抓一次）
                try:
                    jf = await self._db_get("/followers", room=int(room_id), days=DAILY_DAYS)
                    if jf and jf.get("list"):
                        snap.follower_daily = jf["list"]
                except Exception:
                    pass

                dbp = await self.get_db_payers(room_id, days=1, top=20)
                if dbp:
                    snap.payer_list = dbp
                    snap.payer_total = len(dbp)
                dbr = await self.get_db_danmaku_rank(room_id, days=1, top=20)
                if dbr:
                    snap.user_list = dbr

                # 礼物统计（按礼物名聚合，金额降序）
                dbg = await self.get_db_gifts(room_id, days=1, top=20)
                if dbg:
                    snap.gift_list = dbg

                # 按小时的真实分布（弹幕频次图用）
                try:
                    _d = str((db_list[-1] or {}).get("date") or "")
                    if _d:
                        hr = await self.get_db_hourly(room_id, _d)
                        if hr:
                            snap.hourly_list = hr
                except Exception:
                    pass

                # 真实消息总数
                snap.message_total = (
                    int(snap.danmaku or 0) + int(snap.gift_count or 0)
                    + int(snap.sc or 0) + int(snap.enter or 0)
                )

                logger.info(
                    f"[bili_live] 房间 {room_id} SQLite 统计(非当前连接): "
                    f"弹幕{snap.danmaku} 进场{snap.enter} 礼物{snap.gift_count} "
                    f"SC{snap.sc} 舰长{snap.guard} 点赞{snap.like} "
                    f"分享{snap.exact['share']} 营收¥{snap.exact['revenue']}"
                )
            else:
                # 降级：走 HTTP
                snap.daily = await self.get_multi_day_stats(room_id, days=DAILY_DAYS)
                snap.daily_revenue = await self.get_daily_revenue_by_room(room_id, days=DAILY_DAYS)

                try:
                    snap.exact = await self.get_exact_stats(room_id)
                    if snap.exact.get("danmaku"):
                        snap.danmaku = snap.exact["danmaku"]
                    if snap.exact.get("enter"):
                        snap.enter = snap.exact["enter"]
                    if snap.exact.get("gift"):
                        snap.gift = snap.exact["gift"]
                        snap.gift_count = snap.exact["gift"]
                    if snap.exact.get("sc"):
                        snap.sc = snap.exact["sc"]
                    if snap.exact.get("guard"):
                        snap.guard = snap.exact["guard"]
                    lt = snap.exact.get("likeTotalMax") or snap.exact.get("like") or 0
                    if lt:
                        snap.like = int(lt)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[bili_live] 精确统计失败: {exc}")

        return snap

def _payers_from_history(messages: list, top: int = 20) -> list[dict]:
    """从历史消息里统计付费排行（gift 有 price 字段）。

    Returns:
        [{"uid","uname","giftValue","scValue","giftCount"}, ...] 按金额降序
    """
    payers: dict = {}
    for m in (messages or []):
        if not isinstance(m, dict):
            continue
        k = m.get("kind")
        if k not in ("gift", "sc", "superchat"):
            continue
        uid = m.get("uid")
        if not uid:
            continue
        try:
            uid_i = int(uid)
        except (TypeError, ValueError):
            continue
        p = payers.setdefault(
            uid_i,
            {"uid": uid_i, "uname": str(m.get("uname") or "?"),
             "giftValue": 0.0, "scValue": 0.0, "giftCount": 0},
        )
        try:
            price = float(m.get("price") or 0)
        except (TypeError, ValueError):
            price = 0.0
        if k == "gift":
            try:
                num = int(m.get("num") or 1)
            except (TypeError, ValueError):
                num = 1
            p["giftValue"] += price * num
            p["giftCount"] += 1
        else:
            p["scValue"] += price

    out = sorted(
        payers.values(),
        key=lambda p: (p.get("giftValue") or 0) + (p.get("scValue") or 0),
        reverse=True,
    )
    return out[:top]


def _rank_from_timeline(timeline: list, kind: str = "danmaku", top: int = 20) -> list[dict]:
    """从历史消息流里统计用户排行。

    Args:
        timeline: /history 返回的消息列表
        kind: "danmaku" 按弹幕数，其他按发言总数
        top: 取前几名
    """
    from collections import Counter

    cnt: Counter = Counter()
    info: dict = {}
    for m in (timeline or []):
        if not isinstance(m, dict):
            continue
        k = m.get("kind")
        if kind == "danmaku" and k != "danmaku":
            continue
        if k not in ("danmaku", "gift", "sc", "interact"):
            continue
        uid = int(m.get("uid") or 0)
        if not uid:
            continue
        cnt[uid] += 1
        if uid not in info:
            info[uid] = {
                "uid": uid,
                "uname": str(m.get("uname") or "?"),
                "face": str(m.get("face") or ""),
            }

    out = []
    for uid, c in cnt.most_common(top):
        item = dict(info.get(uid) or {"uid": uid, "uname": "?"})
        item["danmaku"] = c
        item["gift"] = 0
        item["sc"] = 0
        out.append(item)
    return out

