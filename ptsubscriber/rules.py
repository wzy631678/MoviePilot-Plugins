"""资源标签解析与订阅规则匹配。

设计参考 MoviePilot 的订阅过滤字段（resolution / quality / effect / include / exclude /
size_range / CNSUB / SPECSUB / FREE / GZ 等），但保持「结构化多选」，不引入规则语法、
优先级、洗版或规则组 —— 不同维度之间 AND，同一维度内 OR。

纯函数，无 IO，便于单测。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

# ---------------------------------------------------------------- 选项定义

RULE_OPTIONS: dict[str, Any] = {
    "resolutions": [
        {"value": "4k", "label": "4K / 2160p / UHD"},
        {"value": "1080p", "label": "1080p / 1080i"},
        {"value": "720p", "label": "720p"},
        {"value": "480p", "label": "480p / SD"},
    ],
    "codecs": [
        {"value": "h264", "label": "H.264 / x264 / AVC"},
        {"value": "h265", "label": "H.265 / x265 / HEVC"},
        {"value": "av1", "label": "AV1"},
        {"value": "vc1", "label": "VC-1"},
        {"value": "xvid", "label": "XviD / DivX"},
    ],
    "sources": [
        {"value": "webdl", "label": "WEB-DL / WEBRip"},
        {"value": "bluray", "label": "BluRay / BDRip"},
        {"value": "remux", "label": "REMUX / 原盘 / 蓝光原盘"},
        {"value": "hdtv", "label": "HDTV / 电视录制"},
        {"value": "dvd", "label": "DVDRip / DVD"},
        {"value": "tc", "label": "TC / 枪版 / 预览版"},
    ],
    "subtitle": [
        {"value": "any", "label": "不限"},
        {"value": "cnsub", "label": "必须中文字幕（内封/内嵌/简繁/中字）"},
        {"value": "specsub", "label": "必须特效字幕"},
        {"value": "both", "label": "中文字幕 + 特效字幕都要"},
        {"value": "exclude_nosub", "label": "排除无字幕 / 生肉"},
    ],
    "promotion": [
        {"value": "any", "label": "不限"},
        {"value": "free", "label": "只要免费 / FREE / 2x 等促销"},
        {"value": "no_hr", "label": "排除 H&R"},
    ],
    "hdr": [
        {"value": "any", "label": "不限"},
        {"value": "require", "label": "必须 HDR / 杜比视界"},
        {"value": "exclude", "label": "排除 HDR / 杜比视界"},
    ],
}

_RESOLUTION_VALUES = tuple(o["value"] for o in RULE_OPTIONS["resolutions"])
_CODEC_VALUES = tuple(o["value"] for o in RULE_OPTIONS["codecs"])
_SOURCE_VALUES = tuple(o["value"] for o in RULE_OPTIONS["sources"])


class RuleSet(BaseModel):
    """一条订阅的资源筛选规则。全部留空 = 不限制（保持原来的极简行为）。"""

    resolutions: list[Literal["4k", "1080p", "720p", "480p"]] = Field(default_factory=list)
    codecs: list[Literal["h264", "h265", "av1", "vc1", "xvid"]] = Field(default_factory=list)
    sources: list[Literal["webdl", "bluray", "remux", "hdtv", "dvd", "tc"]] = Field(
        default_factory=list
    )
    subtitle: Literal["any", "cnsub", "specsub", "both", "exclude_nosub"] = "any"
    promotion: Literal["any", "free", "no_hr"] = "any"
    hdr: Literal["any", "require", "exclude"] = "any"
    min_size_gb: float | None = None
    max_size_gb: float | None = None
    min_seeders: int | None = None

    @model_validator(mode="after")
    def _check(self) -> "RuleSet":
        self.resolutions = list(dict.fromkeys(self.resolutions))
        self.codecs = list(dict.fromkeys(self.codecs))
        self.sources = list(dict.fromkeys(self.sources))
        for name in ("min_size_gb", "max_size_gb"):
            value = getattr(self, name)
            if value is not None and (value < 0 or value > 100000):
                raise ValueError(f"{name} 需在 0 ~ 100000 GB 之间")
        if (self.min_size_gb is not None and self.max_size_gb is not None
                and self.min_size_gb > self.max_size_gb):
            raise ValueError("最小体积不能大于最大体积")
        if self.min_seeders is not None and not (0 <= self.min_seeders <= 100000):
            raise ValueError("最少做种数需在 0 ~ 100000 之间")
        return self

    def is_empty(self) -> bool:
        return not (self.resolutions or self.codecs or self.sources) and (
            self.subtitle == "any" and self.promotion == "any" and self.hdr == "any"
            and self.min_size_gb is None and self.max_size_gb is None
            and self.min_seeders is None
        )

    def summary(self) -> str:
        """给订阅列表用的中文摘要。"""
        if self.is_empty():
            return "不限资源"
        parts: list[str] = []
        label = {
            group: {o["value"]: o["label"].split(" /")[0] for o in items}
            for group, items in RULE_OPTIONS.items()
            if group in ("resolutions", "codecs", "sources")
        }
        for key, values in (("resolutions", self.resolutions),
                            ("codecs", self.codecs), ("sources", self.sources)):
            if values:
                parts.append("/".join(label[key][v] for v in values))
        if self.subtitle != "any":
            parts.append(next(o["label"] for o in RULE_OPTIONS["subtitle"]
                              if o["value"] == self.subtitle))
        if self.promotion != "any":
            parts.append("仅促销" if self.promotion == "free" else "排除 H&R")
        if self.hdr != "any":
            parts.append("要 HDR" if self.hdr == "require" else "排除 HDR")
        if self.min_size_gb is not None or self.max_size_gb is not None:
            parts.append(f"{self.min_size_gb or 0}~{self.max_size_gb or '∞'} GB")
        if self.min_seeders:
            parts.append(f"做种≥{self.min_seeders}")
        return " · ".join(parts)


def parse_rules(raw: Any) -> RuleSet:
    """兼容历史数据：None / 空串 / 非法 JSON 一律回退到空规则。"""
    import json

    if isinstance(raw, RuleSet):
        return raw
    if isinstance(raw, dict):
        try:
            return RuleSet(**raw)
        except Exception:
            return RuleSet()
    if not raw:
        return RuleSet()
    try:
        return RuleSet(**json.loads(raw))
    except Exception:
        return RuleSet()


# ---------------------------------------------------------------- 标签解析

SIZE_VALUE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(TiB|GiB|MiB|KiB|TB|GB|MB|KB|B)(?![A-Za-z])", re.I
)
_SIZE_FACTOR = {
    "tib": 1024.0, "tb": 1024.0,
    "gib": 1.0, "gb": 1.0,
    "mib": 1 / 1024, "mb": 1 / 1024,
    "kib": 1 / 1024 ** 2, "kb": 1 / 1024 ** 2,
    "b": 1 / 1024 ** 3,
}
NUMBER_RE = re.compile(r"\d+")
DATE_RE = re.compile(r"20\d{2}[-/年]\d{1,2}[-/月]\d{1,2}|\d{1,2}\s*(?:小时|分钟|天)前")

_PATTERNS: dict[str, tuple[str, ...]] = {
    "4k": (r"2160\s*[pi]", r"\b4\s*k\b", r"\buhd\b"),
    "1080p": (r"1080\s*[pi]", r"\bfhd\b", r"全高清"),
    "720p": (r"720\s*[pi]", r"\bhd\b"),
    "480p": (r"480\s*[pi]", r"\bsd\b", r"\bdvd\b"),
    "h264": (r"x[\s.]?264", r"h[\s.]?264", r"\bavc\b"),
    "h265": (r"x[\s.]?265", r"h[\s.]?265", r"\bhevc\b"),
    "av1": (r"\bav1\b",),
    "vc1": (r"\bvc-?1\b",),
    "xvid": (r"\bxvid\b", r"\bdivx\b"),
    "webdl": (r"web-?dl", r"web-?rip", r"\bweb\b", r"网络版"),
    "bluray": (r"blu-?ray", r"\bbd-?rip\b", r"\bbr-?rip\b", r"蓝光(?!原盘)"),
    "remux": (r"\bremux\b", r"原盘", r"\bbdmv\b", r"蓝光原盘"),
    "hdtv": (r"\bhdtv\b", r"电视(?:台|版|录制)"),
    "dvd": (r"\bdvd-?rip\b", r"\bdvd\b"),
    "tc": (r"\btc\b", r"枪版", r"\bcam-?rip\b", r"预览版"),
}
_COMPILED = {
    key: tuple(re.compile(p, re.I) for p in patterns)
    for key, patterns in _PATTERNS.items()
}

CN_SUB_RE = re.compile(
    r"中文字幕|中字|简体(?:中文)?|繁体(?:中文)?|简繁|双语字幕|内封|内嵌|内挂|"
    r"字幕组|\bcns(?:ub)?\b|\bgb\b|中文配音|国配", re.I
)
SPEC_SUB_RE = re.compile(r"特效字幕|特效(?:简|繁|中字)?|特校字幕|\bspecsub\b", re.I)
NO_SUB_RE = re.compile(r"无字幕|无中字|无字|生肉|\braw\b|\bno\s*sub\b", re.I)
FREE_RE = re.compile(r"\bfree\b|免费|\b2[\s.]?[0x]?\s?x\b|双倍上传|\b50\s*%\b|\b30\s*%\b|促销", re.I)
HR_RE = re.compile(r"h\s*&\s*r|hit\s*(?:and|&)\s*run|\bhr\b|禁转", re.I)
HDR_RE = re.compile(r"hdr10\+?|\bhdr\b|dolby\s*vision|杜比视界|\bdovi\b|\bdv\b|\bhlg\b", re.I)
AUDIO_RE = re.compile(r"国语|国配|粤语|双音轨|\batmos\b|\btruehd\b|\bdts\b|\bddp?\b|\baac\b|\bac3\b", re.I)
GROUP_RE = re.compile(r"官组|官方|原组|\bgz\b|\bwiki\b|\bcmct\b|\bfrds\b", re.I)

_BADGE_LABEL = {
    "4k": "4K", "1080p": "1080p", "720p": "720p", "480p": "480p/SD",
    "h264": "H.264", "h265": "H.265", "av1": "AV1", "vc1": "VC-1", "xvid": "XviD",
    "webdl": "WEB-DL", "bluray": "BluRay", "remux": "REMUX", "hdtv": "HDTV",
    "dvd": "DVD", "tc": "枪版",
}


@dataclass
class TorrentAttrs:
    """一条种子解析出的可筛选属性 + 展示徽章。"""

    resolutions: set[str] = field(default_factory=set)
    codecs: set[str] = field(default_factory=set)
    sources: set[str] = field(default_factory=set)
    cnsub: bool = False
    specsub: bool = False
    nosub: bool = False
    free: bool = False
    hr: bool = False
    hdr: bool = False
    badges: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        return {
            "resolutions": sorted(self.resolutions),
            "codecs": sorted(self.codecs),
            "sources": sorted(self.sources),
            "cnsub": self.cnsub,
            "specsub": self.specsub,
            "nosub": self.nosub,
            "free": self.free,
            "hr": self.hr,
            "hdr": self.hdr,
            "badges": self.badges,
        }


def parse_size_gb(text: str) -> float | None:
    """'1.2 GiB' / '850 MB' / '1.5TB' → GB（按 1024 进制近似）。"""
    if not text:
        return None
    m = SIZE_VALUE_RE.search(str(text))
    if not m:
        return None
    try:
        return round(float(m.group(1)) * _SIZE_FACTOR[m.group(2).lower()], 3)
    except (ValueError, KeyError):
        return None


def parse_int(text: str) -> int | None:
    if not text:
        return None
    m = NUMBER_RE.search(str(text).replace(",", ""))
    return int(m.group(0)) if m else None


def extract_date(text: str) -> str:
    m = DATE_RE.search(text or "")
    return m.group(0) if m else ""


def extract_attributes(*parts: str) -> TorrentAttrs:
    """从标题、副标题、整行文本、站点标签中识别资源属性。"""
    text = " ".join(str(p or "") for p in parts)
    attrs = TorrentAttrs()
    for key, patterns in _COMPILED.items():
        if any(p.search(text) for p in patterns):
            if key in _RESOLUTION_VALUES:
                attrs.resolutions.add(key)
            elif key in _CODEC_VALUES:
                attrs.codecs.add(key)
            elif key in _SOURCE_VALUES:
                attrs.sources.add(key)
    attrs.cnsub = bool(CN_SUB_RE.search(text))
    attrs.specsub = bool(SPEC_SUB_RE.search(text))
    attrs.nosub = bool(NO_SUB_RE.search(text))
    attrs.free = bool(FREE_RE.search(text))
    attrs.hr = bool(HR_RE.search(text))
    attrs.hdr = bool(HDR_RE.search(text))

    badges: list[str] = []
    for key in ("4k", "1080p", "720p", "480p", "h264", "h265", "av1", "vc1", "xvid",
                "remux", "webdl", "bluray", "hdtv", "dvd", "tc"):
        if key in attrs.resolutions or key in attrs.codecs or key in attrs.sources:
            badges.append(_BADGE_LABEL[key])
    if attrs.specsub:
        badges.append("特效字幕")
    elif attrs.cnsub:
        badges.append("中文字幕")
    if attrs.nosub and not attrs.cnsub:
        badges.append("无字幕")
    if attrs.hdr:
        badges.append("HDR")
    if attrs.free:
        badges.append("FREE")
    if attrs.hr:
        badges.append("H&R")
    audio = AUDIO_RE.search(text)
    if audio:
        badges.append(audio.group(0).upper()[:8])
    if GROUP_RE.search(text):
        badges.append("官组")
    attrs.badges = list(dict.fromkeys(badges))
    return attrs


def attrs_from_public(raw: Any) -> TorrentAttrs:
    """把 TorrentItem.public() 里的 attrs 字典还原为可匹配对象。"""
    if isinstance(raw, TorrentAttrs):
        return raw
    if not isinstance(raw, dict):
        return TorrentAttrs()
    return TorrentAttrs(
        resolutions=set(raw.get("resolutions") or []),
        codecs=set(raw.get("codecs") or []),
        sources=set(raw.get("sources") or []),
        cnsub=bool(raw.get("cnsub")),
        specsub=bool(raw.get("specsub")),
        nosub=bool(raw.get("nosub")),
        free=bool(raw.get("free")),
        hr=bool(raw.get("hr")),
        hdr=bool(raw.get("hdr")),
        badges=list(raw.get("badges") or []),
    )


def match_rules(
    attrs: TorrentAttrs | dict[str, Any],
    rules: RuleSet,
    *,
    size_gb: float | None = None,
    seeders: int | None = None,
) -> tuple[bool, str]:
    """不同维度 AND，同维度 OR。返回 (是否通过, 淘汰原因)。"""
    attrs = attrs_from_public(attrs)
    rules = parse_rules(rules)
    if rules.resolutions and not (attrs.resolutions & set(rules.resolutions)):
        return False, "分辨率不符"
    if rules.codecs and not (attrs.codecs & set(rules.codecs)):
        return False, "编码不符"
    if rules.sources and not (attrs.sources & set(rules.sources)):
        return False, "来源不符"
    if rules.subtitle == "cnsub" and not attrs.cnsub:
        return False, "无中文字幕"
    if rules.subtitle == "specsub" and not attrs.specsub:
        return False, "无特效字幕"
    if rules.subtitle == "both" and not (attrs.cnsub and attrs.specsub):
        return False, "字幕不全"
    if rules.subtitle == "exclude_nosub" and attrs.nosub and not attrs.cnsub:
        return False, "无字幕"
    if rules.promotion == "free" and not attrs.free:
        return False, "非促销"
    if rules.promotion == "no_hr" and attrs.hr:
        return False, "H&R"
    if rules.hdr == "require" and not attrs.hdr:
        return False, "无 HDR"
    if rules.hdr == "exclude" and attrs.hdr:
        return False, "含 HDR"
    if rules.min_size_gb is not None:
        if size_gb is None:
            return False, "体积未知"
        if size_gb < rules.min_size_gb:
            return False, "体积过小"
    if rules.max_size_gb is not None:
        if size_gb is None:
            return False, "体积未知"
        if size_gb > rules.max_size_gb:
            return False, "体积过大"
    if rules.min_seeders is not None:
        if seeders is None:
            return False, "做种未知"
        if seeders < rules.min_seeders:
            return False, "做种不足"
    return True, ""
