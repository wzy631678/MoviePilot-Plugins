"""订阅核心：存储、校验、过滤、计划。与 MoviePilot 解耦，便于本机单测。"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from pydantic import ValidationError

from .meta import META_KEYS, poster_url
from .models import (
    SubscriptionIn,
    episode_progress,
    episode_scope,
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
        if old and "library_eps" not in payload:
            body["library_eps"] = old.get("library_eps") or []
        if old and "lib_in" not in payload:
            body["lib_in"] = bool(old.get("lib_in"))
        if old and "recommend_date" not in payload:
            body["recommend_date"] = old.get("recommend_date") or ""
        if old and "air_state" not in payload:
            body["air_state"] = old.get("air_state") or ""
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
            # 海报/总集数这些是 TMDB 查回来的，编辑表单里没有，编辑时原样沿用
            for key in META_KEYS:
                if key in old:
                    data[key] = old[key]
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

    def set_meta(self, sid: str, meta: dict) -> dict | None:
        """把 TMDB 查到的海报/年份/总集数写进订阅；查不到就不写。"""
        if not meta:
            return self.get_sub(sid)
        items = self.subs()
        for item in items:
            if item.get("id") != sid:
                continue
            for key in META_KEYS:
                if key in meta:
                    item[key] = meta[key]
            item["meta_at"] = _now()
            self.save_subs(items)
            return item
        return None

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

    def migrate_evening_default(self) -> int:
        """把插件老默认的执行时间统一搬到 19:00。

        TMDB 只给「哪天」播出，真正出种基本在当晚 19 点前后：03:00 是插件最早的默认值，
        20:00 是上一版的默认值，两个都留着会比首播出种晚、错过免费种子的窗口。
        只改「等于某个老默认值」的记录（说明用户没自己调过），自己定过时间点的一律不动。
        """
        old_defaults = {"03:00", "20:00"}
        items = self.subs()
        changed = 0
        for sub in items:
            if str(sub.get("schedule_time") or "").strip() not in old_defaults:
                continue
            sub["schedule_time"] = "19:00"
            changed += 1
        if changed:
            self.save_subs(items)
        return changed

    def migrate_daily_to_recommend(self) -> int:
        """把早期默认的「每天」执行模式换成「按 TMDB 推荐时间」。

        v1.10.4 起表单里已经没有「每天」这个选项，「按 TMDB 推荐时间」才是默认：
        那集哪天播，就从那天起每天到点去站点找。老订阅留着 daily，打开表单会落到
        「自定义」页签，用户看不到 TMDB 推荐时间，所以这里统一搬过去。
        只改 daily / 空值；weekly、interval 是用户自己挑的，一律不动。
        """
        items = self.subs()
        changed = 0
        for sub in items:
            mode = str(sub.get("schedule_mode") or "").strip()
            if mode in ("", "daily"):
                sub["schedule_mode"] = "recommend"
                sub["updated_at"] = _now()
                changed += 1
        if changed:
            self.save_subs(items)
        return changed

    def set_sub_fields(self, sid: str, **fields) -> dict | None:
        """给一条订阅就地补几个字段（媒体库缓存 / 推荐更新日等）。"""
        items = self.subs()
        for item in items:
            if item.get("id") == sid:
                for key, value in fields.items():
                    if value is not None:
                        item[key] = value
                item["updated_at"] = _now()
                self.save_subs(items)
                return item
        return None

    def hits(self) -> list[dict]:
        return list(self.backend.load("hits", []) or [])

    def hit_keys(self) -> set[str]:
        return {h.get("dedup_key") for h in self.hits()
                if h.get("status") == "submitted" and h.get("dedup_key")}

    def add_hit(self, hit: dict) -> None:
        items = self.hits()
        items.insert(0, {**hit, "at": _now()})
        self.backend.save("hits", items[: self.hit_limit])

    # ---------------------------------------------- 站点资源偏好（上次用的编码/分辨率等）
    def prefs(self) -> dict:
        raw = self.backend.load("prefs", {}) or {}
        return raw if isinstance(raw, dict) else {}

    def remember_pref(self, domain: str, attrs: Any, title: str = "") -> None:
        """记住「这个站点上次下的那种资源」，下次同站排序优先。

        存的是插件数据里的一个独立键，不写日志：运行记录清空了偏好还在，
        用户担心的「日志清理掉就忘了」不会发生。
        """
        domain = str(domain or "").strip().lower()
        if not domain:
            return
        pref = pref_from_attrs(attrs)
        if not (any(pref.get(k) for k in PREF_FIELDS)
                or pref.get("cnsub") or pref.get("specsub")):
            return
        data = self.prefs()
        old = data.get(domain) or {}
        # 同一站点：用户刚下的这条就是最新规则；某项这次没识别出来就沿用上次的
        merged = {k: (pref.get(k) or old.get(k) or []) for k in PREF_FIELDS}
        merged["cnsub"] = bool(pref.get("cnsub") or old.get("cnsub"))
        merged["specsub"] = bool(pref.get("specsub") or old.get("specsub"))
        data[domain] = {**merged, "at": _now(), "sample": str(title or "")[:120]}
        if len(data) > 40:
            kept = sorted(data.items(), key=lambda kv: str((kv[1] or {}).get("at") or ""),
                          reverse=True)[:40]
            data = dict(kept)
        self.backend.save("prefs", data)

    def clear_prefs(self) -> None:
        self.backend.save("prefs", {})

    # ---------------------------------------------- 保存目录历史（输入过就记住）
    def save_paths(self) -> list[str]:
        raw = self.backend.load("save_paths", []) or []
        return [str(x) for x in raw if str(x).strip()] if isinstance(raw, list) else []

    def remember_save_path(self, path: str) -> None:
        path = str(path or "").strip()
        if not path:
            return
        items = [x for x in self.save_paths() if x != path]
        items.insert(0, path)
        self.backend.save("save_paths", items[:12])

    def logs(self) -> list[dict]:
        return list(self.backend.load("logs", []) or [])

    def add_log(self, entry: dict) -> None:
        items = self.logs()
        items.insert(0, {**entry, "at": _now()})
        self.backend.save("logs", items[: self.log_limit])

    def last_runs(self) -> dict[str, dict]:
        """每条订阅最近一次运行记录（日志本身已按时间倒序）。

        老版本写的日志只有订阅名、没有 sub_id，这里按名字也留一份索引，
        免得卡片明明跑过却一直显示「未执行」。
        """
        out: dict[str, dict] = {}
        for entry in self.logs():
            sid = entry.get("sub_id")
            if sid and str(sid) not in out:
                out[str(sid)] = entry
            name = str(entry.get("sub") or "").strip()
            if name and f"name:{name}" not in out:
                out[f"name:{name}"] = entry
        return out

    def public_subs(self) -> list[dict]:
        out: list[dict] = []
        runs = self.last_runs()
        for sub in self.subs():
            sub = dict(sub)
            sub["schedule_label"] = schedule_label(sub)
            sub["rules_summary"] = parse_rules(sub.get("rules")).summary()
            sub["ep_progress"] = episode_progress(sub)
            sub["next_episode"] = next_episode(sub)
            sub["search_keyword"] = search_keyword(sub)
            # 卡片上的「166/200」：已提交集数 / TMDB 总集数（查不到就退回订阅里填的结束集）
            done = sorted({int(x) for x in (sub.get("delivered_eps") or [])})
            sub["done_eps"] = len(done)
            # 高位水印：已下到第几集。删掉前面看过的集也不会回头重下——只往下补。
            sub["high_ep"] = done[-1] if done else 0
            # 媒体库当前实际有几集入库（TMDB 元数据刷新时缓存，删了就变少，仅作显示）
            lib = sorted({int(x) for x in (sub.get("library_eps") or [])})
            if not lib and not sub.get("lib_synced"):
                # 老订阅还没跟媒体库对齐过：先按「下到过」的集数显示，
                # 等下一轮补搜/保存后写入真实存量再收缩，避免卡片突然变 0/52
                lib = done
            sub["library_eps"] = lib
            sub["lib_count"] = len(lib)
            sub["lib_synced"] = bool(sub.get("lib_synced"))
            # 电影没有集数：lib_in = 媒体库里有没有这部（前端据此显示「已入库」）
            sub["lib_in"] = bool(sub.get("lib_in"))
            sub["total_eps"] = int(sub.get("total_eps") or 0) or int(sub.get("ep_end") or 0)
            # 任务墙上的「3/36」：区间内已下 / 区间总集数（不是整季 200 集）。
            # 用户订阅 165-200，分母就该是 36，这样卡片和表单里的数字才对得上。
            sc = episode_scope(sub)
            sub["scope_start"] = sc["start"]
            sub["scope_end"] = sc["end"]
            sub["scope_total"] = sc["total"]
            sub["scope_done"] = sc["done"]
            # 区间内已下的最高集号：卡片上写「下到第 21 集」，
            # 从中间订阅（10-30）也能一眼看出进度在哪
            sub["scope_high"] = sc["high"]
            sub["scope_left"] = sc["left"]
            sub["poster_url"] = poster_url(sub.get("poster_path"))
            last = runs.get(str(sub.get("id") or "")) or {}
            if not last:
                last = runs.get("name:" + str(sub.get("name") or "").strip()) or {}
            sub["last_run_at"] = last.get("at") or ""
            sub["last_status"] = last.get("status") or ""
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
    target_ep: int | Iterable[int] | None = None,
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


# ---------------------------------------------------------------- 站点资源偏好

PREF_FIELDS = ("resolutions", "codecs", "sources")

_PREF_LABEL = {
    "4k": "4K", "1080p": "1080p", "720p": "720p", "480p": "480p/SD",
    "h264": "H.264", "h265": "H.265", "av1": "AV1", "vc1": "VC-1", "xvid": "XviD",
    "webdl": "WEB-DL", "bluray": "BluRay", "remux": "REMUX", "hdtv": "HDTV",
    "dvd": "DVD", "tc": "枪版",
}


def pref_from_attrs(attrs: Any) -> dict:
    """把一条资源的属性压成可复用的偏好：只留分辨率 / 编码 / 来源 / 字幕。"""
    data = attrs if isinstance(attrs, dict) else {}
    out: dict[str, Any] = {}
    for key in PREF_FIELDS:
        values = [str(v) for v in (data.get(key) or []) if str(v or "").strip()]
        if values:
            out[key] = sorted(set(values))
    out["cnsub"] = bool(data.get("cnsub"))
    out["specsub"] = bool(data.get("specsub"))
    return out


def pref_text(pref: dict) -> str:
    """「H.265 · 1080p · 中文字幕」这种人能读的摘要。"""
    if not pref:
        return ""
    bits: list[str] = []
    for key in PREF_FIELDS:
        bits += [_PREF_LABEL.get(v, v.upper()) for v in (pref.get(key) or [])]
    if pref.get("specsub"):
        bits.append("特效字幕")
    elif pref.get("cnsub"):
        bits.append("中文字幕")
    return " · ".join(dict.fromkeys(bits))


def pref_score(item: dict, pref: dict) -> int:
    """0 = 不相关；正数 = 和上次用的规则同款；负数 = 明显不同（往后排）。"""
    if not pref:
        return 0
    attrs = item.get("attrs") or {}
    score = 0
    for key in PREF_FIELDS:
        want = set(pref.get(key) or [])
        got = set(attrs.get(key) or [])
        if not want:
            continue
        if got & want:
            score += 2
        elif got:
            score -= 1
    if pref.get("specsub") or pref.get("cnsub"):
        if attrs.get("specsub") or attrs.get("cnsub"):
            score += 2
        elif attrs.get("nosub"):
            score -= 2
    return score


def sort_candidates(rows: list[dict], prefs: dict | None = None) -> list[dict]:
    """同一集有多个版本时的排序：先按「这个站上次用的规则」，再看做种数。

    只排不筛：偏好那种恰好这集没有时，其它版本照样能下，不会把结果筛没。
    """
    prefs = prefs or {}

    def _key(row: dict) -> tuple:
        domain = str(row.get("domain") or "").strip().lower()
        pref = prefs.get(domain) or {}
        return (-pref_score(row, pref), -(row.get("seeders") or 0), row.get("size_gb") or 0)

    rows.sort(key=_key)
    return rows


def pref_summary(prefs: dict | None, domains: list[str] | None = None) -> str:
    """给界面提示用：搜的这些站里，上次各用了什么规则（去重后合并成一行）。"""
    prefs = prefs or {}
    keys = [str(d or "").strip().lower() for d in (domains or [])] or list(prefs)
    texts: list[str] = []
    for key in keys:
        text = pref_text(prefs.get(key) or {})
        if text and text not in texts:
            texts.append(text)
    return "｜".join(texts[:3])
