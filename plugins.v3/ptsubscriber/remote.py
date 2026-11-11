"""智能助手 / QQ 等远程入口：把「订阅天下第一」穿透到插件，不经 MP 自带订阅。

两条链路共用同一套「先列同名候选、再回序号确认」的逻辑，都在插件实例方法
remote_start / remote_pick 里：

- 智能助手：get_agent_tools() 注册 MoviePilotTool，LLM 在自然语言对话里调用。
- QQ / 远程命令：get_command() 注册 /ptsub，PluginAction 事件把片名放在
  event_data["arg_str"]。

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
except Exception:  # pragma: no cover - 取决于宿主版本
    MoviePilotTool = object  # type: ignore

TOOL_NAME = "ptsub_subscribe"

PLUGIN_ID = "PtSubscriber"


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


class PtSubSubscribeTool(MoviePilotTool):
    """「订阅天下第一」→ 建本插件的定时补搜订阅，不是 MP 自带的订阅。"""

    name: str = TOOL_NAME
    description: str = (
        "订阅某部剧或电影，走「定时订阅下载」插件的规则（TMDB 认准作品 + 按站点规则下载、"
        "按已播集数补搜追更），不是 MoviePilot 自带的订阅，不要改用其他订阅工具。"
        "第一次调用只传 name 查询同名作品；若返回候选列表，把列表原样给用户看，"
        "等用户回复序号后再调用本工具并传 pick（如 2）。"
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
        **kwargs: Any,
    ) -> str:
        """查候选 / 按序号确认，最终落到插件自己的订阅表。"""
        plugin = _find_plugin()
        if plugin is None:
            return "「定时订阅下载」插件未运行，请先在 MoviePilot 插件页启用它再试。"

        key = f"agent|{getattr(self, '_session_id', '') or ''}"
        if pick is not None:
            try:
                index = int(pick)
            except (TypeError, ValueError):
                return "候选序号需要是数字，例如 1、2、3。"
            return plugin.remote_pick(index, key=key)

        title = str(name or "").strip()
        if not title:
            return "请告诉我片名，例如「订阅天下第一」；我会先列出同名作品让你选。"
        return plugin.remote_start(title, kind=kind or "series", key=key)


def build_agent_tools() -> List[Any]:
    """给插件的 get_agent_tools() 用；宿主不支持智能体时返回空列表。"""
    if MoviePilotTool is object:
        return []
    return [PtSubSubscribeTool]
