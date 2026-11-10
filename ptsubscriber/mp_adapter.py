"""MoviePilot 宿主适配层：站点、搜索、下载器、种子提交。

只在 MP 运行时导入宿主模块；本机单测不加载本文件。
所有站点凭据由 MP 管理，插件不读取、不保存、不回传 Cookie / Passkey。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from .core import enrich_item


class SearchCache:
    """服务端保存原始 TorrentInfo；前端只拿 key。"""

    def __init__(self, ttl: int = 900) -> None:
        self.ttl = ttl
        self._data: dict[str, dict[str, Any]] = {}
        self._ts: dict[str, float] = {}
        self._lock = threading.RLock()
        self._seq = 0

    def put(self, items: dict[str, Any]) -> str:
        with self._lock:
            self._purge()
            self._seq += 1
            token = f"s{int(time.time())}{self._seq}"
            self._data[token] = items
            self._ts[token] = time.time()
            return token

    def get(self, token: str, key: str) -> Any | None:
        with self._lock:
            self._purge()
            return self._data.get(token, {}).get(key)

    def _purge(self) -> None:
        now = time.time()
        for token in [t for t, ts in self._ts.items() if now - ts > self.ttl]:
            self._data.pop(token, None)
            self._ts.pop(token, None)


class MPBridge:
    def __init__(self, logger: Any = None) -> None:
        self.logger = logger
        self.cache = SearchCache()

    def _log(self, level: str, text: str) -> None:
        log = self.logger
        if log and hasattr(log, level):
            try:
                getattr(log, level)(text)
            except Exception:
                pass

    # ---------------------------------------------------------------- 站点
    def list_sites(self) -> list[dict]:
        """站点清单直接来自 MP 的 site 表，无需再手动添加/同步。"""
        from app.db.oper.site import SiteOper

        rows = SiteOper().list_order_by_pri()
        out: list[dict] = []
        for site in rows or []:
            domain = str(getattr(site, "domain", "") or "").strip()
            if not domain:
                continue
            out.append({
                "id": domain,
                "domain": domain,
                "name": str(getattr(site, "name", "") or domain),
                "pri": int(getattr(site, "pri", 0) or 0),
                "is_active": bool(getattr(site, "is_active", True)),
                "has_rss": bool(getattr(site, "rss", "")),
                "downloader": str(getattr(site, "downloader", "") or ""),
            })
        return out

    def searchable_domains(self) -> set[str]:
        try:
            from app.sdk.network import SitesHelper

            domains: set[str] = set()
            for item in SitesHelper().get_indexers() or []:
                value = item.get("domain") or item.get("id") or ""
                if value:
                    domains.add(str(value).strip().lower())
            return domains
        except Exception as exc:
            self._log("debug", f"读取站点索引配置失败（忽略）：{type(exc).__name__}")
            return set()

    # ---------------------------------------------------------------- 下载器
    def list_downloaders(self) -> list[dict]:
        from app.sdk.services import DownloaderHelper

        out: list[dict] = []
        for name, conf in (DownloaderHelper().get_configs() or {}).items():
            try:
                cfg = conf.config if hasattr(conf, "config") else (conf.get("config") or {})
                out.append({
                    "id": name,
                    "name": name,
                    "type": str(getattr(conf, "type", "") or ""),
                    "default": bool(getattr(conf, "default", False)),
                    "save_path": str((cfg or {}).get("save_path") or ""),
                    "category": str((cfg or {}).get("category") or ""),
                })
            except Exception:
                continue
        return out

    def test_downloader(self, name: str) -> dict:
        from app.sdk.services import DownloaderHelper

        service = DownloaderHelper().get_service(name=name)
        if not service or not getattr(service, "instance", None):
            return {"ok": False, "message": "下载器实例不存在或未启用"}
        inst = service.instance
        try:
            inactive = inst.is_inactive() if hasattr(inst, "is_inactive") else False
        except Exception:
            inactive = False
        version = ""
        if hasattr(inst, "get_version"):
            try:
                version = str(inst.get_version() or "")
            except Exception:
                pass
        return {"ok": not inactive, "message": "连接正常" if not inactive else "下载器未连接",
                "version": version}

    # ---------------------------------------------------------------- 搜索
    def search(self, domains: list[str], keyword: str, limit: int = 50) -> dict:
        """按站点域名调用 MP 的 TorrentsChain.browse，返回富信息卡片 + 服务端 token。"""
        from app.chain.torrents import TorrentsChain

        chain = TorrentsChain()
        items: list[dict] = []
        raw_by_key: dict[str, Any] = {}
        errors: list[dict] = []
        # browse 依赖 SitesHelper().get_indexer(domain)。MP 未完成「用户认证」时索引器为空，
        # browse 会静默返回 []，容易被误判为「没有资源」，因此先做一次显式诊断。
        if not self.searchable_domains():
            errors.append({
                "domain": "*",
                "message": "MoviePilot 当前没有可用的站点索引器（日志提示「用户未认证」）。"
                           "请先在 MP 里完成用户认证并确认站点已启用，否则搜索会返回 0 条。",
            })
        for domain in domains:
            try:
                infos = chain.browse(domain=domain, keyword=keyword or None) or []
            except Exception as exc:
                errors.append({"domain": domain, "message": f"{type(exc).__name__}: {exc}"[:160]})
                continue
            for info in infos[:limit]:
                raw = _torrent_to_raw(info)
                # 用 MP 给的站点中文名（如「馒头」），没有才退回域名
                card = enrich_item(domain, str(getattr(info, "site_name", "") or domain), raw)
                key = card["key"]
                if key in raw_by_key:
                    continue
                raw_by_key[key] = info
                items.append(card)
        token = self.cache.put(raw_by_key)
        return {"token": token, "items": items, "errors": errors}

    # ---------------------------------------------------------------- 提交
    @staticmethod
    def _system_tag() -> str:
        """MP 的系统种子标签（默认 MOVIEPILOT）。

        MP 的「下载」页面只列出带这个标签的种子（list_torrents 的 include_all_tags
        默认 False 会按 TORRENT_TAG 过滤）。插件直连下载器时必须自己补上，
        否则 qB 里明明在下载，MP 页面却看不到。
        """
        try:
            from app.runtime.settings import get_runtime_setting

            value = get_runtime_setting("TORRENT_TAG")
            if value:
                return str(value)
        except Exception:
            pass
        try:
            from app.core.config import settings

            return str(getattr(settings, "TORRENT_TAG", "") or "")
        except Exception:
            return ""

    @staticmethod
    def _build_kwargs(inst: Any, *, content: Any, download_dir: str,
                      tag: str, category: str, cookie: str | None,
                      is_qb: bool) -> dict[str, Any]:
        """按下载器 add_torrent 的真实签名拼参数。

        qB=tag/category、Transmission=labels、rTorrent=tags，用签名探测避免 TypeError。
        """
        import inspect

        try:
            params = inspect.signature(inst.add_torrent).parameters
        except (TypeError, ValueError):
            params = {}
        has_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values())

        kwargs: dict[str, Any] = {"content": content}
        for name in ("download_dir", "save_path", "path"):
            if name in params:
                kwargs[name] = download_dir or None
                break
        if cookie and "cookie" in params:
            kwargs["cookie"] = cookie

        if tag:
            for name in ("tag", "tags", "labels"):
                if name in params:
                    kwargs[name] = tag if name == "tag" else [tag]
                    break
            else:
                if has_kwargs and is_qb:
                    kwargs["tag"] = tag

        if category:
            if "category" in params:
                kwargs["category"] = category
            elif "tags" in params:
                kwargs["tags"] = [category]
            elif has_kwargs and is_qb:
                kwargs["category"] = category
        return kwargs

    def submit(self, token: str, keys: list[str], downloader: str,
               category: str, save_path: str) -> dict:
        from app.chain.download import DownloadChain
        from app.sdk.services import DownloaderHelper

        helper = DownloaderHelper()
        configs = helper.get_configs() or {}
        if not downloader:
            downloader = next((n for n, c in configs.items()
                               if bool(getattr(c, "default", False))), "")
        if not downloader and configs:
            downloader = next(iter(configs))
        service = helper.get_service(name=downloader)
        if not service or not getattr(service, "instance", None):
            return {"ok": False, "message": f"下载器「{downloader}」不可用，请检查 MP 下载器配置"}
        inst = service.instance
        is_qb = str(getattr(service, "type", "") or "").lower() == "qbittorrent"
        system_tag = self._system_tag()

        results: list[dict] = []
        for key in keys:
            info = self.cache.get(token, key)
            if info is None:
                results.append({"key": key, "ok": False, "message": "结果已过期，请重新搜索"})
                continue
            try:
                content, _, _ = DownloadChain().download_torrent(info)
                if not content:
                    results.append({"key": key, "ok": False, "message": "种子内容下载失败"})
                    continue
                kwargs = self._build_kwargs(
                    inst, content=content, download_dir=save_path,
                    tag=system_tag, category=category,
                    cookie=getattr(info, "site_cookie", None), is_qb=is_qb,
                )
                result = inst.add_torrent(**kwargs)
                if isinstance(result, tuple):
                    ok = bool(result[0])
                    hashes = result[1] if len(result) > 1 else []
                else:
                    ok, hashes = bool(result), []
                results.append({
                    "key": key, "ok": ok,
                    "hash": (hashes[0] if isinstance(hashes, (list, tuple)) and hashes else ""),
                    "message": "已提交下载器" if ok else "下载器拒绝添加",
                })
            except Exception as exc:
                results.append({"key": key, "ok": False,
                                "message": f"{type(exc).__name__}: {str(exc)[:120]}"})
        return {"ok": any(r["ok"] for r in results), "downloader": downloader,
                "tag": system_tag, "results": results}


def _torrent_to_raw(info: Any) -> dict:
    """TorrentInfo → 非敏感字段字典。enclosure/ Cookie 永不进入前端。"""
    def attr(name: str, default: Any = None) -> Any:
        return getattr(info, name, default)

    labels = attr("labels") or []
    if isinstance(labels, str):
        labels = [labels]
    size = attr("size") or 0
    try:
        size_gb = round(float(size) / 1024 ** 3, 3)
    except (TypeError, ValueError):
        size_gb = None
    return {
        "title": attr("title") or "",
        "description": attr("description") or "",
        "page_url": attr("page_url") or "",
        "size_gb": size_gb,
        "seeders": attr("seeders"),
        "peers": attr("peers"),
        "grabs": attr("grabs"),
        "pubdate": attr("pubdate") or "",
        "category": attr("category") or "",
        "volume_factor": attr("volume_factor") or "",
        "labels": [str(x) for x in labels],
        "downloadvolumefactor": attr("downloadvolumefactor"),
        "hit_and_run": attr("hit_and_run"),
    }
