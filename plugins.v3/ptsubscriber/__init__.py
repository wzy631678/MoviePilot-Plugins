"""PtSubscriber —— MoviePilot v3 插件：定时订阅下载面板。

补 MP 缺的两点：① 自己选周几/几点去指定站点搜资源；② 订阅规则简单直观。
站点、Cookie、下载器全部直接复用 MP，插件不维护第二份凭据。
"""
from __future__ import annotations

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
from .models import next_episode, resolution_of, schedule_label, search_keyword
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


def build_trigger(sub: dict, tz: str):
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.interval import IntervalTrigger

    mode = sub.get("schedule_mode", "daily")
    if mode == "interval":
        return IntervalTrigger(minutes=max(5, int(sub.get("schedule_interval") or 120)), timezone=tz)
    hour, minute = (int(x) for x in str(sub.get("schedule_time") or "03:00").split(":"))
    if mode == "weekly":
        return CronTrigger(day_of_week=sub.get("schedule_weekday") or "mon",
                           hour=hour, minute=minute, timezone=tz)
    return CronTrigger(hour=hour, minute=minute, timezone=tz)


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
    plugin_desc = "自己选周几几点，到指定站点搜资源并按详细规则下载到指定容器；自动对接 TMDB，按片名、年份、剧集/电影、集数逐项比对，只把真正这部排在最前。聊天入口不用打斜杠，一条说全最省事：「订阅 天下第一 观众 1080P 周一5:30」（片名/站点/清晰度/周期顺序随意）；也可以只发「订阅 天才女友」先列同名作品，回序号后再选「从第几集 / 哪些站 / 多久刷一次」，回「确定」立刻补搜并把能下的资源列出来让你挑；「查资源 仙逆 馒头 1080P」只搜一轮直接下，不建订阅。斜杠命令 /ptsub、/ptfind、/ptlist 一样可用；回复全是纯文本不带图。"
    plugin_version = "1.9.1"
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

    def _run_sub(self, sid: str, force: bool = False) -> None:
        """补搜一轮：只盯「下一个还没下到的集」，下到就记账，没下到就等重试。

        force=True 表示手动「立即执行」：即使订阅处于停用状态也照样跑一轮。
        """
        sub = self.store.get_sub(sid)
        if not sub or (not sub.get("enabled") and not force):
            return
        domains = sub.get("site_ids") or []
        keyword = search_keyword(sub)
        is_movie = (sub.get("kind") or "series") == "movie"
        # 填了集数区间就「一集一集补」（追更场景）；没填就一次把命中的都收下来（补老剧）
        one_by_one = (not is_movie) and bool(sub.get("ep_start") or sub.get("ep_end"))
        target = next_episode(sub) if one_by_one else None
        entry: dict[str, Any] = {
            "sub_id": sid, "sub": sub.get("name"), "sites": len(domains), "keyword": keyword,
            "target": ("电影" if is_movie else
                       (f"第 {target} 集" if target else ("全部未下集" if not one_by_one else "已补齐"))),
            "found": 0, "submitted": 0, "status": "ok", "message": "",
            # 明细：站点 / 分辨率 / 集数 / 每条资源，运行记录里直接展示
            "site": "", "resolution": "", "episode": "", "items": [],
        }
        if not domains:
            entry.update(status="skipped", message="未勾选站点")
            self._finish(sub, entry)
            return
        if one_by_one and target is None:
            self._retry_state.pop(sid, None)
            self._cancel_retry(sid)
            entry.update(status="skipped", message="订阅集数已全部下完，无需补搜")
            self._finish(sub, entry)
            return
        try:
            result = self.bridge.search(domains, keyword, self._max_results)
            passed, _ = filter_candidates(sub, result["items"], self.store.hit_keys(),
                                          target_ep=target)
            # 同一集可能有多个版本：按做种数优先，挑最稳的
            passed.sort(key=lambda x: (-(x.get("seeders") or 0), x.get("size_gb") or 0))
            entry["found"] = len(passed)
            if not passed:
                wait = self._schedule_retry(sid, sub)
                level = self.bridge.auth_level()
                auth_note = ("（MP 用户认证临时未通过，站点功能被禁用，会自动重试）"
                             if 0 <= level < 2 else "")
                entry.update(status="waiting", site=self._site_names(domains),
                             message=f"{entry['target']}还没更新{auth_note}；{wait}")
                self._finish(sub, entry)
                return
            # 追更只挑一个版本；单片也只要一个；补老剧才一次收多条（每集留一个）
            picks = passed[:1] if (one_by_one or is_movie) else passed[:5]
            # 同一集只留一个版本，避免一次把同集的不同小组都下下来
            if not one_by_one:
                seen_eps: set[int] = set()
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
                nxt = next_episode(self.store.get_sub(sid) or sub) if not is_movie else None
                tail = (f"；下一集等第 {nxt} 集" if nxt
                        else ("；区间内已补齐" if one_by_one else ""))
                entry["message"] = f"已提交 {len(titles)} 条：{titles[0][:40]}" + tail
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
        items = self.store.public_subs()
        # 下次执行时间直接问调度器，避免重启后库里存的是旧值
        for item in items:
            text = self._next_run_text(item.get("id", ""))
            if text:
                item["next_run_at"] = text
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

    def api_meta_refresh(self, payload: dict = Body(default={})) -> dict:
        """给所有订阅补/刷新 TMDB 海报与总集数；force=True 时连已有的也重查。"""
        force = bool((payload or {}).get("force"))
        updated = 0
        failed: list[str] = []
        for item in self.store.subs():
            if not force and item.get("poster_path"):
                continue
            meta = resolve_meta(item.get("name") or "", item.get("kind") or "series",
                                item.get("season"))
            if meta:
                self.store.set_meta(str(item.get("id") or ""), meta)
                updated += 1
            else:
                failed.append(str(item.get("name") or ""))
        tail = (f"，{len(failed)} 条没认出来：{'、'.join(failed[:5])}" if failed else "")
        return {"ok": True, "updated": updated, "failed": failed,
                "message": f"已更新 {updated} 条海报" + tail}

    def api_sub_save(self, payload: dict = Body(default={})) -> dict:
        try:
            payload = payload or {}
            wanted_id = payload.get("tmdb_id")
            old = self.store.get_sub(str(payload.get("id") or ""))
            sub = self.store.upsert_sub(payload)
            if wanted_id:
                # 用户点选过候选：按 TMDB ID 取，避免同名再认错
                sub = self._fill_meta(sub, int(wanted_id))
            elif self._meta_stale(sub, old):
                # 新建或改了名字/季数时顺手查一次，卡片立刻就有海报和总集数
                sub = self._fill_meta(sub)
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
        sid = str((payload or {}).get("id") or "")
        sub = self.store.get_sub(sid)
        if not sub:
            return {"ok": False, "message": "订阅不存在"}
        self._run_sub(sid, force=True)
        logs = self.store.logs()
        return {"ok": True, "last": logs[0] if logs else {},
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
            for row in result.get("results", []):
                if row.get("ok"):
                    self.store.add_hit({
                        "sub_id": "", "sub": "手动下载", "dedup_key": row["key"],
                        "title": data.get("titles", {}).get(row["key"], row["key"]),
                        "downloader": result.get("downloader"), "status": "submitted",
                    })
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
        try:
            from app.schemas.message import Message as _Message

            # 只传 title、不传 text：与 MP 原生交互回复完全一致。
            # 关键：qqmsg 等渠道的 send 会按 `title + "\\n" + text` 拼正文，
            # 若同时传两者会把同一份内容重复渲染一遍（用户实测见站点报错重复）。
            self.chain.post_message(_Message(
                channel=channel, source=source, userid=user, username=username,
                title=text, save_history=False,
            ))
            return
        except Exception as exc:  # noqa: BLE001 - 消息模型不可用时退回通知链路
            logger.debug(f"[{self.plugin_name}] 直发回复不可用，退回通知链路：{type(exc).__name__}: {exc}")
        try:
            self.post_message(channel=channel, source=source, userid=user,
                              username=username, title=text, text=text)
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
        """新订阅的默认刷新计划（模型默认每天 03:00）。"""
        return "每天 03:00"

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

    def _run_sub_bg(self, sid: str) -> str:
        """立即执行放后台，先回执再搜，QQ 里不用干等一两分钟。"""
        if not sid:
            return ""

        def worker() -> None:
            try:
                self._run_sub(sid, force=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[{self.plugin_name}] 立即执行失败：{type(exc).__name__}: {exc}")

        try:
            import threading

            threading.Thread(target=worker, name=f"ptsub-now-{sid}", daemon=True).start()
        except Exception as exc:  # noqa: BLE001
            return f"立即执行没启动起来：{type(exc).__name__}: {exc}"
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
