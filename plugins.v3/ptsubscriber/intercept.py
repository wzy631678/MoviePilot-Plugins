"""让本插件接管聊天渠道里的「普通文本」（不带斜杠的中文）。

背景：宿主 MoviePilot 处理一条聊天消息的顺序（见 ``app/chain/message.py``）是

    1. ``/命令``            → EventType.CommandExcute（插件的 /ptsub 走这里）
    2. ``/ai`` 前缀         → 智能体
    3. 活跃会话路由          → 例如 MP 原生「搜片 → 回 1」的会话
    4. AI 智能体全局接管      → 默认关闭
    5. ``MediaInteractionChain.handle_text_interaction`` → 「订阅 XXX」原生订阅
    6. UserMessage 广播

所以用户在 QQ 里直接发「订阅 天才女友」「1」时，消息在第 3 / 5 步就被 MP 原生订阅吃掉，
插件的 ``/ptsub`` 根本没机会执行——这就是「回个 1 却跳到 MP 订阅页」的原因。

这里在第 3、5 两步各加一层前置钩子：插件能处理就处理并返回 True，
处理不了就原样交回宿主，未安装插件时的行为完全不变。

补丁是幂等的，并且 ``uninstall`` 会把宿主函数原样还原，插件停用后不残留。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

try:  # 宿主日志；单测环境没有 app 包时降级
    from app.runtime.log import logger
except Exception:  # pragma: no cover - 取决于运行环境
    import logging

    logger = logging.getLogger(__name__)

# 宿主原始函数的暂存属性名（挂在类上，方便幂等判断与还原）
_ORIG_ROUTER = "_ptsub_orig_dispatch_active_text"
_ORIG_MEDIA = "_ptsub_orig_handle_text_interaction"

_handler: Optional[Callable[..., bool]] = None


def _dispatch(channel: Any, source: Any, userid: Any, username: Any, text: Any) -> bool:
    """把文本交给插件判断；插件抛错时一律当作「不处理」，绝不阻塞宿主。"""
    handler = _handler
    if handler is None:
        return False
    try:
        return bool(handler(channel=channel, source=source, userid=userid,
                            username=username, text=text))
    except Exception as exc:  # noqa: BLE001 - 接管层不能把聊天通道搞挂
        logger.warning(f"[PtSubscriber] 文本接管失败：{type(exc).__name__}: {exc}")
        return False


def install(handler: Callable[..., bool]) -> bool:
    """安装两层前置钩子，返回是否至少装上了一层。"""
    global _handler
    _handler = handler
    patched = False

    # 第 3 步：活跃会话路由（裸数字「1」最先被这里吃掉）
    try:
        from app.application.messaging.router import InteractionRouter

        if not hasattr(InteractionRouter, _ORIG_ROUTER):
            original = InteractionRouter.dispatch_active_text

            def dispatch_active_text(self, context, text):
                if _dispatch(
                    getattr(context, "channel", None),
                    getattr(context, "source", ""),
                    getattr(context, "user_id", None),
                    getattr(context, "username", ""),
                    text,
                ):
                    return True
                return original(self, context, text)

            setattr(InteractionRouter, _ORIG_ROUTER, original)
            InteractionRouter.dispatch_active_text = dispatch_active_text
        patched = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[PtSubscriber] 会话路由接管失败：{type(exc).__name__}: {exc}")

    # 第 5 步：原生媒体交互（「订阅 XXX」在这里变成 MP 原生订阅）
    try:
        from app.chain.interaction import MediaInteractionChain

        if not hasattr(MediaInteractionChain, _ORIG_MEDIA):
            original_text = MediaInteractionChain.handle_text_interaction

            def handle_text_interaction(self, channel, source, userid, username, text):
                if _dispatch(channel, source, userid, username, text):
                    return True
                return original_text(self, channel=channel, source=source,
                                     userid=userid, username=username, text=text)

            setattr(MediaInteractionChain, _ORIG_MEDIA, original_text)
            MediaInteractionChain.handle_text_interaction = handle_text_interaction
        patched = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[PtSubscriber] 原生媒体交互接管失败：{type(exc).__name__}: {exc}")

    return patched


def uninstall() -> None:
    """还原宿主函数（插件停用 / 卸载时调用）。"""
    global _handler
    _handler = None
    try:
        from app.application.messaging.router import InteractionRouter

        original = getattr(InteractionRouter, _ORIG_ROUTER, None)
        if original is not None:
            InteractionRouter.dispatch_active_text = original
            delattr(InteractionRouter, _ORIG_ROUTER)
    except Exception:  # noqa: BLE001
        pass
    try:
        from app.chain.interaction import MediaInteractionChain

        original = getattr(MediaInteractionChain, _ORIG_MEDIA, None)
        if original is not None:
            MediaInteractionChain.handle_text_interaction = original
            delattr(MediaInteractionChain, _ORIG_MEDIA)
    except Exception:  # noqa: BLE001
        pass


def is_installed() -> bool:
    """当前进程里钩子是否已装（排障 / 单测用）。"""
    try:
        from app.chain.interaction import MediaInteractionChain
        from app.application.messaging.router import InteractionRouter

        return hasattr(MediaInteractionChain, _ORIG_MEDIA) and hasattr(InteractionRouter, _ORIG_ROUTER)
    except Exception:  # noqa: BLE001
        return False
