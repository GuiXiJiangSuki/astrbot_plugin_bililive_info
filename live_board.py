"""直播间数据看板 —— 三列网格布局（完美复刻参考图）。

布局：固定 6 行网格，三列共用行边界，保证横向对齐。
    ┌──────────────┬──────────────┬──────────────┐
    │ R1 头部      │ 时长         │ 粉丝数       │
    │ R2 基础数据  │ 弹幕条数     │ 上舰人数     │
    │ R3 营收      │ 进场人数     │ 点赞数       │
    │ R4 雷达|金额 │ SC 金额      │ 分享数       │
    │ R5 弹幕|入场 │ 盲盒盈亏     │ 词云         │
    │ R6 礼物|频次 │              │              │
    └──────────────┴──────────────┴──────────────┘
每个图表底部都标注日期（形如 2026-11-18 LIVE）。
"""

from __future__ import annotations

import io
import math
import os
import re
from collections import Counter
from typing import Any

from PIL import Image, ImageDraw, ImageFont

try:
    from .xixi_client import LiveSnapshot
except ImportError:
    from xixi_client import LiveSnapshot  # type: ignore

from astrbot.api import logger


# 柱形图显示最近多少天
CHART_DAYS = 7

# ── 浅色主题 ──
BG = (255, 255, 255)
INK = (34, 34, 38)
SUB = (122, 122, 130)
LINE = (232, 232, 238)
GRID = (240, 240, 244)
ORANGE = (240, 112, 36)
ORANGE_L = (252, 190, 140)
PINK = (238, 118, 158)
BLUE = (90, 160, 220)
GREEN = (110, 190, 100)
PURPLE = (150, 120, 220)
GRAY = (168, 168, 176)
RED = (232, 62, 62)      # 直播中标记

CLOUD_COLORS = [
    (44, 160, 195), (60, 130, 220), (110, 180, 90), (240, 130, 60),
    (200, 80, 160), (240, 170, 50), (100, 150, 180), (160, 110, 190),
]

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSerifCJK-Regular.ttc",
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/msyhbd.ttc",
]

_font_cache: dict[tuple[int, bool], Any] = {}

# ── 网格行边界（逻辑 px）──
# 左列：头部 96 + 基础数据 4 行 × 3 列(约 130) + 5 个内容块
ROWS = [0, 96, 226, 386, 576, 746, 916]
COL_ROWS = [0, 183, 366, 549, 732, 916]   # 中/右列：5 等分
PAD = 16
COL_GAP = 40
TOTAL_W = 1000
TITLE_H = 24      # 小节标题高度
DATE_H = 14       # 图表底部日期高度


def _font(size: int, bold: bool = False) -> Any:
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    pool = FONT_CANDIDATES
    if bold:
        pool = [
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
            "C:/Windows/Fonts/msyhbd.ttc",
        ] + FONT_CANDIDATES
    for path in pool:
        if not os.path.exists(path):
            continue
        try:
            f = ImageFont.truetype(path, size)
            _font_cache[key] = f
            return f
        except Exception:  # noqa: BLE001
            continue
    f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def _num(n: Any) -> str:
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        return "0"
    if v >= 1e8:
        return f"{v / 1e8:.2f}".rstrip("0").rstrip(".") + "亿"
    if v >= 1e4:
        return f"{v / 1e4:.1f}".rstrip("0").rstrip(".") + "万"
    return f"{int(v):,}"


def _short_num_text(v: float, original: str) -> str:
    """把过长的数值文本缩成万单位："19451条" -> "1.9万条"。"""
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        return original
    if n < 10000:
        return original
    short = f"{n / 10000:.1f}".rstrip("0").rstrip(".") + "万"
    # 保留原单位后缀
    for u in ("条", "人", "赞", "次", "艘"):
        if str(original).endswith(u):
            return short + u
    return short


def _split_unit(s: str) -> tuple:
    """"19451条" -> ("19451", "条")；"¥84" -> ("¥84", "")。"""
    t = str(s or "")
    units = ("条", "人", "赞", "次", "艘", "分", "小时")
    for u in units:
        if t.endswith(u):
            return t[: -len(u)], u
    return t, ""


def _gift_people(snap: Any) -> int:
    """送礼人数（不是礼物条数）。

    优先用礼物聚合数据的条数估算 —— 每行是一类礼物，
    不够准；更好的来源是付费排行里 giftCount > 0 的人数。
    """
    # 付费排行里每人带 giftCount，>0 说明送过礼
    n = 0
    for p in (getattr(snap, "payer_list", None) or []):
        if isinstance(p, dict) and float(p.get("giftValue") or 0) > 0:
            n += 1
    if n > 0:
        return n
    return int(getattr(snap, "gift_count", 0) or 0)


def _payer_count(snap: Any) -> int:
    """付费人数（真实值，受排行接口的 top 限制时用 payer_total）。"""
    t = int(getattr(snap, "payer_total", 0) or 0)
    if t > 0:
        return t
    return len([p for p in (getattr(snap, "payer_list", None) or []) if isinstance(p, dict)])


def _message_total(snap: Any) -> int:
    """消息总数。

    snap.messages 来自 /history，被截断在 5000，不能直接用。
    用各类计数相加更接近真实。
    """
    t = int(getattr(snap, "message_total", 0) or 0)
    if t > 0:
        return t
    return (
        int(getattr(snap, "danmaku", 0) or 0)
        + int(getattr(snap, "gift_count", 0) or 0)
        + int(getattr(snap, "sc", 0) or 0)
        + int(getattr(snap, "enter", 0) or 0)
    ) or int(getattr(snap, "messages", 0) or 0)


def _pick_revenue(snap: Any) -> float:
    """挑一个能用的营收值。

    优先级：
      1. daily 最后一天（SQLite 真实值，最可靠）
      2. revenue_today / revenue_all（HTTP 接口算的）
    """
    daily = getattr(snap, "daily", None) or []
    if daily:
        try:
            last = sorted(daily, key=lambda x: str(x.get("date") or ""))[-1]
            v = float(last.get("revenue") or 0)
            if v > 0:
                return v
        except Exception:
            pass
    rt = float(getattr(snap, "revenue_today", 0) or 0)
    if rt > 0:
        return rt
    return float(getattr(snap, "revenue_all", 0) or 0)


def _guard_count(snap: Any) -> int:
    """上舰人数（当日新增）。

    优先级：
      1. daily 最后一天的 guard 数（SQLite 真实统计）
      2. guard 字段（来自 SQLite 覆盖）
      3. guard_list 长度（只在当前连接房间有）

    ⚠️ 不能优先用 guard_list —— 它只包含「本次连接期间监听到的」，
    历史上舰的人不在里面（实测 guard=7 但 guard_list 只有 1）。
    """
    daily = getattr(snap, "daily", None) or []
    if daily:
        try:
            last = sorted(daily, key=lambda x: str(x.get("date") or ""))[-1]
            n = int(last.get("guard") or 0)
            if n > 0:
                return n
        except Exception:
            pass
    n = int(getattr(snap, "guard", 0) or 0)
    if n > 0:
        return n
    return len(getattr(snap, "guard_list", None) or [])


def _exact(n: Any) -> str:
    """精确数字（千分位），不缩写万/亿。

    粉丝数这类需要看具体数值的用这个，
    比如 1748125 → "1,748,125" 而不是 "174.8万"。
    """
    try:
        v = float(n or 0)
    except (TypeError, ValueError):
        return "0"
    return f"{int(v):,}"


def _tl(d: Any, s: str, f: Any) -> float:
    b = d.textbbox((0, 0), s, font=f)
    return b[2] - b[0]


def _session_duration_text(start_ms: int, end_ms: int = 0) -> str:
    import time

    if not start_ms:
        return ""
    try:
        end = end_ms / 1000 if end_ms else time.time()
        sec = max(0, int(end - start_ms / 1000))
    except (TypeError, ValueError, OverflowError):
        return ""
    h, rem = divmod(sec, 3600)
    m, _ = divmod(rem, 60)
    if h:
        return f"{h}小时{m}分" if m else f"{h}小时"
    return f"{m}分" if m else "刚刚"


def _duration_hours(snap: Any) -> float:
    """开播时长（小时），用于柱形图。"""
    import time

    start = int(getattr(snap, "live_start_ts", 0) or 0)
    if not start:
        return 0.0
    end_ms = int(getattr(snap, "live_end_ts", 0) or 0)
    try:
        end = end_ms / 1000 if end_ms else time.time()
        return max(0.0, (end - start / 1000) / 3600.0)
    except (TypeError, ValueError, OverflowError):
        return 0.0


def render_live_board(
    snap: LiveSnapshot,
    *,
    avatar_data: bytes | None = None,
    width: int = TOTAL_W,
    scale: int = 2,
) -> bytes:
    """渲染三列网格看板。"""
    S = scale

    def P(v: float) -> int:
        return int(round(v * S))

    W = width
    H = ROWS[-1] + 26

    Wp, Hp = P(W), P(H)
    img = Image.new("RGB", (Wp, Hp), BG)
    d = ImageDraw.Draw(img, "RGBA")

    col_w = (W - PAD * 2 - COL_GAP * 2) / 3
    x_left = P(PAD)
    x_mid = P(PAD + col_w + COL_GAP)
    x_right = P(PAD + col_w * 2 + COL_GAP * 2)
    cw = P(col_w)

    # 日期标签（所有图共用）
    date_lbl = _date_label(snap)

    # ══════════════════════════════════════
    # 左列
    # ══════════════════════════════════════
    _left_head(img, d, snap, x_left, cw, avatar_data, P, ROWS[0], ROWS[1])

    # R2 基础数据
    _left_base(d, snap, x_left, cw, P, ROWS[1], ROWS[2])

    # R3 营收（近 5 天柱形图）
    _section(d, "营收", x_left, P(ROWS[2]), P, cw)
    _revenue_bars(
        d, snap, x_left, P(ROWS[2] + TITLE_H), cw,
        P(ROWS[3] - ROWS[2] - TITLE_H - DATE_H), P,
    )

    # R4 左：雷达　右：金额排行
    half = (cw - P(10)) // 2
    _section(d, "可视雷达", x_left, P(ROWS[3]), P, half)
    _radar(
        d, x_left, P(ROWS[3] + TITLE_H), half,
        P(ROWS[4] - ROWS[3] - TITLE_H),
        [
            ("弹幕", snap.danmaku),
            ("礼物", snap.gift_count),
            ("营收", int(snap.revenue_today if snap.is_live_now else snap.revenue_all)),
            ("入场", snap.enter),
            ("点赞", snap.like),
            ("观众", snap.users),
        ],
        P,
    )
    _section(d, "金额排行", x_left + half + P(10), P(ROWS[3]), P, half)
    # 按金额降序（上游可能没排序，这里兜一道）
    _payer_sorted = sorted(
        _payers(snap), key=lambda p: _pay_value(p), reverse=True,
    )
    _rank_list(
        d, _payer_sorted, x_left + half + P(10), P(ROWS[3] + TITLE_H),
        half, P(ROWS[4] - ROWS[3] - TITLE_H), P,
        fmt=_pay_text, empty="暂无金额排行",
    )

    # R5 左：弹幕排行　右：入场排行
    _section(d, "弹幕排行", x_left, P(ROWS[4]), P, half,
             sub=f"弹幕人数 - {len(snap.user_list)}")
    _rank_list(
        d, snap.user_list, x_left, P(ROWS[4] + TITLE_H), half,
        P(ROWS[5] - ROWS[4] - TITLE_H), P,
        fmt=lambda u: f"{int(u.get('danmaku') or 0)} 条",
        empty="暂无弹幕排行",
    )
    _section(d, "入场排行", x_left + half + P(10), P(ROWS[4]), P, half,
             sub=f"入场人数 - {len(_enterers(snap.timeline))}")
    _rank_list(
        d, _enterers(snap.timeline), x_left + half + P(10),
        P(ROWS[4] + TITLE_H), half, P(ROWS[5] - ROWS[4] - TITLE_H), P,
        fmt=lambda u: f"{int(u.get('count') or 0)} 次",
        empty="暂无入场排行",
    )

    # R6 左：礼物排行（按礼物价值降序）　右：弹幕频次
    _section(d, "礼物排行", x_left, P(ROWS[5]), P, half)
    _gift_sorted = sorted(
        [g for g in (snap.gift_list or []) if isinstance(g, dict)],
        key=lambda g: float(g.get("yuan") or 0), reverse=True,
    )
    _gift_rank(
        d, _gift_sorted, x_left, P(ROWS[5] + TITLE_H), half,
        P(ROWS[6] - ROWS[5] - TITLE_H), P,
    )
    _section(d, "弹幕频次", x_left + half + P(10), P(ROWS[5]), P, half)
    _danmaku_freq(
        d, snap, x_left + half + P(10), P(ROWS[5] + TITLE_H), half,
        P(ROWS[6] - ROWS[5] - TITLE_H), P,
    )

    # ══════════════════════════════════════
    # 中列（5 个等距区块，全部柱形图）
    # ══════════════════════════════════════
    def _col_block(title: str, idx: int, bars, x: int, *, value_fmt=None) -> None:
        y0 = COL_ROWS[idx]
        y1 = COL_ROWS[idx + 1]
        _section(d, title, x, P(y0), P, cw)
        # 不再在图内画日期标签（底部已统一标注月份/日期）
        _bar_chart(
            d, x, P(y0 + TITLE_H), cw,
            P(y1 - y0 - TITLE_H - DATE_H),
            bars, P, date_lbl="", value_fmt=value_fmt,
        )

    # 单柱图用的日期标签（取最近一天，形如 "11-19"）
    single_lbl = _latest_md(snap)

    # 时长：最近几场对比（有场次记录就多柱，否则单柱）
    dur_bars = _duration_bars(snap)
    _dur_text = _session_duration_text(
        snap.live_start_ts,
        snap.live_end_ts if not snap.is_live_now else 0,
    ) or "0分"
    if dur_bars:
        _col_block("时长", 0, dur_bars, x_mid,
                   value_fmt=lambda v: _hours_text(v))
    else:
        dur_hours = _duration_hours(snap)
        _col_block(
            "时长", 0,
            [(date_lbl.split()[0][5:] if date_lbl else single_lbl, dur_hours, ORANGE)],
            x_mid,
            value_fmt=lambda _v, t=_dur_text: t,
        )
    _col_block(
        "弹幕条数", 1,
        _daily_bars(snap, ("danmaku",), ("弹幕",), (BLUE,)),
        x_mid,
        value_fmt=lambda v: f"{int(v)} 条",
    )
    _col_block(
        "进场人数", 2,
        _daily_bars(snap, ("enter",), ("进场",), (GREEN,)),
        x_mid,
        value_fmt=lambda v: f"{int(v)} 人",
    )
    # SC 金额：有每日数据就按天对比
    sc_bars = _daily_money_bars(snap, "scYuan")
    if not sc_bars:
        sc_bars = [(single_lbl, round(snap.sc_total_yuan), ORANGE)]
    _col_block("SC 金额", 3, sc_bars, x_mid,
               value_fmt=lambda v: f"¥{v:.0f}")
    # 盲盒盈亏：始终按天显示（没数据也要画 0 柱）
    bb_bars = _blind_bars(snap)
    if not bb_bars:
        bb_bars = [(single_lbl, round(snap.blindbox_profit), PURPLE)]
    _col_block(
        "盲盒盈亏", 4, bb_bars, x_mid,
        value_fmt=lambda v: f"¥{v:.0f}",
    )

    # ══════════════════════════════════════
    # 右列（5 个等距区块，前四个柱形图 + 词云）
    # ══════════════════════════════════════
    # 右列：有历史数据的走多天，没有的用当前值单柱
    # 粉丝数：有每日快照就画趋势，否则单柱
    fw_bars = _follower_bars(snap)
    if not fw_bars:
        fw_bars = [(single_lbl, float(snap.followers or 0), BLUE)]
    _col_block(
        "粉丝数", 0, fw_bars, x_right,
        value_fmt=lambda v: _exact(v),
    )
    # 上舰人数：有每日数据就按天对比
    gd_bars = _daily_bars(snap, ("guard",), ("舰长",), (ORANGE,))
    if not gd_bars:
        gd_bars = [(single_lbl, float(len(snap.guard_list) or snap.guards_count or 0), ORANGE)]
    _col_block("上舰人数", 1, gd_bars, x_right,
               value_fmt=lambda v: f"{int(v)} 艘")

    # 点赞 / 分享：有每日数据就按天，否则单柱
    like_bars = _daily_bars(snap, ("like",), ("点赞",), (PINK,))
    if not getattr(snap, "daily", None):
        like_bars = [(single_lbl, float(snap.like or 0), PINK)]
    _col_block("点赞数", 2, like_bars, x_right,
               value_fmt=lambda v: f"{int(v)} 赞")

    # 分享：从 timeline 里统计 interact/share
    share_n = _shares(snap.timeline, snap)
    # 分享数：有每日数据就按天对比
    sh_bars = _daily_bars(snap, ("share",), ("分享",), (PURPLE,))
    if not sh_bars:
        share_n = _shares(snap.timeline, snap)
        sh_bars = [(single_lbl, float(share_n), PURPLE)]
    _col_block("分享数", 3, sh_bars, x_right,
               value_fmt=lambda v: f"{int(v)} 次")

    # 词云（第 5 块）
    _section(d, "词云", x_right, P(COL_ROWS[4]), P, cw)
    _wordcloud(
        d, _top_words(snap.danmaku_samples, 26), x_right,
        P(COL_ROWS[4] + TITLE_H), cw,
        P(COL_ROWS[5] - COL_ROWS[4] - TITLE_H - DATE_H), P,
    )

    # 页脚：当前时间 · AstrBot x 鬼汐酱 · 数据来源
    fy = P(ROWS[-1] + 4)
    d.line([(P(PAD), fy), (Wp - P(PAD), fy)], fill=LINE, width=P(1))
    import datetime as _dt

    now_s = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    d.text(
        (P(PAD), fy + P(4)),
        f"{now_s} · AstrBot x 鬼汐酱　·　数据来源：汐汐工具箱",
        font=_font(P(9)), fill=GRAY,
    )

    """
    按内容裁剪右侧空白：
      找最右侧非白像素，把画布裁到那里 + 一点边距。
      避免固定宽度留下大片空白。
    """
    try:
        from PIL import ImageChops

        bg = Image.new("RGB", img.size, BG)
        bbox = ImageChops.difference(img, bg).getbbox()
        if bbox:
            right = min(img.size[0], bbox[2] + P(PAD))
            if right < img.size[0]:
                img = img.crop((0, 0, right, img.size[1]))
    except Exception:  # noqa: BLE001
        pass

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ══════════════════════════════════════
# 小节 / 头部 / 基础数据
# ══════════════════════════════════════

def _section(d: Any, title: str, x: int, y: int, P: Any, w: int, *, sub: str = "") -> None:
    """小节标题 + 橙色下划线。"""
    f = _font(P(15), bold=True)
    d.text((x, y), title, font=f, fill=INK)
    tw = _tl(d, title, f)
    d.line([(x, y + P(19)), (x + min(tw, w), y + P(19))], fill=ORANGE, width=P(2))
    if sub:
        d.text((x + tw + P(8), y + P(3)), sub, font=_font(P(9)), fill=GRAY)


def _left_head(img, d, snap, x, w, av_data, P, y0, y1) -> None:
    """头部：大标题 + UID + 标题 + 时间 + 头像。

    头像在右上角，标题/UID 右侧预留空间，避免重叠。
    """
    y = y0
    AV = P(64)
    # 文本可用宽度：扣除头像 + 间距
    text_w = w - AV - P(12) if av_data else w

    """
    左上角只显示主播名字（不加后缀）。
    名字过长时依次：
      1. 缩字号（22 → 18）
      2. 还放不下才截断
    """
    f_title = _font(P(22), bold=True)
    title = snap.uname or "未知主播"
    if _tl(d, title, f_title) > text_w:
        f_title = _font(P(18), bold=True)
    while _tl(d, title, f_title) > text_w and len(title) > 4:
        title = title[:-1]
    d.text((x, P(y)), title, font=f_title, fill=INK)

    d.text((x, P(y + 30)), f"UID : {snap.uid} | 直播 · 哔哩哔哩",
           font=_font(P(10)), fill=SUB)

    f_t = _font(P(11))
    t = snap.title or "无标题"
    while _tl(d, f'标题：「{t}」', f_t) > text_w and len(t) > 4:
        t = t[:-1]
    d.text((x, P(y + 46)), f'标题：「{t}」', font=f_t, fill=INK)

    if snap.live_start_text:
        if snap.is_live_now:
            tl = f"时间：{snap.live_start_text} - 正在直播中"
        else:
            tl = f"时间：{snap.live_start_text} → {snap.live_end_text or '—'}"
    else:
        tl = "时间：无场次记录"
    d.text((x, P(y + 62)), tl, font=_font(P(10)), fill=SUB)

    # 头像（右上，与文本不重叠）
    if av_data:
        try:
            av = Image.open(io.BytesIO(av_data)).convert("RGBA")
            aw, ah = av.size
            side = min(aw, ah)
            av = av.crop(((aw - side) // 2, (ah - side) // 2,
                          (aw + side) // 2, (ah + side) // 2))
            av = av.resize((AV, AV), Image.LANCZOS)
            mask = Image.new("L", (AV, AV), 0)
            ImageDraw.Draw(mask).ellipse([0, 0, AV - 1, AV - 1], fill=255)
            img.paste(av, (x + w - AV, P(y + 4)), mask)
        except Exception:  # noqa: BLE001
            pass


def _left_base(d: Any, snap, x: int, w: int, P: Any, y0: int, y1: int) -> None:
    """基础数据：4 列 × 3 行。"""
    _section(d, "基础数据", x, P(y0), P, w)

    """
    营收优先从 daily 取（SQLite 真实值），
    revenue_today 在多分支赋值时可能被覆盖成 0。
    """
    rev = _pick_revenue(snap)
    payers = _payers(snap)
    # 3 列 × 4 行，每列更宽，避免标签和数值重叠
    items = [
        ("粉丝", _exact(snap.followers), INK),
        ("弹幕", _num(snap.danmaku), ORANGE),
        ("进场", _num(snap.enter), INK),
        ("点赞", _num(snap.like), INK),
        ("礼物", f"{_gift_people(snap)}人", ORANGE),
        ("营收", f"¥{rev:.0f}", ORANGE),
        ("互动", _num(snap.users), INK),
        ("上舰", f"{_guard_count(snap)}", INK),
        ("SC", f"{snap.sc}", INK),
        ("付费", f"{_payer_count(snap)}人", INK),
        ("消息", _num(_message_total(snap)), INK),
        ("人气", _num(snap.online), INK),
    ]

    cols = 3
    cw = w / cols
    f_lb = _font(P(9))
    f_v = _font(P(12), bold=True)
    base_y = y0 + TITLE_H + 3
    row_h = (y1 - base_y - 6) / 4

    for i, (lb, val, col) in enumerate(items):
        r, c = divmod(i, cols)
        cx = x + int(c * cw)
        cy = P(int(base_y + r * row_h))
        lb_txt = f"{lb}："
        d.text((cx, cy), lb_txt, font=f_lb, fill=SUB)
        lw = _tl(d, lb_txt, f_lb)

        avail = int(cw - lw - P(4))
        vtxt = str(val)
        while _tl(d, vtxt, f_v) > avail and len(vtxt) > 1:
            vtxt = vtxt[:-1]
        d.text((cx + lw + P(1), cy - P(1)), vtxt, font=f_v, fill=col)


# ══════════════════════════════════════
# 图表
# ══════════════════════════════════════

def _area_chart(
    d: Any, x: int, y: int, w: int, h: int, label: str, P: Any,
    *, value: float = 0, color: tuple = ORANGE, date_lbl: str = "",
) -> None:
    """区域图：Y 轴刻度 + 三角形填充 + 底部日期。"""
    if h <= 0:
        return
    ax_w = P(36)
    f_ax = _font(P(8))
    ticks = 3

    for i in range(ticks + 1):
        gy = y + int(h * i / ticks)
        d.line([(x + ax_w, gy), (x + w, gy)], fill=GRID, width=P(1))
        v = value * (ticks - i) / ticks
        lab = f"{v / 10000:.1f}万" if v >= 10000 else f"{v:.0f}"
        d.text((x, gy - P(4)), lab, font=f_ax, fill=GRAY)

    base_y = y + h
    d.line([(x + ax_w, base_y), (x + w, base_y)], fill=GRAY, width=P(1))

    span = w - ax_w
    cx = x + ax_w + int(span * 0.46)
    top_y = y + int(h * 0.12)
    pts = [
        (x + ax_w, base_y), (cx - P(34), base_y), (cx, top_y),
        (cx + P(34), base_y), (x + w, base_y),
    ]
    d.polygon(pts, fill=color + (46,))
    d.line(pts[1:4], fill=color, width=P(2))
    d.ellipse([cx - P(3), top_y - P(3), cx + P(3), top_y + P(3)], fill=color)

    if label:
        f_v = _font(P(11), bold=True)
        d.text((cx + P(7), top_y - P(5)), label, font=f_v, fill=INK)

    if date_lbl:
        f_d = _font(P(8))
        d.text((x + ax_w, base_y + P(2)), date_lbl, font=f_d, fill=GRAY)


def _bar_chart(
    d: Any, x: int, y: int, w: int, h: int,
    bars: list, P: Any, *, date_lbl: str = "", value_fmt=None,
) -> None:
    """柱状图：柱宽固定，带数值 + 类别标签 + 底部日期。

    bars 每项：(类别标签, 数值, 颜色)
    value_fmt: 可选，把数值转成显示文本（如时长转「4小时43分」）
    """
    if not bars or h <= 0:
        return
    n = len(bars)
    slot = w / n
    """
    柱宽用**固定像素**，所有图绝对一致。
    ─────────────────────────────────────────────
    之前用 `slot * 0.5` 算，槽位宽时柱子就宽：
      单柱 slot = 整列宽 629 → bw = min(314, 92) = 92
      双柱 slot = 314       → bw = min(157, 92) = 92
    理论一样，但一旦某图列宽不同就立刻不一致。
    改成直接 `P(30)`（60px），不受槽位影响，永远一样粗。
    """
    bw = int(P(26))
    raw_vals = [float(v or 0) for _, v, _ in bars]
    # 注意：不能写 `max(...) or 1`，否则全 0 时 maxv 会变成 1，
    # 导致 0 值被当成「有数据」画成满柱。
    maxv = max(raw_vals) if raw_vals else 0.0

    bar_area_h = h - P(14)
    bh1 = P(2)
    if n == 1:
        if maxv <= 0:
            bh1 = P(2)                     # 全 0：只画一条细线
        else:
            """
            单柱的高度**不能顶满**，否则看起来像一块色块而不是柱子。
            ─────────────────────────────────────────────
            原来用 maxv/_nice_ceiling 算出 5%~95%，但 _nice_ceiling
            对 25（分钟）这种小数会取到 25 附近，ratio 接近 1 → 满柱。
            改成固定 55%：既看得出是个柱子，又不占满整块区域。
            """
            bh1 = max(P(6), int(bar_area_h * 0.55))
    base_y = y + h

    f_v = _font(P(10), bold=True)
    f_lb = _font(P(9))

    base_of_bars = y + bar_area_h

    for i, (lb, v, col) in enumerate(bars):
        fv = float(v or 0)
        if fv <= 0:
            # 0 值：画一条细横线表示「无数据」，不画柱子
            bh = P(2)
        elif n == 1:
            bh = bh1
        else:
            bh = max(P(3), int(bar_area_h * (fv / maxv if maxv > 0 else 0)))
        bh = min(bh, bar_area_h - P(14))
        # 柱子左对齐、紧挨着排列（缝隙很小，像一组）
        bx = int(x + i * (bw + P(14)))
        d.rectangle([bx, base_of_bars - bh, bx + bw, base_of_bars], fill=col)

        vs = value_fmt(fv) if value_fmt else (_num(fv) if fv >= 1 else "0")
        """
        标签宽度控制：
          柱子之间只有 6px 缝，标签必须比柱宽更窄，否则会重叠。
          超宽时按顺序降级：8px -> 7px > 缩写数字（19451 -> 1.9万）。
        """
        f_use = _font(P(8), bold=True)
        tw = _tl(d, vs, f_use)
        if tw > bw + P(14):
            f_use = _font(P(7), bold=True)
            tw = _tl(d, vs, f_use)
        # 粉丝数这类带千分位逗号的不能缩写（1,748,741 要完整显示）
        is_exact = ("," in str(vs))
        if tw > bw + P(14) and not is_exact:
            vs = _short_num_text(fv, vs)
            tw = _tl(d, vs, f_use)
        d.text((bx + (bw - tw) / 2, base_of_bars - bh - P(11)),
               vs, font=f_use, fill=INK)
        # 类别标签（日期/月份）放在柱子正下方
        tw2 = _tl(d, lb, f_lb)
        lx = bx + (bw - tw2) / 2
        ly = base_of_bars + P(2)
        pad_x, pad_y = P(4), P(1)
        d.rectangle(
            [lx - pad_x, ly - pad_y, lx + tw2 + pad_x, ly + P(11) + pad_y],
            fill=(248, 248, 250),
        )
        d.text((lx, ly), lb, font=f_lb, fill=SUB)

    # 日期放在类别标签下方
    if date_lbl:
        d.text((x, base_of_bars + P(16)), date_lbl, font=_font(P(8)), fill=GRAY)


def _revenue_bars(d: Any, snap, x: int, y: int, w: int, h: int, P: Any) -> None:
    """营收：近 5 天柱形图，显示**真实金额**（元）。

    数据来自 /gift/summary 的 byDay，已按日期升序（左→右由早到晚）。
    """
    if h <= 0:
        return

    rev_daily = getattr(snap, "daily_revenue", None) or []
    bars: list = []

    if rev_daily:
        for it in rev_daily[-CHART_DAYS:]:          # 已升序，取最近 CHART_DAYS 天
            dt = str(it.get("date") or "")
            mmdd = dt[5:] if len(dt) >= 10 else dt
            yuan = float(it.get("yuan") or 0)
            bars.append((mmdd, yuan, ORANGE))
        value_fmt = lambda v: f"¥{v:.0f}" if v >= 1 else "¥0"
    else:
        rev = snap.revenue_today if snap.is_live_now else snap.revenue_all
        bars.append(("本场", float(rev or 0), ORANGE))
        value_fmt = lambda v: f"¥{v:.0f}"

    _bar_chart(d, x, y, w, h, bars, P, date_lbl="", value_fmt=value_fmt)


def _daily_bars(
    snap: Any,
    keys: tuple,
    labels: tuple,
    colors: tuple,
) -> list:
    """把近 N 天的某个指标做成柱形图数据。

    有 daily 数据时：按日期分柱（最近 5 天）。
    没有时：退化为本场累计的单柱。
    """
    daily = getattr(snap, "daily", None) or []
    if not daily:
        # 退化：本场累计值
        fallback = {
            "danmaku": getattr(snap, "danmaku", 0),
            "gift": getattr(snap, "gift_count", 0) or getattr(snap, "gift", 0),
            "enter": getattr(snap, "enter", 0),
            "total": getattr(snap, "messages", 0),
            "like": getattr(snap, "like", 0),
        }
        default_lbl = {"danmaku": "弹幕", "gift": "礼物", "enter": "进场",
                       "total": "互动", "like": "点赞"}
        bars = []
        for k, c in zip(keys, colors):
            bars.append((default_lbl.get(k, k), fallback.get(k, 0), c))
        return bars

    # 按日期分柱：升序（左→右由早到晚），最多 5 天
    daily_sorted = sorted(
        [it for it in daily if isinstance(it, dict)],
        key=lambda x: str(x.get("date") or ""),
    )
    bars = []
    for it in daily_sorted[-CHART_DAYS:]:
        dt = str(it.get("date") or "")
        mmdd = dt[5:] if len(dt) >= 10 else dt
        v = int(it.get(keys[0]) or 0)
        bars.append((mmdd, v, colors[0]))
    return bars


def _daily_money_bars(snap: Any, key: str) -> list:
    """把近 N 天的「金额类」指标做成柱形图数据（元）。

    key 取值如 scYuan / guardYuan / revenue。
    """
    daily = getattr(snap, "daily", None) or []
    if not daily:
        return []
    rows = sorted(
        [it for it in daily if isinstance(it, dict)],
        key=lambda x: str(x.get("date") or ""),
    )
    bars = []
    for it in rows[-CHART_DAYS:]:
        dt = str(it.get("date") or "")
        mmdd = dt[5:] if len(dt) >= 10 else dt
        v = round(float(it.get(key) or 0), 2)
        bars.append((mmdd, v, ORANGE))
    return bars


def _hours_text(hours: float) -> str:
    """小时数 → "8小时39分" 这种文本。"""
    total_min = int(round(float(hours or 0) * 60))
    if total_min <= 0:
        return "0分"
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h}小时{m}分"
    if h:
        return f"{h}小时"
    return f"{m}分"


def _follower_bars(snap: Any) -> list:
    """粉丝数每日趋势（汐汐每 10 分钟抓一次的快照）。"""
    rows = getattr(snap, "follower_daily", None) or []
    if len(rows) < 2:
        return []
    bars = []
    for it in sorted(rows, key=lambda x: str(x.get("date") or ""))[-CHART_DAYS:]:
        dt = str(it.get("date") or "")
        mmdd = dt[5:] if len(dt) >= 10 else dt
        bars.append((mmdd, float(it.get("count") or 0), BLUE))
    return bars


def _blind_bars(snap: Any) -> list:
    """盲盒每日盈亏。就算全是 0 也要返回柱子，让图有内容。"""
    rows = getattr(snap, "blind_daily", None) or []
    if not rows:
        return []
    bars = []
    for it in sorted(rows, key=lambda x: str(x.get("date") or ""))[-CHART_DAYS:]:
        dt = str(it.get("date") or "")
        mmdd = dt[5:] if len(dt) >= 10 else dt
        bars.append((mmdd, float(it.get("profit") or 0), PURPLE))
    return bars


def _duration_bars(snap: Any) -> list:
    """最近几场的开播时长，做成多柱对比数据。

    数据来自 live_log（含已结束和进行中的场次）。
    只取最近 5 场，按时间正序（左→右由早到晚）。
    """
    sessions = getattr(snap, "live_sessions", None) or []
    if len(sessions) < 2:
        return []
    import time as _t

    rows = []
    for it in sessions:
        if not isinstance(it, dict):
            continue
        start = int(it.get("start") or 0)
        end = int(it.get("end") or 0)
        if start <= 0:
            continue
        if not end:
            # 进行中：算到当前时刻
            end = int(_t.time() * 1000)
        hours = max(0.0, (end - start) / 3600000.0)
        if hours <= 0:
            continue
        import datetime as _dt
        mmdd = _dt.datetime.fromtimestamp(start / 1000).strftime("%m-%d")
        rows.append((start, mmdd, round(hours, 3)))
    if len(rows) < 2:
        return []
    rows.sort(key=lambda x: x[0])
    return [(mmdd, h, ORANGE) for _s, mmdd, h in rows[-CHART_DAYS:]]


def _radar(d: Any, x: int, y: int, w: int, h: int,
           dims: list[tuple[str, int]], P: Any) -> None:
    """六维雷达（log 归一化，避免量纲互相压扁）。"""
    if h <= 0:
        return
    cx = x + w // 2
    cy = y + h // 2
    R = int(min(w, h) * 0.33)

    logs = [math.log1p(max(0.0, v)) for _, v in dims]
    mx = max(logs or [1]) or 1

    for ring in (0.25, 0.5, 0.75, 1.0):
        pts = [(cx + R * ring * math.cos(-math.pi / 2 + i * math.pi / 3),
                cy + R * ring * math.sin(-math.pi / 2 + i * math.pi / 3))
               for i in range(6)]
        d.polygon(pts, outline=LINE)

    for i in range(6):
        ang = -math.pi / 2 + i * math.pi / 3
        d.line([(cx, cy), (cx + R * math.cos(ang), cy + R * math.sin(ang))],
               fill=LINE, width=P(1))

    pts = []
    for i, lg in enumerate(logs):
        ang = -math.pi / 2 + i * math.pi / 3
        rr = max(R * (lg / mx if mx else 0), R * 0.06)
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    if len(pts) >= 3:
        d.polygon(pts, fill=ORANGE + (58,), outline=ORANGE)
        for pxx, pyy in pts:
            d.ellipse([pxx - P(2), pyy - P(2), pxx + P(2), pyy + P(2)], fill=ORANGE)

    f_lb = _font(P(10), bold=True)
    f_v = _font(P(8))
    for i, (lb, v) in enumerate(dims):
        ang = -math.pi / 2 + i * math.pi / 3
        lx = cx + (R + P(24)) * math.cos(ang)
        ly = cy + (R + P(24)) * math.sin(ang)
        tw = _tl(d, lb, f_lb)
        d.text((lx - tw / 2, ly - P(9)), lb, font=f_lb, fill=INK)
        vs = _num(v)
        vw = _tl(d, vs, f_v)
        d.text((lx - vw / 2, ly + P(1)), vs, font=f_v, fill=SUB)


# ══════════════════════════════════════
# 列表
# ══════════════════════════════════════

def _rank_list(
    d: Any, items: list, x: int, y: int, w: int, h: int, P: Any,
    *, fmt, empty: str = "暂无数据",
) -> None:
    """通用排行列表：`昵称 (uid) - 数值`。"""
    if h <= 0:
        return
    items = [i for i in (items or []) if isinstance(i, dict)]
    if not items:
        d.text((x, y + P(4)), empty, font=_font(P(10)), fill=GRAY)
        return

    f = _font(P(10))
    row_h = P(17)
    rows = max(1, int(h / row_h))
    for it in items[:rows]:
        nm = str(it.get("uname") or it.get("name") or "?")
        uid = it.get("uid") or it.get("userid") or 0
        line = f"{nm} ({uid}) - {fmt(it)}" if uid else f"{nm} - {fmt(it)}"
        while _tl(d, line, f) > w and len(nm) > 3:
            nm = nm[:-1]
            line = f"{nm}… ({uid}) - {fmt(it)}" if uid else f"{nm}… - {fmt(it)}"
        d.text((x, y), line, font=f, fill=INK)
        y += row_h


def _gift_rank(d: Any, gifts: list, x: int, y: int, w: int, h: int, P: Any) -> None:
    """礼物排行：名次 + 礼物名（×数量） + 总金额。

    数据来自 /gifts（按礼物名聚合），已按金额降序。
    与「金额排行」（按人）区分开：这里看什么礼物最贵。
    """
    if h <= 0:
        return
    rows_in = [g for g in (gifts or []) if isinstance(g, dict)]
    if not rows_in:
        d.text((x, y + P(4)), "暂无礼物排行", font=_font(P(10)), fill=GRAY)
        return

    f_n = _font(P(10))
    f_v = _font(P(10), bold=True)
    row_h = P(17)
    rows = max(1, int(h / row_h))
    for i, g in enumerate(rows_in[:rows]):
        nm = str(g.get("name") or "?")
        num = int(g.get("num") or 0)
        yuan = float(g.get("yuan") or 0)
        # 名称过长截断，给金额留空间
        label = f"{i + 1}. {nm[:9]}"
        if num > 1:
            label += f" x{num}"
        amt = f"¥{yuan:.0f}" if yuan >= 1 else f"¥{yuan:.2f}"
        d.text((x, y), label, font=f_n, fill=INK)
        aw = _tl(d, amt, f_v)
        d.text((x + w - aw, y), amt, font=f_v, fill=ORANGE)
        y += row_h


def _danmaku_freq(d: Any, snap, x: int, y: int, w: int, h: int, P: Any) -> None:
    """弹幕频次：按时间段分桶，显示 HH:MM - HH:MM。

    ⚠️ 优先用 hourly_list（SQLite 按小时聚合，覆盖全量消息）。
    退回 timeline 分桶不准确 —— timeline 只有 300~20000 条采样，
    实际弹幕可能几万条，分桶后大部分时段是 0。
    """
    if h <= 0:
        return

    # 首选：SQLite 的真实小时分布
    hourly = getattr(snap, "hourly_list", None) or []
    hourly = [x for x in hourly if isinstance(x, dict)]
    if hourly:
        _danmaku_freq_hourly(d, hourly, x, y, w, h, P)
        return

    ts_list = []
    for m in (snap.timeline or []):
        if isinstance(m, dict):
            ts = m.get("ts")
            if isinstance(ts, (int, float)) and ts > 0:
                ts_list.append(float(ts))
    if not ts_list:
        d.text((x, y + P(4)), "暂无弹幕数据", font=_font(P(10)), fill=GRAY)
        return

    ts_list.sort()
    t0, t1 = ts_list[0], ts_list[-1]
    span = max(1.0, t1 - t0)

    # 跨度太小（比如只拿到几分钟的实时消息）时，用开播时间做起点
    start_ms = int(getattr(snap, "live_start_ts", 0) or 0)
    if start_ms and span < 600_000:      # 跨度 < 10 分钟
        if t1 > start_ms:
            t0 = float(start_ms)
            span = max(1.0, t1 - t0)
    end_ms = int(getattr(snap, "live_end_ts", 0) or 0)
    if end_ms and end_ms > t1 and span < 600_000:
        t1 = float(end_ms)
        span = max(1.0, t1 - t0)

    # 分桶数：按行高算，最多 4 桶
    row_h = P(17)
    max_rows = max(2, int(h / row_h))
    buckets_n = min(4, max_rows, max(2, int(span / 1800) + 1))

    buckets = [0] * buckets_n
    for ts in ts_list:
        i = min(buckets_n - 1, int((ts - t0) / span * buckets_n))
        buckets[i] += 1

    def _hhmm(ms: float) -> str:
        import datetime

        try:
            return datetime.datetime.fromtimestamp(ms / 1000).strftime("%H:%M")
        except (ValueError, OSError, OverflowError):
            return ""

    f = _font(P(10))
    for i, cnt in enumerate(buckets):
        seg_start = t0 + span * i / buckets_n
        seg_end = t0 + span * (i + 1) / buckets_n
        # 圆点
        d.ellipse([x, y + P(4), x + P(6), y + P(10)],
                  fill=ORANGE if i % 2 == 0 else ORANGE_L)
        label = f"{_hhmm(seg_start)} - {_hhmm(seg_end)}　{cnt} 条"
        d.text((x + P(12), y), label, font=f, fill=INK)
        y += row_h


def _danmaku_freq_hourly(
    d: Any, hourly: list, x: int, y: int, w: int, h: int, P: Any,
) -> None:
    """用 SQLite 的小时聚合数据画弹幕频次。

    每行一个时段：HH:00 - HH:00　N 条。
    时段太多时合并成最多 5 段。
    """
    rows = sorted(
        [x for x in hourly if isinstance(x, dict)],
        key=lambda x: int(x.get("hour") or 0),
    )
    max_rows = max(2, min(5, int(h / P(17))))
    n = len(rows)
    if n <= max_rows:
        groups = [[r] for r in rows]
    else:
        per = (n + max_rows - 1) // max_rows
        groups = [rows[i2:i2 + per] for i2 in range(0, n, per)]

    f = _font(P(10))
    row_h = P(17)
    for gi, g in enumerate(groups):
        h0 = int(g[0].get("hour") or 0)
        h1 = int(g[-1].get("hour") or 0) + 1
        cnt = sum(int(r.get("danmaku") or 0) for r in g)
        d.ellipse(
            [x, y + P(4), x + P(6), y + P(10)],
            fill=ORANGE if gi % 2 == 0 else ORANGE_L,
        )
        label = f"{h0:02d}:00 - {h1:02d}:00　{cnt} 条"
        d.text((x + P(12), y), label, font=f, fill=INK)
        y += row_h


def _wordcloud(d: Any, words: list[tuple[str, int]], x: int, y: int,
               w: int, h: int, P: Any) -> None:
    """词云：流式排版，左→右自动换行，不越界。"""
    if h <= 0:
        return
    if not words:
        d.text((x, y + P(4)), "暂无弹幕数据", font=_font(P(10)), fill=GRAY)
        return

    maxc = words[0][1] or 1
    cx, cy = x, y
    line_h = 0

    for i, (word, c) in enumerate(words):
        ratio = c / maxc
        size = int(11 + 15 * ratio)
        f = _font(P(size))
        tw = _tl(d, word, f)
        th = P(size) + P(3)

        if tw > w:
            ns = max(9, int(size * w / max(tw, 1)))
            f = _font(P(ns))
            tw = _tl(d, word, f)
            th = P(ns) + P(3)

        if cx + tw > x + w:
            cx = x
            cy += line_h + P(3)
            line_h = 0

        if cy + th > y + h:
            break

        d.text((cx, cy), word, font=f, fill=CLOUD_COLORS[i % len(CLOUD_COLORS)])
        cx += tw + P(7)
        line_h = max(line_h, th)


# ══════════════════════════════════════
# 数据辅助
# ══════════════════════════════════════

def _date_label(snap: Any) -> str:
    """图表底部的日期标签，形如「2026-11-18 LIVE」。"""
    s = str(snap.live_start_text or "")
    d = s[:10] if len(s) >= 10 else ""
    if not d:
        import datetime

        d = datetime.date.today().isoformat()
    tag = "LIVE" if snap.is_live_now else "END"
    return f"{d} {tag}"


def _payers(snap: Any) -> list:
    out = []
    for p in (snap.payer_list or []):
        if isinstance(p, dict) and _pay_value(p) > 0:
            out.append(p)
    return out


def _pay_value(p: dict) -> float:
    return float(p.get("giftValue") or 0) + float(p.get("scValue") or 0)


def _pay_text(p: dict) -> str:
    v = _pay_value(p)
    return f"¥{v:.2f}" if v < 100 else f"¥{v:.0f}"


def _enterers(timeline: list) -> list[dict]:
    """从时间序列统计入场排行。"""
    if not timeline:
        return []
    cnt: Counter = Counter()
    info: dict = {}
    for m in timeline:
        if not isinstance(m, dict):
            continue
        if m.get("kind") == "interact" and m.get("sub") == "enter":
            uid = int(m.get("uid") or 0)
            cnt[uid] += 1
            if uid not in info:
                info[uid] = str(m.get("uname") or "?")
    return [
        {"uid": uid, "uname": info.get(uid, "?"), "count": c}
        for uid, c in cnt.most_common(8)
    ]


def _nice_ceiling(v: float) -> float:
    """把数值向上取整到「好看的刻度」，用于单柱图的参考高度。

    例：5 → 10, 27 → 50, 124 → 200, 1836 → 2000, 2931 → 5000
    """
    if v <= 0:
        return 1.0
    import math as _m

    exp = _m.floor(_m.log10(v))
    base = 10 ** exp
    frac = v / base
    for step in (1, 2, 5, 10):
        if frac <= step:
            return step * base
    return 10 * base


def _latest_md(snap: Any) -> str:
    """取最近一天的 "MM-DD" 标签，用于单柱图 X 轴。

    优先用 daily_revenue 的最后一天，其次 daily，最后取当天。
    """
    import datetime as _dt

    for attr in ("daily_revenue", "daily"):
        lst = getattr(snap, attr, None) or []
        if lst and isinstance(lst[-1], dict):
            d = str(lst[-1].get("date") or "")
            if len(d) >= 10:
                return d[5:10]
    s = str(getattr(snap, "live_start_text", "") or "")
    if len(s) >= 10:
        return s[5:10]
    return _dt.date.today().isoformat()[5:10]


def _shares(timeline: list, snap: Any = None) -> int:
    """分享次数：优先用精确统计（不受 5000 条截断），否则从 timeline 数。"""
    if snap is not None:
        ex = getattr(snap, "exact", None) or {}
        if isinstance(ex, dict):
            v = ex.get("share")
            if v is not None:
                return int(v)
    n = 0
    for m in (timeline or []):
        if isinstance(m, dict) and m.get("kind") == "interact" and m.get("sub") == "share":
            n += 1
    return n


# 平台自动发送的模板弹幕（福袋/抽奖/任务引导），不参与词云统计
AUTO_DANMAKU_PATTERNS = (
    "参与天选福袋",
    "点点关注",
    "天选福袋",
    "关注主播参与",
    "参与抽奖",
    "点击领取",
    "完成任务",
    "加入粉丝团",
    "分享了直播间",
    "领取红包",
    "领取福袋",
    "关注后参与",
    "发送弹幕参与",
    "参与互动",
    "签到成功",
    "已关注主播",
    "为主播点赞",
    "投喂",
    "开通大航海",
)


def _is_auto_danmaku(text: str) -> bool:
    """判断是不是平台自动发的模板弹幕。

    这类弹幕人人一样、刷屏量极大，会把词云完全淹掉
    （实测「点点关注，参与天选福袋啦！」几分钟刷几百条）。
    """
    t = str(text or "").strip()
    if not t:
        return True
    if len(t) < 2:
        return True
    for p in AUTO_DANMAKU_PATTERNS:
        if p in t:
            return True
    # 纯符号 / 纯数字
    if not re.search(r"[\u4e00-\u9fa5A-Za-z]", t):
        return True
    return False


def _top_words(samples: list[str], n: int = 26) -> list[tuple[str, int]]:
    if not samples:
        return []
    stop = {
        "的", "了", "是", "我", "你", "他", "在", "有", "和", "就", "都", "也",
        "不", "吗", "呢", "吧", "啊", "呀", "哦", "嗯", "这", "那", "个", "会",
        "要", "到", "说", "看", "好", "人", "很", "还", "对", "can", "the",
    }
    cnt: Counter = Counter()
    for s in samples:
        text = str(s or "")
        # 跳过平台自动发的模板弹幕（福袋/抽奖/任务），否则词云全是这些
        if _is_auto_danmaku(text):
            continue
        for w in re.findall(r"[A-Za-z]{2,}", text):
            if w.lower() not in stop:
                cnt[w] += 1
        zh = re.sub(r"[^\u4e00-\u9fa5]+", " ", text)
        for seg in zh.split():
            for size in (2, 3):
                for i in range(0, max(1, len(seg) - size + 1)):
                    g = seg[i:i + size]
                    if len(g) == size and g not in stop:
                        cnt[g] += 1
    items = [(w, c) for w, c in cnt.items() if c >= 2]
    items.sort(key=lambda x: -x[1])
    picked: list[tuple[str, int]] = []
    for w, c in items:
        if any(w != p and w in p for p, _ in picked):
            continue
        picked.append((w, c))
        if len(picked) >= n:
            break
    return picked



def render_sub_list(
    rooms: list,
    *,
    avatars: dict | None = None,
    width: int = TOTAL_W,
    scale: int = 2,
) -> bytes:
    """渲染「订阅列表」图片。

    分区展示：
      · 正在直播（红点 + 头像 + 人气 + 标题 + 房间号）
      · 未开播（灰点 + 头像 + 上次直播时间）

    rooms 每项：
      {"roomId","uname","face","live","online","title",
       "lastStartText","lastDurationText"}
    avatars: {roomId: bytes} 预先下载好的头像
    """
    S = scale

    def P(v: float) -> int:
        return int(round(v * S))

    living = [r for r in (rooms or []) if isinstance(r, dict) and r.get("live")]
    offline = [r for r in (rooms or []) if isinstance(r, dict) and not r.get("live")]

    head_h = 74
    row_h = 48          # 在播行（含头像 36）
    row_h_off = 40      # 未播行
    sect_h = 34
    n_live = len(living)
    n_off = len(offline)

    H = head_h
    if n_live:
        H += sect_h + n_live * row_h
    if n_off:
        H += sect_h + n_off * row_h_off
    H = int(H + 46)

    """
    宽度自适应：先按主体内容估算，页脚也按这个宽度画，
    最后统一裁剪右侧空白。
    """
    def _text_px(txt: str, px: int, bold: bool = False) -> int:
        """粗略估算文字宽度（中文按 1em，英文按 0.55em）。"""
        n = 0.0
        for ch in str(txt or ""):
            n += 1.08 if ord(ch) > 0x2E80 else 0.58
        return int(n * px)

    AV_W = 34 + 12          # 头像 + 间距
    need = 380              # 最小宽度
    for r in living:
        nm = str(r.get("uname") or "")
        pop = _num(int(r.get("online") or 0)) if r.get("online") else "—"
        rid = str(int(r.get("roomId") or 0))
        need = max(need, AV_W + _text_px(rid, 10) + 8 + _text_px(nm, 12)
                   + 10 + _text_px("人气 " + pop, 10) + 24)
        t = str(r.get("title") or "")
        if t:
            need = max(need, AV_W + _text_px(t, 10) + 24)
    for r in offline:
        nm = str(r.get("uname") or "")
        rid = str(int(r.get("roomId") or 0))
        w2 = AV_W + _text_px(nm, 11) + 4 + _text_px("（" + rid + "）", 9) + 12
        ls = str(r.get("lastStartText") or "")
        if ls:
            ld = str(r.get("lastDurationText") or "")
            meta = "上次 " + ls + (f"（{ld}）" if ld else "")
            w2 += _text_px(meta, 9) + 12
        need = max(need, w2 + 24)

    W = max(520, min(int(width), need + PAD * 2))
    Wp, Hp = P(W), P(H)
    img = Image.new("RGB", (Wp, Hp), BG)
    d = ImageDraw.Draw(img, "RGBA")

    x = P(PAD)
    w = P(W - PAD * 2)
    y = P(PAD)
    avatars = avatars or {}

    def _paste_avatar(room_id: int, ax: int, ay: int, size: int) -> bool:
        """贴头像，返回是否成功（失败画个灰圆占位）。"""
        data = avatars.get(room_id)
        if data:
            try:
                av = Image.open(io.BytesIO(data)).convert("RGBA")
                aw, ah = av.size
                side = min(aw, ah)
                av = av.crop(((aw - side) // 2, (ah - side) // 2,
                              (aw + side) // 2, (ah + side) // 2))
                av = av.resize((size, size), Image.LANCZOS)
                mask = Image.new("L", (size, size), 0)
                ImageDraw.Draw(mask).ellipse([0, 0, size - 1, size - 1], fill=255)
                img.paste(av, (ax, ay), mask)
                return True
            except Exception:  # noqa: BLE001
                pass
        # 占位：彩色圆 + 首字
        pal = [(232, 122, 108), (110, 160, 210), (140, 180, 120),
               (200, 150, 90), (160, 130, 200), (100, 170, 170)]
        c = pal[room_id % len(pal)]
        d.ellipse([ax, ay, ax + size, ay + size], fill=c)
        ch = ""
        for it in (rooms or []):
            if int(it.get("roomId") or 0) == room_id:
                ch = str(it.get("uname") or "")[:1]
                break
        if ch:
            fz = _font(int(size * 0.5))
            cw2 = _tl(d, ch, fz)
            bb = d.textbbox((0, 0), ch, font=fz)
            chh = bb[3] - bb[1]
            d.text((ax + (size - cw2) / 2, ay + (size - chh) / 2 - bb[1]),
                   ch, font=fz, fill=(255, 255, 255))
        return False

    # ── 头部 ──
    total = n_live + n_off
    d.text((x, y), "订阅列表", font=_font(P(22), bold=True), fill=INK)
    d.text((x, y + P(30)), f"共 {total} 个主播　·　{n_live} 个正在直播",
           font=_font(P(11)), fill=SUB)
    y += P(head_h)
    d.line([(x, y - P(10)), (x + w, y - P(10))], fill=LINE, width=P(1))

    AV = P(34)          # 头像直径
    TX = AV + P(12)     # 文字起始偏移

    # ── 正在直播 ──
    if n_live:
        d.text((x, y), f"正在直播（{n_live}）", font=_font(P(13), bold=True), fill=RED)
        y += P(sect_h)

        f_nm = _font(P(12), bold=True)
        f_md = _font(P(10))
        for r in living:
            room_id = int(r.get("roomId") or 0)
            _paste_avatar(room_id, x, y, AV)

            uname = str(r.get("uname") or "?")
            online = int(r.get("online") or 0)

            # 房间号在最前
            cx1 = x + TX
            d.text((cx1, y + P(2)), str(room_id), font=f_md, fill=SUB)
            cx1 += _tl(d, str(room_id), f_md) + P(8)

            # 主播名
            d.text((cx1, y), uname, font=f_nm, fill=INK)
            nw = _tl(d, uname, f_nm)
            cx1 += nw + P(10)

            # 人气
            pop = _num(online) if online else "—"
            d.text((cx1, y + P(2)), f"人气 {pop}", font=f_md, fill=ORANGE)

            # 标题第二行
            title = str(r.get("title") or "")
            if title:
                max_w = w - TX
                if _tl(d, title, f_md) > max_w:
                    while title and _tl(d, title + "…", f_md) > max_w:
                        title = title[:-1]
                    title += "…"
                d.text((x + TX, y + P(19)), title, font=f_md, fill=SUB)
            y += P(row_h)

    # ── 未开播 ──
    if n_off:
        d.text((x, y), f"未开播（{n_off}）", font=_font(P(13), bold=True), fill=GRAY)
        y += P(sect_h)

        f_nm2 = _font(P(11))
        f_meta = _font(P(9))
        for r in offline:
            room_id = int(r.get("roomId") or 0)
            _paste_avatar(room_id, x, y, AV)

            uname = str(r.get("uname") or "?")
            """
            未开播行（从左往右紧凑排列）：
              头像　主播名（UID）　上次 2026-11-18 19:58（4小时18分钟）
            """
            cx2 = x + TX

            # ① 主播名（宽度已按内容算好，这里只做兜底截断）
            last_start0 = str(r.get("lastStartText") or "")
            last_dur0 = str(r.get("lastDurationText") or "")
            meta0 = ""
            if last_start0:
                meta0 = f"上次 {last_start0}"
                if last_dur0:
                    meta0 += f"（{last_dur0}）"
            rid0 = f"（{room_id}）"
            reserve = _tl(d, rid0, f_meta) + P(12)
            if meta0:
                reserve += _tl(d, meta0, f_meta) + P(12)
            max_w = w - (cx2 - x) - reserve
            if max_w < P(60):
                max_w = P(60)
            if _tl(d, uname, f_nm2) > max_w:
                while uname and _tl(d, uname + "…", f_nm2) > max_w:
                    uname = uname[:-1]
                uname += "…"
            d.text((cx2, y), uname, font=f_nm2, fill=INK)
            cx2 += _tl(d, uname, f_nm2) + P(4)

            # ② UID（紧跟名字，括号包裹）
            uid_txt = f"（{room_id}）"
            d.text((cx2, y + P(2)), uid_txt, font=f_meta, fill=SUB)
            cx2 += _tl(d, uid_txt, f_meta) + P(12)

            # ③ 上次直播时间
            last_start = str(r.get("lastStartText") or "")
            last_dur = str(r.get("lastDurationText") or "")
            if last_start:
                meta = f"上次 {last_start}"
                if last_dur:
                    meta += f"（{last_dur}）"
                d.text((cx2, y + P(3)), meta, font=f_meta, fill=SUB)
            y += P(row_h_off)

    # ── 页脚 ──
    fy = P(H - 30)
    d.line([(x, fy - P(6)), (x + w, fy - P(6))], fill=LINE, width=P(1))
    import datetime as _dt

    now_s = _dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    d.text((x, fy), f"{now_s} · AstrBot x 鬼汐酱　·　数据来源：汐汐工具箱",
           font=_font(P(9)), fill=GRAY)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    return out.getvalue()
