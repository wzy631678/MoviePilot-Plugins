"""智能助手 / QQ 等远程入口：把「订阅天下第一」穿透到插件，不经 MP 自带订阅。

两条链路共用同一套「先列同名候选、再回序号确认」的逻辑，都在插件实例方法
remote_start / remote_pick 里：

- 智能助手：get_agent_tools() 注册 MoviePilotTool，LLM 在自然语言对话里调用。
- QQ / 远程命令：get_command() 注册 /ptsub，PluginAction 事件把片名放在
  event_data["arg_str"]。

本模块只放「工具类」和「入参模型」，业务逻辑在 PtSubscriber 上，避免跨文件
搬 store / bridge / 调度器。
"""
from __future__ import annotations

from typing import Optional, Type

from pydantic import BaseModel, Field

try:  # 宿主没装智能体（旧版本）时降级为普通类，get_agent_tools 返回空即可
    from app.agent.tools.base import MoviePilotTool
except Exception:  # pragma: no cover - 取决于宿主版本
    MoviePilotTool = object  # type: ignore


class PtSubSubscribeInput(BaseModel):
    """工具入参：第一次传 name 查候选；拿到候选列表后第二次传 pick 确认。"""

    name: Optional[str] = Field(None, description="要订阅的片名，如「天下第一」")
    pick: Optional[int] = Field(None, description="候选序号（1 起）；收到候选列表后回传")
    kind: Optional[str] = Field(None, description="series 剧集 / movie 电影；默认剧集")


# 插件实例的模块级引用，由 PtSubscriber.init_plugin() 注入。
# 智能体自行实例化工具类，不持有插件引用，用这一条引用桥接最稳。
plugin_ref = None


class PtSubSubscribeTool(MoviePilotTool):
    """「订阅天下第一」→ 建本插件的定时补搜订阅，不是 MP 自带的订阅。"""

    name = "ptsub_subscribe"
    description = (
        "订阅某部剧/电影，使用「定时订阅下载」插件的规则（TMDB 认准 + 站点按规则下载、"
        "按集数追更），不是 MoviePilot 自带的订阅。第一次调用传入 name 查询同名候选；"
        "若返回候选列表，第二次调用传入 pick 序号（如 2）确认。"
    )
    args_schema: Type[BaseModel] = PtSubSubscribeInput

    def get_tool_message(self, **kwargs) -> Optional[str]:
        return "正在查询同名作品并创建订阅…"

    async def run(self, name=None, pick=None, kind=None, **kwargs) -> str:
        plugin = plugin_ref
        if plugin is None:
            return "插件未初始化，请先到插件页启用「定时订阅下载」后再试。"
        key = str(getattr(self, "_session_id", "") or "agent")
        if pick is not None:
            return plugin.remote_pick(int(pick), key=key)
        return plugin.remote_start(str(name or ""), kind=kind or "series", key=key)
