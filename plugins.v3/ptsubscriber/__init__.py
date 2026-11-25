"""PtSubscriber —— MoviePilot v3 插件：定时订阅下载面板。

补 MP 缺的两点：① 自己选周几/几点去指定站点搜资源；② 订阅规则简单直观。
站点、Cookie、下载器全部直接复用 MP，插件不维护第二份凭据。
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body

from app.plugins import _PluginBase

try:
    from app.sdk.logging import logger
except ImportError:  # 兼容旧版宿主
    import logging

    logger = logging.getLogger("PtSubscriber")

from . import intercept, remote, remote_ui
from .core import Store, filter_candidates
from .meta import poster_url as meta_poster_url
from .meta import resolve as resolve_meta
from .meta import search as search_meta
from .meta import search_fast as search_meta_fast
from .meta import air_dates as meta_air_dates
from .meta import library_eps as meta_library_eps
from .meta import library_movie as meta_library_movie
from .meta import transferred_eps as meta_transferred_eps
from .meta import downloaded_eps as meta_downloaded_eps
from .models import (
    next_episode,
    parse_episode,
    recommend_from_air,
    resolution_of,
    schedule_label,
    search_keyword,
)
from .mp_adapter import MPBridge
from .rules import RULE_OPTIONS, parse_rules

try:
    from app.core.config import settings as _settings
    TZ = getattr(_settings, "TZ", None) or "Asia/Shanghai"
except Exception:
    TZ = "Asia/Shanghai"

# 事件总线：V3 走 app.sdk.events，旧宿主回退 app.core.event；都没有（本机单测）时降级。
try:
    from app.sdk.events import eventmanager
except Exception:
    try:
        from app.core.event import eventmanager
    except Exception:
        eventmanager = None
try:
    from app.schemas.types import EventType
except Exception:
    EventType = None


def _register(etype):
    """注册事件响应；单测环境没有事件总线时退化为直接返回函数。"""
    if eventmanager is None or etype is None:
        return lambda func: func
    return eventmanager.register(etype)


# 装饰器表达式会在导入期求值，先抽出来判空，避免 EventType 为 None 时崩掉
_PLUGIN_ACTION_EVENT = getattr(EventType, "PluginAction", None) if EventType is not None else None


class PluginDataBackend:
    """把 _PluginBase.save_data / get_data 包装成 Store 的存储接口。"""

    def __init__(self, plugin: "PtSubscriber") -> None:
        self.plugin = plugin

    def load(self, key: str, default: Any = None) -> Any:
        try:
            value = self.plugin.get_data(key=key)
        except Exception:
            value = None
        return value if value is not None else default

    def save(self, key: str, value: Any) -> None:
        self.plugin.save_data(key=key, value=value)


def _recommend_start(sub: dict, tz: str):
    """recommend 模式的锚点：recommend_date 那天 HH:MM（本地时区）。

    用户没定星期几几点（schedule_mode=recommend）时，按 TMDB 推荐更新时间当天开始
    每天守候，避免错过免费种子；日期填错/过期就返回 None，退回普通每天。
    """
    from datetime import datetime

    text = str(sub.get("recommend_date") or "")
    if len(text) < 10:
        return None
    try:
        hour, minute = (int(x) for x in str(sub.get("schedule_time") or "19:00").split(":"))
        tzinfo = None
        try:
            from zoneinfo import ZoneInfo

            tzinfo = ZoneInfo(tz)
        except Exception:  # noqa: BLE001 - 老环境没 zoneinfo 就按本地
            pass
        return datetime(int(text[:4]), int(text[5:7]), int(text[8:10]), hour, minute, tzinfo=tzinfo)
    except (ValueError, TypeError):
        return None


def _parse_dt(text, now):
    """把 "2026-11-21 20:00:00" 这类本地时间串解析成带时区的 datetime；解析不了返回 None。"""
    from datetime import datetime

    raw = str(text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=now.tzinfo)
        except (ValueError, TypeError):
            continue
    return None


_WEEKDAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def last_fire_time(sub: dict, now, last_at=None):
    """这条订阅「最近一次本该执行」的时间点（<= now）。

    用来判断开机时有没有错过计划：APScheduler 用的是内存调度器，NAS 关机、断网、
    MP 重启期间到点的 cron 不会补跑，重启后从下一个计划时间才开始，
    于是「昨晚该下的集」就彻底漏掉了。拿它和日志里的上次运行时间比对就能发现。
    """
    from datetime import timedelta

    mode = sub.get("schedule_mode", "recommend")
    if mode == "interval":
        # 间隔模式没有固定时间点：上次跑过之后隔了一个间隔就该跑，超时即视为错过
        base = last_at or _parse_dt(sub.get("created_at"), now)
        if base is None:
            return None
        return base + timedelta(minutes=max(5, int(sub.get("schedule_interval") or 120)))
    try:
        hour, minute = (int(x) for x in str(sub.get("schedule_time") or "19:00").split(":"))
    except (TypeError, ValueError):
        hour, minute = 19, 0
    fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if mode == "weekly":
        key = str(sub.get("schedule_weekday") or "mon").lower()[:3]
        fire -= timedelta(days=(fire.weekday() - _WEEKDAY_INDEX.get(key, 0)) % 7)
        if fire > now:
            fire -= timedelta(days=7)
    elif fire > now:
        fire -= timedelta(days=1)
    # 注意：recommend 模式不看 recommend_date 这个锚点。
    # 锚点取的是 TMDB 的「下一集播出日」，一旦台账落后（该下没下），锚点会被推到
    # 更晚的那一集，照它判断就会认为「还没开始守候」而永远不补（《仙逆》真机案例：
    # 已播到 168、水位 167，锚点却是 169 的 11-29）。有没有东西可补交给
    # _need_catchup 按节目表判断，这里只给「最近该跑的时间」。
    return fire


def build_trigger(sub: dict, tz: str):
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    mode = sub.get("schedule_mode", "recommend")
    if mode == "interval":
        return IntervalTrigger(minutes=max(5, int(sub.get("schedule_interval") or 120)), timezone=tz)
    hour, minute = (int(x) for x in str(sub.get("schedule_time") or "19:00").split(":"))
    start = _recommend_start(sub, tz) if mode == "recommend" else None
    if mode == "weekly":
        return CronTrigger(day_of_week=sub.get("schedule_weekday") or "mon",
                           hour=hour, minute=minute, start_date=start, timezone=tz)
    return CronTrigger(hour=hour, minute=minute, start_date=start, timezone=tz)


# ---- 资源名精准匹配（排序用） ------------------------------------------------
# 搜「天下第一」时 PT 站会混进《天下第一剑》《天下第一楼》《安居·又名天下第一难》
# 这类同名前缀/同名的杂项。下面先把「片名」归一化，再按命中精度分层排序。
_EP_WORDS_RE = re.compile(
    r"(?:全|第|共)?\d{1,4}\s*(?:集|话|期|季)|S\d{1,2}\s*E?\d{1,3}"
    r"|\bEP?\s*\d{1,3}\b|全集|全季|全\d+集|第\d+季|第\d+季?集|第\d+集",
    re.I)
_YEAR_RE = re.compile(r"[（(]\s*(?:19|20)\d{2}\s*[）)]|\b(?:19|20)\d{2}\s*版?\b")
_PURE_PATTERN = (
    r"(?:\d{3,4}p|2160p|bluray|blu-ray|bdrip|webdl|webrip|x26[45]|h\.?26[45]"
    r"|hevc|avc|1080|720|480|4k|2k|hdr|sdr|remux|bd|web|\d+bit"
    r"|简繁|简中|繁中|国语|粤语|双语|中字|字幕|修复|重制|高码|完结|高清|蓝光"
    r"|全集|全季|全\d+集|第?\d{1,3}集|第?\d{1,2}季|S\d{1,2}E?\d{1,3}|EP?\d{1,3}"
    r"|[0-9]+)")
_PURE_SUFFIX_RE = re.compile(r"^(?:" + _PURE_PATTERN + r")$", re.I)
_PURE_HEAD_RE = re.compile(r"^(?:" + _PURE_PATTERN + r")", re.I)
_FOREIGN_WORDS = re.compile(r"[剑楼传记录史志侠之篇外传前传后传上下部]|又名|别名")


def _norm_name(s: str) -> str:
    """归一化片名：去年份/版本括号、集数季词、非文字符号，统一小写。"""
    s = str(s or "")
    s = _YEAR_RE.sub("", s)
    s = _EP_WORDS_RE.sub("", s)
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "", s)
    return s.lower()


def _is_pure_suffix(rest: str) -> bool:
    """去掉片名后剩下的尾巴是否为「纯版本/封装词」——是则视为同一部。

    例如「1080pwebdl」「国语中字1080p」都是同一部的封装/配音/字种，
    算 tier0；而「剑」「楼」这类是真正的异名，算 tier1。
    """
    if not rest:
        return True
    if _PURE_SUFFIX_RE.fullmatch(rest):
        return True
    if _FOREIGN_WORDS.search(rest):
        return False
    # 逐个吞噬「版本词 / 短中文词」，能全部吞掉就是纯封装尾巴
    while rest:
        m = _PURE_HEAD_RE.match(rest)
        if m and m.end() > 0:
            rest = rest[m.end():]
            continue
        m2 = re.match(r"[\u4e00-\u9fff]{1,4}", rest)
        if m2 and not _FOREIGN_WORDS.search(m2.group(0)):
            rest = rest[m2.end():]
            continue
        return False
    return True


def _name_tier(core: str, title: str) -> int:
    """命中精度分层：0=完全同名 1=标题含片名 2=片名含标题 3=勉强沾边。"""
    if not core:
        return 3
    t = _norm_name(title)
    if not t:
        return 3
    if core == t:
        return 0
    if t.startswith(core) or t.endswith(core):
        rest = t[len(core):] if t.startswith(core) else t[: len(t) - len(core)]
        return 0 if _is_pure_suffix(rest) else 1
    if core in t:
        return 1
    if t in core:
        return 2
    return 3


class PtSubscriber(_PluginBase):
    plugin_name = "定时订阅下载"
    plugin_desc = "默认按 TMDB 推荐更新时间自动到指定站点搜资源（也可自选每天/每周指定星期几点），按详细规则下载到指定容器；自动对接 TMDB，按片名、年份、剧集/电影、集数逐项比对，只把真正这部排在最前，并把每集播出日列成表；读媒体库只补没入库的集，已下过的（含删掉的）不重下。聊天入口不用打斜杠，一条说全最省事：「订阅 天下第一 观众 1080P 周一5:30」（片名/站点/清晰度/周期顺序随意）；也可以只发「订阅 天才女友」先列同名作品，回序号后再选「从第几集 / 哪些站 / 多久刷一次」，回「确定」立刻补搜并把能下的资源列出来让你挑；「查资源 仙逆 馒头 1080P」只搜一轮直接下，不建订阅。斜杠命令 /ptsub、/ptfind、/ptlist 一样可用；回复全是纯文本不带图。"
    plugin_version = "1.10.25"
    plugin_author = "wzy"
    plugin_config_prefix = "ptsubscriber_"
    plugin_order = 90
    auth_level = 1

    def __init__(self) -> None:
        super().__init__()
        self._enabled = True
        self._max_results = 50
        self._scheduler = None
        # 每轮重试的运行时状态：{订阅 id: {started: 首次开搜时间戳, tries: 已重试次数}}
        self._retry_state: Dict[str, Dict[str, Any]] = {}
        # 远程「列候选→回序号」的进行中状态：{key: {name, kind, candidates, at}}
        self._pending: Dict[str, Dict[str, Any]] = {}
        # 远程「列资源→回序号直下」的进行中状态：{key: {name, token, rows, at}}
        self._pending_dl: Dict[str, Dict[str, Any]] = {}
        self.store: Store = Store(PluginDataBackend(self))
        self.bridge = MPBridge(logger)

    # ------------------------------------------------------------ 生命周期
    def init_plugin(self, config: Optional[dict] = None) -> None:
        self.stop_service()
        cfg = config or {}
        self._enabled = bool(cfg.get("enabled", True))
        self._max_results = max(10, min(200, int(cfg.get("max_results") or 50)))
        self.store = Store(PluginDataBackend(self))
        self.bridge = MPBridge(logger)
        # 老订阅里插件早/上一版默认的 03:00、20:00 统一搬到晚上 19:00（自己定过时间的模式不动）
        try:
            fixed = self.store.migrate_evening_default()
            if fixed:
                logger.info(f"[{self.plugin_name}] {fixed} 条订阅的默认执行时间统一改为 19:00")
        except Exception as exc:  # noqa: BLE001 - 迁移失败不影响启动
            logger.debug(f"[{self.plugin_name}] 执行时间迁移跳过：{type(exc).__name__}: {exc}")
        # 「每天」是老默认值，现在默认是「按 TMDB 推荐时间」，顺手把老订阅搬过去
        try:
            moved = self.store.migrate_daily_to_recommend()
            if moved:
                logger.info(f"[{self.plugin_name}] {moved} 条订阅的执行模式改为「按 TMDB 推荐时间」")
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{self.plugin_name}] 执行模式迁移跳过：{type(exc).__name__}: {exc}")
        # 智能体工具靠模块级引用拿插件实例；重载后指向新实例
        try:
            remote.plugin_ref = self
        except Exception:
            pass
        if not self._enabled:
            return
        self._self_check()
        # 接管聊天渠道里的普通中文消息（「订阅 X」「1」），否则会被 MP 原生订阅链抢先
        try:
            ok = intercept.install(self.remote_intercept_text)
            logger.info(f"[{self.plugin_name}] 聊天文本接管："
                        + ("已启用（订阅 / 找资源 / 回序号都由插件处理）" if ok else "未启用"))
        except Exception as exc:  # noqa: BLE001 - 接管失败也要能正常用斜杠命令
            logger.warning(f"[{self.plugin_name}] 聊天文本接管失败：{type(exc).__name__}: {exc}")
        try:
            from apscheduler.schedulers.background import BackgroundScheduler

            self._scheduler = BackgroundScheduler(timezone=TZ)
            self._rebuild_jobs()
            self._scheduler.start()
            logger.info(f"[{self.plugin_name}] 已启动，{len(self._scheduler.get_jobs())} 条订阅计划")
            # 关机/断网/重启会丢掉到点的计划：开机 75 秒后先扫一遍补上，之后每 30 分钟巡检
            self._install_catchup_jobs()
            # 入库状态也要自己跟上：下载完 → 转移 → 媒体库同步有十几分钟延迟，
            # 界面不能等用户手动点「同步海报」才刷新
            self._install_lib_job()
        except Exception as exc:
            logger.error(f"[{self.plugin_name}] 调度器启动失败：{type(exc).__name__}: {exc}")
            self._scheduler = None

    def _self_check(self) -> None:
        """在 MP 主进程内自检数据源（此时启动组合根已就绪），只记数量不记凭据。"""
        parts: list[str] = []
        try:
            parts.append(f"站点 {len(self.bridge.list_sites())} 个")
        except Exception as exc:
            parts.append(f"站点读取失败 {type(exc).__name__}: {str(exc)[:80]}")
        try:
            names = [d["name"] for d in self.bridge.list_downloaders()]
            parts.append(f"下载器 {len(names)} 个 [{', '.join(names)}]")
        except Exception as exc:
            parts.append(f"下载器读取失败 {type(exc).__name__}: {str(exc)[:80]}")
        try:
            indexers = self.bridge._indexers()
            parts.append(f"索引器 {len(indexers)} 条")
        except Exception as exc:  # noqa: BLE001
            parts.append(f"索引读取失败 {type(exc).__name__}: {str(exc)[:80]}")
        logger.info(f"[{self.plugin_name}] 数据源自检：" + "；".join(parts))
        for line in self._index_diag():
            logger.info(f"[{self.plugin_name}] {line}")

    def _index_diag(self) -> list[str]:
        """排障：MP 用户认证级别 + 索引器/站点数量（只打数量与状态，不含凭据）。"""
        out: list[str] = []
        try:
            indexers = self.bridge._indexers()
            sites = self.bridge.list_sites()
            level = self.bridge.auth_level()
        except Exception as exc:  # noqa: BLE001
            return [f"索引诊断失败 {type(exc).__name__}: {str(exc)[:120]}"]
        out.append(f"用户认证级别 {level}（≥2 才可搜）；索引器 {len(indexers)} 条；站点 {len(sites)} 个")
        if level == -1:
            out.append("用户认证级别读取失败（不影响搜索，交由 MP 判断）")
        elif level < 2:
            out.append("MP 系统级用户认证未通过：站点功能被临时禁用，搜索会返回空。"
                       "这是「站点认证好了却搜不了」的真正原因")
        mt = [s for s in sites if "m-team" in str(s.get("domain") or "").lower()]
        if mt:
            s = mt[0]
            out.append(f"馒头 sid={s.get('sid')} active={s.get('is_active')} "
                       f"认证={'有' if s.get('has_auth') else '无'}")
        return out

    def stop_service(self) -> None:
        try:
            intercept.uninstall()
        except Exception:  # noqa: BLE001 - 还原失败不影响停服
            pass
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
        self._retry_state.clear()
        self._pending.clear()
        self._pending_dl.clear()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_render_mode() -> Tuple[str, Optional[str]]:
        return "vue", "dist/assets"

    def get_sidebar_nav(self) -> List[Dict[str, Any]]:
        """左侧导航两个入口：任务墙 + 找资源。

        找资源以前是插件页里的一个 tab，和工具条按钮重复；现在独立成一页，
        nav_key=search 进去只渲染搜索面板（前端的 AppPage 按 navKey 分支）。
        """
        if not self.get_state():
            return []
        return [
            {
                "nav_key": "main",
                "title": "定时订阅下载",
                "icon": "mdi-download-box",
                "section": "subscribe",
                "permission": "subscribe",
                "order": 20,
            },
            {
                "nav_key": "search",
                "title": "找资源",
                "icon": "mdi-magnify",
                "section": "subscribe",
                "permission": "subscribe",
                "order": 21,
            },
        ]

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return None, {"enabled": self._enabled, "max_results": self._max_results}

    def get_page(self) -> Optional[List[dict]]:
        return None

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """注册远程命令（QQ / 微信 / TG 里直接发，穿透到插件，不走 MP 自带订阅）。

        /ptsub  片名 站点 清晰度 周期 → 一条建订阅（如：天下第一 观众 1080P 周一5:30）
        /ptfind 片名 站点 清晰度 → 只找资源直接下发下载器，不建订阅
        /ptlist      → 看自己有哪些订阅、下到第几集
        """
        try:
            event_type = EventType.PluginAction
        except Exception:
            event_type = None
        return [
            {
                "cmd": "/ptsub",
                "event": event_type,
                "desc": "订阅（定时订阅下载插件）",
                "category": "订阅",
                "data": {"action": "ptsub_subscribe"},
            },
            {
                "cmd": "/ptfind",
                "event": event_type,
                "desc": "找资源直接下载（不订阅）",
                "category": "下载",
                "data": {"action": "ptsub_find"},
            },
            {
                "cmd": "/ptlist",
                "event": event_type,
                "desc": "我的订阅与进度",
                "category": "订阅",
                "data": {"action": "ptsub_list"},
            },
        ]

    def get_agent_tools(self) -> List[Any]:
        """注册智能助手工具：说「订阅片名」「找资源下载」即可，不经 MP 自带订阅。"""
        try:
            return remote.build_agent_tools()
        except Exception as err:  # noqa: BLE001 - 智能助手不可用不影响插件其它功能
            logger.debug(f"{self.plugin_name}：智能助手工具不可用：{err}")
            return []

    @_register(_PLUGIN_ACTION_EVENT)
    def command_action(self, event: Any) -> None:
        """QQ / TG / 微信等渠道的远程命令：event_data["arg_str"] 是片名或序号。"""
        event_data = (event.event_data or {}) if event is not None else {}
        action = str(event_data.get("action") or "")
        if action not in ("ptsub_subscribe", "ptsub_find", "ptsub_list"):
            return
        channel = event_data.get("channel")
        source = event_data.get("source")
        user = event_data.get("user")
        arg = str(event_data.get("arg_str") or "").strip()
        self._run_remote_command(arg, channel, source, user, action=action)

    def get_service(self) -> Optional[List[dict]]:
        # 每条订阅独立计划，用插件自有调度器动态增删，不占用宿主公共服务表。
        return None

    def get_api(self) -> List[Dict[str, Any]]:
        # 动态插件路由默认 auth="apikey"，校验的是 API_TOKEN；本 NAS 上 API_TOKEN 为空，
        # 因此必须显式声明 bear，走宿主登录态 JWT（与 BrushFlow / CourseOrganizer 一致）。
        # 宿主前端注入的 api 实例会自动携带 Authorization: Bearer <jwt>。
        routes = [
            {"path": "/options", "endpoint": self.api_options, "methods": ["GET"], "summary": "站点/下载器/规则选项"},
            {"path": "/subs", "endpoint": self.api_subs, "methods": ["GET"], "summary": "订阅列表"},
            {"path": "/logs", "endpoint": self.api_logs, "methods": ["GET"], "summary": "运行日志与命中"},
            {"path": "/probe", "endpoint": self.api_probe, "methods": ["GET"], "summary": "自检"},
            {"path": "/meta/refresh", "endpoint": self.api_meta_refresh, "methods": ["POST"], "summary": "同步海报与总集数"},
            {"path": "/meta/search", "endpoint": self.api_meta_search, "methods": ["GET"], "summary": "按名字查 TMDB 候选"},
            {"path": "/meta/advance", "endpoint": self.api_meta_advance, "methods": ["GET"], "summary": "TMDB 下一集播出日期与媒体库已入库集数"},
            {"path": "/mp/subs", "endpoint": self.api_mp_subs, "methods": ["GET"], "summary": "列出 MoviePilot 自带订阅，供导入"},
            {"path": "/mp/import", "endpoint": self.api_mp_import, "methods": ["POST"], "summary": "把 MP 自带订阅导入本插件（可同时删除 MP 里的）"},
            {"path": "/mp/delete", "endpoint": self.api_mp_delete, "methods": ["POST"], "summary": "删除 MoviePilot 自带的订阅"},
            {"path": "/notify/test", "endpoint": self.api_notify_test, "methods": ["POST"], "summary": "发送测试通知"},
            {"path": "/downloaders/test", "endpoint": self.api_downloader_test, "methods": ["GET"], "summary": "测试下载器连接"},
            {"path": "/sub", "endpoint": self.api_sub_save, "methods": ["POST"], "summary": "新增或修改订阅"},
            {"path": "/sub/delete", "endpoint": self.api_sub_delete, "methods": ["POST"], "summary": "删除订阅"},
            {"path": "/sub/toggle", "endpoint": self.api_sub_toggle, "methods": ["POST"], "summary": "启用或停用订阅"},
            {"path": "/sub/reset", "endpoint": self.api_sub_reset, "methods": ["POST"], "summary": "清空已下载集数台账"},
            {"path": "/sub/run", "endpoint": self.api_sub_run, "methods": ["POST"], "summary": "立即试运行"},
            {"path": "/search", "endpoint": self.api_search, "methods": ["POST"], "summary": "按站点搜索资源"},
            {"path": "/download", "endpoint": self.api_download, "methods": ["POST"], "summary": "提交种子到下载器"},
        ]
        for route in routes:
            route["auth"] = "bear"
        return routes

    # ------------------------------------------------------------ 调度
    def _rebuild_jobs(self) -> None:
        if not self._scheduler:
            return
        for job in list(self._scheduler.get_jobs()):
            if job.id.startswith("sub:") or job.id.startswith("retry:"):
                try:
                    self._scheduler.remove_job(job.id)
                except Exception:
                    pass
        self._retry_state.clear()
        for sub in self.store.subs():
            if not sub.get("enabled"):
                continue
            try:
                trigger = build_trigger(sub, TZ)
                job = self._scheduler.add_job(
                    self._run_sub, trigger, args=[sub["id"]],
                    id=f"sub:{sub['id']}", name=f"补搜：{sub['name']}",
                    replace_existing=True, max_instances=1, coalesce=True,
                )
                # APScheduler 在调度器启动前是「待定任务」，此时读 next_run_time
                # 会抛 AttributeError，所以用 getattr 兜底（下次执行时间在 api_subs 里补）
                run_at = getattr(job, "next_run_time", None)
                if run_at:
                    self.store.set_next_run(sub["id"], run_at.strftime("%Y-%m-%d %H:%M"))
            except Exception as exc:
                logger.warning(f"[{self.plugin_name}] 订阅 {sub.get('name')} 计划注册失败：{exc}")

    def _next_run_text(self, sid: str) -> str:
        """实时读调度器里的下次执行时间，读不到就返回空。"""
        if not self._scheduler or not sid:
            return ""
        try:
            job = self._scheduler.get_job(f"sub:{sid}")
            run_at = getattr(job, "next_run_time", None) if job else None
            return run_at.strftime("%Y-%m-%d %H:%M") if run_at else ""
        except Exception:
            return ""

    def _now_dt(self):
        """带时区的当前时间，用于排重试任务。"""
        from datetime import datetime

        try:
            from zoneinfo import ZoneInfo

            return datetime.now(ZoneInfo(TZ))
        except Exception:
            return datetime.now()

    def _schedule_retry(self, sid: str, sub: dict) -> str:
        """没搜到（还没更新）时排一次重试；超出窗口就收工等下一次计划。

        返回给日志用的说明文字。
        """
        interval = int(sub.get("retry_interval") or 0)
        window = int(sub.get("retry_window") or 0)
        if not self._scheduler or interval <= 0 or window <= 0:
            return "未开启重试"
        import time as _time

        state = self._retry_state.get(sid) or {"started": _time.time(), "tries": 0}
        self._retry_state[sid] = state
        elapsed = (_time.time() - state["started"]) / 60.0
        if elapsed + interval > window:
            self._retry_state.pop(sid, None)
            return f"已守候 {int(elapsed)} 分钟仍无更新，本轮结束，等下次计划"
        state["tries"] += 1
        from datetime import timedelta

        run_at = self._now_dt() + timedelta(minutes=interval)
        try:
            self._scheduler.add_job(
                self._run_sub, "date", run_date=run_at, args=[sid],
                id=f"retry:{sid}", name=f"补搜重试：{sub.get('name')}",
                replace_existing=True, max_instances=1,
            )
        except Exception as exc:
            return f"排重试失败：{exc}"
        return f"{interval} 分钟后再搜（第 {state['tries']} 次重试，窗口共 {window} 分钟）"

    # ------------------------------------------------- 错过的计划补跑
    # APScheduler 内存调度器不补跑：NAS 关机 / 断网 / 容器重启期间到点的计划
    # 就等于没跑过，重启后要从下一个计划时间才开始，昨晚该下的集就漏了。
    # 所以开机后先扫一遍「计划时间已过 + 那之后没跑过 + 确实有已播未下的集」，
    # 直接补跑；运行期间每 30 分钟再扫一次，网络恢复后几十分钟内就能补上。
    CATCHUP_MAX = 3      # 同一天同一订阅最多自动补 3 次，避免整天狂搜站点
    CATCHUP_GAP = 7200   # 跑过但没下到：至少隔 2 小时再补，不跟重试窗口打架

    def _install_catchup_jobs(self) -> None:
        """开机补偿（75 秒后，等 MP 站点/下载器就绪）+ 每 30 分钟巡检一次。"""
        if not self._scheduler:
            return
        from datetime import timedelta

        from apscheduler.triggers.interval import IntervalTrigger

        try:
            self._scheduler.add_job(
                self._catch_up_scan, "date",
                run_date=self._now_dt() + timedelta(seconds=75),
                kwargs={"boot": True}, id="catchup:boot",
                name="错过的计划：开机补跑", replace_existing=True, max_instances=1,
            )
            self._scheduler.add_job(
                self._catch_up_scan, IntervalTrigger(minutes=30, timezone=TZ),
                id="catchup:watch", name="错过的计划：定时巡检",
                replace_existing=True, max_instances=1, coalesce=True,
            )
        except Exception as exc:  # noqa: BLE001 - 装不上也不影响正常计划
            logger.warning(f"[{self.plugin_name}] 补跑巡检注册失败：{type(exc).__name__}: {exc}")

    def _catchup_state(self) -> dict:
        try:
            return dict(self.store.backend.load("catchup", {}) or {})
        except Exception:
            return {}

    def _catchup_used(self, sid: str, day: str) -> int:
        row = self._catchup_state().get(str(sid)) or {}
        return int(row.get("n") or 0) if str(row.get("day") or "") == day else 0

    def _catchup_mark(self, sid: str, day: str) -> None:
        state = self._catchup_state()
        row = state.get(str(sid)) or {}
        n = (int(row.get("n") or 0) + 1) if str(row.get("day") or "") == day else 1
        state[str(sid)] = {"day": day, "n": n}
        try:
            self.store.backend.save("catchup", state)
        except Exception:
            pass

    def _pending_episodes(self, sub: dict, now) -> List[int]:
        """已播出、但台账里还没下到的集号（升序），用来「一次把积压的都下完」。

        以前只盯 next_episode()（水位 +1），同一天连播两集（比如第 2、3 集都是 11/23）
        就得等两轮：下完第 2 集等 2 分钟接力，才轮到第 3 集；关机几天回来积了 5 集
        更要一轮一轮排队。这里一次性把「已播未下」全给出来，一轮全提交。

        只有节目表（TMDB）确认播过的才算，避免把还没播的集提前拉下来。
        查不到节目表 / 没填 tmdb_id 时返回空，调用方回退到 next_episode()，行为与以前一致。
        """
        if (sub.get("kind") or "series") == "movie":
            return []
        tmdb_id = sub.get("tmdb_id")
        if not tmdb_id:
            return []
        try:
            air = meta_air_dates(int(tmdb_id), sub.get("season")) or {}
        except Exception as exc:  # noqa: BLE001 - 查不到就按单集水位走
            logger.debug(f"[{self.plugin_name}] 查节目表失败：{type(exc).__name__}: {exc}")
            return []
        eps = air.get("eps") or []
        if not eps:
            return []
        # 水位下限：只从「已经下到的最高集 + 1」往后算。
        # 绝不按「缺口」回头补中间那几集——那些多半是看完删掉/转移失败的，
        # 回头补就会把老集重下一遍（《光阴之外》第 43 集重下就是这么来的）。
        floor = next_episode(sub)
        if floor is None:
            return []
        start = int(sub.get("ep_start") or 0) or 1
        end = int(sub.get("ep_end") or 0) or 0
        done = {int(x) for x in (sub.get("delivered_eps") or [])}
        today = now.strftime("%Y-%m-%d")
        try:
            fh, fm = (int(x) for x in str(sub.get("schedule_time") or "19:00").split(":"))
        except (TypeError, ValueError):
            fh, fm = 19, 0
        # 今天才播的集要等当天计划时间点之后才认：白天站点多半还没出种
        allow_today = now >= now.replace(hour=fh, minute=fm, second=0, microsecond=0)
        out: List[int] = []
        for row in eps:
            ep = int(row.get("ep") or 0)
            day = str(row.get("air_date") or "")[:10]
            if not ep or not day or ep in done or ep in out:
                continue
            if ep < floor or ep < start or (end and ep > end):
                continue
            if day < today or (day == today and allow_today):
                out.append(ep)
        return sorted(out)

    def _need_catchup(self, sub: dict, now) -> bool:
        """是不是真有「已经播出、但还没下到」的集？没有就别白搜一趟。

        节目表（TMDB）说新集已经播了、而台账的最高水位还没到那集，才算漏了。
        查不到节目表 / 没填 tmdb_id 时保守放行：宁可多搜一次，也别漏更新。

        「今天才播」的那集要等到当天计划时间（schedule_time）之后才认：
        站点一般是晚上才出种，白天就催只会空搜一趟（用户说的「别过早狂搜」）。
        昨天及更早播的照旧算数，所以关机错过的集第二天开机仍会补。
        """
        if (sub.get("kind") or "series") == "movie":
            # 已入库的电影没必要再补：补了也是立刻跳过，白留一条记录
            return not bool(sub.get("delivered_eps")) and not bool(sub.get("lib_in"))
        if next_episode(sub) is None:
            return False  # 区间内已经补齐
        if not (sub.get("ep_start") or sub.get("ep_end")):
            return True  # 补老剧（不逐集追更），有新资源就收
        tmdb_id = sub.get("tmdb_id")
        if not tmdb_id:
            return True
        try:
            air = meta_air_dates(int(tmdb_id), sub.get("season")) or {}
        except Exception as exc:  # noqa: BLE001 - 查不到就按计划跑一次
            logger.debug(f"[{self.plugin_name}] 补跑查节目表失败：{type(exc).__name__}: {exc}")
            return True
        today = now.strftime("%Y-%m-%d")
        try:
            fh, fm = (int(x) for x in str(sub.get("schedule_time") or "19:00").split(":"))
        except (TypeError, ValueError):
            fh, fm = 19, 0
        # 今天播的集，在计划时间点之前先不催：白天站点多半还没种。
        # 到点 cron 会正常跑；万一那天关机错过，第二天它已是「过去播的」，仍会补。
        allow_today = now >= now.replace(hour=fh, minute=fm, second=0, microsecond=0)
        eps = air.get("eps") or []
        aired = [int(e.get("ep") or 0) for e in eps
                 if e.get("ep") and e.get("air_date")
                 and (str(e["air_date"])[:10] < today
                      or (str(e["air_date"])[:10] == today and allow_today))]
        if not aired:
            # 节目表有集数据但一集都还没播（新剧还没开播），或今天才播还没到点
            # → 确实没得下，别白搜
            return not eps
        high = max((int(x) for x in (sub.get("delivered_eps") or [])), default=0)
        return max(aired) > high

    def _catch_up_scan(self, boot: bool = False) -> None:
        """扫一遍有没有「该跑没跑 / 跑了没下到」的订阅，有就排队补跑。

        两条触发路径：
          ① 计划时间已过，而那个时间点之后一条运行记录都没有（关机、断网、
             重启把 cron 丢了）——属于真·错过，立刻补；
          ② 那个时间点跑过，但确实还有「已播出没下到」的集，且距上次已经
             超过 2 小时（站点晚上才上资源、断网重试窗口过了才恢复）——补一次，
             同一天最多 3 次，避免站点一直没资源时整天狂搜。
        """
        if not self._scheduler:
            return
        now = self._now_dt()
        day = now.strftime("%Y-%m-%d")
        runs = self.store.last_runs()
        queued = 0
        for sub in self.store.subs():
            if not sub.get("enabled"):
                continue
            sid = str(sub.get("id") or "")
            if not sid:
                continue
            last = runs.get(sid) or runs.get("name:" + str(sub.get("name") or "")) or {}
            last_at = _parse_dt(last.get("at"), now)
            due = last_fire_time(sub, now, last_at)
            if due is None or now < due:
                continue  # 还没到该跑的时间
            created = _parse_dt(sub.get("created_at"), now)
            if created and created > due:
                continue  # 建订阅之前的时间点，不算错过
            if last_at and last_at >= due:
                # 这个时间点跑过：只有「确实还差集 + 隔了很久」才再补。
                # 上一轮是 error（多半是断网、站点连不上）就放宽到 20 分钟后再试，
                # 且不占每日次数，网络恢复后能自己接上，不用等一整天。
                #
                # 注意这里不再区分「开机那一次」：插件会被 MP 反复重载，重载会把
                # 30 分钟的巡检计时重置掉，只靠巡检很可能一直轮不到；而这条分支
                # 已经有「间隔 + 每日 3 次 + 确实有已播未下的集」三重限制，安全。
                status = str(last.get("status") or "")
                gap = 1200 if status == "error" else self.CATCHUP_GAP
                if (now - last_at).total_seconds() < gap:
                    continue
                if status != "error" and self._catchup_used(sid, day) >= self.CATCHUP_MAX:
                    continue
                if not self._need_catchup(sub, now):
                    continue
                if status != "error":
                    self._catchup_mark(sid, day)
            elif not self._need_catchup(sub, now):
                continue
            # 几条订阅错开几十秒，别同时打同一个站点
            self._schedule_catchup(sid, 30 + queued * 20, "开机补偿" if boot else "补漏", 3)
            queued += 1
        if queued:
            logger.info(f"[{self.plugin_name}] 有 {queued} 条订阅错过了计划时间，已排队补跑")

    def _schedule_catchup(self, sid: str, delay: int, note: str, remaining: int) -> bool:
        if not self._scheduler:
            return False
        from datetime import timedelta

        try:
            self._scheduler.add_job(
                self._catch_up_run, "date",
                run_date=self._now_dt() + timedelta(seconds=max(5, int(delay))),
                args=[sid, note, int(remaining)],
                id=f"catchup:{sid}", name=f"补跑：{note}",
                replace_existing=True, max_instances=1,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{self.plugin_name}] 补跑排队失败：{type(exc).__name__}: {exc}")
            return False
        return True

    # --------------------------------------------- 入库状态：媒体库 / 转移历史
    LIB_SYNC_MINUTES = 5   # 常驻巡检间隔：只查本地库，不发网络请求，5 分钟一次很轻

    def _sync_library(self, sub: dict) -> bool:
        """把一条订阅的「入库状态」对齐媒体库与转移历史，有变化才写盘。

        电影只看「媒体库里有没有这部」（没有记录时用转移成功兜底），剧集补已入库集数，
        并把「下到过」的集并进台账——删掉看过的集也不会被当成缺失重下。
        返回是否真的改动了数据，给定时巡检用来判断要不要记日志。
        """
        sid = str(sub.get("id") or "")
        try:
            tmdb_id = int(sub.get("tmdb_id") or 0)
        except (TypeError, ValueError):
            return False
        if not sid or not tmdb_id:
            return False
        changed = False
        if (sub.get("kind") or "series") == "movie":
            # 电影没有集数：只判「媒体库里有没有这条」，有就算已入库。
            # 返回 None 表示数据库这一轮没读上（MP 刚启动/库被锁），保持原状态别误判成没入库
            now_in = meta_library_movie(tmdb_id, sub.get("meta_title") or sub.get("name") or "",
                                        sub.get("year") or "")
            if now_in is None:
                return False
            now_in = bool(now_in)
            if now_in != bool(sub.get("lib_in")) or not sub.get("lib_synced"):
                self.store.set_sub_fields(sid, lib_in=now_in, lib_synced=True)
                changed = True
            return changed
        season = sub.get("season")
        lib = sorted({int(x) for x in (meta_library_eps(tmdb_id, season) or [])})
        before = sorted({int(x) for x in (sub.get("library_eps") or [])})
        if lib != before or not sub.get("lib_synced"):
            self.store.set_sub_fields(sid, library_eps=lib, lib_synced=True)
            changed = True
        hist = meta_transferred_eps(tmdb_id, season) or []
        dl = meta_downloaded_eps(tmdb_id, season) or []
        done = {int(x) for x in (sub.get("delivered_eps") or [])}
        add = sorted({*lib, *hist, *dl} - done)
        if add:
            self.store.mark_delivered(sid, add)
            changed = True
        return changed

    def _sync_libraries(self, note: str = "") -> int:
        """把所有订阅的入库状态对齐一次（含停用的：停用了也可能已经入库）。

        下载完成 → 做种 → 转移 → 媒体服务器同步，这一串走完 MP 才写进媒体库表，
        中间十几二十分钟很常见。界面不能等用户手动点「同步海报」才更新，所以常驻
        一个小时轻量巡检；只查本地数据库，不碰站点和下载器。
        """
        changed = 0
        for sub in self.store.subs():
            try:
                if self._sync_library(sub):
                    changed += 1
            except Exception as exc:  # noqa: BLE001 - 单条读不到不影响其它
                logger.debug(f"[{self.plugin_name}] 入库状态刷新失败"
                             f"（{sub.get('name')}）：{type(exc).__name__}: {exc}")
        if changed:
            logger.info(f"[{self.plugin_name}] 入库状态刷新{note}：{changed} 条订阅已更新")
        return changed

    def _install_lib_job(self) -> None:
        """装常驻巡检：启动 40 秒后先跑一遍，之后每 5 分钟一次。"""
        if not self._scheduler:
            return
        from datetime import timedelta

        from apscheduler.triggers.interval import IntervalTrigger

        try:
            self._scheduler.add_job(
                self._sync_libraries, "date",
                run_date=self._now_dt() + timedelta(seconds=40),
                id="libsync:boot", name="入库状态：开机对齐",
                replace_existing=True, max_instances=1,
            )
            self._scheduler.add_job(
                self._sync_libraries, IntervalTrigger(minutes=self.LIB_SYNC_MINUTES, timezone=TZ),
                id="libsync:watch", name="入库状态：定时巡检",
                replace_existing=True, max_instances=1, coalesce=True,
            )
        except Exception as exc:  # noqa: BLE001 - 装不上就只靠手动同步
            logger.warning(f"[{self.plugin_name}] 入库状态巡检注册失败：{type(exc).__name__}: {exc}")

    def _downloader_ready(self, sub: dict) -> tuple[bool, str]:
        """补跑前先确认下载器实例在（刚开机时 qb / transmission 可能还没就绪）。

        只做不联网的检查（联网探测遇到掉线的下载器反而会卡住）：
        实例不存在就直接推后，实例在就交给下面带超时的执行去看结果。
        """
        try:
            from app.sdk.services import DownloaderHelper

            helper = DownloaderHelper()
            configs = helper.get_configs() or {}
            name = str(sub.get("downloader_id") or "")
            if not name:
                name = next((n for n, c in configs.items()
                             if bool(getattr(c, "default", False))), "") or next(iter(configs), "")
            if not name:
                return True, ""  # 一条都没配：交给提交环节报错更准确
            service = helper.get_service(name=name)
            if not service or not getattr(service, "instance", None):
                return False, f"「{name}」还没就绪"
            return True, ""
        except Exception as exc:  # noqa: BLE001 - 检查本身出错就不拦
            logger.debug(f"[{self.plugin_name}] 下载器预检跳过：{type(exc).__name__}: {exc}")
            return True, ""

    def _catch_up_run(self, sid: str, note: str = "补漏", remaining: int = 1) -> None:
        """补跑一轮；关机几天积了好几集时，一轮会把已播未下的一起下，还差再排下一轮。"""
        sub = self.store.get_sub(sid)
        if not sub or not sub.get("enabled"):
            return
        ok, why = self._downloader_ready(sub)
        if not ok:
            # 开机时下载器还没起来：别硬提交（会卡住），10 分钟后再来
            logger.info(f"[{self.plugin_name}] 补跑暂缓：{sub.get('name')} 的下载器{why}，10 分钟后再试")
            self._schedule_catchup(sid, 600, note, remaining)
            return
        before = {int(x) for x in (sub.get("delivered_eps") or [])}
        logger.info(f"[{self.plugin_name}] {note}：{sub.get('name')} 立刻补跑一轮")
        # 提交环节偶发卡死（下载器掉线、种子站没响应）时至少能留下一条记录并改期重试，
        # 不然这一轮会一直挂着，日志里永远看不到结果。
        done = threading.Event()

        def _work() -> None:
            try:
                self._run_sub(sid, reason=note)
            finally:
                done.set()

        threading.Thread(target=_work, name=f"ptsub-catchup-{sid}", daemon=True).start()
        if not done.wait(timeout=600):
            logger.warning(f"[{self.plugin_name}] 补跑超时（10 分钟没有结果）：{sub.get('name')}，"
                           f"多半是下载器或站点没响应，30 分钟后再试")
            self._schedule_catchup(sid, 1800, note, remaining)
            return
        if remaining <= 1:
            return
        after_sub = self.store.get_sub(sid) or {}
        after = {int(x) for x in (after_sub.get("delivered_eps") or [])}
        # 一轮已经能把已播未下的都提交上去；还剩下的多半是站点还没出种，间隔 2 分钟再试一轮
        if after - before and self._need_catchup(after_sub, self._now_dt()):
            self._schedule_catchup(sid, 120, note, remaining - 1)

    def _run_sub(self, sid: str, force: bool = False, reason: str = "") -> None:
        """补搜一轮：把「已播出、还没下到」的集一次都提交，没下到的等重试。

        以前只盯 next_episode()（水位 +1），同一天连播两集要分两轮才补齐，
        现在按 TMDB 播出表把已播未下的一起给出来，一轮全下。

        force=True 表示手动「立即执行」：即使订阅处于停用状态也照样跑一轮。
        reason 非空表示这轮是「开机补偿 / 补漏」触发的，日志里会标出来。
        """
        sub = self.store.get_sub(sid)
        if not sub or (not sub.get("enabled") and not force):
            return
        domains = sub.get("site_ids") or []
        keyword = search_keyword(sub)
        is_movie = (sub.get("kind") or "series") == "movie"
        # 补搜前先对齐媒体库 + 转移历史：真正「下到过」的集直接算已下，只增不减。
        # 关键是两条来源：
        #   ① 媒体库当前存量（library_eps）——现在还在库里的；
        #   ② 下载/转移历史（transferred_eps）——下到过、后来看完删掉的也在里面。
        # 只查媒体库是不够的：用户删掉的集不在库里，但转移历史记得住，
        # 否则「看完删了的集」会被当成缺失反复重下（用户反馈过的严重问题）。
        if sub.get("tmdb_id"):
            try:
                self._sync_library(sub)
                sub = self.store.get_sub(sid) or sub
            except Exception as exc:  # noqa: BLE001 - 媒体库/历史读不到不影响补搜
                logger.debug(f"[{self.plugin_name}] 补搜前读媒体库/转移历史失败：{type(exc).__name__}: {exc}")
        # 填了集数区间就按集补（追更场景）；没填就一次把命中的都收下来（补老剧）
        one_by_one = (not is_movie) and bool(sub.get("ep_start") or sub.get("ep_end"))
        # 同一天连播两集（或关机几天积了几集）不能只盯水位 +1，否则一轮只下一集、
        # 剩下的靠 2 分钟接力一轮一轮排队。这里先把「节目表确认播过、台账还没下」的集
        # 一次拿全，一轮全提交；节目表查不到时退回单集水位，行为和以前一致。
        targets: List[int] = self._pending_episodes(sub, self._now_dt()) if one_by_one else []
        if one_by_one and not targets:
            nxt = next_episode(sub)
            if nxt is not None:
                targets = [nxt]
        target = targets[0] if targets else None
        target_text = ("、".join(str(x) for x in targets) if targets else "")
        entry: dict[str, Any] = {
            "sub_id": sid, "sub": sub.get("name"), "sites": len(domains), "keyword": keyword,
            # 非计划时间触发的补跑，日志里标一下，方便核对「昨晚漏的今天补上了」
            "note": reason,
            "target": ("电影" if is_movie else
                       (f"第 {target_text} 集" if targets
                        else ("全部未下集" if not one_by_one else "已补齐"))),
            "found": 0, "submitted": 0, "status": "ok", "message": "",
            # 明细：站点 / 分辨率 / 集数 / 每条资源，运行记录里直接展示
            "site": "", "resolution": "", "episode": "", "items": [],
        }
        if not domains:
            entry.update(status="skipped", message="未勾选站点")
            self._finish(sub, entry)
            return
        # 电影已经入库了就别再搜一遍：媒体库里有、下载器里还留着种子，很容易又下一遍。
        # 想换版本时用顶部「找资源」手动挑一条，比让计划任务盲搜更准。
        if is_movie and sub.get("lib_in"):
            self._retry_state.pop(sid, None)
            self._cancel_retry(sid)
            entry.update(status="skipped",
                         message="媒体库里已经有这部电影，不再重复下载（想换版本用「找资源」手动挑一条）")
            self._finish(sub, entry)
            return
        if one_by_one and not targets:
            self._retry_state.pop(sid, None)
            self._cancel_retry(sid)
            entry.update(status="skipped", message="订阅集数已全部下完，无需补搜")
            self._finish(sub, entry)
            return
        try:
            result = self.bridge.search(domains, keyword, self._max_results)
            passed, _ = filter_candidates(sub, result["items"], self.store.hit_keys(),
                                          target_ep=(targets or None))
            # 补老剧（没填集数区间）也要跳过台账里下过的集：
            # 否则把 1-10 删了腾空间，下一轮又会把 1-10 重新下回来
            if not one_by_one and not is_movie:
                done_eps = {int(x) for x in (sub.get("delivered_eps") or [])}
                if done_eps:
                    passed = [r for r in passed
                              if not (r.get("episode") and int(r["episode"]) in done_eps)]
            # 同一集可能有多个版本：按做种数优先，挑最稳的
            passed.sort(key=lambda x: (-(x.get("seeders") or 0), x.get("size_gb") or 0))
            entry["found"] = len(passed)
            if not passed:
                wait = self._schedule_retry(sid, sub)
                level = self.bridge.auth_level()
                auth_note = ("（MP 用户认证临时未通过，站点功能被禁用，会自动重试）"
                             if 0 <= level < 2 else "")
                # 把「站点到底搜到了哪几集」写进记录：用户说「明明有第 2 集却没下」时，
                # 一眼能分清是站点没种、还是集号/规则/去重把它挡掉了。
                seen = sorted({ep for ep in (
                    parse_episode(str(it.get("title") or it.get("display_title") or ""))
                    for it in (result.get("items") or [])) if ep})
                seen_note = ""
                if seen:
                    head = "、".join(str(e) for e in seen[:12]) + ("…" if len(seen) > 12 else "")
                    want = f"第 {target_text} 集" if targets else "目标集"
                    if any(x in seen for x in targets):
                        seen_note = f"；站点已搜到第 {head} 集，{want}有资源但被规则/去重挡掉了"
                    else:
                        seen_note = f"；站点只搜到第 {head} 集，没有{want}"
                entry.update(status="waiting", site=self._site_names(domains),
                             message=f"{entry['target']}还没更新{auth_note}{seen_note}；{wait}")
                self._finish(sub, entry)
                return
            # 一轮挑选：追更时「每个目标集只留一个版本」（passed 已按做种数排序），
            # 同一天连播的几集各得一条，一次全提交；电影只要一条；补老剧收 5 条。
            if one_by_one:
                seen_eps: set[int] = set()
                picks: list[dict] = []
                for row in passed:
                    ep = row.get("episode")
                    if ep is not None:
                        ep = int(ep)
                        if ep in seen_eps:
                            continue
                        seen_eps.add(ep)
                    picks.append(row)
                    if len(picks) >= max(1, len(targets)):
                        break
            elif is_movie:
                picks = passed[:1]
            else:
                picks = passed[:5]
                # 同一集只留一个版本，避免一次把同集的不同小组都下下来
                seen_eps = set()
                uniq: list[dict] = []
                for row in picks:
                    ep = row.get("episode")
                    if ep and ep in seen_eps:
                        continue
                    if ep:
                        seen_eps.add(ep)
                    uniq.append(row)
                picks = uniq
            submit = self.bridge.submit(
                result["token"], [p["key"] for p in picks],
                sub.get("downloader_id") or "", sub.get("qb_category") or "",
                sub.get("save_path") or "",
            )
            ok_map = {r["key"]: r for r in submit.get("results", []) if r.get("ok")}
            done_eps: list[int] = []
            titles: list[str] = []
            details: list[dict] = []
            for pick in picks:
                row = ok_map.get(pick["key"])
                if not row:
                    continue
                res = resolution_of(pick.get("attrs"))
                site = pick.get("site") or pick.get("domain") or ""
                title = pick.get("display_title") or pick.get("title") or ""
                self.store.add_hit({
                    "sub_id": sid, "sub": sub.get("name"),
                    "dedup_key": pick.get("key", ""),
                    "title": title, "site": site, "domain": pick.get("domain"),
                    "resolution": res,
                    "save_path": sub.get("save_path") or "",
                    "size_gb": pick.get("size_gb"),
                    "downloader": submit.get("downloader"),
                    "hash": row.get("hash", ""),
                    "episode": pick.get("episode"),
                    "status": "submitted",
                })
                if pick.get("episode"):
                    done_eps.append(int(pick["episode"]))
                titles.append(title)
                details.append({"title": title, "site": site, "resolution": res,
                                "episode": pick.get("episode"),
                                "size_gb": pick.get("size_gb")})
            entry["submitted"] = len(titles)
            entry["items"] = details
            entry["site"] = "、".join(dict.fromkeys(d["site"] for d in details if d["site"]))
            entry["resolution"] = " / ".join(dict.fromkeys(d["resolution"] for d in details if d["resolution"]))
            entry["episode"] = ("第 " + "、".join(str(e) for e in sorted(done_eps)) + " 集"
                                if done_eps else ("电影" if is_movie else ""))
            if done_eps:
                self.store.mark_delivered(sid, done_eps)
            if titles:
                self._retry_state.pop(sid, None)
                self._cancel_retry(sid)
                cur = self.store.get_sub(sid) or sub
                # 这一轮下完后再看还剩哪些「已播没下」：同一天连播两集、或积压几集时，
                # 常常是其中一集站点还没出种，剩下的隔 2 分钟自动再补一轮。
                left = self._pending_episodes(cur, self._now_dt()) if one_by_one else []
                nxt = next_episode(cur) if not is_movie else None
                # 补跑触发的那一轮（reason 非空）自带接力，这里不重复排，避免两个 job 抢同一集。
                chained = False
                if one_by_one and left and not reason:
                    chained = self._schedule_catchup(sid, 120, "追更接续", min(3, len(left)))
                head = f"已提交 {len(titles)} 条"
                if len(done_eps) > 1:
                    head += f"（第 {'、'.join(str(e) for e in sorted(done_eps))} 集）"
                if chained:
                    tail = f"；第 {'、'.join(str(x) for x in left)} 集还没种子，2 分钟后自动再补"
                elif left:
                    tail = f"；下一轮再补第 {'、'.join(str(x) for x in left)} 集"
                else:
                    tail = (f"；下一集等第 {nxt} 集" if nxt
                            else ("；区间内已补齐" if one_by_one else ""))
                entry["message"] = f"{head}：{titles[0][:40]}" + tail
            else:
                msg = (submit.get("results") or [{}])[0].get("message") or "下载器未接受"
                wait = self._schedule_retry(sid, sub)
                entry.update(status="waiting", message=f"提交失败：{msg}；{wait}")
        except Exception as exc:
            entry.update(status="error", message=f"{type(exc).__name__}: {str(exc)[:160]}")
            logger.error(f"[{self.plugin_name}] 补搜 {sub.get('name')} 失败：{entry['message']}")
        self._finish(sub, entry)

    # ------------------------------------------------------- 记录 + 通知
    def _finish(self, sub: dict, entry: dict) -> None:
        """落一条运行记录，需要的话顺手推一条 MP 通知。"""
        if entry.get("note") and entry.get("message"):
            entry["message"] = f"【{entry['note']}】{entry['message']}"
        self.store.add_log(entry)
        try:
            self._notify_result(sub, entry)
        except Exception as exc:  # 通知失败绝不影响补搜本身
            logger.warning(f"[{self.plugin_name}] 通知发送失败：{type(exc).__name__}: {exc}")

    def _site_names(self, site_ids: list) -> str:
        """站点 id / 域名 → MP 里的站点中文名，日志和通知里看着更直观。"""
        if not site_ids:
            return ""
        try:
            sites = self.bridge.list_sites()
            by_sid = {int(s.get("sid") or 0): s.get("name") for s in sites}
            by_domain = {s.get("domain"): s.get("name") for s in sites}
        except Exception:
            by_sid, by_domain = {}, {}
        names = []
        for x in site_ids:
            if isinstance(x, int):
                names.append(str(by_sid.get(x) or x))
            else:
                names.append(str(by_domain.get(x) or x))
        head = "、".join(names[:3])
        return head + (f" 等 {len(names)} 个" if len(names) > 3 else "")

    def _notify(self, title: str, text: str) -> dict:
        """走 MP 的通知链路（设置 → 通知 里配的 QQ / 微信 / TG / 飞书等）。"""
        post = getattr(self, "post_message", None)
        if not post:
            return {"ok": False, "message": "当前 MP 版本不支持插件通知（缺少 post_message）"}
        mtype = None
        try:
            from app.schemas.types import MessageType

            mtype = MessageType.Plugin
        except Exception:  # 宿主没这个枚举时退回默认场景
            mtype = None
        try:
            post(mtype=mtype, title=title, text=text)
        except Exception as exc:
            logger.warning(f"[{self.plugin_name}] post_message 失败：{type(exc).__name__}: {exc}")
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:160]}"}
        return {"ok": True}

    def _notify_result(self, sub: dict, entry: dict) -> None:
        """只推有意义的结果：下到了 / 出错了。没更新那种每 10 分钟一次的不推，免得刷屏。"""
        if not sub.get("notify", True):
            return
        submitted = int(entry.get("submitted") or 0)
        is_error = entry.get("status") == "error"
        if not submitted and not is_error:
            return
        head = "✅ 已提交下载" if submitted else "⚠️ 补搜出错"
        lines = [
            f"订阅：{sub.get('name')}",
            f"目标：{entry.get('target') or '—'}",
        ]
        if entry.get("site"):
            lines.append(f"站点：{entry['site']}")
        for item in (entry.get("items") or [])[:3]:
            bits = [b for b in [
                f"第 {item['episode']} 集" if item.get("episode") else "",
                item.get("resolution") or "",
                item.get("site") or "",
                f"{item['size_gb']} GB" if item.get("size_gb") else "",
            ] if b]
            lines.append(f"资源：{item.get('title') or ''}" + (f"（{' · '.join(bits)}）" if bits else ""))
        if entry.get("message"):
            lines.append(f"结果：{entry['message']}")
        self._notify(f"{head}｜{sub.get('name')}", "\n".join(lines))

    def _cancel_retry(self, sid: str) -> None:
        if not self._scheduler:
            return
        try:
            self._scheduler.remove_job(f"retry:{sid}")
        except Exception:
            pass

    # ------------------------------------------------------- TMDB 元数据
    def _refresh_schedule_cache(self, sub: dict) -> dict:
        """保存后把「媒体库已入库集数」缓存到订阅（卡片显示用），
        并给空的推荐更新日补上 TMDB 下一集播出日期（默认值，用户可改）。

        已删掉看过的集（如 1-10）不会回头重下：台账 delivered_eps 记的是「下到过」，
        删除只影响 media 库展示，不影响补搜目标。
        任何一步读不到都当没这回事，绝不影响订阅本身。
        """
        sid = str(sub.get("id") or "")
        if not sid:
            return sub
        from datetime import datetime as _dt
        try:
            lib: list[int] = []
            if sub.get("tmdb_id") and (sub.get("kind") or "series") == "movie":
                # 电影只看媒体库有没有这条记录（没有季集可数）
                self.store.set_sub_fields(
                    sid, lib_in=bool(meta_library_movie(int(sub["tmdb_id"]))),
                    lib_synced=True,
                    lib_at=_dt.now().strftime("%Y-%m-%d %H:%M:%S"))
            elif sub.get("tmdb_id"):
                tmdb_id = int(sub["tmdb_id"])
                season = sub.get("season")
                lib = meta_library_eps(tmdb_id, season) or []
                # 已删掉看过的集也要算「下到过」：转移历史 + 下载历史都记得住，
                # 只查媒体库当前存量会把删掉的集当成缺失又下一遍。
                hist = meta_transferred_eps(tmdb_id, season) or []
                dl = meta_downloaded_eps(tmdb_id, season) or []
                delivered = sorted({*lib, *hist, *dl})
                if delivered:
                    self.store.mark_delivered(sid, delivered)
                self.store.set_sub_fields(sid, library_eps=lib, lib_synced=True,
                                          lib_at=_dt.now().strftime("%Y-%m-%d %H:%M:%S"))
            # TMDB 播出日只在还没查过时查（网络请求慢），查过就不再重复打 TMDB
            if (sub.get("tmdb_id") and (sub.get("kind") or "series") != "movie"
                    and (not sub.get("air_state") or not (sub.get("recommend_date") or "").strip())):
                # 推荐更新日自动补全：先看还没播的下一集；整季已经播完（如 Doctor X 12/12）
                # 就用最后一集的播出日兜底，别让「推荐更新」永远挂着红色的「未设置」。
                air = meta_air_dates(int(sub["tmdb_id"]), sub.get("season")) or {}
                rec, state = recommend_from_air(air)
                patch: dict = {"air_state": state}
                if rec and not (sub.get("recommend_date") or "").strip():
                    patch["recommend_date"] = rec
                # 下一集播出日期也顺手缓存，卡片右下角显示「下次真正下载的日子」
                if air.get("next_air_date"):
                    patch["air_next_ep"] = air.get("next_ep")
                    patch["air_next_date"] = air.get("next_air_date")
                elif air.get("last_air_date"):
                    # 整季已播完：下一集没有了，用最后一集播出日，前端会显示「已完成」
                    patch["air_next_ep"] = None
                    patch["air_next_date"] = air.get("last_air_date")
                self.store.set_sub_fields(sid, **patch)
            return self.store.get_sub(sid) or sub
        except Exception as exc:  # noqa: BLE001 - 读不到媒体库/TMDB 不影响订阅
            logger.debug(f"[{self.plugin_name}] 刷新媒体库缓存失败：{type(exc).__name__}: {exc}")
            return sub

    def _meta_stale(self, sub: dict, old: dict | None) -> bool:
        """名字/季/类型变了，或还没查到过海报，就重新查一次 TMDB。"""
        if not sub.get("poster_path"):
            return True
        if not old:
            return False
        return (str(old.get("name") or "") != str(sub.get("name") or "")
                or old.get("season") != sub.get("season")
                or str(old.get("kind") or "") != str(sub.get("kind") or "series"))

    def _fill_meta(self, sub: dict, tmdb_id: int | None = None) -> dict:
        """查海报/年份/总集数；查不到就原样返回，不影响订阅本身。

        前端点选过候选时会带上 tmdb_id，按 ID 取详情，不再按名字猜。
        """
        try:
            meta = resolve_meta(sub.get("name") or "", sub.get("kind") or "series",
                                sub.get("season"), tmdb_id)
        except Exception as exc:
            logger.warning(f"[{self.plugin_name}] TMDB 元数据失败：{type(exc).__name__}: {exc}")
            return sub
        if not meta:
            return sub
        return self.store.set_meta(str(sub.get("id") or ""), meta) or sub

    # ------------------------------------------------------------ API
    def api_options(self) -> dict:
        try:
            sites = self.bridge.list_sites()
            downloaders = self.bridge.list_downloaders()
            searchable = self.bridge.searchable_domains()
            for site in sites:
                site["searchable"] = (not searchable) or site["domain"].lower() in searchable
            return {"ok": True, "sites": sites, "downloaders": downloaders,
                    "rule_options": RULE_OPTIONS, "timezone": TZ,
                    "scheduler_running": bool(self._scheduler and self._scheduler.running)}
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:200]}"}

    def api_subs(self) -> dict:
        # 打开列表时顺手做一次「每天 → 按 TMDB 推荐时间」的搬家：老订阅不用等重启，
        # 下次进表单默认就落在推荐模式上。只在本进程第一次调用时跑，之后不再动数据。
        if not getattr(self, "_mode_migrated", False):
            self._mode_migrated = True
            try:
                moved = self.store.migrate_daily_to_recommend()
                if moved:
                    logger.info(f"[{self.plugin_name}] {moved} 条订阅的执行模式改为「按 TMDB 推荐时间」")
                    self._rebuild_jobs()
            except Exception as exc:  # noqa: BLE001 - 迁移失败不能影响列表加载
                logger.debug(f"[{self.plugin_name}] 执行模式迁移失败：{type(exc).__name__}: {exc}")
        # 老订阅升级上来还没跟媒体库对齐过（library_eps 空），或还没查过播出状态（air_state 空）：
        # 顺手补一次推荐更新日 + 媒体库集数。每次请求最多补 2 条，避免列表接口被 TMDB
        # 查询拖慢；补过一次就置位（查不到也记 unknown），不会每次打开页面都重查。
        try:
            # 「媒体库存量」要定期刷新：用户看完就删，库里的集会越来越少，
            # 只在首次同步一次的话卡片会一直挂着旧数字。每次请求最多刷 2 条、
            # 最久没刷过的优先，打开几次页面就陆续对齐，且只打 PG（TMDB 只在没查过时打）。
            from datetime import datetime as _dt
            now_ts = _dt.now()
            stale = []
            for s in self.store.subs():
                if not s.get("tmdb_id") or (s.get("kind") or "series") == "movie":
                    continue
                if not s.get("lib_synced") or not s.get("air_state"):
                    stale.append((0, s))
                    continue
                raw = str(s.get("lib_at") or "")
                try:
                    age = (now_ts - _dt.strptime(raw, "%Y-%m-%d %H:%M:%S")).total_seconds()
                except (TypeError, ValueError):
                    age = 1e9   # 没有时间戳的当「很久没刷」
                if age > 600:   # 10 分钟以上才重刷，避免刷新页面就重复查
                    stale.append((age, s))
            stale.sort(key=lambda x: -x[0])
            for _, sub in stale[:2]:
                self._refresh_schedule_cache(sub)
        except Exception as exc:  # noqa: BLE001 - 补缓存失败不能影响列表加载
            logger.debug(f"[{self.plugin_name}] 列表接口补媒体库缓存失败：{type(exc).__name__}: {exc}")
        items = self.store.public_subs()
        # 下次执行时间直接问调度器，避免重启后库里存的是旧值
        for item in items:
            text = self._next_run_text(item.get("id", ""))
            if text:
                item["next_run_at"] = text
            # 下一集播出日期：卡片右下角显示「下次真正下载的日子」。
            # 保存时可能还没查到 TMDB 播出表（air_next_date 为空），这里兜底补一次；
            # 查不到就保持原样，前端回退到 next_run_at。
            if (item.get("tmdb_id") and (item.get("kind") or "series") != "movie"
                    and not (item.get("air_next_date") or "").strip()):
                try:
                    air = meta_air_dates(int(item["tmdb_id"]), item.get("season")) or {}
                    if air.get("next_air_date"):
                        item["air_next_ep"] = air.get("next_ep")
                        item["air_next_date"] = air.get("next_air_date")
                    elif air.get("last_air_date"):
                        item["air_next_ep"] = None
                        item["air_next_date"] = air.get("last_air_date")
                except Exception as exc:  # noqa: BLE001 - 查不到就不改，绝不影响列表
                    logger.debug(f"[{self.plugin_name}] 下一集播出日兜底失败：{type(exc).__name__}: {exc}")
        return {"ok": True, "items": items}

    def api_notify_test(self, payload: dict = Body(default={})) -> dict:
        """发一条测试通知，确认 MP 的通知渠道（QQ / 微信 / TG 等）通不通。"""
        channels: list[str] = []
        try:
            from app.sdk.services import NotificationHelper

            channels = [str(x) for x in (NotificationHelper().get_configs() or {}).keys()]
        except Exception as exc:
            logger.warning(f"[{self.plugin_name}] 读取通知渠道失败：{type(exc).__name__}: {exc}")
        result = self._notify(
            "【定时订阅下载】通知测试",
            "收到这条说明 MP 的通知链路是通的，补搜到资源后会按同样方式推送。",
        )
        if not result.get("ok"):
            return {"ok": False, "channels": channels,
                    "message": result.get("message") or "发送失败"}
        if not channels:
            return {"ok": True, "channels": [],
                    "message": "已交给 MoviePilot，但 MP 里还没配置通知渠道（设置 → 通知）"}
        return {"ok": True, "channels": channels,
                "message": f"已推送，MP 通知渠道 {len(channels)} 个：" + "、".join(channels[:5])}

    def api_downloader_test(self, name: str = "") -> dict:
        if not name:
            return {"ok": False, "message": "缺少下载器名称"}
        try:
            return self.bridge.test_downloader(name)
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:160]}"}

    def api_logs(self) -> dict:
        return {"ok": True, "logs": self.store.logs(), "hits": self.store.hits()}

    def api_probe(self) -> dict:
        """部署后自检：确认站点/下载器/调度器都通，便于排障。"""
        out: dict[str, Any] = {"ok": True, "enabled": self._enabled, "timezone": TZ}
        try:
            sites = self.bridge.list_sites()
            out["site_count"] = len(sites)
            out["active_site_count"] = sum(1 for s in sites if s.get("is_active"))
        except Exception as exc:
            out["site_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        try:
            out["downloaders"] = [d["name"] for d in self.bridge.list_downloaders()]
        except Exception as exc:
            out["downloader_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        try:
            out["searchable_count"] = len(self.bridge.searchable_domains())
        except Exception as exc:
            out["indexer_error"] = f"{type(exc).__name__}: {str(exc)[:160]}"
        out["scheduler_running"] = bool(self._scheduler and self._scheduler.running)
        out["jobs"] = [j.id for j in (self._scheduler.get_jobs() if self._scheduler else [])]
        out["sub_count"] = len(self.store.subs())
        return out

    def api_meta_search(self, name: str = "", kind: str = "series") -> dict:
        """新建订阅时实时查 TMDB：列出同名作品的年份/类型/集数，供用户挑选。"""
        items = search_meta(name, kind or "series")
        return {"ok": True, "items": items,
                "message": "" if items else "TMDB 没找到同名作品，可以直接保存，之后在卡片上点「同步海报」再试"}

    def api_meta_advance(self, tmdb_id: int = 0, season: int | None = None) -> dict:
        """新建订阅的「下一步」信息：TMDB 下一集播出日期（推荐更新时间）+ 媒体库已入库集数。

        前端选中 TMDB 作品后调它一次，就能同时把「推荐哪天更新」和
        「媒体库里已经入库了哪几集」拿到手，补全其它集数用。
        """
        if not tmdb_id:
            return {"ok": True, "next_ep": None, "next_air_date": "",
                    "last_ep": None, "last_air_date": "", "eps": [], "library_eps": []}
        try:
            air = meta_air_dates(int(tmdb_id), season)
        except Exception as exc:
            logger.warning(f"[{self.plugin_name}] TMDB 播出日期失败：{type(exc).__name__}: {exc}")
            air = {}
        try:
            lib = meta_library_eps(int(tmdb_id), season)
        except Exception as exc:
            logger.warning(f"[{self.plugin_name}] 媒体库读取失败：{type(exc).__name__}: {exc}")
            lib = []
        try:
            hist = meta_transferred_eps(int(tmdb_id), season)
        except Exception as exc:
            logger.warning(f"[{self.plugin_name}] 转移历史读取失败：{type(exc).__name__}: {exc}")
            hist = []
        try:
            dl = meta_downloaded_eps(int(tmdb_id), season)
        except Exception as exc:
            logger.warning(f"[{self.plugin_name}] 下载历史读取失败：{type(exc).__name__}: {exc}")
            dl = []
        return {"ok": True,
                "next_ep": air.get("next_ep"), "next_air_date": air.get("next_air_date") or "",
                "last_ep": air.get("last_ep"), "last_air_date": air.get("last_air_date") or "",
                "eps": air.get("eps") or [], "library_eps": lib,
                # 下到过（含已删）的集数合计：新建订阅时按这个提示从第几集补
                "delivered_eps": sorted({*lib, *hist, *dl})}

    def api_meta_refresh(self, payload: dict = Body(default={})) -> dict:
        """给所有订阅补/刷新 TMDB 海报与总集数；force=True 时连已有的也重查。

        顺手把「媒体库入库状态」也对齐一次：海报和入库是一件事的两半，
        用户点一次同步，看到的卡片就该是最新的（哪些已入库、哪些还没入库）。
        """
        force = bool((payload or {}).get("force"))
        updated = 0
        failed: list[str] = []
        for item in self.store.subs():
            if not force and item.get("poster_path"):
                continue
            meta = resolve_meta(item.get("name") or "", item.get("kind") or "series",
                                item.get("season"))
            if meta:
                fresh = self.store.set_meta(str(item.get("id") or ""), meta)
                self._refresh_schedule_cache(fresh or item)
                updated += 1
            else:
                failed.append(str(item.get("name") or ""))
        lib_changed = self._sync_libraries("（手动同步）")
        tail = (f"，{len(failed)} 条没认出来：{'、'.join(failed[:5])}" if failed else "")
        msg = f"已更新 {updated} 条海报"
        if lib_changed:
            msg += f"，入库状态刷新 {lib_changed} 条"
        return {"ok": True, "updated": updated, "failed": failed, "lib_changed": lib_changed,
                "message": msg + tail}

    # -------------------------------------------------- MP 自带订阅（导入 / 删除）
    # 用户原话：「mp 里面订阅了的弄一个可以复制到插件里面的，然后删除 mp 里面订阅的，这样方便点」。
    # 只做三件事：读 MP 的 subscribe 表 → 映射成本插件订阅落库 → 可选顺手删掉 MP 那条。
    _MP_KINDS = {"电影": "movie", "电视剧": "series"}
    _MP_STATES = {"R": "订阅中", "P": "待定", "S": "已暂停"}

    @staticmethod
    def _mp_site_ids(value: Any) -> list[int]:
        """MP 订阅的 sites 可能是 [34]、["34"] 或 '[34]' 字符串，统一成 int 列表。"""
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                value = [x for x in re.split(r"[,;\s]+", value) if x.strip()]
        out: list[int] = []
        for item in value or []:
            try:
                num = int(str(item).strip())
            except (TypeError, ValueError):
                continue
            if num > 0 and num not in out:
                out.append(num)
        return out

    @staticmethod
    def _mp_words(value: Any) -> str:
        """MP 的 include / exclude 是正则（如 (?=.*HHWEB)），只取字母数字词做包含匹配。"""
        words = [w for w in re.split(r"[^0-9A-Za-z\u4e00-\u9fff]+", str(value or ""))
                 if len(w) >= 2]
        return " ".join(dict.fromkeys(words))

    @staticmethod
    def _mp_rules(quality: Any, resolution: Any) -> dict:
        """MP 的画质正则翻译成本插件的多选规则：认得几个算几个，认不出就留空。"""
        text = f"{resolution or ''} {quality or ''}".lower()
        rules: dict[str, Any] = {}
        res = [v for v, keys in (("4k", ("2160", "4k", "uhd")), ("1080p", ("1080",)),
                                 ("720p", ("720",)), ("480p", ("480",)))
               if any(k in text for k in keys)]
        src = [v for v, keys in (("remux", ("remux",)),
                                 ("webdl", ("web-?dl", "webdl", "web-?rip", "webrip")),
                                 ("bluray", ("bluray", "blu-?ray", "bdrip")),
                                 ("hdtv", ("hdtv",)), ("dvd", ("dvd",)))
               if any(k in text for k in keys)]
        if res:
            rules["resolutions"] = res
        if src:
            rules["sources"] = src
        return rules

    def _mp_rows(self) -> list[Any]:
        """读 MP 自带订阅（subscribe 表）。

        优先直查库：`Subscribe.get_by_state` 就是 select 全表，不按用户过滤，
        结果最确定；官方 Oper 需要启动组合根配好事务执行器，读不到时再退回它。
        """
        try:
            from app.db import get_db
            from app.db import models as M

            db = next(get_db())
            return list(db.query(M.Subscribe).all())
        except Exception as exc:  # noqa: BLE001 - 老版本/表名不同再试 Oper
            logger.debug(f"[{self.plugin_name}] 直查 MP 订阅失败，改走 Oper：{type(exc).__name__}: {exc}")
        from app.db.oper.subscribe import SubscribeOper

        return list(SubscribeOper().list() or [])

    def _mp_site_map(self) -> dict[int, dict]:
        try:
            return {int(s["sid"]): s for s in self.bridge.list_sites() if s.get("sid")}
        except Exception:
            return {}

    def _mp_imported_map(self) -> dict[str, str]:
        """插件里已订阅的 TMDB ID → 插件订阅 id，用来给导入列表标「已导入」。"""
        out: dict[str, str] = {}
        for sub in self.store.subs():
            tid = str(sub.get("tmdb_id") or "")
            if tid:
                out[tid] = str(sub.get("id") or "")
        return out

    def _mp_item(self, row: Any, site_map: dict[int, dict],
                 imported: dict[str, str]) -> dict:
        """把一条 MP 订阅翻成前端能显示的扁平字典。"""
        def val(name: str, default: Any = "") -> Any:
            got = getattr(row, name, None)
            return default if got is None else got

        kind = self._MP_KINDS.get(str(val("type")), "series")
        tmdb_id = 0
        if str(val("media_source")).lower() in ("themoviedb", "tmdb"):
            try:
                tmdb_id = int(val("media_id", 0))
            except (TypeError, ValueError):
                tmdb_id = 0
        domains: list[str] = []
        names: list[str] = []
        for sid in self._mp_site_ids(val("sites", None)):
            site = site_map.get(sid) or {}
            domains.append(str(site.get("domain") or site.get("name") or sid))
            names.append(str(site.get("name") or site.get("domain") or sid))
        try:
            season = int(val("season", 0) or 0)
        except (TypeError, ValueError):
            season = 0
        state = str(val("state")).upper()
        try:
            total = int(val("total_episode", 0) or 0)
        except (TypeError, ValueError):
            total = 0
        try:
            start = int(val("start_episode", 0) or 0)
        except (TypeError, ValueError):
            start = 0
        try:
            lack = int(val("lack_episode", 0) or 0)
        except (TypeError, ValueError):
            lack = 0
        return {
            "id": int(val("id", 0) or 0),
            "name": str(val("name")).strip(),
            "year": str(val("year")),
            "kind": kind,
            "season": (season or 1) if kind == "series" else None,
            "tmdb_id": tmdb_id,
            "total_episode": total,
            "start_episode": start,
            "lack_episode": lack,
            "state": state,
            "state_text": self._MP_STATES.get(state, state or "未知"),
            "enabled": state == "R",
            "site_ids": domains,
            "site_names": names,
            "quality": str(val("quality")),
            "resolution": str(val("resolution")),
            "include": str(val("include")),
            "exclude": str(val("exclude")),
            "downloader": str(val("downloader")),
            "save_path": str(val("save_path")),
            "poster": str(val("poster")),
            "imported_id": imported.get(str(tmdb_id), "") if tmdb_id else "",
        }

    def api_mp_subs(self) -> dict:
        """列出 MP 自带订阅，标出插件里是否已经导入过（按 TMDB ID 判重）。"""
        try:
            rows = self._mp_rows()
        except Exception as exc:
            return {"ok": False, "items": [],
                    "message": f"读取 MP 订阅失败：{type(exc).__name__}: {str(exc)[:160]}"}
        site_map = self._mp_site_map()
        imported = self._mp_imported_map()
        items: list[dict] = []
        for row in rows:
            try:
                items.append(self._mp_item(row, site_map, imported))
            except Exception as exc:  # noqa: BLE001 - 单条脏数据不影响整表
                logger.debug(f"[{self.plugin_name}] 解析 MP 订阅失败：{type(exc).__name__}: {exc}")
        items.sort(key=lambda x: x["id"])
        return {"ok": True, "items": items,
                "message": "" if items else "MP 里没有订阅任务，不需要导入"}

    def api_mp_import(self, payload: dict = Body(default={})) -> dict:
        """把勾选的 MP 订阅复制成插件订阅；remove=True 时顺手删掉 MP 那条。"""
        data = payload or {}
        ids: list[int] = []
        for raw in data.get("ids") or []:
            try:
                num = int(str(raw).strip())
            except (TypeError, ValueError):
                continue
            if num > 0 and num not in ids:
                ids.append(num)
        if not ids:
            return {"ok": False, "message": "请先勾选要导入的 MP 订阅"}
        remove = bool(data.get("remove"))
        with_library = data.get("with_library") is not False
        try:
            rows = {int(getattr(r, "id", 0) or 0): r for r in self._mp_rows()}
        except Exception as exc:
            return {"ok": False,
                    "message": f"读取 MP 订阅失败：{type(exc).__name__}: {str(exc)[:160]}"}
        site_map = self._mp_site_map()
        imported, removed = 0, 0
        notes: list[str] = []
        for sid in ids:
            row = rows.get(sid)
            if row is None:
                notes.append(f"#{sid} MP 里已经没有了")
                continue
            try:
                item = self._mp_item(row, site_map, {})
            except Exception as exc:
                notes.append(f"#{sid} 解析失败：{type(exc).__name__}")
                continue
            if not item["name"]:
                notes.append(f"#{sid} 没有名字，跳过")
                continue
            body = {
                "name": item["name"],
                "kind": item["kind"],
                "season": item["season"],
                "site_ids": item["site_ids"],
                # MP 的 start_episode 是「接下来要补的集」，语义和本插件的 ep_start 一致
                "ep_start": item["start_episode"] or 1,
                "ep_end": item["total_episode"] or None,
                "include_words": self._mp_words(item["include"]),
                "exclude_words": self._mp_words(item["exclude"]),
                "rules": self._mp_rules(item["quality"], item["resolution"]),
                "downloader_id": item["downloader"] or "",
                "save_path": item["save_path"] or "",
                "notify": True,
                "enabled": bool(item["enabled"]),
            }
            try:
                sub = self.store.upsert_sub(body)
            except Exception as exc:
                notes.append(f"{item['name']} 落库失败：{str(exc)[:60]}")
                continue
            # 媒体库已入库 + 转移历史 + 下载历史里下到过（含已删）的集并进台账
            if with_library and item["tmdb_id"]:
                try:
                    done = set(meta_library_eps(int(item["tmdb_id"]), item["season"]) or [])
                    done.update(meta_transferred_eps(int(item["tmdb_id"]), item["season"]) or [])
                    done.update(meta_downloaded_eps(int(item["tmdb_id"]), item["season"]) or [])
                    if done:
                        self.store.mark_delivered(str(sub.get("id") or ""), sorted(done))
                except Exception as exc:  # noqa: BLE001 - 媒体库读不到不影响导入
                    logger.debug(f"[{self.plugin_name}] 导入时读媒体库/转移历史失败：{type(exc).__name__}: {exc}")
            try:
                sub2 = self._fill_meta(self.store.get_sub(str(sub.get("id") or "")) or sub,
                                       int(item["tmdb_id"]) or None)
                self._refresh_schedule_cache(sub2)
            except Exception:
                pass
            imported += 1
            if remove and not self._mp_delete_one(sid):
                notes.append(f"{item['name']} 已导入，但 MP 那条没删掉")
            elif remove:
                removed += 1
        self._rebuild_jobs()
        head = f"已导入 {imported} 条订阅"
        if remove:
            head += f"，并从 MP 删除 {removed} 条"
        if notes:
            head += "；" + "、".join(notes[:5])
        return {"ok": imported > 0, "imported": imported, "removed": removed,
                "notes": notes, "message": head if imported else (head or "没有可导入的订阅")}

    def _mp_delete_one(self, sid: int) -> bool:
        """删 MP 那一条订阅：优先走官方 Oper（会维护缓存与一致性），失败再直删。"""
        try:
            from app.db.oper.subscribe import SubscribeOper

            SubscribeOper().delete(int(sid))
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[{self.plugin_name}] SubscribeOper 删除失败，改直删：{type(exc).__name__}: {exc}")
        try:
            from app.db import get_db
            from app.db import models as M

            db = next(get_db())
            hit = db.query(M.Subscribe).filter(M.Subscribe.id == int(sid)).delete()
            db.commit()
            return bool(hit)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{self.plugin_name}] 删除 MP 订阅 {sid} 失败：{type(exc).__name__}: {exc}")
            return False

    def api_mp_delete(self, payload: dict = Body(default={})) -> dict:
        """只删 MP 里的订阅（不动插件）。"""
        ids: list[int] = []
        for raw in (payload or {}).get("ids") or []:
            try:
                num = int(str(raw).strip())
            except (TypeError, ValueError):
                continue
            if num > 0 and num not in ids:
                ids.append(num)
        if not ids:
            return {"ok": False, "message": "没有指定要删除的 MP 订阅", "removed": 0}
        done: list[int] = []
        failed: list[int] = []
        for sid in ids:
            ok = self._mp_delete_one(sid)
            (done if ok else failed).append(sid)
        text = f"已从 MP 删除 {len(done)} 条" if done else "删除失败"
        if failed:
            text += f"，{len(failed)} 条没删掉（#{'、#'.join(map(str, failed[:5]))}）"
        return {"ok": bool(done), "removed": len(done), "failed": failed,
                "message": text}

    def api_sub_save(self, payload: dict = Body(default={})) -> dict:
        try:
            payload = payload or {}
            wanted_id = payload.get("tmdb_id")
            # 媒体库补全：前端带上「媒体库已入库集数」，并入台账，订阅只补缺失的
            library_eps = payload.pop("library_eps", None)
            old = self.store.get_sub(str(payload.get("id") or ""))
            sub = self.store.upsert_sub(payload)
            if library_eps:
                try:
                    eps = [int(x) for x in library_eps if str(x).strip().isdigit()]
                    if eps:
                        sub = self.store.mark_delivered(str(sub.get("id") or ""), eps) or sub
                except (TypeError, ValueError):
                    pass
            if wanted_id:
                # 用户点选过候选：按 TMDB ID 取，避免同名再认错
                sub = self._fill_meta(sub, int(wanted_id))
            elif self._meta_stale(sub, old):
                # 新建或改了名字/季数时顺手查一次，卡片立刻就有海报和总集数
                sub = self._fill_meta(sub)
            # 缓存媒体库已入库集数 + 补默认推荐更新日（卡片 / 执行时间展示用）
            sub = self._refresh_schedule_cache(sub)
            self._rebuild_jobs()
            item = {**sub, "rules_summary": parse_rules(sub.get("rules")).summary()}
            item["done_eps"] = len(sub.get("delivered_eps") or [])
            item["poster_url"] = meta_poster_url(sub.get("poster_path"))
            return {"ok": True, "item": item}
        except (ValueError, TypeError) as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:200]}"}

    def api_sub_delete(self, payload: dict = Body(default={})) -> dict:
        sid = str((payload or {}).get("id") or "")
        ok = self.store.delete_sub(sid)
        self._rebuild_jobs()
        return {"ok": ok, "message": "" if ok else "订阅不存在"}

    def api_sub_toggle(self, payload: dict = Body(default={})) -> dict:
        data = payload or {}
        item = self.store.set_enabled(str(data.get("id") or ""), bool(data.get("enabled")))
        self._rebuild_jobs()
        return {"ok": bool(item), "message": "" if item else "订阅不存在"}

    def api_sub_run(self, payload: dict = Body(default={})) -> dict:
        """立即试运行：放后台线程立即返回，避免同步搜索让前端超时误报失败。

        之前这里同步调 _run_sub，站点多/搜索慢时请求可能几十秒，浏览器或
        网关等不到响应就断开，页面提示「执行失败」，但服务端实际跑完并
        提交成功（用户反馈：点立即执行提示失败，实际却下下去了）。
        """
        sid = str((payload or {}).get("id") or "")
        sub = self.store.get_sub(sid)
        if not sub:
            return {"ok": False, "message": "订阅不存在"}
        if not self._start_sub_bg(sid):
            return {"ok": True, "message": "该订阅已在执行中，稍候按订阅通知看结果"}
        logs = self.store.logs()
        return {"ok": True, "message": "已在后台执行一轮，完成后会按订阅通知发送结果",
                "last": logs[0] if logs else {},
                "item": next((s for s in self.store.public_subs() if s.get("id") == sid), {})}

    def api_sub_reset(self, payload: dict = Body(default={})) -> dict:
        """清空「已下载集数」台账，让订阅从起始集重新补。"""
        sid = str((payload or {}).get("id") or "")
        item = self.store.reset_delivered(sid)
        if not item:
            return {"ok": False, "message": "订阅不存在"}
        self._retry_state.pop(sid, None)
        self._cancel_retry(sid)
        return {"ok": True, "message": "已清空补搜进度"}

    def api_search(self, payload: dict = Body(default={})) -> dict:
        data = payload or {}
        try:
            domains = [str(x) for x in (data.get("domains") or []) if str(x).strip()]
            sub: dict[str, Any] = {}
            if data.get("sub_id"):
                sub = self.store.get_sub(str(data["sub_id"])) or {}
            if not domains:
                domains = sub.get("site_ids") or []
            if not domains:
                return {"ok": False, "message": "请先勾选要搜索的站点"}
            keyword = str(data.get("keyword") or "").strip()
            result = self.bridge.search(domains, keyword, self._max_results)
            rules = data.get("rules")
            if rules is None:
                rules = sub.get("rules")
            known = set() if data.get("preview") else self.store.hit_keys()
            pseudo = {
                "include_words": data.get("include_words") or sub.get("include_words", ""),
                "exclude_words": data.get("exclude_words") or sub.get("exclude_words", ""),
                "ep_start": sub.get("ep_start"), "ep_end": sub.get("ep_end"),
                "season": sub.get("season"),
                "kind": sub.get("kind", "series"), "rules": rules,
            }
            passed, rejected = filter_candidates(pseudo, result["items"], known,
                                                 rules_override=rules)
            return {"ok": True, "token": result["token"], "items": passed + rejected,
                    "passed": len(passed), "errors": result["errors"]}
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:200]}"}

    def api_download(self, payload: dict = Body(default={})) -> dict:
        data = payload or {}
        try:
            keys = [str(x) for x in (data.get("keys") or [])]
            if not keys:
                return {"ok": False, "message": "请先勾选资源"}
            result = self.bridge.submit(
                str(data.get("token") or ""), keys,
                str(data.get("downloader") or ""), str(data.get("category") or ""),
                str(data.get("save_path") or ""),
            )
            # 手动下载也要留痕 + 推通知：用户反馈过「找资源里下完了，QQ 一点动静没有」
            titles = data.get("titles") or {}
            src = str(data.get("sub") or "").strip() or "手动下载"
            accepted = []
            for row in result.get("results", []):
                if not row.get("ok"):
                    continue
                title = titles.get(row["key"]) or row["key"]
                accepted.append(title)
                self.store.add_hit({
                    "sub_id": str(data.get("sub_id") or ""), "sub": src,
                    "dedup_key": row["key"], "title": title,
                    "downloader": result.get("downloader"), "status": "submitted",
                })
            if accepted:
                lines = [f"来源：{src}",
                         f"下载器：{result.get('downloader') or '默认'}"]
                for title in accepted[:3]:
                    lines.append(f"资源：{title}")
                if len(accepted) > 3:
                    lines.append(f"……共 {len(accepted)} 条")
                self._notify(f"✅ 已提交下载｜{src} {len(accepted)} 条", "\n".join(lines))
            return result
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:200]}"}

    # --------------------------------------------- 远程入口（智能助手 / QQ / 微信等）
    @staticmethod
    def _remote_key(channel=None, source=None, user=None) -> str:
        """同一个聊天窗口的「选候选 / 选资源」状态按这个键隔离。"""
        return f"{channel}|{source}|{user}"

    def _run_remote_command(self, arg: str, channel=None, source=None, user=None,
                            action: str = "ptsub_subscribe") -> None:
        """远程命令总入口：按 action 分发到订阅 / 找资源 / 列订阅。"""
        key = self._remote_key(channel, source, user)
        try:
            if action == "ptsub_list":
                text = self._remote_list()
            elif action == "ptsub_find":
                text = self.remote_find(arg, key=key)
            else:
                text = self.remote_sub(arg, key=key)
        except Exception as exc:  # noqa: BLE001 - 远程入口不能把宿主拖崩
            logger.warning(f"[{self.plugin_name}] 远程命令处理失败：{type(exc).__name__}: {exc}")
            text = f"处理失败：{type(exc).__name__}: {str(exc)[:120]}"
        self._remote_post(channel, source, user, text)

    def _remote_post(self, channel=None, source=None, user=None, text: str = "",
                     username=None) -> None:
        """把结果回发给触发入口（QQ / TG / 微信等），与 MP 原生交互回复完全一致。

        关键点（对照 ``app/chain/interaction.py`` 的原生回复）：

        - 正文走 ``title``：QQ / TG 等渠道按聊天消息渲染，``text`` 在部分渠道会被截断或折叠；
        - ``save_history=False``：这是交互回执，不该混进「设置 → 通知」历史；
        - 带上 ``username``：部分渠道按昵称展示；
        - 不附加插件页链接：避免在聊天里显示成一张通知卡片。

        失败时退回插件通知链路（``_PluginBase.post_message``），再失败只记日志，
        绝不影响业务本身。
        """
        if not text:
            return
        # 首行当标题、其余当正文。
        # 关键：qqmsg 渠道的 send 会拼 `caption = f"*{title}*\n{text}"`。markdown 的
        # `*...*` 强调块一旦跨行，块内所有 `\n` 都会被折叠成空格——整份资源列表就
        # 挤成一大段（用户实测）。把正文挪到 text 字段后，正文落在 `*` 外面，
        # `\n` 才按行渲染；标题也正好只占一行，不再重复正文。
        head, _, body = text.partition("\n")
        head = head.strip()
        body = body.strip("\n")
        # 行尾两个空格 = markdown 硬换行：即便渠道把普通 `\n` 折叠成空格，
        # 硬换行仍会断行；单行消息不受影响（body 为空时不加）。
        if body:
            body = body.replace("\n", "  \n")
        try:
            from app.schemas.message import Message as _Message

            self.chain.post_message(_Message(
                channel=channel, source=source, userid=user, username=username,
                title=head, text=body or None, save_history=False,
            ))
            return
        except Exception as exc:  # noqa: BLE001 - 消息模型不可用时退回通知链路
            logger.debug(f"[{self.plugin_name}] 直发回复不可用，退回通知链路：{type(exc).__name__}: {exc}")
        try:
            self.post_message(channel=channel, source=source, userid=user,
                              username=username, title=head, text=body)
        except Exception as exc:  # noqa: BLE001 - 回执失败不影响订阅
            logger.warning(f"[{self.plugin_name}] 远程回复发送失败：{type(exc).__name__}: {exc}")

    # ------------------------------------------- 接管聊天渠道的普通文本（不带斜杠）
    def remote_intercept_text(self, channel=None, source=None, userid=None,
                              username: str = "", text: str = "") -> bool:
        """聊天渠道里的普通中文先给插件一次机会，返回 True 表示这条已处理。

        要接管的三类（都是 QQ 里最常见的说法）：

        - 「订阅 天才女友」「找资源 仙逆」：不用打斜杠，直接进插件流程
        - 列完候选后回「1」「1-3」：进第二屏选集数 / 站点 / 刷新时间
        - 第二屏里回「[从5集]」「[站点馒头]」「[每30分钟]」「确定」

        其余聊天内容返回 False，原样交给宿主处理，行为与没装插件时一致。
        """
        raw = str(text or "").strip()
        if not raw or raw.startswith("/"):
            return False
        key = self._remote_key(channel, source, userid)
        self._pending_prune()
        logger.info(f"[{self.plugin_name}] 收到聊天文本：{raw!r}（channel={channel} source={source} user={userid}）")

        # 刚搜完资源，用户回「订阅」= 把这部改成追更，不能当成新一次订阅查询
        if self._pending_dl.get(key) and raw in ("订阅", "订阅它", "订阅这个", "追更", "改订阅"):
            self._remote_post(channel, source, userid, self.remote_dl_subscribe(key=key), username=username)
            return True

        kind, rest = remote_ui.split_intent(raw)
        if kind:
            logger.info(f"[{self.plugin_name}] 识别为 {kind} 入口，片名={rest!r}，开始列候选")
            if not rest:
                self._remote_post(channel, source, userid, remote_ui.render_intent_hint(kind), username=username)
                return True
            try:
                text_out = (self.remote_find(rest, key=key) if kind == "find"
                            else self.remote_sub(rest, key=key))
            except Exception as exc:  # noqa: BLE001 - 出错也要接管，别让它掉回 MP 原生订阅
                logger.warning(f"[{self.plugin_name}] {kind} 处理失败：{type(exc).__name__}: {exc}")
                text_out = (f"处理「{raw}」时出错：{type(exc).__name__}: {str(exc)[:150]}\n"
                            f"可以再发一次，或用斜杠命令 /ptsub {rest}（不受此影响）。")
            self._remote_post(channel, source, userid, text_out, username=username)
            return True

        pending = self._pending.get(key)
        if pending:
            logger.info(f"[{self.plugin_name}] 待选状态：stage={pending.get('stage')}，收到 {raw!r}")
            try:
                text_out = self._intercept_pending(pending, key, raw)
            except Exception as exc:  # noqa: BLE001 - 回序号出错也要接管，别掉回 MP 原生
                logger.warning(f"[{self.plugin_name}] 回序号处理失败：{type(exc).__name__}: {exc}")
                text_out = f"处理「{raw}」时出错：{type(exc).__name__}: {str(exc)[:120]}；发「取消」可重新开始。"
            if text_out is None:
                return False
            self._remote_post(channel, source, userid, text_out, username=username)
            return True

        dl = self._pending_dl.get(key)
        if dl:
            logger.info(f"[{self.plugin_name}] 待下资源状态，收到 {raw!r}")
            try:
                text_out = self._intercept_dl(dl, key, raw)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{self.plugin_name}] 资源选择处理失败：{type(exc).__name__}: {exc}")
                text_out = f"处理「{raw}」时出错：{type(exc).__name__}: {str(exc)[:120]}；发「取消」可重新开始。"
            if text_out is None:
                return False
            self._remote_post(channel, source, userid, text_out, username=username)
            return True
        return False

    _CANCEL_WORDS = ("退出", "取消", "算了", "不了", "不要了", "q", "Q", "quit", "exit")

    def _intercept_pending(self, pending: Dict[str, Any], key: str,
                           raw: str) -> Optional[str]:
        """订阅流程里的第二/三步；返回 None 表示这条不该插件管。"""
        if raw in self._CANCEL_WORDS:
            self._pending.pop(key, None)
            return "已取消这次选择。要重来就发「订阅 片名」。"
        if pending.get("stage") == "opts":
            if remote_ui.is_confirm(raw):
                return self.remote_confirm(key)
            intent = remote_ui.parse_intent(raw, site_names=self._site_labels())
            known = any(intent.get(f) for f in (
                "ep_start", "ep_end", "sites", "schedule_mode", "pick"))
            if intent.get("name") and not known:
                return None  # 用户改主意说了别的片名，交给上层重新搜
            # 第二屏里又回了个数字 → 当成重选作品
            return self.remote_opt(raw, key=key)
        cands = pending.get("candidates") or []
        picks = remote_ui.pick_rows(raw, len(cands), multi=False)
        if picks:
            return self.remote_pick(picks[0], key=key)
        # 「1 从5集 站点馒头」/「[1] [从5集] [站点馒头]」：序号 + 参数一条带全，
        # 直接建订阅，省掉第二屏
        idx, rest = remote_ui.split_pick(raw)
        if idx is not None and 1 <= idx <= len(cands):
            return self.remote_pick(idx, key=key,
                                    opts=remote_ui.parse_intent(rest, site_names=self._site_labels()) if rest else None)
        intent = remote_ui.parse_intent(raw, site_names=self._site_labels())
        if any(intent.get(f) for f in ("ep_start", "ep_end", "sites", "schedule_mode")):
            return (f"先回序号告诉我要哪一部（1 ~ {len(cands)}），再说集数和站点。\n"
                    "也可以一条发：[2] [从5集] [站点馒头] [每天3:00]")
        return None

    def _intercept_dl(self, pending: Dict[str, Any], key: str,
                      raw: str) -> Optional[str]:
        """找资源流程里的回序号 / 转订阅；返回 None 表示这条不该插件管。"""
        preview_skip = ("不用", "不下了", "跳过", "先不下", "只订阅", "自动", "算了", "0")
        # 「订阅已建好、顺便列了资源」这一屏：回「不用」只是不现在下，订阅照旧挂着
        if pending.get("mode") == "preview" and raw in preview_skip:
            self._pending_dl.pop(key, None)
            return "好，只挂着订阅。到计划时间我自动搜并下发，不用再管。"
        if raw in self._CANCEL_WORDS:
            self._pending_dl.pop(key, None)
            return "已取消这次下载选择。要重搜就发「找资源 片名」。"
        if raw in ("订阅", "订阅它", "订阅这个", "追更", "改订阅"):
            return self.remote_dl_subscribe(key=key)
        # 翻页：一屏放不下时看后面的，序号全表连续，翻到哪页都能直接回数字
        if raw in ("下一页", "下页", "下一批", "更多", "再列", "继续", "后面的",
                   "后面", "往下翻", "下一条"):
            return self.remote_dl_page(key=key, delta=1)
        if raw in ("上一页", "上页", "上批", "往前翻", "前面"):
            return self.remote_dl_page(key=key, delta=-1)
        m = re.fullmatch(r"第?\s*([2-9]\d?)\s*[页屏]", raw)
        if m:
            return self.remote_dl_page(key=key, page=int(m.group(1)))
        rows = self._dl_rows(pending)
        if remote_ui.pick_rows(raw, len(rows), multi=True):
            return self.remote_dl_pick(raw, key=key)
        return None

    def _site_total(self) -> int:
        """可搜索站点数，用于第二屏写「全部 N 个可搜站」。"""
        try:
            return len(self._default_sites())
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _default_plan_text() -> str:
        """新订阅的默认刷新计划（模型默认每天 19:00）。"""
        return "每天 19:00"

    # ------------------------------------------------------------------ /ptsub
    def remote_sub(self, arg: str, key: str = "") -> str:
        """`/ptsub ...` 的调度：选候选 / 列站点 / 看帮助 / 带参数建订阅。"""
        text = str(arg or "").strip()
        if text.lower() in ("help", "?", "？", "帮助", "用法"):
            return remote_ui.render_help()
        # 已经有待选/待定状态：这条消息就是那一步的回答（序号 / 参数 / 确定）
        pending = self._pending.get(key)
        if pending:
            handled = self._intercept_pending(pending, key, text)
            if handled is not None:
                return handled
        intent = remote_ui.parse_intent(text, site_names=self._site_labels())
        if intent.get("list_sites") and not intent.get("name"):
            return self._remote_sites_text()
        if intent.get("pick") is not None:
            return self.remote_pick(intent["pick"], key=key, opts=intent)
        name = intent.get("name") or ""
        if not name:
            return remote_ui.render_help()
        return self.remote_start(name, kind=intent.get("kind") or "series",
                                 key=key, opts=intent)

    def remote_start(self, name: str, kind: str = "series", key: str = "",
                     opts: Optional[Dict[str, Any]] = None) -> str:
        """第一步：解析片名，列 TMDB 同名候选；唯一候选或没候选就直接建。

        opts 里可以带「从第几集 / 哪些站点 / 几点刷新 / 立即执行」，
        候选确认后会原样带进订阅。
        """
        name = (name or "").strip()
        if not name:
            return ("没有收到片名。用法：/ptsub 片名；带参数例："
                    "/ptsub 天下第一 [从5集] [站点馒头] [立即]")
        opts = dict(opts or {})
        extra, msg = self._resolve_sites_or_msg(opts.get("sites") or [])
        if msg:
            return msg
        opts.update(extra)
        want_kind = "movie" if str(opts.get("kind") or kind) == "movie" else "series"

        self._pending_prune()
        try:
            items = search_meta_fast(name, want_kind) or []
        except Exception as exc:  # noqa: BLE001 - TMDB 挂了也要能建订阅
            logger.warning(f"[{self.plugin_name}] TMDB 快查失败：{type(exc).__name__}: {exc}")
            items = []
        cands = self._brief_candidates(items, name, want_kind)

        if not cands:
            sub = self._remote_create(name, want_kind, None, opts)
            return ("TMDB 没找到同名作品，已直接建订阅「%s」（按%s）。\n%s"
                    % (name, self._kind_label(want_kind), self._remote_confirmed(sub, opts)))
        # 只认出一部、且用户没带任何参数时，也走「选集数/站点/刷新时间」那一屏：
        # 订阅本来就要定这些，别替用户默默按默认建。
        if len(cands) == 1 and not self._has_explicit_opts(opts):
            c = cands[0]
            self._pending[key] = {
                "name": name, "kind": c["kind"], "candidates": cands,
                "chosen": c, "opts": opts, "stage": "opts", "at": self._now_ts(),
            }
            return remote_ui.render_pick_menu(
                c, opts=opts, site_total=self._site_total(),
                plan_text=self._default_plan_text())
        if len(cands) == 1:
            c = cands[0]
            return self._create_with_preview(name, c["kind"], c["tmdb_id"], opts, key)

        self._pending[key] = {"name": name, "kind": want_kind,
                              "candidates": cands, "opts": opts, "at": self._now_ts()}
        extra = self._opts_hint(opts)
        head = remote_ui.render_candidates(name, cands)
        return f"{head}\n{extra}" if extra else head

    def remote_pick(self, pick, key: str = "",
                    opts: Optional[Dict[str, Any]] = None,
                    ask: bool = True) -> str:
        """第二步：用户回了序号。

        什么参数都没带时先**不建订阅**，而是进入第二屏让用户选
        「从第几集开始 / 用哪些站 / 多久刷一次」——这正是用户要的
        「回 1 → 跳出去选集数和订阅时间」。
        如果用户在同一条消息里带了参数（「[1] [从5集] [站点馒头] [每天3:00]」），
        就直接按参数建，省一轮。
        """
        self._pending_prune()
        pending = self._pending.get(key)
        if not pending:
            return remote_ui.render_no_choice()
        try:
            idx = int(pick)
        except (TypeError, ValueError):
            return "序号无效，回数字就行（如 2）。"
        cands = pending.get("candidates") or []
        if idx < 1 or idx > len(cands):
            return f"序号超出范围（1 ~ {len(cands)}），重发一次吧。"
        c = cands[idx - 1]
        merged = dict(pending.get("opts") or {})
        for field, value in (opts or {}).items():
            if field in ("pick", "pick_end", "extra", "name", "candidates"):
                continue
            if value not in (None, [], "", False):
                merged[field] = value
        # 用户可能在「回序号」这条消息里才补参数（如「[2] [从7集] [站点馒头]」）
        if merged.get("sites"):
            extra, msg = self._resolve_sites_or_msg(merged["sites"])
            if msg:
                return msg
            merged.update(extra)
        explicit = self._has_explicit_opts(merged)
        if ask and not explicit:
            # 进入「选集数 / 站点 / 刷新时间」这一屏
            self._pending[key] = {
                "name": pending.get("name") or c["title"],
                "kind": c["kind"],
                "candidates": cands,
                "chosen": c,
                "opts": merged,
                "stage": "opts",
                "at": self._now_ts(),
            }
            return remote_ui.render_pick_menu(
                c, opts=merged, site_total=self._site_total(),
                plan_text=self._default_plan_text())
        self._pending.pop(key, None)
        return self._create_with_preview(pending.get("name") or c["title"], c["kind"],
                                         c["tmdb_id"], merged, key)

    def remote_opt(self, text: str, key: str = "") -> str:
        """第二屏里改一项：集数 / 站点 / 刷新时间，累加到待建参数上。"""
        self._pending_prune()
        pending = self._pending.get(key)
        if not pending or pending.get("stage") != "opts":
            return remote_ui.render_no_choice()
        intent = remote_ui.parse_intent(text, site_names=self._site_labels())
        merged = dict(pending.get("opts") or {})
        cands = pending.get("candidates") or []
        # 顺手允许重选候选（如「3」）
        if intent.get("pick") is not None and not intent.get("name"):
            idx = int(intent["pick"])
            if idx < 1 or idx > len(cands):
                return f"序号超出范围（1 ~ {len(cands)}）。"
            pending["chosen"] = cands[idx - 1]
        before = dict(merged)
        for field in ("ep_start", "ep_end", "season", "schedule_mode",
                      "schedule_time", "schedule_interval", "schedule_weekday",
                      "resolution"):
            value = intent.get(field)
            if value not in (None, "", [], 0):
                merged[field] = value
        if intent.get("run_now"):
            merged["run_now"] = True
        if intent.get("sites"):
            extra, msg = self._resolve_sites_or_msg(intent["sites"])
            if msg:
                return msg
            merged.update(extra)
            merged["site_names"] = self._site_names(merged.get("site_ids") or [])
        if merged == before and pending.get("chosen") is not None and not intent.get("pick"):
            return ("没看出要改哪一项。回 [从5集] [站点馒头] [1080P] [每天3:00] "
                    "[每30分钟] 这类，或回「确定」直接建。\n"
                    + remote_ui.render_pick_menu(
                        pending.get("chosen") or {}, opts=merged,
                        site_total=self._site_total(), plan_text=self._default_plan_text()))
        pending["opts"] = merged
        pending["at"] = self._now_ts()
        self._pending[key] = pending
        return remote_ui.render_opts_state(
            pending.get("chosen") or {}, opts=merged,
            site_total=self._site_total(), plan_text=self._default_plan_text())

    def remote_confirm(self, key: str = "") -> str:
        """第二屏里回「确定」：按当前设置建订阅并立刻补搜一轮。"""
        self._pending_prune()
        pending = self._pending.get(key)
        if not pending or pending.get("stage") != "opts":
            return remote_ui.render_no_choice()
        chosen = pending.get("chosen") or {}
        opts = dict(pending.get("opts") or {})
        opts["run_now"] = True
        self._pending.pop(key, None)
        return self._create_with_preview(
            pending.get("name") or chosen.get("title") or "",
            chosen.get("kind") or pending.get("kind") or "series",
            chosen.get("tmdb_id"), opts, key)

    @staticmethod
    def _brief_candidates(items: List[dict], name: str, kind: str) -> List[dict]:
        """TMDB 结果 → 候选列表（去重、只留能认人的字段）。"""
        cands: List[dict] = []
        seen: set = set()
        for it in items or []:
            tid = it.get("tmdb_id")
            if not tid or int(tid) in seen:
                continue
            seen.add(int(tid))
            cands.append({
                "tmdb_id": int(tid),
                "kind": "series" if it.get("kind") == "series" else "movie",
                "title": it.get("meta_title") or name,
                "year": str(it.get("year") or ""),
                "eps": int(it.get("total_eps") or 0),
                "is_anim": bool(it.get("is_anim")),
                "overview": str(it.get("overview") or ""),
            })
        return cands

    @staticmethod
    def _has_explicit_opts(opts: Optional[Dict[str, Any]]) -> bool:
        """用户是否已经明确指定了集数 / 站点 / 分辨率 / 刷新计划中的任意一项。"""
        opts = dict(opts or {})
        return any(opts.get(f) for f in (
            "ep_start", "ep_end", "season", "site_ids", "resolution",
            "schedule_mode", "schedule_interval", "run_now",
        ))

    @staticmethod
    def _opts_hint(opts: Dict[str, Any]) -> str:
        """把已经带上的参数回显一行，避免用户以为自己没说上。"""
        bits: List[str] = []
        if opts.get("ep_start"):
            end = opts.get("ep_end")
            bits.append(f"第{opts['ep_start']}-{end}集" if end and end != opts["ep_start"]
                        else f"从第{opts['ep_start']}集起")
        if opts.get("site_ids"):
            bits.append(f"{len(opts['site_ids'])} 个站点")
        if opts.get("resolution"):
            bits.append(f"只要{str(opts['resolution']).upper()}")
        if opts.get("schedule_mode"):
            bits.append("立即执行" if opts.get("run_now") else "自定义时间")
        elif opts.get("run_now"):
            bits.append("建完立即跑一轮")
        return ("已带参数：" + " · ".join(bits)) if bits else ""

    # ------------------------------------------------------------------ /ptfind
    def remote_find(self, arg: str, key: str = "") -> str:
        """`/ptfind ...`：只找资源直接下发下载器，不建订阅。"""
        text = str(arg or "").strip()
        if text.lower() in ("help", "?", "？", "帮助", "用法"):
            return remote_ui.render_help()
        if text in ("订阅", "订阅它", "订阅这个", "就这个", "sub"):
            return self.remote_dl_subscribe(key=key)
        intent = remote_ui.parse_intent(text, site_names=self._site_labels())
        if intent.get("list_sites") and not intent.get("name"):
            return self._remote_sites_text()
        if intent.get("pick") is not None and not intent.get("name"):
            return self.remote_dl_pick(intent.get("extra") or "", key=key)
        name = intent.get("name") or ""
        if not name:
            return ("用法：/ptfind 片名 —— 只找资源直接下载，不建订阅。\n"
                    "可加：/ptfind 天下第一 [5-8集] [站点馒头]")
        return self.remote_dl_start(name, intent, key=key)

    def remote_dl_start(self, name: str, intent: Optional[Dict[str, Any]] = None,
                        key: str = "") -> str:
        """搜一轮资源，按做种数排序列出来，等用户回序号。"""
        intent = dict(intent or {})
        extra, msg = self._resolve_sites_or_msg(intent.get("sites") or [])
        if msg:
            return msg
        domains = extra.get("site_ids") or self._default_sites()
        if not domains:
            return ("MP 里还没有启用任何站点：去 MoviePilot 站点页添加并启用站点，"
                    "然后重试。")
        keyword = search_keyword({"name": name})
        try:
            result = self.bridge.search(domains, keyword, self._max_results)
        except Exception as exc:  # noqa: BLE001
            return f"搜索失败：{type(exc).__name__}: {str(exc)[:140]}"

        # 只按集数过滤，不套订阅规则：用户说的是「直接找资源下」，别把结果筛没了。
        # 但用户带了「1080P」这类分辨率时要按它筛。
        pseudo = {
            "include_words": "", "exclude_words": "",
            "ep_start": intent.get("ep_start"), "ep_end": intent.get("ep_end"),
            "season": intent.get("season"),
            "kind": "movie" if intent.get("kind") == "movie" else "series",
        }
        rules_override = ({"resolutions": [str(intent["resolution"])]}
                          if intent.get("resolution") else {})
        passed, _ = filter_candidates(pseudo, result.get("items") or [], set(),
                                      rules_override=rules_override)
        passed = self._rank_rows(name, passed)
        if not passed:
            return (f"「{name}」没搜到可用资源。\n"
                    "换个片名写法，或去掉集数限制，或去插件页确认站点可搜索。"
                    f"{self._search_fail_reason(result)}")

        rows = passed
        self._pending_dl[key] = {"name": name, "token": result.get("token") or "",
                                 "rows": rows, "all_rows": rows, "offset": 0,
                                 "ep_start": intent.get("ep_start"),
                                 "errors": len(result.get("errors") or []),
                                 "at": self._now_ts()}
        return self._dl_text(self._pending_dl[key], key)

    @staticmethod
    def _rank_rows(name: str, rows: list) -> list:
        """名称精准命中优先，组内按做种从多到少。

        PT 站关键词搜索会给一堆同名前缀/同名作品（搜「天下第一」会混进
        《天下第一剑》《天下第一楼》）。这里按「标题剥掉集数/年份/季之后和
        片名完全一致」→「标题带片名」→「片名带标题」→「勉强沾边」分层，
        每一层内部都按做种从多到少排——名称对得上的先列，做种多的先下。
        """
        core = _norm_name(name)

        def _key(r: dict):
            title = str(r.get("display_title") or r.get("title") or "")
            return (_name_tier(core, title),
                    -(r.get("seeders") or 0),
                    r.get("size_gb") or 0)

        return sorted(rows, key=_key)

    def _dl_rows(self, pending: Dict[str, Any]) -> list:
        """待选资源的完整清单（分页只是显示层，序号始终对应这份全表）。"""
        return list(pending.get("all_rows") or pending.get("rows") or [])

    def _dl_text(self, pending: Dict[str, Any], key: str = "") -> str:
        """按当前页渲染资源清单，并接上这个场景该给的下一步指令。"""
        rows = self._dl_rows(pending)
        offset = int(pending.get("offset") or 0)
        name = str(pending.get("name") or "")
        text = remote_ui.render_resources(name, rows, offset=offset, total=len(rows))
        if pending.get("mode") == "preview":
            text += ("\n回序号就下这条（可回 1-3 多条）；回「不用」就只挂着订阅，"
                     "等计划时间自动搜。")
        else:
            text += "\n回「1」下第 1 条；多条回「1-3」或「1,3」；回「订阅」改成定时追更。"
        if pending.get("errors"):
            text += f"\n（{pending['errors']} 个站点报错，已跳过）"
        return text

    def remote_dl_page(self, key: str = "", delta: int = 1,
                       page: Optional[int] = None) -> str:
        """翻页看后面的资源；序号保持全表连续，翻到哪页都能直接回数字。"""
        self._pending_prune()
        pending = self._pending_dl.get(key)
        if not pending:
            return ("没有待选资源了。先发「找资源 片名」重新搜一次。")
        rows = self._dl_rows(pending)
        total = len(rows)
        size = max(1, int(remote_ui.MAX_RESOURCES))
        if total <= size:
            return self._dl_text(pending, key) + "\n（就这些了，没有下一页）"
        offset = int(pending.get("offset") or 0)
        if page is not None:
            new_off = max(0, (int(page) - 1) * size)
        else:
            new_off = offset + int(delta) * size
        if new_off < 0:
            return "已经在第一页了。"
        if new_off >= total:
            return (f"没有更多了，一共 {total} 条，已经在最后一页。\n"
                    "回「上一页」往回翻，或直接回序号下。")
        pending["offset"] = new_off
        return self._dl_text(pending, key)

    def remote_dl_pick(self, addr: str, key: str = "") -> str:
        """用户回了序号：把对应资源提交到 MP 默认下载器。"""
        self._pending_prune()
        pending = self._pending_dl.get(key)
        if not pending:
            return "没有待选资源。先发 /ptfind 片名 搜一下。"
        rows = self._dl_rows(pending)
        picks = remote_ui.pick_rows(addr, len(rows), multi=True)
        if not picks:
            return f"序号无效（1 ~ {len(rows)}）。可以回「1」或「1-3」。"
        chosen = [rows[i - 1] for i in picks]
        try:
            result = self.bridge.submit(
                pending.get("token") or "", [c.get("key") or "" for c in chosen],
                "", "", "",
            )
        except Exception as exc:  # noqa: BLE001
            return f"提交下载器失败：{type(exc).__name__}: {str(exc)[:140]}"
        ok_keys = {r.get("key") for r in (result.get("results") or []) if r.get("ok")}
        done = [c for c in chosen if (c.get("key") or "") in ok_keys]
        sub_id = str(pending.get("sub_id") or "")
        for c in done:
            self.store.add_hit({
                "sub_id": sub_id, "sub": "远程直下" if not sub_id else "订阅先手",
                "dedup_key": c.get("key") or "",
                "title": c.get("display_title") or c.get("title") or "",
                "site": c.get("site") or "", "domain": c.get("domain") or "",
                "resolution": resolution_of(c.get("attrs")),
                "size_gb": c.get("size_gb"),
                "downloader": result.get("downloader"), "status": "submitted",
            })
        if not done:
            msg = (result.get("results") or [{}])[0].get("message") or "下载器未接受"
            return f"没下成：{msg}"
        self._pending_dl.pop(key, None)
        lines = [f"已提交 {len(done)} 条到「{result.get('downloader') or '默认下载器'}」："]
        for c in done:
            lines.append("· " + str(c.get("display_title") or c.get("title") or "")[:56])
        if len(chosen) > len(done):
            lines.append(f"（{len(chosen) - len(done)} 条被下载器拒绝）")
        if sub_id:
            lines.append("订阅照旧挂着，按计划自动追更。")
        else:
            lines.append("进度在 MP 的下载页看；想改成追更就发「/ptsub %s」。"
                         % (pending.get("name") or ""))
        return "\n".join(lines)

    def remote_dl_subscribe(self, key: str = "") -> str:
        """把刚才搜到的作品直接改成订阅（用户从「只下这次」转成「追更」）。"""
        self._pending_prune()
        pending = self._pending_dl.get(key)
        if not pending:
            return "还没有搜过资源。先发 /ptfind 片名，再回「订阅」。"
        name = pending.get("name") or ""
        self._pending_dl.pop(key, None)
        opts = {"ep_start": pending.get("ep_start")}
        sub = self._remote_create(name, "series", None, opts)
        return self._remote_confirmed(sub, opts)

    # ------------------------------------------------------------------ /ptlist
    def _remote_list(self) -> str:
        try:
            subs = self.store.public_subs()
        except Exception as exc:  # noqa: BLE001
            return f"读取订阅失败：{type(exc).__name__}: {str(exc)[:120]}"
        return remote_ui.render_sub_list(subs)

    # ------------------------------------------------------------------ 公共零件
    def _remote_create(self, name: str, kind: str, tmdb_id: Optional[int],
                       opts: Optional[Dict[str, Any]] = None) -> dict:
        """落库一条订阅（带远程参数）并补 TMDB 元数据，重建调度计划。"""
        opts = dict(opts or {})
        payload: Dict[str, Any] = {
            "name": name,
            "kind": "movie" if str(kind) == "movie" else "series",
            "site_ids": opts.get("site_ids") or self._default_sites(),
            "enabled": True,
        }
        for field in ("ep_start", "ep_end", "season", "schedule_interval"):
            value = opts.get(field)
            if value:
                payload[field] = int(value)
        for field in ("schedule_mode", "schedule_time", "schedule_weekday"):
            value = opts.get(field)
            if value:
                payload[field] = value
        # 远程带的分辨率写进订阅规则，补搜时按规则过滤（只挑这个清晰度）
        if opts.get("resolution"):
            payload["rules"] = {"resolutions": [str(opts["resolution"])]}
        sub = self.store.upsert_sub(payload)
        if tmdb_id:
            sub = self._fill_meta(sub, int(tmdb_id)) or sub
        else:
            sub = self._fill_meta(sub) or sub
        self._rebuild_jobs()
        return sub

    def _remote_confirmed(self, sub: dict,
                          opts: Optional[Dict[str, Any]] = None) -> str:
        """把一条订阅翻译成回执（作品 / 站点 / 计划 / 是否立即跑）。"""
        opts = dict(opts or {})
        try:
            plan = schedule_label(sub)
        except Exception:
            plan = "按计划到勾选站点搜资源"
        text = remote_ui.render_sub_created(
            sub, site_names=self._site_names(sub.get("site_ids") or []), plan=plan)
        unknown = opts.get("unknown_sites") or []
        if unknown:
            text += f"\n（没认出的站点：{'、'.join(unknown[:3])}，已按其余站点建）"
        if opts.get("run_now"):
            text += "\n" + self._run_sub_bg(str(sub.get("id") or ""))
        return text

    def _create_with_preview(self, name: str, kind: str, tmdb_id: Optional[int],
                             opts: Optional[Dict[str, Any]] = None,
                             key: str = "") -> str:
        """建订阅 + 顺手先搜一轮，把能下的资源摆出来给用户挑（回序号才下）。

        用户的原话是「提前把需要的查找完给我资源选择，我再告诉它要哪个」，
        所以这里不等计划到点：订阅先落库（不丢），紧接着同步搜一轮，
        把结果按做种数排好列出来；回序号就下那条，回「不用」就只挂着订阅。
        """
        opts = dict(opts or {})
        run_now = bool(opts.pop("run_now", False))
        sub = self._remote_create(name, kind, tmdb_id, opts)
        text = self._remote_confirmed(sub, opts)
        preview, found = self._preview_resources(sub, opts, key)
        if found:
            # 已经把资源摆出来了：别再让后台重复搜一轮
            return text + "\n\n" + preview
        if run_now:
            text += "\n" + self._run_sub_bg(str(sub.get("id") or ""))
        return text + ("\n\n" + preview if preview else "")

    def _preview_resources(self, sub: dict, opts: Optional[Dict[str, Any]] = None,
                           key: str = "") -> Tuple[str, bool]:
        """建完订阅先搜一轮；返回 (文案, 是否搜到资源)。搜到则把待选资源挂到 key 上。"""
        opts = dict(opts or {})
        if not key:
            return "", False
        domains = list(opts.get("site_ids") or sub.get("site_ids")
                       or self._default_sites() or [])
        name = str(sub.get("name") or "")
        if not domains or not name:
            return "", False
        try:
            result = self.bridge.search(domains, search_keyword(sub), self._max_results)
        except Exception as exc:  # noqa: BLE001 - 搜不到不影响订阅本身
            return (f"顺手先搜一轮没成：{type(exc).__name__}: {str(exc)[:80]}（不影响订阅）",
                    False)
        pseudo = {
            "include_words": "", "exclude_words": "",
            "ep_start": sub.get("ep_start"), "ep_end": sub.get("ep_end"),
            "season": sub.get("season"),
            "kind": "movie" if str(sub.get("kind")) == "movie" else "series",
        }
        rules = sub.get("rules") or {}
        rules_override = ({"resolutions": rules.get("resolutions")}
                          if rules.get("resolutions") else {})
        passed, _ = filter_candidates(pseudo, result.get("items") or [], set(),
                                      rules_override=rules_override)
        passed = self._rank_rows(name, passed)
        if not passed:
            tail = self._search_fail_reason(result)
            return ("顺手先搜了一轮：现在还没搜到能下的资源，到计划时间会自动再搜。" + tail,
                    False)
        rows = passed
        self._pending_dl[key] = {
            "name": name, "token": result.get("token") or "", "rows": rows,
            "all_rows": rows, "offset": 0,
            "sub_id": str(sub.get("id") or ""), "ep_start": sub.get("ep_start"),
            "mode": "preview", "at": self._now_ts(),
        }
        return self._dl_text(self._pending_dl[key], key), True

    def _start_sub_bg(self, sid: str) -> bool:
        """启动后台补搜线程；同一订阅已在跑时返回 False（防重复点击）。

        立即执行必须放后台：同步跑一轮含网络搜索（站点多时可能几十秒），
        浏览器或网关等不到响应就断开，页面会误报「执行失败」，但服务端
        实际跑完并提交成功（用户反馈：点立即执行提示失败，实际却下下去了）。
        """
        if not sid:
            return False
        name = f"ptsub-now-{sid}"
        try:
            for t in threading.enumerate():
                if t.name == name and t.is_alive():
                    return False

            def worker() -> None:
                try:
                    self._run_sub(sid, force=True)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[{self.plugin_name}] 立即执行失败：{type(exc).__name__}: {exc}")

            threading.Thread(target=worker, name=name, daemon=True).start()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[{self.plugin_name}] 立即执行启动失败：{type(exc).__name__}: {exc}")
            return False

    def _run_sub_bg(self, sid: str) -> str:
        """立即执行放后台，先回执再搜，QQ 里不用干等一两分钟。"""
        if not self._start_sub_bg(sid):
            return "该订阅已在执行中，稍等一下再看结果。"
        return "已在后台立即搜一轮，结果会按订阅通知再发你。"

    def _resolve_sites(self, words: List[str]) -> Tuple[List[str], List[str], List[str]]:
        """站点口语名 → MP 站点域名。返回 (命中的域名, 没认出来的词, 存在但被停用的站名)。

        用户只会说「馒头」「岛」，不会记域名，所以按 名字包含 / 域名包含 双向模糊匹配。
        能不能搜完全交给 MP（搜索时按官方链执行，内部再换成站点 id），这里只把
        「被手动停用」当不可用——绝不用索引器清单做否定判断，否则 MP 的系统级用户认证
        临时掉线时会把所有站点误判成「认证没做好」，而用户侧的站点认证一直是好的。
        """
        words = [str(w).strip() for w in (words or []) if str(w).strip()]
        if not words:
            return [], [], []
        try:
            sites = self.bridge.list_sites()
        except Exception:
            sites = []
        picked: List[str] = []
        unknown: List[str] = []
        blocked: List[str] = []
        for word in words:
            low = word.lower()
            hit = None
            for site in sites:
                domain = str(site.get("domain") or "")
                name = str(site.get("name") or "")
                if low in (domain.lower(), name.lower()):
                    hit = site
                    break
                if low and (low in name.lower() or low in domain.lower()):
                    hit = site
                    break
            if not hit:
                unknown.append(word)
                continue
            domain = str(hit.get("domain") or "")
            if not domain:
                unknown.append(word)
                continue
            if not hit.get("is_active", True):
                # 站点在 MP 里被手动停用了，这才是真的搜不了
                blocked.append(f"{hit.get('name') or domain}（站点已停用）")
                continue
            if domain not in picked:
                picked.append(domain)
        return picked, unknown, blocked

    def _resolve_sites_or_msg(self, words: List[str]) -> Tuple[Dict[str, Any], str]:
        """把站点词解析成 opts，认不出来就返回提示文案（调用方直接回给用户）。"""
        picked, unknown, blocked = self._resolve_sites(words)
        if unknown and not picked:
            return {}, (f"没认出这些站点：{'、'.join(unknown[:4])}\n"
                        f"{self._remote_sites_text(limit=8)}")
        if blocked and not picked:
            return {}, (f"站点「{'、'.join(blocked[:4])}」现在搜不了："
                        f"{self._remote_sites_text(limit=8)}")
        return {"site_ids": picked, "unknown_sites": unknown + blocked}, ""

    def _remote_sites_text(self, limit: int = 9) -> str:
        """能用的站点清单 + 用法；用户发「站点」或站点打错时给提示。"""
        try:
            sites = self.bridge.list_sites()
        except Exception as exc:  # noqa: BLE001
            return f"读取站点失败：{type(exc).__name__}: {str(exc)[:100]}"
        # 只把「停用」当不可用；索引器清单读不到时不再据此判定（会大面积误伤）
        active = [s for s in sites if s.get("is_active", True)]
        if not active:
            return "MP 里还没有启用任何站点：去 MoviePilot 站点页添加并启用站点。"
        names = [str(s.get("name") or s.get("domain")) for s in active[:limit]]
        more = f" 等 {len(active)} 个" if len(active) > limit else ""
        return f"可用站点 {len(active)} 个：{'、'.join(names)}{more}。\n用法：站点 馒头"

    def _default_sites(self) -> List[str]:
        """远程建订阅的默认站点：所有已启用的站点（存域名，搜索时再换成站点 id）。"""
        try:
            sites = self.bridge.list_sites()
        except Exception:
            return []
        return [str(s["domain"]) for s in sites if s.get("is_active", True)]

    def _site_labels(self) -> List[str]:
        """站点口语名 + 域名清单，供 parse_intent 识别「观众」「馒头」这类裸站名。"""
        try:
            sites = self.bridge.list_sites()
        except Exception:
            return []
        labels: List[str] = []
        for s in sites:
            for k in ("name", "domain"):
                v = str(s.get(k) or "").strip()
                if v:
                    labels.append(v)
        return labels

    def _search_fail_reason(self, result: Optional[Dict[str, Any]] = None) -> str:
        """搜不到时给出准确原因（追加到提示末尾）。

        优先识别 MP 的系统级「用户认证」门槛：认证不过时所有站点都被禁用、索引器为空，
        但用户侧的站点认证可能是好的——这正是「认证好了却搜不了」的错位来源。
        返回空串表示没有额外原因可提示。
        """
        level = self.bridge.auth_level()
        if 0 <= level < 2:
            return ("\n\nMP 的「用户认证」没过（这是 MP 自己每约 10 分钟跑一次的账号自检，"
                    "不是你的站点登录失效，cookie/密钥不用重填）。\n"
                    "自检失败期间 MP 会关掉「搜索链」，所以聊天里怎么搜都是 0 条，"
                    "而站点页面直接浏览还能用。等下次自检通过（10 分钟内）自动恢复。")
        errors = (result or {}).get("errors") or []
        if errors:
            sample = errors[0].get("message") or ""
            return f"\n\n搜索时部分站点报错：{sample[:120]}"
        return ""

    @staticmethod
    def _kind_label(kind: str) -> str:
        return "剧集" if str(kind) == "series" else "电影"

    def _pending_prune(self) -> None:
        """清理超过 30 分钟没确认的候选 / 资源选择。"""
        now = self._now_ts()
        for store in (self._pending, self._pending_dl):
            for k in [k for k, v in store.items() if now - (v.get("at") or 0) > 1800]:
                store.pop(k, None)

    @staticmethod
    def _now_ts() -> float:
        import time as _time

        return _time.time()
