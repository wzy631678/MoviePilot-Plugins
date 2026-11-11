"""PtSubscriber —— MoviePilot v3 插件：定时订阅下载面板。

补 MP 缺的两点：① 自己选周几/几点去指定站点搜资源；② 订阅规则简单直观。
站点、Cookie、下载器全部直接复用 MP，插件不维护第二份凭据。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from fastapi import Body

from app.plugins import _PluginBase

try:
    from app.sdk.logging import logger
except ImportError:  # 兼容旧版宿主
    import logging

    logger = logging.getLogger("PtSubscriber")

from . import remote
from .core import Store, filter_candidates
from .meta import poster_url as meta_poster_url
from .meta import resolve as resolve_meta
from .meta import search as search_meta
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


class PtSubscriber(_PluginBase):
    plugin_name = "定时订阅下载"
    plugin_desc = "自己选周几几点，到指定站点搜资源并按详细规则下载到指定容器；自动对接 TMDB，搜到的资源会按片名、年份、剧集/电影、集数逐项比对，只把真正这部排在最前。支持智能助手和 QQ 等远程入口：说「订阅片名」或发 /ptsub 片名，直接穿透到插件建订阅，不走 MP 自带订阅。"
    plugin_version = "1.3.0"
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
            parts.append(f"可搜索索引 {len(self.bridge.searchable_domains())} 个")
        except Exception as exc:
            parts.append(f"索引读取失败 {type(exc).__name__}: {str(exc)[:80]}")
        logger.info(f"[{self.plugin_name}] 数据源自检：" + "；".join(parts))

    def stop_service(self) -> None:
        if self._scheduler:
            try:
                self._scheduler.shutdown(wait=False)
            except Exception:
                pass
            self._scheduler = None
        self._retry_state.clear()
        self._pending.clear()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_render_mode() -> Tuple[str, Optional[str]]:
        return "vue", "dist/assets"

    def get_form(self) -> Tuple[Optional[List[dict]], Dict[str, Any]]:
        return None, {"enabled": self._enabled, "max_results": self._max_results}

    def get_page(self) -> Optional[List[dict]]:
        return None

    def get_command(self) -> List[Dict[str, Any]]:
        """注册远程命令：QQ / 微信 / TG 里发 /ptsub 片名 即可建订阅（穿透到插件）。"""
        try:
            event_type = EventType.PluginAction
        except Exception:
            event_type = None
        return [{
            "cmd": "/ptsub",
            "event": event_type,
            "desc": "订阅（定时订阅下载插件）",
            "category": "订阅",
            "data": {"action": "ptsub_subscribe"},
        }]

    def get_agent_tools(self) -> List[Any]:
        """注册智能助手工具：说「订阅片名」即可建订阅，不经 MP 自带订阅。"""
        try:
            return [remote.PtSubSubscribeTool]
        except Exception:
            return []

    @_register(_PLUGIN_ACTION_EVENT)
    def command_action(self, event: Any) -> None:
        """QQ / TG / 微信等渠道的 /ptsub 命令：event_data["arg_str"] 是片名或序号。"""
        event_data = (event.event_data or {}) if event is not None else {}
        if event_data.get("action") != "ptsub_subscribe":
            return
        channel = event_data.get("channel")
        source = event_data.get("source")
        user = event_data.get("user")
        arg = str(event_data.get("arg_str") or "").strip()
        self._run_remote_command(arg, channel, source, user)

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
                entry.update(status="waiting", site=self._site_names(domains),
                             message=f"{entry['target']}还没更新；{wait}")
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

    def _site_names(self, domains: list[str]) -> str:
        """域名 → MP 里的站点中文名，日志和通知里看着更直观。"""
        if not domains:
            return ""
        try:
            mapping = {s.get("domain"): s.get("name") for s in self.bridge.list_sites()}
        except Exception:
            mapping = {}
        names = [str(mapping.get(d) or d) for d in domains]
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

    # ------------------------------------------------------- 远程入口（智能助手 / QQ 等）
    def _run_remote_command(self, arg: str, channel=None, source=None, user=None) -> None:
        """/ptsub 命令入口：arg 是纯数字就走「选候选」，否则当片名查候选。"""
        key = f"{channel}|{source}|{user}"
        if arg and arg.isdigit():
            text = self.remote_pick(int(arg), key=key)
        else:
            text = self.remote_start(arg, key=key)
        self._remote_post(channel, source, user, text)

    def _remote_post(self, channel=None, source=None, user=None, text: str = "") -> None:
        """把结果回发给触发入口（QQ / TG / 微信等）。通知失败不影响订阅本身。"""
        try:
            self.post_message(channel=channel, source=source, userid=user,
                              title="【定时订阅下载】", text=text)
        except Exception as exc:
            logger.warning(f"[{self.plugin_name}] 远程回复发送失败：{type(exc).__name__}: {exc}")

    def remote_start(self, name: str, kind: str = "series", key: str = "") -> str:
        """第一步：解析片名，列 TMDB 同名候选；唯一候选或没候选就直接建。"""
        name = (name or "").strip()
        if not name:
            return ("用法：智能助手说「订阅 片名」，或 QQ 等渠道发 /ptsub 片名。"
                    "没有收到片名。")
        self._pending_prune()
        items = search_meta(name, kind or "series") or []
        cands: list[dict] = []
        seen: set[int] = set()
        for it in items:
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
            })
        if not cands:
            sub = self._remote_create(name, "series", None)
            return (f"TMDB 没找到同名作品，已直接建订阅「{name}」（按剧集）。"
                    f"\n可到插件页点「同步海报」补年份/集数。\n{self._remote_confirmed(sub)}")
        if len(cands) == 1:
            c = cands[0]
            sub = self._remote_create(name, c["kind"], c["tmdb_id"])
            return self._remote_confirmed(sub)
        self._pending[key] = {"name": name, "kind": kind or "series",
                              "candidates": cands, "at": self._now_ts()}
        lines = [f"「{name}」在 TMDB 找到 {len(cands)} 个同名作品，回复序号确认："]
        for i, c in enumerate(cands, 1):
            tag = "动画" if c["is_anim"] else "真人"
            eps = f"共 {c['eps']} 集" if c["kind"] == "series" else "电影"
            lines.append(f"{i}. {c['title']}（{c['year'] or '年份未知'} · "
                         f"{self._kind_label(c['kind'])} · {tag} · {eps}）")
        lines.append("回复序号（如 2）确认；QQ 里发 /ptsub 2 也行。")
        return "\n".join(lines)

    def remote_pick(self, pick, key: str = "") -> str:
        """第二步：用户回了序号，按候选建订阅。"""
        self._pending_prune()
        pending = self._pending.get(key)
        if not pending:
            return "没有进行中的订阅选择，请重新说「订阅 片名」。"
        try:
            idx = int(pick)
        except (TypeError, ValueError):
            return "序号无效，请回复数字（如 2）。"
        cands = pending.get("candidates") or []
        if idx < 1 or idx > len(cands):
            return f"序号超出范围（1 ~ {len(cands)}），请重试。"
        c = cands[idx - 1]
        self._pending.pop(key, None)
        sub = self._remote_create(pending.get("name") or c["title"], c["kind"], c["tmdb_id"])
        return self._remote_confirmed(sub)

    def _remote_create(self, name: str, kind: str, tmdb_id: int | None) -> dict:
        """落库一条订阅并补 TMDB 元数据，重建调度计划。"""
        payload: dict[str, Any] = {
            "name": name,
            "kind": "movie" if kind == "movie" else "series",
            "site_ids": self._default_sites(),
            "enabled": True,
        }
        sub = self.store.upsert_sub(payload)
        if tmdb_id:
            sub = self._fill_meta(sub, int(tmdb_id)) or sub
        else:
            sub = self._fill_meta(sub) or sub
        self._rebuild_jobs()
        return sub

    def _remote_confirmed(self, sub: dict) -> str:
        """把一条订阅翻译成回执文本。"""
        name = sub.get("name") or ""
        kind = sub.get("kind") or "series"
        sites = sub.get("site_ids") or []
        bits: list[str] = []
        if sub.get("year"):
            bits.append(f"{sub['year']} 年")
        bits.append(self._kind_label(kind))
        if kind == "series" and sub.get("total_eps"):
            bits.append(f"共 {sub['total_eps']} 集")
        elif kind == "movie":
            bits.append("电影")
        desc = " · ".join(bits) or self._kind_label(kind)
        try:
            plan = schedule_label(sub)
        except Exception:
            plan = "按计划到勾选站点搜资源"
        return (f"已订阅：{name}（{desc}）\n"
                f"已勾选 {len(sites)} 个站点\n"
                f"计划：{plan}\n"
                f"站点/时间/集数可到插件页调整。")

    def _default_sites(self) -> list[str]:
        """远程建订阅的默认站点：所有已启用且可搜索的站点，之后可在插件页收窄。"""
        try:
            sites = self.bridge.list_sites()
            searchable = self.bridge.searchable_domains()
        except Exception:
            return []
        return [s["domain"] for s in sites
                if s.get("is_active") and (not searchable or str(s["domain"]).lower() in searchable)]

    @staticmethod
    def _kind_label(kind: str) -> str:
        return "剧集" if str(kind) == "series" else "电影"

    def _pending_prune(self) -> None:
        """清理超过 30 分钟没确认的候选选择。"""
        now = self._now_ts()
        for k in [k for k, v in self._pending.items() if now - (v.get("at") or 0) > 1800]:
            self._pending.pop(k, None)

    @staticmethod
    def _now_ts() -> float:
        import time as _time

        return _time.time()
