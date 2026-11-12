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
        self._search_timeout = 120.0

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
            # 只暴露「有没有配凭据」，绝不回传 cookie / apikey / token 内容
            has_auth = bool(
                str(getattr(site, "cookie", "") or "").strip()
                or str(getattr(site, "apikey", "") or "").strip()
                or str(getattr(site, "token", "") or "").strip()
            )
            out.append({
                # 兼容旧约定：id 沿用域名；MP 真实站点 id 放在 sid，供官方搜索链使用
                "id": domain,
                "sid": int(getattr(site, "id", 0) or 0),
                "domain": domain,
                "name": str(getattr(site, "name", "") or domain),
                "pri": int(getattr(site, "pri", 0) or 0),
                "is_active": bool(getattr(site, "is_active", True)),
                "has_auth": has_auth,
                "has_rss": bool(getattr(site, "rss", "")),
                "downloader": str(getattr(site, "downloader", "") or ""),
            })
        return out

    @staticmethod
    def _normalize_domain(value: object) -> str:
        """把索引器里的 domain 字段归一化为纯域名。

        这版 MP 的索引器字典里 `domain` 可能为空、只剩整数 `id`；直接
        `str(id)` 会得到 "12" 这类值，混进域名集合后会让所有站点都匹配不上，
        于是误报「认证没做好」。这里只保留看起来像域名的值。
        """
        text = str(value or "").strip().lower()
        if not text:
            return ""
        if "://" in text:
            text = text.split("://", 1)[1]
        text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
        text = text.split(":", 1)[0].lstrip(".")
        # 纯数字（索引器 id）不是域名，直接丢弃
        if "." not in text:
            return ""
        return text

    @staticmethod
    def _domain_variants(value: object) -> set[str]:
        """域名的等价写法：原样、去掉 www.、补上 www.（覆盖 kp./www. 这类前缀差）。"""
        base = MPBridge._normalize_domain(value)
        if not base:
            return set()
        out = {base}
        if base.startswith("www."):
            out.add(base[4:])
        else:
            out.add("www." + base)
        return out

    def _indexers(self) -> list[dict]:
        """读 MP 的索引器清单；同步接口读不到时用异步接口补一次。"""
        try:
            from app.sdk.network import SitesHelper

            helper = SitesHelper()
            items = [i for i in (helper.get_indexers() or []) if isinstance(i, dict)]
            if items:
                return items
            # 这版 MP 的索引资源是异步懒加载的，插件在同步上下文里常常读到空
            try:
                import asyncio

                async def _load() -> list:
                    return [i for i in (await helper.async_get_indexers() or []) if isinstance(i, dict)]

                def _run() -> list:
                    return asyncio.run(_load())

                result: list = []
                thread = threading.Thread(target=lambda: result.extend(_run()), daemon=True)
                thread.start()
                thread.join(timeout=8)
                return result
            except Exception:
                return []
        except Exception as exc:  # noqa: BLE001
            self._log("debug", f"读取站点索引配置失败（忽略）：{type(exc).__name__}")
            return []

    def searchable_domains(self) -> set[str]:
        """MP 索引器认识的站点域名（含 www. 等价写法）。

        返回空集合表示「读不到 / 无法判定」，调用方应当放过而不是拦住。
        """
        domains: set[str] = set()
        for item in self._indexers():
            domains |= self._domain_variants(item.get("domain"))
        return domains

    def resolve_domain(self, domain: str) -> str:
        """返回 MP 真正认得的站点域名，用于 browse。

        站点表里的 domain 与索引器里的 domain 可能差一个前缀（www. / kp.），
        直接拿站点表的写法去 browse 会被 MP 判成「站点不存在」。
        """
        target = self._normalize_domain(domain)
        if not target:
            return str(domain or "")
        try:
            from app.sdk.network import SitesHelper

            helper = SitesHelper()
            for cand in (target, "www." + target):
                try:
                    if helper.get_indexer(cand):
                        return cand
                except Exception:
                    pass
        except Exception:
            pass
        variants = self._domain_variants(target)
        for item in self._indexers():
            candidate = self._normalize_domain(item.get("domain"))
            if not candidate:
                continue
            if candidate in variants or candidate.endswith("." + target) or target.endswith("." + candidate):
                return candidate
        return target

    def has_indexer(self, domain: str) -> bool:
        """MP 是否认识这个站（用于「搜不到」时给出准确原因）。"""
        target = self.resolve_domain(domain)
        try:
            from app.sdk.network import SitesHelper

            if SitesHelper().get_indexer(target):
                return True
        except Exception:
            pass
        if not self._indexers():
            # 清单读不到 -> 无法判定，按「认」处理
            return True
        return target in self.searchable_domains()

    def can_search(self, domain: str) -> bool:
        """能否搜索。只做宽松判断：读不到索引器清单、或站点有等价域名时都放行。

        经验：把「索引器清单里没有」当成「搜不了」会大面积误伤（站点表与索引器的
        域名写法经常差一个 www./kp. 前缀），真正的失败原因交给真实搜索去暴露。
        """
        if not self._normalize_domain(domain):
            return False
        return True

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

    # ---------------------------------------------------------------- 异步桥
    def _run_async(self, coro_factory, timeout: float = 60.0):
        """同步上下文里跑 MP 的 async 链。coro_factory 返回协程。"""
        import asyncio

        result: list = []

        def _run() -> None:
            try:
                result.append(asyncio.run(coro_factory()))
            except Exception as exc:  # noqa: BLE001
                result.append(exc)

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        if not result:
            raise TimeoutError(f"MP 搜索超时（>{timeout}s）")
        value = result[0]
        if isinstance(value, Exception):
            raise value
        return value

    def auth_level(self) -> int:
        """MP 系统级用户认证级别；<2 表示未通过（站点功能被整体禁用）。

        这是 MP V3.14 的隐藏门槛：认证不过时索引器清单为空、所有站点都搜不到，
        但用户侧的站点认证（cookie/apikey）可能是好的——这正是「认证好了却搜不了」的根源。
        读取失败返回 -1（调用方按「无法判定」处理，不要据此拦截）。
        """
        try:
            from app.sdk.network import SitesHelper

            return int(SitesHelper().auth_level or 0)
        except Exception:
            return -1

    def resolve_site_ids(self, sites) -> list[int]:
        """把「站点域名 或 站点id」统一解析成 MP 真实站点 id（整数）。

        域名与 MP 站点地址的主机名可能差前缀（m-team.cc vs kp.m-team.cc），
        但站点 id 是权威的，官方搜索链按 id 走，能彻底避开前缀匹配问题。
        """
        result: list[int] = []
        seen: set[int] = set()
        by_domain = {str(s.get("domain") or "").lower(): s.get("sid")
                     for s in self.list_sites()}
        for item in sites or []:
            sid = None
            if isinstance(item, int):
                sid = item
            elif isinstance(item, str) and item.strip():
                key = item.strip().lower()
                sid = by_domain.get(key)
                if not sid:
                    # 站名也可能被直接传进来
                    for s in self.list_sites():
                        if str(s.get("name") or "").strip().lower() == key:
                            sid = s.get("sid")
                            break
            if sid and int(sid) > 0 and int(sid) not in seen:
                seen.add(int(sid))
                result.append(int(sid))
        return result

    # ---------------------------------------------------------------- 搜索
    def search(self, sites, keyword: str, limit: int = 50) -> dict:
        """按 MP 官方搜索链执行，返回富信息卡片 + 服务端 token。

        `sites` 可以是站点 id（int）或域名（str）的混合；插件只做交互层，
        真正的搜索与站点有效性判断全部交给 MP（SearchChain.search_by_title）。
        这样插件不会再出现「自己判成搜不了、MP 实际能搜」的错位。
        """
        from app.chain.search.facade import SearchChain

        site_ids = self.resolve_site_ids(sites)
        items: list[dict] = []
        raw_by_key: dict[str, Any] = {}
        errors: list[dict] = []
        if not site_ids:
            errors.append({"domain": ",".join(str(s) for s in (sites or [])),
                           "message": "没有匹配到 MP 站点"})
            return {"token": self.cache.put(raw_by_key), "items": items, "errors": errors}

        chain = SearchChain()
        contexts: list = []
        try:
            # 同步门面：与 MP 自己搜索页走同一套逻辑（按站点 id，最权威）
            contexts = chain.search_by_title(
                title=keyword or "", sites=site_ids, cache_local=True) or []
        except Exception as exc:  # noqa: BLE001 - 事件循环冲突时退回异步门面
            self._log("debug", f"同步搜索失败，改用异步门面：{type(exc).__name__}: {exc}")
            try:
                contexts = self._run_async(
                    lambda: chain.async_search_by_title(
                        title=keyword or "", sites=site_ids, cache_local=True),
                    timeout=self._search_timeout,
                ) or []
            except Exception as exc2:  # noqa: BLE001
                errors.append({"domain": ",".join(map(str, site_ids)),
                               "message": f"{type(exc2).__name__}: {exc2}"[:160]})
                contexts = []

        for ctx in contexts:
            info = getattr(ctx, "torrent_info", None)
            if info is None and isinstance(ctx, dict):
                info = ctx.get("torrent_info")
            if info is None:
                continue
            raw = _torrent_to_raw(info)
            site_name = str(getattr(info, "site_name", "") or "")
            # 站点名来自 MP 结果本身（权威）；domain 只用作去重 key
            card = enrich_item(site_name or "", site_name, raw)
            key = card["key"]
            if key in raw_by_key:
                continue
            raw_by_key[key] = info
            items.append(card)
            if len(items) >= limit:
                break
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
