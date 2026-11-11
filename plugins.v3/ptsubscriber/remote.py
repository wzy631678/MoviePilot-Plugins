"""智能助手 / QQ 等远程入口：把「订阅天下第一」穿透到插件，不经 MP 自带订阅。

两条链路共用同一套「先列候选、再回序号确认」的逻辑，都在插件实例方法
remote_sub / remote_pick / remote_find / remote_dl_pick 里：

- 智能助手：get_agent_tools() 注册 MoviePilotTool，LLM 在自然语言对话里调用。
- QQ / 远程命令：get_command() 注册 /ptsub、/ptfind、/ptlist，PluginAction 事件
  把后面的自由文本放进 event_data["arg_str"]。

本模块只放「工具类」和「入参模型」，业务逻辑在 PtSubscriber 上，避免跨文件
搬 store / bridge / 调度器。

注意：MoviePilotTool 是 langchain 的 pydantic 模型，子类的 name / description /
args_schema 必须写成**带类型注解的字段**，否则 pydantic v2 会在建类时报
"Field 'name' defined on a base class was overridden by a non-annotated attribute"，
整个插件都会加载失败。
"""
from __future__ import annotations

from typing import Any, List, Optional, Type

from pydantic import BaseModel, Field

try:  # 宿主没装智能体（旧版本）时降级为普通类，get_agent_tools 返回空即可
    from app.agent.tools.base import MoviePilotTool
    from app.agent.tools.tags import ToolTag
except Exception:  # pragma: no cover - 取决于宿主版本
    MoviePilotTool = object  # type: ignore
    ToolTag = None  # type: ignore

TOOL_NAME = "ptsub_subscribe"
FIND_TOOL_NAME = "ptsub_find"

PLUGIN_ID = "PtSubscriber"


def _tool_tags(*names: str) -> list[str]:
    """把 ToolTag 成员名转成标签值；宿主无智能体标签时返回空列表。

    智能助手的工具选择器按能力标签（ToolTag）分组：用户说「订阅/追剧」会选
    Subscription 组、说「资源/种子」会选 Resource 组。插件工具必须声明这些
    标签，否则会被选择器过滤掉，用户的「订阅 X」「找资源 X」就会落到 MP 原生
    订阅工具上（这也是订阅跑进 MP 订阅页、回复带海报图的原因）。
    """
    if ToolTag is None:
        return []
    return [getattr(ToolTag, n).value for n in names if hasattr(ToolTag, n)]


class PtSubSubscribeInput(BaseModel):
    """工具入参：第一次传 name 查候选；拿到候选列表后第二次传 pick 确认。"""

    name: Optional[str] = Field(
        default=None,
        description="要订阅的片名，如「天下第一」；首次调用必须提供。",
    )
    pick: Optional[int] = Field(
        default=None,
        description="候选序号（从 1 开始）。仅在已返回候选列表、用户选定后回传。",
    )
    kind: Optional[str] = Field(
        default=None,
        description="series 表示剧集、movie 表示电影；不传按剧集处理。",
    )
    ep_start: Optional[int] = Field(
        default=None,
        description="从第几集开始补（用户说「从第5集开始」时传 5）；不传表示从第 1 集起。",
    )
    ep_end: Optional[int] = Field(
        default=None,
        description="补到第几集（可空）。只在用户明确给了区间时传。",
    )
    sites: Optional[str] = Field(
        default=None,
        description="限定站点的名字或域名，多个用逗号分隔，如「馒头,岛」；不传用全部可搜索站点。",
    )
    time: Optional[str] = Field(
        default=None,
        description="每天几点去搜，HH:MM，如 03:00；用户说「每天凌晨3点」就传 03:00。",
    )
    interval: Optional[int] = Field(
        default=None,
        description="每隔多少分钟搜一次（用户说「每半小时」传 30）；与 time 二选一。",
    )
    run_now: Optional[bool] = Field(
        default=None,
        description="用户要求「现在就搜/立即下载」时传 true，建完订阅立刻跑一轮。",
    )


class PtSubFindInput(BaseModel):
    """「不订阅，直接找资源下载」的入参。"""

    name: Optional[str] = Field(
        default=None,
        description="要下载的片名，如「天下第一」；首次调用必须提供。",
    )
    pick: Optional[int] = Field(
        default=None,
        description="资源序号（从 1 开始）。仅在已返回资源列表后回传；"
                    "多个用逗号，如「1,3」用 pick_range。",
    )
    pick_range: Optional[str] = Field(
        default=None,
        description="要下载的序号区间，如「1-3」或「1,3」；用户要下多条时传这个。",
    )
    episode: Optional[int] = Field(
        default=None,
        description="只要某一集时传集数，如 5；不传按全部。",
    )
    sites: Optional[str] = Field(
        default=None,
        description="限定站点名字或域名，多个用逗号分隔；不传搜全部可搜索站点。",
    )


# 插件实例的模块级引用，由 PtSubscriber.init_plugin() 注入，作为兜底寻址。
plugin_ref: Any = None


def _find_plugin() -> Any:
    """优先从宿主插件管理器取运行中的实例，失败再退回模块级引用。"""
    try:
        from app.sdk.plugin import PluginManager

        running = getattr(PluginManager(), "running_plugins", None) or {}
        for key, instance in dict(running).items():
            if key == PLUGIN_ID and hasattr(instance, "remote_start"):
                return instance
        for instance in dict(running).values():
            if (
                instance.__class__.__name__ == PLUGIN_ID
                and hasattr(instance, "remote_start")
            ):
                return instance
    except Exception:  # noqa: BLE001 - 宿主无该模块时走兜底
        pass
    return plugin_ref


def _opts(
    *,
    ep_start: Optional[int] = None,
    ep_end: Optional[int] = None,
    sites: Optional[str] = None,
    time: Optional[str] = None,
    interval: Optional[int] = None,
    run_now: Optional[bool] = None,
) -> dict:
    """工具入参 → 插件用的 opts 字典（只放有值的，避免覆盖默认值）。"""
    site_words = [x.strip() for x in str(sites or "").replace("，", ",").split(",") if x.strip()]
    opts: dict = {"sites": site_words}
    if ep_start:
        opts["ep_start"] = int(ep_start)
    if ep_end:
        opts["ep_end"] = int(ep_end)
    if time:
        opts["schedule_time"] = str(time).strip()
        opts["schedule_mode"] = "daily"
    if interval:
        opts["schedule_interval"] = int(interval)
        opts["schedule_mode"] = "interval"
    if run_now:
        opts["run_now"] = True
    return opts


class PtSubSubscribeTool(MoviePilotTool):
    """「订阅天下第一」→ 建本插件的定时补搜订阅，不是 MP 自带的订阅。"""

    name: str = TOOL_NAME
    description: str = (
        "【插件专用】这是处理「订阅某部剧/电影」的唯一入口：用户说「订阅X」「追X」"
        "「订一下X」「X更新了帮我订」时，必须用本工具，不要改用 MoviePilot 通用订阅工具"
        "或其它订阅插件。走「定时订阅下载」插件的规则（TMDB 认准作品 + 按站点规则下载、"
        "按已播集数补搜追更），与 MoviePilot 自带订阅完全无关。"
        "第一次调用只传 name 查询同名作品；若返回候选列表，把列表原样给用户看，"
        "等用户回复序号后再调用本工具并传 pick（如 2）。"
        "用户提到「从第几集开始」「只搜某几个站」「每天几点」「现在就搜」时，"
        "分别传 ep_start / sites / time / run_now。"
    )
    tags: list[str] = Field(
        default_factory=lambda: _tool_tags(
            "Subscription", "Media", "Download", "Site", "Scheduler", "Plugin")
    )
    args_schema: Type[BaseModel] = PtSubSubscribeInput

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """生成工具执行期间的提示文案。"""
        pick = kwargs.get("pick")
        if pick is not None:
            return f"正在按第 {pick} 个候选创建订阅…"
        name = str(kwargs.get("name") or "").strip()
        return f"正在查询「{name}」的同名作品…" if name else "正在查询同名作品…"

    async def run(  # type: ignore[override]
        self,
        name: Optional[str] = None,
        pick: Optional[int] = None,
        kind: Optional[str] = None,
        ep_start: Optional[int] = None,
        ep_end: Optional[int] = None,
        sites: Optional[str] = None,
        time: Optional[str] = None,
        interval: Optional[int] = None,
        run_now: Optional[bool] = None,
        **kwargs: Any,
    ) -> str:
        """查候选 / 按序号确认，最终落到插件自己的订阅表。"""
        plugin = _find_plugin()
        if plugin is None:
            return "「定时订阅下载」插件未运行，请先在 MoviePilot 插件页启用它再试。"

        key = f"agent|{getattr(self, '_session_id', '') or ''}"
        opts = _opts(ep_start=ep_start, ep_end=ep_end, sites=sites,
                     time=time, interval=interval, run_now=run_now)
        if pick is not None:
            try:
                index = int(pick)
            except (TypeError, ValueError):
                return "候选序号需要是数字，例如 1、2、3。"
            return plugin.remote_pick(index, key=key, opts=opts)

        title = str(name or "").strip()
        if not title:
            return "请告诉我片名，例如「订阅天下第一」；我会先列出同名作品让你选。"
        return plugin.remote_start(title, kind=kind or "series", key=key, opts=opts)


class PtSubFindTool(MoviePilotTool):
    """「不订阅，直接帮我找资源下载」→ 搜一轮、列资源、按序号下发下载器。"""

    name: str = FIND_TOOL_NAME
    description: str = (
        "【插件专用】这是处理「不订阅、直接找资源下载」的唯一入口：用户说「帮我找X的资源」"
        "「找一下X」「不用订阅直接下X」「搜X的种子」时，必须用本工具，不要用 MoviePilot "
        "通用搜索/下载工具。只找资源并下载一次，不创建订阅、不追更。"
        "第一次调用传 name 搜索，把返回的资源列表原样给用户看；"
        "用户回序号后再调用一次，传 pick 或 pick_range（如「1-3」）提交下载。"
        "如果用户其实想要持续追更，改用 ptsub_subscribe 那个订阅工具。"
    )
    tags: list[str] = Field(
        default_factory=lambda: _tool_tags(
            "Resource", "Download", "Site", "Media", "Plugin")
    )
    args_schema: Type[BaseModel] = PtSubFindInput

    def get_tool_message(self, **kwargs: Any) -> Optional[str]:
        """生成工具执行期间的提示文案。"""
        if kwargs.get("pick") or kwargs.get("pick_range"):
            return "正在把选中的资源提交到下载器…"
        name = str(kwargs.get("name") or "").strip()
        return f"正在搜索「{name}」的资源…" if name else "正在搜索资源…"

    async def run(  # type: ignore[override]
        self,
        name: Optional[str] = None,
        pick: Optional[int] = None,
        pick_range: Optional[str] = None,
        episode: Optional[int] = None,
        sites: Optional[str] = None,
        **kwargs: Any,
    ) -> str:
        """搜资源 / 按序号提交下载。"""
        plugin = _find_plugin()
        if plugin is None:
            return "「定时订阅下载」插件未运行，请先在 MoviePilot 插件页启用它再试。"

        key = f"agent|{getattr(self, '_session_id', '') or ''}"
        addr = str(pick_range or "").strip()
        if not addr and pick is not None:
            addr = str(pick).strip()
        if addr:
            return plugin.remote_dl_pick(addr, key=key)

        title = str(name or "").strip()
        if not title:
            return "请告诉我片名，例如「帮我找一下天下第一的资源」。"
        intent = _opts(sites=sites, ep_start=episode)
        return plugin.remote_dl_start(title, intent, key=key)


def build_agent_tools() -> List[Any]:
    """给插件的 get_agent_tools() 用；宿主不支持智能体时返回空列表。"""
    if MoviePilotTool is object:
        return []
    return [PtSubSubscribeTool, PtSubFindTool]
