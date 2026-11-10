"""订阅输入模型与关键词/集数匹配。纯逻辑，可在 MP 之外单测。"""
from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .rules import RuleSet

TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class SubscriptionIn(BaseModel):
    """一条订阅。site_ids 存 MP 站点域名（domain），downloader_id 存 MP 下载器名。"""

    name: str = Field(min_length=1, max_length=200)
    kind: Literal["series", "movie"] = "series"
    # 第几部 / 第几季：有些剧分多部，靠它区分搜索关键词（剧名 + 第N部）
    season: int | None = None
    site_ids: list[str] = Field(default_factory=list)
    include_words: str = ""
    exclude_words: str = ""
    ep_start: int | None = None
    ep_end: int | None = None
    # 已提交过下载的集数台账：补搜只找「第一个还没下到的集」，避免重复下载
    delivered_eps: list[int] = Field(default_factory=list)
    rules: RuleSet = Field(default_factory=RuleSet)
    downloader_id: str = ""
    qb_category: str = ""
    save_path: str = ""
    schedule_mode: Literal["daily", "weekly", "interval"] = "daily"
    schedule_time: str = "03:00"
    schedule_weekday: Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"] = "mon"
    schedule_interval: int = 120
    # 本轮没搜到（还没更新）时，每 retry_interval 分钟再搜一次，
    # 连续重试不超过 retry_window 分钟，避免整天挂着空转。
    retry_interval: int = 10
    retry_window: int = 120
    # 每轮有结果时推送到 MP 通知（QQ / 微信 / TG 等，取决于 MP 里的通知配置）
    notify: bool = True
    enabled: bool = False

    @field_validator("name", "include_words", "exclude_words", "qb_category", "downloader_id")
    @classmethod
    def strip_text(cls, v: str) -> str:
        return v.strip()

    @field_validator("site_ids")
    @classmethod
    def clean_sites(cls, v: list[str]) -> list[str]:
        return list(dict.fromkeys(str(x).strip() for x in (v or []) if str(x).strip()))

    @field_validator("save_path", "qb_category")
    @classmethod
    def safe_path(cls, v: str) -> str:
        v = v.strip()
        if ".." in v:
            raise ValueError("路径不允许包含 '..'")
        return v

    @field_validator("schedule_time")
    @classmethod
    def valid_time(cls, v: str) -> str:
        if not TIME_RE.match(v or ""):
            raise ValueError("时间格式必须为 HH:MM")
        return v

    @field_validator("schedule_interval")
    @classmethod
    def valid_interval(cls, v: int) -> int:
        if v < 5 or v > 10080:
            raise ValueError("间隔需在 5 ~ 10080 分钟之间")
        return v

    @field_validator("retry_interval")
    @classmethod
    def valid_retry_interval(cls, v: int) -> int:
        if v < 1 or v > 1440:
            raise ValueError("重试间隔需在 1 ~ 1440 分钟之间")
        return v

    @field_validator("retry_window")
    @classmethod
    def valid_retry_window(cls, v: int) -> int:
        if v < 0 or v > 10080:
            raise ValueError("重试总时长需在 0 ~ 10080 分钟之间，0 表示不重试")
        return v

    @field_validator("delivered_eps")
    @classmethod
    def clean_delivered(cls, v: list[int]) -> list[int]:
        out: list[int] = []
        for raw in v or []:
            try:
                num = int(raw)
            except (TypeError, ValueError):
                continue
            if 1 <= num <= 999 and num not in out:
                out.append(num)
        return sorted(out)

    @model_validator(mode="after")
    def valid_episode(self) -> "SubscriptionIn":
        for field in ("ep_start", "ep_end"):
            v = getattr(self, field)
            if v is not None and not (1 <= v <= 999):
                raise ValueError("集数需在 1 ~ 999 之间")
        if self.season is not None and not (0 <= self.season <= 99):
            raise ValueError("第几部需在 0 ~ 99 之间")
        if self.ep_start and self.ep_end and self.ep_start > self.ep_end:
            raise ValueError("起始集不能大于结束集")
        return self


def split_words(raw: str) -> list[str]:
    parts = re.split(r"[,，]", raw or "")
    return list(dict.fromkeys(p.strip().lower() for p in parts if p.strip()))


EPISODE_PATTERNS = (
    re.compile(r"[sS]\d{1,2}[eE](\d{1,3})"),
    re.compile(r"[eE][pP]?(\d{1,3})(?!\d)"),
    re.compile(r"第\s*(\d{1,3})\s*[集话話]"),
    re.compile(r"[\[【\s](\d{1,3})[\]】\s]"),
)

SEASON_PATTERNS = (
    re.compile(r"[sS](\d{1,2})[eE]\d{1,3}"),
    re.compile(r"第\s*([0-9一二三四五六七八九十]{1,3})\s*[季部]"),
    re.compile(r"\bS(\d{1,2})\b(?!\d)"),
)

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def parse_season(title: str) -> int | None:
    """从标题里解析第几季/第几部；认不出就返回 None（按不限制处理）。"""
    text = title or ""
    for pattern in SEASON_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        raw = m.group(1)
        if raw.isdigit():
            num = int(raw)
        elif raw in _CN_NUM:
            num = _CN_NUM[raw]
        elif raw.startswith("十"):
            num = 10 + (_CN_NUM.get(raw[1:], 0) if len(raw) > 1 else 0)
        else:
            continue
        if 0 <= num <= 99:
            return num
    return None


def next_episode(sub: dict) -> int | None:
    """下一个还没下到的集数；电影或全部补齐时返回 None。

    补搜的核心就靠它：只盯「第一个缺失集」，下到就记一笔，下次自动看下一集。
    """
    if (sub.get("kind") or "series") == "movie":
        return None
    start = int(sub.get("ep_start") or 1)
    end = int(sub.get("ep_end") or 999)
    done = {int(x) for x in (sub.get("delivered_eps") or [])}
    ep = start
    while ep <= end:
        if ep not in done:
            return ep
        ep += 1
    return None


def search_keyword(sub: dict) -> str:
    """站点搜索用的关键词：订阅名称本身。

    第几部不进关键词——很多站点的标题写法不统一，进关键词反而搜不到东西；
    季/部改在结果侧用 parse_season 过滤。
    """
    return str(sub.get("name") or "").strip()


def episode_progress(sub: dict) -> str:
    """卡片上显示的补搜进度，例如「已下 3 集 · 下一个第 4 集」。"""
    if (sub.get("kind") or "series") == "movie":
        return "电影 · 下到即止"
    start = int(sub.get("ep_start") or 1)
    end = int(sub.get("ep_end") or 0)
    done = sorted({int(x) for x in (sub.get("delivered_eps") or [])})
    if not done:
        return f"第 {start} 集起，一集一集补"
    nxt = next_episode(sub)
    if nxt is None:
        return f"已下 {len(done)} 集，区间内已补齐" if end else f"已下 {len(done)} 集"
    return f"已下 {len(done)} 集 · 下一个第 {nxt} 集"


def parse_episode(title: str) -> int | None:
    for pattern in EPISODE_PATTERNS:
        m = pattern.search(title or "")
        if m:
            num = int(m.group(1))
            if 1 <= num <= 999:
                return num
    return None


_RES_LABELS = {"4k": "4K", "1080p": "1080p", "720p": "720p", "480p": "480p"}


def resolution_of(attrs: dict | None) -> str:
    """把 attrs 里的分辨率集合转成一行可读文本：['4k'] → '4K'。"""
    values = (attrs or {}).get("resolutions") or []
    out: list[str] = []
    for value in values:
        text = _RES_LABELS.get(str(value).lower(), str(value))
        if text and text not in out:
            out.append(text)
    return " / ".join(out)


def match_title(
    title: str,
    include_words: str,
    exclude_words: str,
    ep_start: int | None = None,
    ep_end: int | None = None,
    kind: str = "series",
    season: int | None = None,
    target_ep: int | None = None,
) -> tuple[bool, int | None]:
    """包含词全部命中、排除词一个都不命中、季/集数落在范围内。

    target_ep 有值时只认这一集（一集一集补），避免一次把整季都下下来。
    """
    low = (title or "").lower()
    for word in split_words(include_words):
        if word not in low:
            return False, None
    for word in split_words(exclude_words):
        if word in low:
            return False, None
    if kind == "movie":
        return True, None
    episode = parse_episode(title)
    if season is not None:
        found = parse_season(title)
        # 标题里能认出季/部且对不上，才排除；认不出的（纯集数命名）放行
        if found is not None and found != int(season):
            return False, episode
    if target_ep is not None:
        if episode != int(target_ep):
            return False, episode
        return True, episode
    if ep_start is not None or ep_end is not None:
        if episode is None:
            return False, None
        if ep_start is not None and episode < ep_start:
            return False, episode
        if ep_end is not None and episode > ep_end:
            return False, episode
    return True, episode


def schedule_label(sub: dict) -> str:
    """把执行计划翻译成中文，明确「周几几点去勾选站点找资源」。"""
    mode = sub.get("schedule_mode", "daily")
    time_text = sub.get("schedule_time") or "03:00"
    retry = int(sub.get("retry_interval") or 0)
    window = int(sub.get("retry_window") or 0)
    tail = f"；没更新就每 {retry} 分钟再搜，最多守 {window} 分钟" if retry and window else ""
    if mode == "interval":
        return f"每隔 {sub.get('schedule_interval') or 120} 分钟到勾选站点搜一次" + tail
    weekday = sub.get("schedule_weekday") or "mon"
    names = {"mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
             "fri": "周五", "sat": "周六", "sun": "周日"}
    if mode == "weekly":
        return f"每{names.get(weekday, '周一')} {time_text} 到勾选站点搜资源" + tail
    return f"每天 {time_text} 到勾选站点搜资源" + tail
