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

**为什么把 handler 存在宿主类上而不是本模块的全局变量里**

MP 的插件加载器会清除 ``app.plugins.ptsubscriber.*`` 的模块缓存后重新导入
（日志里能看到「共清除 N 个模块缓存」）。若 handler 存在本模块的 ``_handler``，
重载后新模块的 ``_handler`` 与旧钩子闭包引用的旧 ``_handler`` 是两份，
旧钩子读到的永远是 None，于是「日志说已启用，实际每条消息都被放行」。
把 handler 挂到宿主类（``InteractionRouter`` / ``MediaInteractionChain``，
它们不在插件模块里、不会被清缓存）上，任何版本的钩子闭包都读到同一份最新 handler。

**为什么 uninstall 只清 handler 不还原钩子**

钩子在 handler 为空时直接 ``return original(...)``，等价于没装，不会改变宿主行为。
反过来若 uninstall 去还原钩子，一旦出现「新实例 install → 旧实例 uninstall」的
顺序，就会把刚装好的新钩子冲掉。只清 handler 既安全又不会有竞态。
"""
from __future__ import annotations

from typing import Any, Callable, Optional

try:  # 宿主日志；单测环境没有 app 包时降级
    from app.runtime.log import logger
except Exception:  # pragma: no cover - 取决于运行环境
    import logging

    logger = logging.getLogger(__name__)

# 宿主原始函数的暂存属性名（挂在类上，方便幂等判断）
_ORIG_ROUTER = "_ptsub_orig_dispatch_active_text"
_ORIG_MEDIA = "_ptsub_orig_handle_text_interaction"
# handler 存在宿主类上：模块重载不丢，钩子闭包始终读到最新一份
_HANDLER_ATTR = "_ptsub_handler"


def _handler_of(cls: Any) -> Optional[Callable[..., bool]]:
    return getattr(cls, _HANDLER_ATTR, None)


def _dispatch(cls: Any, channel: Any, source: Any, userid: Any,
              username: Any, text: Any) -> bool:
    """把文本交给插件判断；插件抛错时一律当作「不处理」，绝不阻塞宿主。"""
    handler = _handler_of(cls)
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
    patched = False

    # 第 3 步：活跃会话路由（裸数字「1」最先被这里吃掉）
    try:
        from app.application.messaging.router import InteractionRouter

        # handler 先挂到宿主类上：无论钩子是这次新打的还是上次残留的，都能读到
        setattr(InteractionRouter, _HANDLER_ATTR, handler)
        if not hasattr(InteractionRouter, _ORIG_ROUTER):
            original = InteractionRouter.dispatch_active_text

            def dispatch_active_text(self, context, text):
                if _dispatch(
                    InteractionRouter,
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

        setattr(MediaInteractionChain, _HANDLER_ATTR, handler)
        if not hasattr(MediaInteractionChain, _ORIG_MEDIA):
            original_text = MediaInteractionChain.handle_text_interaction

            def handle_text_interaction(self, channel, source, userid, username, text):
                if _dispatch(MediaInteractionChain, channel, source, userid, username, text):
                    return True
                return original_text(self, channel=channel, source=source,
                                     userid=userid, username=username, text=text)

            setattr(MediaInteractionChain, _ORIG_MEDIA, original_text)
            MediaInteractionChain.handle_text_interaction = handle_text_interaction
        patched = True
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[PtSubscriber] 原生媒体交互接管失败：{type(exc).__name__}: {exc}")

    r_patched = getattr(_cls_safe("app.application.messaging.router", "InteractionRouter"),
                        _ORIG_ROUTER, None) is not None
    m_patched = getattr(_cls_safe("app.chain.interaction", "MediaInteractionChain"),
                        _ORIG_MEDIA, None) is not None
    logger.info(f"[PtSubscriber] 接管钩子安装：router={r_patched} media={m_patched} 处理函数={handler}")
    return patched


def _cls_safe(module: str, name: str) -> Any:
    """尽力拿到宿主类，拿不到就返回一个空壳对象（只给日志用）。"""
    try:
        import importlib

        return getattr(importlib.import_module(module), name, object())
    except Exception:  # noqa: BLE001
        return object()


def uninstall() -> None:
    """停用接管：把宿主类上的 handler 置空，钩子保留（空 handler 时透传）。

    只清 handler、不还原钩子，既等价于「没装」，又能在插件重载的
    「新 install → 旧 uninstall」竞态下不冲掉新钩子。
    """
    for module, name in (
        ("app.application.messaging.router", "InteractionRouter"),
        ("app.chain.interaction", "MediaInteractionChain"),
    ):
        try:
            cls = _cls_safe(module, name)
            if getattr(cls, _HANDLER_ATTR, "missing") != "missing":
                setattr(cls, _HANDLER_ATTR, None)
        except Exception:  # noqa: BLE001
            pass
    logger.info("[PtSubscriber] 文本接管已停用（handler 置空，钩子透传）")


def is_installed() -> bool:
    """当前进程里接管是否处于「启用」状态（排障 / 单测用）。"""
    try:
        router = _cls_safe("app.application.messaging.router", "InteractionRouter")
        media = _cls_safe("app.chain.interaction", "MediaInteractionChain")
        return (_handler_of(router) is not None) or (_handler_of(media) is not None)
    except Exception:  # noqa: BLE001
        return False
