"""订阅核心：存储、校验、过滤、计划。与 MoviePilot 解耦，便于本机单测。"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import ValidationError

from .models import (
    SubscriptionIn,
    episode_progress,
    match_title,
    next_episode,
    schedule_label,
    search_keyword,
)
from .rules import RuleSet, extract_attributes, match_rules, parse_rules


class MemoryBackend:
    """单测用存储；MP 运行时由插件注入 save_data/get_data 后端。"""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    def load(self, key: str, default: Any = None) -> Any:
        value = self.data.get(key)
        return json.loads(json.dumps(value)) if value is not None else default

    def save(self, key: str, value: Any) -> None:
        self.data[key] = json.loads(json.dumps(value))


class Store:
    """订阅 / 命中 / 日志的薄封装。"""

    def __init__(self, backend: Any, log_limit: int = 200, hit_limit: int = 500) -> None:
        self.backend = backend
        self.log_limit = log_limit
        self.hit_limit = hit_limit

    def subs(self) -> list[dict]:
        return list(self.backend.load("subs", []) or [])

    def get_sub(self, sid: str) -> dict | None:
        return next((s for s in self.subs() if s.get("id") == sid), None)

    def save_subs(self, items: list[dict]) -> None:
        self.backend.save("subs", items)

    def upsert_sub(self, payload: dict) -> dict:
        """新建或更新一条订阅，返回落库后的字典。"""
        items = self.subs()
        sid = str(payload.get("id") or "").strip()
        old = next((s for s in items if s.get("id") == sid), None) if sid else None
        body = dict(payload or {})
        # 前端不传台账时沿用旧记录，避免编辑一次订阅就把「已下过的集」清空重下
        if old and "delivered_eps" not in payload:
            body["delivered_eps"] = old.get("delivered_eps") or []
        try:
            model = SubscriptionIn(**body)
        except ValidationError as exc:
            first = exc.errors()[0]
            raise ValueError(f"{first.get('loc', [''])[-1]}: {first.get('msg')}") from exc
        data = model.model_dump(mode="json")
        data["rules"] = model.rules.model_dump(mode="json")
        now = _now()
        if old is not None:
            data["id"] = sid
            data["created_at"] = old.get("created_at") or now
            data["updated_at"] = now
            data["next_run_at"] = old.get("next_run_at") or ""
            items[items.index(old)] = data
            self.save_subs(items)
            return data
        data["id"] = uuid.uuid4().hex[:12]
        data["created_at"] = now
        data["updated_at"] = now
        data["next_run_at"] = ""
        items.append(data)
        self.save_subs(items)
        return data

    def mark_delivered(self, sid: str, episodes: list[int]) -> dict | None:
        """把成功提交的集数记进台账，下次补搜自动跳到下一集。"""
        items = self.subs()
        for item in items:
            if item.get("id") != sid:
                continue
            done = {int(x) for x in (item.get("delivered_eps") or [])}
            done.update(int(ep) for ep in episodes if ep)
            item["delivered_eps"] = sorted(done)
            item["updated_at"] = _now()
            self.save_subs(items)
            return item
        return None

    def reset_delivered(self, sid: str) -> dict | None:
        """清空台账，从头补（换剧源/想重下时用）。"""
        items = self.subs()
        for item in items:
            if item.get("id") != sid:
                continue
            item["delivered_eps"] = []
            item["updated_at"] = _now()
            self.save_subs(items)
            return item
        return None

    def delete_sub(self, sid: str) -> bool:
        items = self.subs()
        remain = [s for s in items if s.get("id") != sid]
        if len(remain) == len(items):
            return False
        self.save_subs(remain)
        return True

    def set_enabled(self, sid: str, enabled: bool) -> dict | None:
        items = self.subs()
        for item in items:
            if item.get("id") == sid:
                item["enabled"] = bool(enabled)
                item["updated_at"] = _now()
                self.save_subs(items)
                return item
        return None

    def set_next_run(self, sid: str, when: str) -> None:
        items = self.subs()
        for item in items:
            if item.get("id") == sid:
                item["next_run_at"] = when
                self.save_subs(items)
                return

    def hits(self) -> list[dict]:
        return list(self.backend.load("hits", []) or [])

    def hit_keys(self) -> set[str]:
        return {h.get("dedup_key") for h in self.hits()
                if h.get("status") == "submitted" and h.get("dedup_key")}

    def add_hit(self, hit: dict) -> None:
        items = self.hits()
        items.insert(0, {**hit, "at": _now()})
        self.backend.save("hits", items[: self.hit_limit])

    def logs(self) -> list[dict]:
        return list(self.backend.load("logs", []) or [])

    def add_log(self, entry: dict) -> None:
        items = self.logs()
        items.insert(0, {**entry, "at": _now()})
        self.backend.save("logs", items[: self.log_limit])

    def public_subs(self) -> list[dict]:
        out: list[dict] = []
        for sub in self.subs():
            sub = dict(sub)
            sub["schedule_label"] = schedule_label(sub)
            sub["rules_summary"] = parse_rules(sub.get("rules")).summary()
            sub["ep_progress"] = episode_progress(sub)
            sub["next_episode"] = next_episode(sub)
            sub["search_keyword"] = search_keyword(sub)
            out.append(sub)
        return out


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def make_key(domain: str, page_url: str, title: str) -> str:
    """服务端稳定 key；不把带 passkey 的链接交给前端。"""
    raw = f"{domain}|{page_url or ''}|{title or ''}"
    return hashlib.sha1(raw.encode("utf-8", "ignore")).hexdigest()[:16]


def enrich_item(domain: str, site_name: str, raw: dict) -> dict:
    """把 MP 的 TorrentInfo 转成前端可展示的富信息卡片。
    raw 由 mp_adapter 提供，只含非敏感字段。
    """
    title = str(raw.get("title") or "")
    description = str(raw.get("description") or "")
    labels = [str(x) for x in (raw.get("labels") or [])]
    volume = str(raw.get("volume_factor") or "")
    attrs = extract_attributes(title, description, " ".join(labels), volume)

    # MP 的结构化促销字段优先于文本猜测
    factor = raw.get("downloadvolumefactor")
    if factor is not None:
        try:
            # 注意：0 是合法值且代表免费，不能用 `or 1` 兜底
            attrs.free = float(factor) < 1
        except (TypeError, ValueError):
            pass
    if raw.get("hit_and_run"):
        attrs.hr = True
    if attrs.free and "FREE" not in attrs.badges:
        attrs.badges.insert(0, "FREE")
    if attrs.hr and "H&R" not in attrs.badges:
        attrs.badges.append("H&R")

    display_title, display_sub, original = _display_names(title, description)
    size_gb = raw.get("size_gb")
    seeders = raw.get("seeders")
    clean_labels = _clean_labels(labels + attrs.badges)

    return {
        "key": make_key(domain, str(raw.get("page_url") or ""), title),
        "domain": domain,
        "site": site_name or domain,
        "title": title,
        "description": description,
        "display_title": display_title,
        "display_subtitle": display_sub,
        "original_title": original,
        "page_url": _safe_page_url(raw.get("page_url")),
        "size_gb": size_gb,
        "seeders": seeders,
        "peers": raw.get("peers"),
        "grabs": raw.get("grabs"),
        "pubdate": raw.get("pubdate") or "",
        "category": raw.get("category") or "",
        "volume_factor": volume,
        "badges": clean_labels,
        "attrs": attrs.public(),
    }


_CJK = __import__("re").compile(r"[\u4e00-\u9fff]")


def _display_names(title: str, subtitle: str) -> tuple[str, str, str]:
    """中文名作主标题，中文说明作副标题，英文原名单独一行（MP 风格）。"""
    if not subtitle or len(_CJK.findall(title or "")) >= 2:
        return title, subtitle, ""
    if len(_CJK.findall(subtitle)) < 2:
        return title, subtitle, ""
    parts = __import__("re").split(r"[|｜]", subtitle, maxsplit=1)
    chinese = (parts[0] or "").strip()[:70]
    rest = (parts[1] or "").strip()[:160] if len(parts) > 1 else ""
    if not chinese:
        return title, subtitle, ""
    return chinese, rest, title


_LABEL_NOISE = {"sticky", "imdb", "douban", "download", "unbookmarked", "bookmark", "edit", "nfo"}


def _clean_labels(values: list[str]) -> list[str]:
    out: list[str] = []
    for raw in values:
        text = str(raw or "").strip()
        low = text.lower()
        if not text or low in _LABEL_NOISE or len(text) > 24:
            continue
        if low in ("free", "freeleech", "2x free", "免费"):
            text = "FREE"
        elif low in ("h&r", "hr"):
            text = "H&R"
        if text not in out:
            out.append(text)
    return out[:14]


def _safe_page_url(url: Any) -> str:
    """去掉 URL 上的 passkey / token 等查询凭据。"""
    text = str(url or "")
    if not text.startswith(("http://", "https://")):
        return ""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(text)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in ("passkey", "token", "authkey", "secret_key", "uid", "rsstoken")]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def filter_candidates(
    sub: dict,
    items: list[dict],
    known: set[str],
    *,
    rules_override: RuleSet | dict | None = None,
    target_ep: int | None = None,
) -> tuple[list[dict], list[dict]]:
    """关键词/季/集数 → 规则 → 去重。返回 (通过, 淘汰含原因)。"""
    passed: list[dict] = []
    rejected: list[dict] = []
    rules = parse_rules(rules_override if rules_override is not None else sub.get("rules"))
    seen: set[str] = set()
    for item in items:
        key = item.get("key") or ""
        if key in seen:
            continue
        seen.add(key)
        text = f"{item.get('title') or ''} {item.get('description') or ''}"
        ok, episode = match_title(
            text, sub.get("include_words", ""), sub.get("exclude_words", ""),
            sub.get("ep_start"), sub.get("ep_end"), sub.get("kind", "series"),
            sub.get("season"), target_ep,
        )
        reason = ""
        if not ok:
            reason = "关键词/集数不符"
        else:
            ok, reason = match_rules(
                item.get("attrs") or {}, rules,
                size_gb=item.get("size_gb"), seeders=item.get("seeders"),
            )
        row = {**item, "reason": reason, "duplicate": key in known,
               "episode": episode}
        (passed if ok else rejected).append(row)
        if ok and key in known:
            row["reason"] = "已下载过"
    return passed, rejected
