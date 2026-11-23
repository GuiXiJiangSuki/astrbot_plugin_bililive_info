"""
监听转发 · 把汐汐工具箱的实时事件转成 QQ 群消息
═══════════════════════════════════════════════════════════
工作方式
─────────────────────────────────────────────
汐汐工具箱开了一个转发专用的 WebSocket：`/bililive/api/relay`

  ws://<汐汐地址>/bililive/api/relay

这个通道和前端用的 /ws 有三点不同：
  ① **所有房间**的事件都推，不管前端当前在看哪个房间
  ② 只推四类：醒目留言 sc / 礼物 gift / 盲盒 blindbox / 舰长 guard
  ③ 首包发一份「当前监听房间列表」（type=hello）

插件这边做了什么
─────────────────────────────────────────────
  连接 → 收 hello 拿到房间列表 → 收 event 按「群 + 房间」转发

转发规则存在状态文件里：
    state.json
    {
      "relay": {
        "群ID:房间号": { "群ID": ..., "roomId": ..., "alias": "...", "addedAt": ... }
      }
    }

指令
─────────────────────────────────────────────
    /添加监听转发 直播间号     加监听 + 开始转发到这个群
    /撤销监听转发 直播间号     停止转发到这个群
    /监听转发列表              看当前都转发哪些

为什么指令叫「添加监听转发」而不是「订阅直播」
─────────────────────────────────────────────
两个功能有重叠但不完全一样：
  · /订阅直播      只加进汐汐的监听列表，不推送消息
  · /添加监听转发   加监听 + 往这个群推事件
拆成两个指令，用户想「只监听不推送」时不会被打扰。

消息格式
─────────────────────────────────────────────
和 astrbot_plugin_bilibili_live 保持一致：

    [礼物] Asaki大人(194484313)投喂了1个粉丝团灯牌(¥0.1)
    [醒目留言] 某某(123456)说: 谢谢主播(¥30)
    [盲盒] 某某(123456)投喂了1个幸运盲盒，开出 星光铃铛(¥5.2)
    [上舰] 某某(123456)成为了舰长
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from astrbot.api import logger

try:
    import aiohttp
except ImportError:  # pragma: no cover
    aiohttp = None  # type: ignore[assignment]


# ── 事件类型 → 显示标签 ──────────────────────────────────
KIND_LABEL = {
    "gift": "礼物",
    "sc": "醒目留言",
    "blindbox": "盲盒",
    "guard": "上舰",
}

# 只处理这四类
RELAY_KINDS = set(KIND_LABEL.keys())


def _fmt_money(v: Any) -> str:
    """金额格式化：30 → 30，5.2 → 5.2，0.1 → 0.1"""
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return "0"
    if f == int(f):
        return str(int(f))
    return f"{f:g}"


def _pick(d: dict, *keys, default=""):
    """按顺序取第一个非空字段"""
    for k in keys:
        v = d.get(k)
        if v not in (None, ""):
            return v
    return default


def format_event(ev: dict) -> str | None:
    """
    把一个汐汐事件格式化成群消息文本。
    返回 None 表示这类事件不转发。
    """
    kind = str(ev.get("kind") or "")
    if kind not in RELAY_KINDS:
        return None

    uname = str(_pick(ev, "uname") or "匿名")
    uid = ev.get("uid") or 0
    who = f"{uname}({uid})" if uid else uname

    if kind == "gift":
        """
        盲盒的礼物事件要跳过
        ─────────────────────────────────────────────
        开盲盒时汐汐会推两条事件：
          · kind='gift'      带 isBlind=true / blindBox 字段
          · kind='blindbox'  完整的开箱信息
        只转发后者，否则群里会出现两条盲盒消息。

        新版汐汐已经用 skipRelay 标记拦掉了，这里再兜一层，
        兼容旧版汐汐（没升级的话也只会收到 blindbox 那条）。
        """
        if ev.get("skipRelay") or ev.get("isBlind") or ev.get("blindBox"):
            return None

        act = str(_pick(ev, "action", default="投喂"))
        num = int(ev.get("num") or 1)
        name = str(_pick(ev, "giftName", "gift_name", default="礼物"))
        price = ev.get("price")
        coin = str(_pick(ev, "coinType", "coin_type", default="gold"))
        if coin == "silver" or not price:
            return f"[礼物] {who}{act}了{num}个{name}(免费)"
        return f"[礼物] {who}{act}了{num}个{name}(¥{_fmt_money(price)})"

    if kind == "sc":
        price = ev.get("price")
        text = str(_pick(ev, "text", "message", default=""))
        return f"[醒目留言] {who}说: {text}(¥{_fmt_money(price)})"

    if kind == "blindbox":
        box = str(_pick(ev, "boxName", default="盲盒"))
        gift = str(_pick(ev, "giftName", default="礼物"))
        value = ev.get("value")
        profit = ev.get("profit")
        cost = ev.get("cost")
        """
        盲盒消息格式：
            [盲盒] 小猪头奇犽(352034898)投喂了1个武器盲盒(¥9)，开出 鸭鸭装备(¥6)，亏3¥

        组成：投喂了 N 个 <盲盒名>(<花费>) ，开出 <礼物名>(<价值>) ，<赚|亏><盈亏>¥
        缺哪个字段就省略哪一段。
        """
        num = int(ev.get("num") or 1)

        # ① 投喂部分：盲盒名 + 花费
        cost_txt = f"(¥{_fmt_money(cost)})" if cost else ""
        head = f"投喂了{num}个{box}{cost_txt}"

        # ② 开出部分：礼物名 + 价值
        val_txt = f"(¥{_fmt_money(value)})" if value else ""
        out = f"，开出 {gift}{val_txt}" if gift else ""

        # ③ 盈亏部分
        tail = ""
        if profit is not None:
            try:
                p = float(profit)
                sign = "赚" if p >= 0 else "亏"
                tail = f"，{sign}{_fmt_money(abs(p))}¥"
            except (TypeError, ValueError):
                tail = ""

        return f"[盲盒] {who}{head}{out}{tail}"

    if kind == "guard":
        lv = str(_pick(ev, "levelName", "guardName", default="舰长"))
        # 上舰消息不带金额（用户要求：只显示谁成为了什么舰）
        return f"[上舰] {who}成为了{lv}"

    return None


class RelayForwarder:
    """
    维护一条到汐汐的 WebSocket，把事件按「群 + 房间」规则转发。

    astrbot 那边的推送接口由调用方注入（send_func(group_id, text)），
    这样这个类不直接依赖 AstrBot 的发送 API，好测也好替换。
    """

    def __init__(
        self,
        xixi_url: str,
        state_file: str,
        send_func,
        log=None,
        show_prefix: bool = True,
        gift_min_yuan: float = 0.0,
    ) -> None:
        self.xixi_url = xixi_url.rstrip("/")
        self.state_file = state_file
        self.send_func = send_func
        self.log = log or (lambda m, lv="info": logger.info(f"[bili_live] {m}"))
        # 多房间同群时加「【主播名】」前缀，方便区分
        self.show_prefix = bool(show_prefix)
        # 礼物最低转发金额（元），0 = 不限制
        self.gift_min_yuan = float(gift_min_yuan or 0)

        self._task: asyncio.Task | None = None
        self._ws = None
        self._running = False
        # 房间号 → 最近一次事件时间（避免重复转发同一条）
        self._last_seen: dict[str, float] = {}

        self.state = self._load()

    # ── 状态持久化 ─────────────────────────────────────
    def _load(self) -> dict:
        try:
            with open(self.state_file, encoding="utf-8") as f:
                j = json.load(f)
            if isinstance(j, dict):
                j.setdefault("relay", {})
                return j
        except Exception:  # noqa: BLE001
            pass
        return {"relay": {}}

    def _save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.state_file)
        except Exception as e:  # noqa: BLE001
            self.log(f"[转发] 保存状态失败: {e}", "warn")

    # ── 转发规则管理 ───────────────────────────────────
    @staticmethod
    def _key(group_id: str, room_id: int) -> str:
        return f"{group_id}:{int(room_id)}"

    def add_rule(self, group_id: str, room_id: int, alias: str = "", min_price: float = 0.0) -> bool:
        """添加转发规则。已存在返回 False。

        min_price：这条规则单独的最低礼物金额（元），0 = 用全局配置。
        """
        k = self._key(group_id, room_id)
        rules = self.state.setdefault("relay", {})
        if k in rules:
            return False
        rules[k] = {
            "group": str(group_id),
            "roomId": int(room_id),
            "alias": alias or "",
            "minPrice": float(min_price or 0),
            "addedAt": int(time.time() * 1000),   # 毫秒（前端直接 new Date 用）
        }
        self._save()
        return True

    def set_rule_min_price(self, group_id: str, room_id: int, min_price: float) -> bool:
        """修改某条规则的最低礼物金额。"""
        k = self._key(group_id, room_id)
        rules = self.state.setdefault("relay", {})
        if k not in rules:
            return False
        rules[k]["minPrice"] = float(min_price or 0)
        self._save()
        return True

    def remove_rule(self, group_id: str, room_id: int) -> bool:
        """删除转发规则。不存在返回 False。"""
        k = self._key(group_id, room_id)
        rules = self.state.setdefault("relay", {})
        if k not in rules:
            return False
        rules.pop(k, None)
        self._save()
        return True

    def list_rules(self, group_id: str | None = None) -> list[dict]:
        rules = self.state.get("relay") or {}
        out = [v for v in rules.values() if isinstance(v, dict)]
        if group_id is not None:
            out = [r for r in out if str(r.get("group")) == str(group_id)]
        out.sort(key=lambda r: (str(r.get("alias") or ""), int(r.get("roomId") or 0)))
        return out

    def rooms_for_group(self, group_id: str) -> set[int]:
        return {int(r.get("roomId") or 0) for r in self.list_rules(group_id)}

    def groups(self) -> list[str]:
        """所有配置了转发规则的群 ID（去重）"""
        out = []
        seen = set()
        for r in (self.state.get("relay") or {}).values():
            if not isinstance(r, dict):
                continue
            g = str(r.get("group") or "")
            if g and g not in seen:
                seen.add(g)
                out.append(g)
        return out

    async def room_meta(self) -> dict[int, dict]:
        """
        拉一次汐汐的房间列表，拿到「房间号 → 主播名 / 是否在播」。
        失败返回空 dict（不影响列表展示）。
        """
        try:
            import aiohttp as _aio

            url = self.xixi_url + "/bililive/api/rooms"
            tmo = _aio.ClientTimeout(total=8)
            async with _aio.ClientSession(timeout=tmo) as sess:
                async with sess.get(url) as resp:
                    j = await resp.json()
            out = {}
            for r in (j.get("rooms") or []):
                if not isinstance(r, dict):
                    continue
                rid = int(r.get("roomId") or 0)
                if not rid:
                    continue
                anchor_info = r.get("anchor") or {}
                out[rid] = {
                    "anchor": str(anchor_info.get("uname") or ""),
                    "face": str(anchor_info.get("face") or ""),
                    "live": anchor_info.get("liveStatus") == 1,
                    "state": str(r.get("state") or ""),
                }
            return out
        except Exception:  # noqa: BLE001
            return {}

    # ── WebSocket ─────────────────────────────────────
    @property
    def ws_url(self) -> str:
        """http(s) → ws(s)"""
        u = self.xixi_url
        if u.startswith("https://"):
            return "wss://" + u[len("https://"):] + "/bililive/api/relay"
        if u.startswith("http://"):
            return "ws://" + u[len("http://"):] + "/bililive/api/relay"
        return "ws://" + u + "/bililive/api/relay"

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self.log(f"[转发] 已启动 · {self.ws_url}")

    async def stop(self) -> None:
        self._running = False
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001
                pass
            self._ws = None
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        """断线重连循环"""
        backoff = 5
        while self._running:
            try:
                await self._connect_once()
                backoff = 5   # 正常断开 → 重置退避
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.log(f"[转发] 连接异常: {e}（{backoff}s 后重试）", "warn")
            if not self._running:
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def _connect_once(self) -> None:
        if aiohttp is None:
            self.log("[转发] 缺少 aiohttp 依赖，无法连接", "error")
            await asyncio.sleep(60)
            return

        timeout = aiohttp.ClientTimeout(total=None, sock_connect=10)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.ws_connect(self.ws_url, heartbeat=30) as ws:
                self._ws = ws
                self.log("[转发] 已连上汐汐，开始接收事件")
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            await self._on_message(json.loads(msg.data))
                        except Exception as e:  # noqa: BLE001
                            self.log(f"[转发] 处理消息失败: {e}", "warn")
                    elif msg.type in (
                        aiohttp.WSMsgType.CLOSED,
                        aiohttp.WSMsgType.CLOSE,
                        aiohttp.WSMsgType.ERROR,
                    ):
                        break
        self._ws = None
        if self._running:
            self.log("[转发] 与汐汐的连接断开，准备重连", "warn")

    async def _on_message(self, j: dict) -> None:
        t = j.get("type")
        if t == "hello":
            rooms = j.get("rooms") or []
            self.log(f"[转发] 汐汐当前监听 {len(rooms)} 个直播间")
            return
        if t != "event":
            return

        room_id = int(j.get("roomId") or 0)
        ev = j.get("data") or {}
        if not room_id or not isinstance(ev, dict):
            return

        kind = str(ev.get("kind") or "")
        if kind not in RELAY_KINDS:
            return

        # 去重：同一条事件可能因重连被补发
        sig = f"{room_id}:{kind}:{ev.get('ts')}:{ev.get('uid')}"
        now = time.time()
        if self._last_seen.get(sig):
            return
        self._last_seen[sig] = now
        # 清理超过 5 分钟的旧记录，避免无限增长
        if len(self._last_seen) > 500:
            cutoff = now - 300
            self._last_seen = {k: v for k, v in self._last_seen.items() if v > cutoff}



        text = format_event(ev)
        if not text:
            return

        # 礼物金额（元），用于按规则过滤
        gift_price = 0.0
        if kind == "gift":
            try:
                gift_price = float(ev.get("price") or 0)
            except (TypeError, ValueError):
                gift_price = 0.0

        # 找出要转发到哪些群（按每条规则自己的金额门槛过滤）
        rules = self.state.get("relay") or {}
        targets = []
        for r in rules.values():
            if not isinstance(r, dict) or int(r.get("roomId") or 0) != room_id:
                continue
            # 规则自己的门槛；没设过就用全局配置
            try:
                rule_min = float(r.get("minPrice") or 0)
            except (TypeError, ValueError):
                rule_min = 0.0
            eff_min = rule_min if rule_min > 0 else self.gift_min_yuan
            if kind == "gift" and eff_min > 0 and gift_price < eff_min:
                continue   # 这条规则不满足门槛，跳过
            targets.append(r)

        if not targets:
            # 没配规则 / 全被金额过滤掉 —— 不打日志（不然会刷屏）
            return
        self.log(f"[转发] 房间 {room_id} {kind} → {len(targets)} 个群: {text[:50]}")

        # 带上房间备注，多房间转发到同一个群时能区分
        alias = str((targets[0].get("alias") or "")).strip()
        prefix = f"【{alias}】" if (self.show_prefix and alias) else ""
        line = prefix + text

        for r in targets:
            gid = str(r.get("group") or "")
            if not gid:
                continue
            try:
                ok = await self.send_func(gid, text=line)
                if ok:
                    self.log(f"[转发] ✓ 已发送到群 {gid[:16]}…")
                else:
                    self.log(f"[转发] ✗ 发送到群 {gid[:16]}… 返回 False", "warn")
            except Exception as e:  # noqa: BLE001
                self.log(f"[转发] ✗ 发送到群 {gid[:16]}… 异常: {e}", "warn")
