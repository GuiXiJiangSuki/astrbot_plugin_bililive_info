"""AstrBot 插件：哔哩哔哩查成分 · 直播信息。

第一版功能：
    /查直播 <房间号>   查询直播间数据，渲染成看板图片

数据来源：汐汐工具箱（本地 HTTP API，默认 http://<NAS>:8800）
"""

from __future__ import annotations

import asyncio
import base64
import os
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star

try:
    from .live_board import render_live_board, render_sub_list
    from .relay import RelayForwarder, format_event
    from .xixi_client import LiveSnapshot, XixiClient
except ImportError:
    from live_board import render_live_board, render_sub_list  # type: ignore
    from relay import RelayForwarder, format_event  # type: ignore
    from xixi_client import LiveSnapshot, XixiClient  # type: ignore


class BiliLiveInfoPlugin(Star):
    """B站直播间数据看板。"""

    def __init__(self, context: Context, config: AstrBotConfig) -> None:
        super().__init__(context)
        self.astrbot_config = config

    # ───────── 配置 ─────────

    def _cfg(self, key: str, default: Any = None) -> Any:
        try:
            v = self.astrbot_config.get(key, default)
            return default if v is None else v
        except Exception:  # noqa: BLE001
            return default

    @property
    def xixi_url(self) -> str:
        return str(self._cfg("xl_xixi_url", "http://127.0.0.1:8800")).strip()

    @property
    def db_api_url(self) -> str:
        """SQLite 查询服务地址（可选）。

        不填则只用汐汐的 HTTP 接口（弹幕会被截断在 20000 条）。
        填了能拿到完整数据。
        """
        return str(self._cfg("xl_db_api_url", "http://127.0.0.1:8801")).strip()

    # ── 转发行为的固定值（这些配置项已从配置页面移除，避免误调）──

    @property
    def relay_enabled(self) -> bool:
        """是否启用监听转发（配置项 xl_relay_enabled）"""
        return bool(self._cfg("xl_relay_enabled", True))

    @property
    def relay_prefix(self) -> bool:
        """转发消息带「【主播名】」前缀（多房间同群时区分来源）"""
        return True

    @property
    def relay_gift_min(self) -> float:
        """
        全局礼物金额门槛。
        配置项已移除，固定 0（不限制）——
        金额过滤改成「每条监听规则单独设置」（管理页面里的 ≥¥ 输入框）。
        """
        return 0.0


    @property
    def show_avatar(self) -> bool:
        return bool(self._cfg("xl_show_avatar", True))

    @property
    def data_dir(self) -> str:
        """
        插件数据目录：<AstrBot>/data/plugin_data/astrbot_plugin_bililive_info/

        规范要求持久化数据必须写这里（不能写插件源码目录），
        否则插件升级/重装会丢数据，过审核也会被驳回。

        兼容旧目录：
            插件从 astrbot_plugin_bili_live_info 改名为
            astrbot_plugin_bililive_info，老用户的数据还在旧目录里。
            这里做一次「首次启动迁移」，把旧目录的内容搬过来，
            迁移成功后旧目录改名加 .migrated 后缀（保留以便回滚）。
        """
        here = os.path.dirname(os.path.abspath(__file__))
        # here = .../data/plugins/astrbot_plugin_bililive_info
        data_root = os.path.dirname(os.path.dirname(here))   # → .../data

        new_dir = os.path.join(data_root, "plugin_data", "astrbot_plugin_bililive_info")
        old_dir = os.path.join(data_root, "plugin_data", "astrbot_plugin_bili_live_info")

        try:
            os.makedirs(new_dir, exist_ok=True)
        except Exception:  # noqa: BLE001
            pass

        # 旧目录存在且新目录没数据 → 迁移
        try:
            if os.path.isdir(old_dir):
                new_has_data = any(os.scandir(new_dir))
                if not new_has_data:
                    import shutil
                    for item in os.listdir(old_dir):
                        src = os.path.join(old_dir, item)
                        dst = os.path.join(new_dir, item)
                        if os.path.isdir(src):
                            shutil.copytree(src, dst, dirs_exist_ok=True)
                        else:
                            shutil.copy2(src, dst)
                    logger.info(f"[bili_live] 已从旧数据目录迁移: {old_dir}")
                    # 改名标记，避免下次重复迁移（失败也不影响）
                    try:
                        os.rename(old_dir, old_dir + ".migrated")
                    except Exception:  # noqa: BLE001
                        pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] 旧数据迁移失败（不影响使用）: {exc}")

        return new_dir

    async def initialize(self) -> None:
        logger.info(f"[bili_live] 已启动 · 汐汐={self.xixi_url}")
        # 启动监听转发（连汐汐的 /bililive/api/relay，收四类事件推群）
        self.relay = None
        if not self.relay_enabled:
            logger.info("[bili_live] 监听转发已关闭（配置 xl_relay_enabled = false）")
            return
        try:
            self.relay = RelayForwarder(
                xixi_url=self.xixi_url,
                state_file=os.path.join(self.data_dir, "state.json"),
                send_func=self._relay_send,
                log=lambda m, lv="info": logger.info(f"[bili_live] {m}"),
                show_prefix=self.relay_prefix,
                gift_min_yuan=self.relay_gift_min,
            )
            self.relay.start()
        except Exception as exc:  # noqa: BLE001
            self.relay = None
            logger.warning(f"[bili_live] 转发模块启动失败: {exc}")

        # 注册管理页面用的 API（转发列表 / 删除规则）
        try:
            self.context.register_web_api(
                "/astrbot_plugin_bililive_info/relay-list",
                self.web_relay_list,
                ["GET"],
                "监听转发列表",
            )
            self.context.register_web_api(
                "/astrbot_plugin_bililive_info/relay-delete",
                self.web_relay_delete,
                ["POST"],
                "删除转发规则",
            )
            self.context.register_web_api(
                "/astrbot_plugin_bililive_info/relay-min-price",
                self.web_relay_min_price,
                ["POST"],
                "修改转发金额门槛",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] 注册管理页面 API 失败: {exc}")

    async def _read_body(self) -> dict:
        """
        读插件 Web API 的请求体。

        AstrBot 4.29 里 handler 只收到 URL 路径参数（**path_values），
        请求体要通过 astrbot.api.web.request（模块级单例）拿。

        这里把每一步的结果都打进日志，方便排查「请求体读不到」的问题。
        """
        data = {}
        try:
            from astrbot.api.web import request  # type: ignore
            logger.info(f"[bili_live] _read_body 开始 · method={getattr(request, 'method', '?')} "
                        f"ctype={getattr(request, 'content_type', '?')}")
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] 导入 request 失败: {exc}")
            return data

        # ① JSON
        try:
            j = await request.json(default=None)
            logger.info(f"[bili_live] request.json() → {type(j).__name__} {str(j)[:200]}")
            if isinstance(j, dict) and j:
                data.update(j)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] request.json() 失败: {exc}")

        # ② 原始 body（json 失败时用）
        if not data:
            try:
                raw = await request.body()
                logger.info(f"[bili_live] request.body() → {len(raw) if raw else 0} bytes: {raw[:200] if raw else b''}")
                if raw:
                    import json as _json
                    try:
                        j2 = _json.loads(raw)
                        if isinstance(j2, dict) and j2:
                            data.update(j2)
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_live] request.body() 失败: {exc}")

        # ③ 表单
        if not data:
            try:
                form = await request.form()
                if form:
                    data.update(dict(form))
                    logger.info(f"[bili_live] request.form() → {dict(form)}")
            except Exception:  # noqa: BLE001
                pass

        # ④ query
        if not data:
            try:
                q = request.query
                if q:
                    data.update({k: q[k] for k in q})
                    logger.info(f"[bili_live] request.query → {dict(data)}")
            except Exception:  # noqa: BLE001
                pass

        logger.info(f"[bili_live] _read_body 结果: {data}")
        return data

    # ───────── 管理页面 API ─────────

    async def web_relay_list(self, *args, **kwargs):
        """
        GET /astrbot_plugin_bililive_info/relay-list

        给管理页面用：返回所有转发规则 + 房间元信息。

        返回：
            {
              "success": true,
              "groups": [
                {
                  "group": "A13C97...",
                  "rooms": [
                    {"roomId": 52030, "alias": "阿萨Aza", "anchor": "...", "live": true,
                     "state": "open", "addedAt": 1795278771}
                  ]
                }
              ],
              "total": 1
            }
        """
        if not getattr(self, "relay", None):
            return {"success": False, "message": "转发模块未启用（请检查配置里的「启用监听转发」开关）"}

        try:
            meta = await self.relay.room_meta()
        except Exception:  # noqa: BLE001
            meta = {}

        by_group: dict[str, list] = {}
        for r in self.relay.list_rules():
            gid = str(r.get("group") or "")
            rid = int(r.get("roomId") or 0)
            m = meta.get(rid) or {}
            try:
                mp = float(r.get("minPrice") or 0)
            except (TypeError, ValueError):
                mp = 0.0
            item = {
                "roomId": rid,
                "alias": str(r.get("alias") or ""),
                "anchor": str(m.get("anchor") or ""),
                "face": str(m.get("face") or ""),
                "live": bool(m.get("live")),
                "state": str(m.get("state") or ""),
                "minPrice": mp,
                "addedAt": int(r.get("addedAt") or 0),
            }
            by_group.setdefault(gid, []).append(item)

        groups = []
        for gid, rooms in by_group.items():
            rooms.sort(key=lambda x: (not x["live"], x["alias"] or str(x["roomId"])))
            groups.append({"group": gid, "rooms": rooms})

        return {
            "success": True,
            "groups": groups,
            "total": sum(len(g["rooms"]) for g in groups),
            "prefix": self.relay_prefix,
            "giftMin": self.relay_gift_min,
        }

    async def web_relay_min_price(self, **kwargs):
        """
        POST /astrbot_plugin_bililive_info/relay-min-price

        改某条规则的最低礼物金额。
        请求体：{"group": "...", "roomId": 52030, "minPrice": 1}
        minPrice = 0 表示不限制（用全局配置）

        ⚠️ AstrBot 4.29 的 web api handler **只拿到 URL 路径参数**，
        请求体必须通过 PluginRequest 读，不能靠函数形参。
        """
        if not getattr(self, "relay", None):
            return {"success": False, "message": "转发模块未启用"}

        data = await self._read_body()

        gid = str(data.get("group") or "")
        try:
            rid = int(data.get("roomId") or 0)
            mp = float(data.get("minPrice") or 0)
        except (TypeError, ValueError):
            return {"success": False, "message": "参数格式错误"}

        if not gid or not rid:
            return {"success": False, "message": "缺少 group 或 roomId"}
        if mp < 0:
            return {"success": False, "message": "金额不能为负数"}

        ok = self.relay.set_rule_min_price(gid, rid, mp)
        logger.info(f"[bili_live] 改金额门槛 group={gid[:16]}… room={rid} minPrice={mp} ok={ok}")
        return {"success": ok, "message": "已保存" if ok else "规则不存在"}

    async def web_relay_delete(self, **kwargs):
        """
        POST /astrbot_plugin_bililive_info/relay-delete

        删除一条转发规则。
        请求体：{"group": "...", "roomId": 52030}
        """
        if not getattr(self, "relay", None):
            return {"success": False, "message": "转发模块未启用"}

        data = await self._read_body()

        gid = str(data.get("group") or "")
        try:
            rid = int(data.get("roomId") or 0)
        except (TypeError, ValueError):
            rid = 0

        if not gid or not rid:
            return {"success": False, "message": "缺少 group 或 roomId"}

        ok = self.relay.remove_rule(gid, rid)
        logger.info(f"[bili_live] 删除转发规则 group={gid[:16]}… room={rid} ok={ok}")
        return {"success": ok, "message": "已删除" if ok else "规则不存在"}

    async def terminate(self) -> None:
        """插件卸载/重载时断开转发连接"""
        try:
            if getattr(self, "relay", None):
                await self.relay.stop()
        except Exception:  # noqa: BLE001
            pass

    # ───────── 主动推送到群 ─────────

    async def _send_official_direct(self, platform, group_openid: str, text: str) -> bool:
        """
        直接用 botpy 客户端发群消息，绕过 AstrBot 的 scene 缓存。

        背景
        ────
        AstrBot 的 send_by_session 在群会话里会做：
            session.session_id.rsplit("_", 1)[-1]
        但如果 session_id 里带下划线（群 openid 本身不含，是我们拼错了格式），
        截断后查不到 _session_scene → 判定为非 group →
            [QQOfficial] No cached msg_id for session ... skip send_by_session
        消息被静默丢弃（不抛异常，最坑）。

        这里直接调 client.api.post_group_message，不依赖任何缓存。
        """
        import random as _random

        client = getattr(platform, "client", None) or getattr(platform, "_client", None)
        if client is None:
            return False
        api = getattr(client, "api", None)
        if api is None or not hasattr(api, "post_group_message"):
            return False

        try:
            ret = await api.post_group_message(
                group_openid=group_openid,
                content=text,
                msg_type=0,
                msg_seq=_random.randint(1, 100000),
            )
            ret_id = getattr(ret, "id", None) or (ret.get("id") if isinstance(ret, dict) else None)
            logger.info(f"[bili_live] 直连发送成功 group={group_openid[:20]} id={ret_id}")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] 直连发送失败: {exc}")
            return False

    async def _relay_send(self, group_id: str, text: str) -> bool:
        """
        把一条文本发到指定群。

        AstrBot 4.29 的两种平台发送方式：
          · qq_official → 优先直连 botpy（绕过 scene 缓存），失败再退回 send_by_session
          · aiocqhttp   → context.send_message("{meta.id}:GroupMessage:{群号}")
        """
        from astrbot.core.message.message_event_result import MessageChain

        chain = MessageChain(chain=[Plain(text)])
        gid = str(group_id)
        # 注意：group_id 本身就是纯群 openid（如 A13C979D...），**不要**再切下划线。
        # unified_msg_origin 里的 "用户openid_群openid" 复合串是另一回事，
        # AstrBot 的 send_by_session 内部会 rsplit("_",1)[-1] 取群部分。

        OFFICIAL = ("qq_official", "qqbot", "qq_official_webhook")

        # ── QQ 官方机器人 ──
        for platform in self.context.platform_manager.platform_insts:
            try:
                meta = platform.meta()
            except Exception:  # noqa: BLE001
                continue
            if meta.name not in OFFICIAL:
                continue

            # ① 优先直连 botpy（AstrBot 的 send_by_session 会因缓存缺失静默丢消息）
            if await self._send_official_direct(platform, gid, text):
                return True

            # ② 退回官方 send_by_session
            try:
                from astrbot.core.platform.sources.qqofficial.qqofficial_platform_adapter import (  # noqa: E501
                    MessageSesion,
                    MessageType,
                )
                session = MessageSesion(meta.id, MessageType.GROUP_MESSAGE, gid)
                await platform.send_by_session(session, chain)
                return True
            except ImportError:
                pass
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[bili_live] send_by_session 失败: {exc}")

        # ── OneBot / 其它平台 ──
        try:
            origin = None
            for platform in self.context.platform_manager.platform_insts:
                try:
                    meta = platform.meta()
                except Exception:  # noqa: BLE001
                    continue
                origin = f"{meta.id}:GroupMessage:{gid}"
                break
            if not origin:
                logger.warning("[bili_live] 找不到可用平台，无法发群消息")
                return False
            await self.context.send_message(origin, chain)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] 发送到群 {gid} 失败: {exc}")
            return False

    @filter.command("查直播")
    async def cmd_live(self, event: AstrMessageEvent):
        """查询 B 站直播间数据看板。

        用法：
            /查直播 6154037              房间号
            /查直播 Asaki大人            主播名字（自动搜索）
            /查直播 194484313            主播 UID
            /查直播 --uid 194484313      强制按 UID
            /查直播 --name Asaki大人     强制按名字
        """
        raw = (event.message_str or "").strip()
        # 去掉首行的命令前缀，取剩余参数（支持名字带空格）
        body = raw
        if raw.startswith("/"):
            body = raw.split(None, 1)[1] if " " in raw else ""
        elif " " in raw:
            body = raw.split(None, 1)[1]
        else:
            body = ""

        body = body.strip()
        if not body:
            yield event.plain_result(
                "用法：\n"
                "  /查直播 <房间号>        例：/查直播 6154037\n"
                "  /查直播 <主播名字>      例：/查直播 Asaki大人\n"
                "  /查直播 <主播UID>       例：/查直播 194484313\n"
                "  /查直播 --name <名字>   强制按名字搜索\n"
                "  /查直播 --uid <UID>     强制按 UID 解析"
            )
            return

        # 解析 --name / --uid 前缀
        force_name = False
        force_uid = False
        for tag in ("--name", "-name", "--用户名", "-用户名"):
            if body.lower().startswith(tag.lower()):
                force_name = True
                body = body[len(tag):].strip()
                break
        if not force_name:
            for tag in ("--uid", "-uid", "--id", "-id"):
                if body.lower().startswith(tag.lower()):
                    force_uid = True
                    body = body[len(tag):].strip()
                    break

        if not body:
            yield event.plain_result("参数为空，请提供房间号 / 主播名字 / UID")
            return

        client = XixiClient(self.xixi_url, db_api_url=self.db_api_url)
        rooms = await client.get_rooms()
        tracked_rooms = {
            int(r.get("roomId") or 0) for r in rooms if isinstance(r, dict)
        }
        tracked_uids: dict[int, int] = {}
        tracked_names: dict[str, int] = {}
        for r in rooms:
            if not isinstance(r, dict):
                continue
            a = r.get("anchor") or {}
            try:
                u = int(a.get("uid") or 0)
                rid = int(r.get("roomId") or 0)
                if u and rid:
                    tracked_uids[u] = rid
                nm = str(a.get("uname") or "").strip().lower()
                if nm and rid:
                    tracked_names[nm] = rid
            except (TypeError, ValueError):
                continue

        room_id = 0
        display = ""

        # ── 情况 1：纯数字 ──
        if body.isdigit():
            n = int(body)
            if force_name:
                # 非数字才能按名字，这里退回搜索
                yield event.plain_result("--name 后面需要跟主播名字，不是数字")
                return

            if force_uid:
                rid = tracked_uids.get(n, 0) or await client.resolve_uid_to_room(n)
                if not rid:
                    yield event.plain_result(
                        f"❌ 找不到 UID {n} 对应的直播间。\n"
                        "可能 UID 填错，或该主播未开通直播间。"
                    )
                    return
                room_id = rid
                display = f"UID {n}"
            elif n in tracked_rooms:
                room_id = n
                display = f"房间 {n}"
            else:
                # 先试 UID，再试房间号
                rid = tracked_uids.get(n, 0)
                if not rid:
                    rid = await client.resolve_uid_to_room(n)
                if rid:
                    room_id = rid
                    display = f"UID {n}"
                else:
                    # 当作房间号处理（会在下面提示未跟踪）
                    room_id = n
                    display = f"房间 {n}"

        # ── 情况 2：名字 ──
        else:
            name = body
            # 不做「正在按名字查找」提示 —— 用户不需要知道中间步骤，
            # 只保留最后「正在拉取 xxx 的数据…」，避免连续两条消息刷屏。

            # 2.1 本地已跟踪房间精确/模糊匹配
            rid, uname = await client.find_local_by_name(name)
            if rid:
                room_id = rid
                display = uname or name
            else:
                # 2.2 B 站搜索
                rid2, uname2, uid2 = await client.search_anchor_by_name(name)
                if rid2:
                    room_id = rid2
                    display = uname2 or name
                else:
                    yield event.plain_result(
                        f"❌ 没找到主播「{name}」。\n\n"
                        "可能原因：\n"
                        "· 名字拼写不对（注意繁简、大小写）\n"
                        "· B站搜索被限流（稍后重试）\n\n"
                        "建议改用房间号：/查直播 6154037"
                    )
                    return

        if not room_id:
            yield event.plain_result(
                f"❌ 没能定位到「{body}」对应的直播间。\n"
                "请确认名字/房间号/UID 是否正确。"
            )
            return

        # ── 在发提示前先解析出主播名，提示语里带上 ──
        anchor_name = await self._peek_anchor_name(client, room_id, rooms)
        if anchor_name:
            tip_text = f"正在拉取 {anchor_name}「房间号 {room_id}」的数据…"
        else:
            tip_text = f"正在拉取「房间号 {room_id}」的数据…"
        yield event.plain_result(tip_text)

        snap = await client.snapshot(room_id)
        if snap is None:
            """
            房间没被汐汐监听 → 不出图，只回文字提示。
            ─────────────────────────────────────────────
            原因：没监听就没有弹幕/礼物/SC/舰长数据，
            画出来的图全是 0，反而误导人（实测全是空柱子）。
            """
            logger.info(f"[bili_live] 房间 {room_id} 未监听，只回文字提示")
            name = await self._peek_anchor_name(client, room_id, rooms)
            who = f"「{name}」" if name else ""
            yield event.plain_result(
                f"ℹ️ 房间 {room_id} {who}尚未被汐汐监听，无法提供统计数据。\n\n"
                f"如需弹幕 / 礼物 / SC / 舰长 等实时统计，请先发：\n"
                f"/订阅直播 {room_id}\n\n"
                "订阅后汐汐会自动连接该直播间，之后就能查了。"
            )
            return

        # 拉头像
        avatar = None
        if self.show_avatar and snap.face:
            try:
                avatar = await self._fetch_bytes(snap.face)
            except Exception:  # noqa: BLE001
                avatar = None

        try:
            png = render_live_board(snap, avatar_data=avatar)
            yield event.chain_result(
                [Image.fromBase64(base64.b64encode(png).decode("ascii"))]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] 渲染失败: {exc}", exc_info=True)
            yield event.plain_result(
                f"【直播数据】{snap.uname}（房间 {snap.room_id}）\n"
                f"状态：{snap.state or ('直播中' if snap.is_live_now else '未开播')}　人气：{snap.online}\n"
                f"弹幕 {snap.danmaku}　进场 {snap.enter}　礼物 {snap.gift}\n"
                f"SC {snap.sc}（¥{snap.sc_total_yuan:.0f}）　舰长 {snap.guards_count}\n"
                f"观众 {snap.users}"
            )

    @filter.command("订阅列表")
    async def cmd_sub_list(self, event: AstrMessageEvent):
        """查看已订阅（已监听）的主播列表，渲染成图片。

        用法：
            /订阅列表
        """
        client = XixiClient(self.xixi_url, db_api_url=self.db_api_url)
        try:
            rooms = await client.get_rooms()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] 查订阅列表失败: {exc}")
            yield event.plain_result("⚠️ 查询失败，请确认汐汐工具箱正在运行。")
            return

        if not rooms:
            yield event.plain_result(
                "📋 当前还没有订阅任何主播。\n\n"
                "发送以下命令可以添加：\n"
                "  /订阅直播 <房间号>\n"
                "  /订阅直播 <主播名字>\n"
                "  /订阅直播 <主播UID>"
            )
            return

        # 整理数据（get_rooms 兼容新旧结构）
        items = []
        for r in rooms:
            if not isinstance(r, dict):
                continue
            a = r.get("anchor") or {}
            room_id = int(r.get("roomId") or 0)
            items.append({
                "roomId": room_id,
                "uname": str(a.get("uname") or r.get("uname") or r.get("alias") or f"房间 {room_id}"),
                "face": str(a.get("face") or r.get("face") or ""),
                "alias": str(r.get("alias") or ""),
                "live": (
                    int(a.get("liveStatus") or 0) == 1
                    if "liveStatus" in a
                    else bool(r.get("live"))
                ),
                "online": int(a.get("online") or r.get("online") or 0),
                "title": str(a.get("title") or r.get("title") or ""),
                "lastStartText": str(r.get("lastStartText") or ""),
                "lastEndText": str(r.get("lastEndText") or ""),
                "lastDurationText": str(r.get("lastDurationText") or ""),
            })

        # 按「在播优先、人气降序」排
        items.sort(key=lambda x: (not x["live"], -x["online"]))

        # 并发下载头像（最多 12 个，避免太慢）
        avatars = {}
        face_list = [(it["roomId"], it["face"]) for it in items if it.get("face")][:12]
        if face_list:
            async def _one(rid, url):
                try:
                    b = await self._fetch_bytes(url)
                    if b:
                        avatars[rid] = b
                except Exception:  # noqa: BLE001
                    pass
            await asyncio.gather(*[_one(rid, u) for rid, u in face_list])

        try:
            png = render_sub_list(items, avatars=avatars)
            yield event.chain_result(
                [Image.fromBase64(base64.b64encode(png).decode("ascii"))]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] 订阅列表渲染失败: {exc}", exc_info=True)
            # 渲染失败退回文字
            living = [x for x in items if x["live"]]
            offline = [x for x in items if not x["live"]]
            lines = [f"📋 已订阅 {len(items)} 个主播：", ""]
            if living:
                lines.append(f"🔴 正在直播（{len(living)}）：")
                for it in living:
                    lines.append(f"  · {it['uname']}（{it['roomId']}）人气 {self._fmt_num(it['online'])}")
                lines.append("")
            if offline:
                lines.append(f"⚪ 未开播（{len(offline)}）：")
                for it in offline:
                    lines.append(f"  · {it['uname']}（{it['roomId']}）")
            yield event.plain_result("\n".join(lines).strip())

    @filter.command("订阅直播")
    async def cmd_subscribe(self, event: AstrMessageEvent):
        """把主播添加进汐汐工具箱的监听列表。

        用法：
            /订阅直播 6154037
            /订阅直播 Asaki大人
            /订阅直播 194484313
        """
        body = self._strip_cmd(event.message_str)
        if not body:
            yield event.plain_result(
                "用法：\n"
                "  /订阅直播 <房间号>    例：/订阅直播 6154037\n"
                "  /订阅直播 <主播名字>  例：/订阅直播 Asaki大人\n"
                "  /订阅直播 <主播UID>   例：/订阅直播 194484313\n\n"
                "添加后汐汐会持续监听该主播，/查直播 才有实时数据。"
            )
            return

        client = XixiClient(self.xixi_url, db_api_url=self.db_api_url)
        room_id, display = await self._resolve_target(client, body)
        if not room_id:
            yield event.plain_result(
                f"❌ 没能定位到「{body}」对应的直播间。\n"
                "请确认名字/房间号/UID 是否正确。"
            )
            return

        # 已经在列表里？
        rooms = await client.get_rooms()
        tracked = {int(r.get("roomId") or 0) for r in rooms if isinstance(r, dict)}
        if room_id in tracked:
            yield event.plain_result(
                f"ℹ️ 「{display}」（房间 {room_id}）已经在监听列表里了。\n"
                "如果查不到数据，可能需要到汐汐工具箱里切换到该房间。"
            )
            return

        who = display or f"房间 {room_id}"
        yield event.plain_result(
            f"正在把「{who}」（{room_id}）加入汐汐监听…"
        )

        # alias 只在有真实名字时填（避免把「房间 123」写出别名）
        ok, err = await client.add_room(room_id, alias=display or "")
        if not ok:
            yield event.plain_result(
                f"❌ 添加失败：{err}\n\n"
                "可能原因：\n"
                "· 汐汐工具箱未运行\n"
                "· 该房间已存在（但状态异常）\n"
                "· 房间号无效"
            )
            return

        after = await client.get_rooms()
        cnt = len(after)
        yield event.plain_result(
            f"✅ 已添加「{display or f'房间 {room_id}'}」（{room_id}）到汐汐监听列表。\n"
            f"当前共 {cnt} 个直播间被监听。\n\n"
            f"汐汐会自动连接该房间，稍等几秒后就可以用：\n"
            f"/查直播 {room_id}"
        )

    # ───────── 监听转发 ─────────

    def _group_id_of(self, event: AstrMessageEvent) -> str:
        """
        从事件里取群 openid；私聊返回空串（转发只支持群）。

        QQ 官方机器人的群标识是加密的 group_openid，
        这里把几个可能的来源都打出来，方便排查「发错群」的问题。
        """
        cand = {}
        try:
            cand["get_group_id"] = event.get_group_id() if hasattr(event, "get_group_id") else ""
        except Exception as e:  # noqa: BLE001
            cand["get_group_id"] = f"<err {e}>"
        try:
            cand["message_obj.group_id"] = getattr(event.message_obj, "group_id", "")
        except Exception:  # noqa: BLE001
            pass
        try:
            cand["unified_msg_origin"] = getattr(event, "unified_msg_origin", "")
        except Exception:  # noqa: BLE001
            pass
        try:
            cand["session_id"] = getattr(event.message_obj, "session_id", "")
        except Exception:  # noqa: BLE001
            pass
        try:
            cand["self_id"] = getattr(event.message_obj, "self_id", "")
        except Exception:  # noqa: BLE001
            pass

        logger.info(f"[bili_live] 群标识候选: {cand}")

        gid = cand.get("get_group_id") or cand.get("message_obj.group_id") or ""
        if not gid:
            # 退化：从 unified_msg_origin 里剥（格式 platform:GroupMessage:xxx）
            u = str(cand.get("unified_msg_origin") or "")
            if ":GroupMessage:" in u:
                gid = u.split(":GroupMessage:", 1)[1]
        return str(gid or "")

    @filter.command("添加监听转发")
    async def cmd_add_relay(self, event: AstrMessageEvent):
        """添加直播间监听，并把礼物/SC/盲盒/上舰 事件转发到这个群。

        用法：
            /添加监听转发 6154037
            /添加监听转发 Asaki大人
            /添加监听转发 194484313
        """
        gid = self._group_id_of(event)
        if not gid:
            yield event.plain_result("❌ 这个指令只能在群里用（转发目标是当前群）。")
            return

        if not getattr(self, "relay", None):
            yield event.plain_result("❌ 转发模块未启用，请先检查插件配置和日志。")
            return

        body = self._strip_cmd(event.message_str)
        if not body:
            yield event.plain_result(
                "用法：\n"
                "  /添加监听转发 <房间号>    例：/添加监听转发 6154037\n"
                "  /添加监听转发 <主播名字>  例：/添加监听转发 Asaki大人\n"
                "  /添加监听转发 <主播UID>   例：/添加监听转发 194484313\n\n"
                "效果：把该直播间的 礼物 / 醒目留言 / 盲盒 / 上舰 实时转发到本群。\n"
                "如果汐汐还没监听这个房间，会自动加进去。"
            )
            return

        client = XixiClient(self.xixi_url, db_api_url=self.db_api_url)
        room_id, display = await self._resolve_target(client, body)
        if not room_id:
            yield event.plain_result(
                f"❌ 没能定位到「{body}」对应的直播间。\n"
                "请确认名字/房间号/UID 是否正确。"
            )
            return

        # ① 确保汐汐在监听这个房间（没监听就加上）
        added = False
        try:
            rooms = await client.get_rooms()
            tracked = {int(r.get("roomId") or 0) for r in rooms if isinstance(r, dict)}
            if room_id not in tracked:
                ok, err = await client.add_room(room_id, alias=display or "")
                if ok:
                    added = True
                else:
                    yield event.plain_result(
                        f"⚠️ 定位到了「{display or room_id}」，但加进汐汐监听失败：{err}\n"
                        "转发规则仍然会保存，等汐汐恢复后自动生效。"
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[bili_live] 检查监听列表失败: {exc}")

        # ② 保存转发规则
        # 新规则默认用全局门槛（0 = 不限制），之后可以在管理页面单独调
        ok_rule = self.relay.add_rule(
            gid, room_id, alias=display or "", min_price=self.relay_gift_min,
        )
        if not ok_rule:
            yield event.plain_result(
                f"ℹ️ 本群已经在转发「{display or f'房间 {room_id}'}」（{room_id}）了。"
            )
            return

        lines = [
            f"✅ 已开启转发：{display or f'房间 {room_id}'}（{room_id}）→ 本群",
            "",
            "转发内容：",
            "  · 礼物        [礼物] 昵称(uid)投喂了1个xxx(¥0.1)",
            "  · 醒目留言    [醒目留言] 昵称(uid)说: ...(¥30)",
            "  · 盲盒        [盲盒] 昵称(uid)投喂了1个xxx，开出 xxx(¥5.2)",
            "  · 上舰        [上舰] 昵称(uid)成为了舰长",
        ]
        if added:
            lines.append("")
            lines.append(f"（已自动把该房间加入汐汐监听，正在连接…）")
        lines += [
            "",
            f"停止转发：/撤销监听转发 {room_id}",
            "查看全部：/监听转发列表",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("测试转发")
    async def cmd_test_relay(self, event: AstrMessageEvent):
        """往当前群发一条测试消息，确认转发链路是否通。"""
        gid = self._group_id_of(event)
        if not gid:
            yield event.plain_result("❌ 这个指令只能在群里用。")
            return

        if not getattr(self, "relay", None):
            yield event.plain_result("❌ 转发模块未启用。")
            return

        text = "[测试] 如果你看到这条消息，说明主动推送正常"
        try:
            ok = await self._relay_send(gid, text)
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"❌ 发送异常：{exc}")
            return

        if ok:
            yield event.plain_result(
                f"✅ 已调用发送接口（群 {gid[:16]}…）\n"
                "如果群里看不到这条测试消息，说明是 QQ 平台侧的限制。"
            )
        else:
            yield event.plain_result("❌ 发送接口返回失败，看日志里的具体错误。")

    @filter.command("撤销监听转发")
    async def cmd_del_relay(self, event: AstrMessageEvent):
        """停止把某个直播间的事件转发到这个群。

        用法：
            /撤销监听转发 6154037
        """
        gid = self._group_id_of(event)
        if not gid:
            yield event.plain_result("❌ 这个指令只能在群里用。")
            return

        if not getattr(self, "relay", None):
            yield event.plain_result("❌ 转发模块未启用。")
            return

        body = self._strip_cmd(event.message_str)
        if not body:
            yield event.plain_result(
                "用法：\n"
                "  /撤销监听转发 <房间号>    例：/撤销监听转发 6154037\n\n"
                "只停止转发，直播间仍然在汐汐的监听列表里。\n"
                "想彻底取消监听：/订阅列表 里看房间号后到工具箱里移除。"
            )
            return

        client = XixiClient(self.xixi_url, db_api_url=self.db_api_url)
        room_id, display = await self._resolve_target(client, body)
        if not room_id:
            yield event.plain_result(f"❌ 没能定位到「{body}」对应的直播间。")
            return

        if not self.relay.remove_rule(gid, room_id):
            yield event.plain_result(
                f"ℹ️ 本群本来就没有转发「{display or f'房间 {room_id}'}」（{room_id}）。"
            )
            return

        yield event.plain_result(
            f"✅ 已停止转发「{display or f'房间 {room_id}'}」（{room_id}）到本群。\n\n"
            "该直播间仍在汐汐监听列表里（/查直播 还能用）。"
        )

    @filter.command("监听转发列表")
    async def cmd_relay_list(self, event: AstrMessageEvent):
        """查看当前群里都转发哪些直播间。"""
        gid = self._group_id_of(event)
        if not getattr(self, "relay", None):
            yield event.plain_result("❌ 转发模块未启用。")
            return

        rules = self.relay.list_rules(gid if gid else None)
        if not rules:
            where = "本群" if gid else "任何群"
            yield event.plain_result(
                f"📋 {where}还没有配置监听转发。\n\n"
                "添加：/添加监听转发 <房间号>\n"
                "例：  /添加监听转发 6154037"
            )
            return

        lines = [f"📋 当前转发规则（{len(rules)} 条）：", ""]
        for r in rules:
            rid = r.get("roomId")
            alias = str(r.get("alias") or "").strip()
            name = alias or f"房间 {rid}"
            grp = r.get("group")
            grp_txt = "" if gid else f"  → 群 {grp}"
            lines.append(f"  · {name}（{rid}）{grp_txt}")
        lines += [
            "",
            "移除：/撤销监听转发 <房间号>",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("查成分")
    async def cmd_profile(self, event: AstrMessageEvent):
        """占位：后续做 UP 主/观众成分分析。"""
        yield event.plain_result(
            "「查成分」功能开发中。\n"
            "目前可用：\n"
            "  /查直播 <房间号|名字|UID>\n"
            "  /订阅直播 <房间号|名字|UID>"
        )

    # ───────── 工具 ─────────

    @staticmethod
    def _fmt_num(n) -> str:
        """数字转易读格式：1748125 -> 174.8万，1234 -> 1234。"""
        try:
            v = float(n or 0)
        except (TypeError, ValueError):
            return "0"
        if v >= 1e8:
            return f"{v / 1e8:.1f}".rstrip("0").rstrip(".") + "亿"
        if v >= 1e4:
            return f"{v / 1e4:.1f}".rstrip("0").rstrip(".") + "万"
        return f"{int(v)}"

    def _strip_cmd(self, message_str: str) -> str:
        """去掉命令前缀，返回剩余参数。"""
        raw = (message_str or "").strip()
        if raw.startswith("/"):
            return raw.split(None, 1)[1].strip() if " " in raw else ""
        if " " in raw:
            return raw.split(None, 1)[1].strip()
        return raw

    async def _resolve_target(self, client: XixiClient, body: str) -> tuple[int, str]:
        """把 房间号/名字/UID 统一解析成 (room_id, 显示名)。"""
        rooms = await client.get_rooms()
        tracked_rooms = {
            int(r.get("roomId") or 0) for r in rooms if isinstance(r, dict)
        }
        tracked_uids: dict[int, int] = {}
        for r in rooms:
            if not isinstance(r, dict):
                continue
            a = r.get("anchor") or {}
            try:
                u = int(a.get("uid") or 0)
                rid = int(r.get("roomId") or 0)
                if u and rid:
                    tracked_uids[u] = rid
            except (TypeError, ValueError):
                continue

        body = body.strip()

        # 纯数字
        # 不返回「房间 xxx」当显示名，否则调用方模板会重复一遍。
        # 尽量解析真实主播名，拿不到就返回空串。
        if body.isdigit():
            n = int(body)
            if n in tracked_rooms:
                target_rid = n
            else:
                target_rid = tracked_uids.get(n, 0) or await client.resolve_uid_to_room(n) or n
            name = ""
            try:
                name = await self._peek_anchor_name(client, target_rid, rooms)
            except Exception:  # noqa: BLE001
                name = ""
            return target_rid, name

        # 名字
        rid, uname = await client.find_local_by_name(body)
        if rid:
            return rid, uname or body
        rid2, uname2, _uid2 = await client.search_anchor_by_name(body)
        if rid2:
            return rid2, uname2 or body
        return 0, ""

    async def _peek_anchor_name(
        self, client: XixiClient, room_id: int, rooms: list | None = None
    ) -> str:
        """在正式拉数据前，先快速取主播名（用于提示语）。

        优先级：
          1. 汐汐 /rooms 里的 anchor.uname（最快，无外网）
          2. B 站官方 Master/info
        拿不到就返回空字符串。
        """
        try:
            # 1. 本地房间列表
            if rooms is None:
                rooms = await client.get_rooms()
            for r in rooms:
                if not isinstance(r, dict):
                    continue
                try:
                    if int(r.get("roomId") or 0) != int(room_id):
                        continue
                except (TypeError, ValueError):
                    continue
                a = r.get("anchor") or {}
                nm = str(a.get("uname") or "").strip()
                if nm:
                    return nm

            # 2. B 站官方：先查房间拿 uid，再查主播信息
            info = await client.get_room_info(room_id)
            if info:
                uid = int(info.get("uid") or 0)
                if uid:
                    mi = await client.get_master_info(uid)
                    if mi:
                        nm = str((mi.get("info") or {}).get("uname") or "").strip()
                        if nm:
                            return nm
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili_live] 预取主播名失败: {exc}")
        return ""

    async def _fetch_bytes(self, url: str) -> bytes | None:
        try:
            import aiohttp
        except ImportError:
            return None
        try:
            tmo = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://live.bilibili.com/",
                    },
                ) as resp:
                    if resp.status == 200:
                        return await resp.read()
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[bili_live] 头像下载失败: {exc}")
        return None

