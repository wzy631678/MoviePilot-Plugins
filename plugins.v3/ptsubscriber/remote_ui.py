"""远程入口（QQ / 微信 / TG / 智能助手）的纯文本交互层。

这一层只做三件事，不碰 MP、不碰数据库，方便单测：

1. `parse_intent`：把 `/ptsub 天下第一 从3集 站点馒头 立即` 拆成
   「片名 / 选第几个候选 / 从第几集 / 用哪个站点 / 什么时候执行」。
2. `render_*`：把候选、资源列表、订阅回执排版成**不带图片、尽量短**的纯文本，
   手机上看一眼就能回个数字。
3. `pick_rows`：把「3、1-5」这类用户输入翻成序号集合。

设计约束（都是为 QQ 交互服务）：
- 不输出图片 / Markdown 表格，只输出短行 + 序号。
- 每条消息末尾都写清楚「下一条该发什么」，用户不用记命令。
- 数量上限固定，避免一条消息刷屏被渠道截断。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

# 一条消息里最多列几个候选 / 几个资源（QQ 长消息体验差，也不利于回复序号）
MAX_CANDIDATES = 5
MAX_RESOURCES = 8

_RANGE_RE = re.compile(r"^\s*(\d{1,3})\s*(?:[-~—到]\s*(\d{1,3}))?\s*$")
_FW = str.maketrans({"：": ":", "＝": "=", "，": ",", "（": "(", "）": ")", "　": " "})

# 参数别名：中文口语优先，英文缩写兜底
_ALIAS = {
    "集": "ep_start", "起始集": "ep_start", "开始": "ep_start", "from": "ep_start",
    "期": "ep_end", "结束集": "ep_end", "to": "ep_end",
    "站": "sites", "站点": "sites", "site": "sites", "sites": "sites",
    "时": "schedule_time", "时间": "schedule_time", "time": "schedule_time",
    "间隔": "schedule_interval", "interval": "schedule_interval",
    "季": "season", "season": "season",
    "类": "kind", "kind": "kind",
}

_NOW_WORDS = ("立即", "马上", "现在", "立刻", "现在就", "直接下")


def _clean(text: str) -> str:
    s = str(text or "").translate(_FW)
    return re.sub(r"\s+", " ", s).strip()


def _norm(text: str) -> str:
    """站点名比对用：去空格、去常见后缀、小写。"""
    s = re.sub(r"[\s·\-_.]+", "", str(text or "")).lower()
    for tail in ("站点", "网站", "pt站", "站"):
        if s.endswith(tail) and len(s) > len(tail):
            s = s[: -len(tail)]
    return s


def _num(text: str) -> int | None:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


def parse_intent(text: str) -> dict[str, Any]:
    """把一条远程指令拆成结构化意图（顺序随意、中英文混写都行）。

        /ptsub 天下第一 从3集 站点馒头 立即
        /ptsub 天下第一 从5 每天3:00
        /ptsub 天下第一 每周三21:30 站点馒头,岛
        /ptfind 天下第一 5-8集
        2 / 1-3
    """
    raw = _clean(text)
    # 去掉 /ptsub、/ptfind 这类命令前缀，命令本身由调用方决定
    raw = re.sub(r"^/\s*[A-Za-z]+\s*", "", raw).strip()
    out: dict[str, Any] = {
        "name": "", "pick": None, "pick_end": None, "ep_start": None, "ep_end": None,
        "sites": [], "run_now": False, "schedule_mode": None, "schedule_time": None,
        "schedule_interval": None, "schedule_weekday": None, "season": None,
        "kind": None, "list_sites": False, "extra": raw,
    }
    if not raw:
        return out

    # 整条就是一个数字 / 数字区间 → 纯选择（选候选、选资源都走这个）
    if _RANGE_RE.match(raw):
        out["pick"] = int(_RANGE_RE.match(raw).group(1))
        tail = _RANGE_RE.match(raw).group(2)
        out["pick_end"] = int(tail) if tail else out["pick"]
        return out

    # 「用 馒头」「站点 馒头」这种拆成两个词的先合并，否则「用」会被当成空站点
    merged: list[str] = []
    tokens = raw.split(" ")
    skip_next = False
    for i, tok in enumerate(tokens):
        if skip_next:
            skip_next = False
            continue
        if tok in ("用", "站", "站点", "在") and i + 1 < len(tokens):
            merged.append(tok + tokens[i + 1])
            skip_next = True
        else:
            merged.append(tok)

    name_parts: list[str] = []
    for tok in merged:
        tok = tok.strip(" ,、")
        if not tok:
            continue
        # 动词不是片名的一部分
        if tok in ("订阅", "下载", "搜索", "找", "找一下", "帮我", "添加", "来", "开始"):
            continue

        # key=value 写法也认，方便老用户照抄
        if "=" in tok:
            key, _, value = tok.partition("=")
            key, value = key.strip(), value.strip()
            target = _ALIAS.get(key.lower()) or _ALIAS.get(key)
            if target and value:
                if target in ("ep_start", "ep_end", "season", "schedule_interval"):
                    num = _num(value)
                    if num is not None:
                        out[target] = num
                elif target == "sites":
                    out["sites"].extend(w for w in (_norm(x) for x in re.split(r"[,、/+]", value)) if w)
                elif target == "schedule_time":
                    out["schedule_time"] = value
                    out["schedule_mode"] = "daily"
                elif target == "kind":
                    out["kind"] = "movie" if value in ("movie", "电影", "影") else "series"
                continue

        if tok in _NOW_WORDS:
            out["run_now"] = True
            continue

        # 每隔 N 分钟 / 每小时
        m = re.fullmatch(r"每\s*(\d{1,4})\s*(分钟|分|min|m|小时|h)", tok, re.I)
        if m:
            num = int(m.group(1))
            out["schedule_interval"] = num * 60 if m.group(2).lower() in ("小时", "h") else num
            out["schedule_mode"] = "interval"
            continue

        # 每周三 21:30 / 每周三21:30 / 每周三晚9点
        m = re.fullmatch(r"每(?:周|星期)([一二三四五六日天])\s*(?:晚|早上|上午|下午)?\s*"
                         r"(\d{1,2})(?::(\d{1,2}))?(?:点|时)?", tok)
        if m:
            out["schedule_mode"] = "weekly"
            out["schedule_weekday"] = _WEEK.get(m.group(1), "mon")
            hour = int(m.group(2))
            if "下午" in tok and hour < 12:
                hour += 12
            out["schedule_time"] = f"{hour:02d}:{int(m.group(3) or 0):02d}"
            continue

        # 每天 3:00 / 每天3点 / 3:00
        m = re.fullmatch(r"每?天?\s*(\d{1,2})(?::(\d{1,2}))?(?:点|时|:00)?", tok)
        if m and (":" in tok or "点" in tok or tok.startswith("每天")):
            hour = int(m.group(1))
            if 0 <= hour <= 23:
                out["schedule_mode"] = "daily"
                out["schedule_time"] = f"{hour:02d}:{int(m.group(2) or 0):02d}"
                continue

        # 从3集 / 第3集 / 从第3集开始 / 5-8集 / 从1到3集
        m = re.fullmatch(r"(?:从|第)?\s*第?\s*(\d{1,3})\s*[-~—到]\s*(\d{1,3})\s*[集话話]?", tok)
        if m:
            out["ep_start"], out["ep_end"] = int(m.group(1)), int(m.group(2))
            continue
        m = re.fullmatch(r"(?:从|第)?\s*第?\s*(\d{1,3})\s*[集话話](?:开始|起|走)?", tok)
        if m:
            out["ep_start"] = int(m.group(1))
            continue
        # 从5 / 从第5（省略「集」字）
        m = re.fullmatch(r"(?:从|第)\s*第?\s*(\d{1,3})", tok)
        if m:
            out["ep_start"] = int(m.group(1))
            continue

        # 站点馒头 / 站馒头,岛 / 用馒头 / 站点（单独出现=想看站点清单）
        m = re.fullmatch(r"(?:站点|站|用|在)\s*[:]?\s*(.*)", tok)
        if m:
            rest = m.group(1).strip()
            if not rest:
                out["list_sites"] = True
                continue
            if rest in ("全部", "所有", "全站"):
                out["sites"] = []
                continue
            out["sites"].extend(w for w in (_norm(x) for x in re.split(r"[,、/+和]", rest)) if w)
            continue

        if tok in ("电影", "movie", "影"):
            out["kind"] = "movie"
            continue
        if tok in ("剧集", "剧", "series", "tv"):
            out["kind"] = "series"
            continue
        if re.fullmatch(r"\d{1,4}", tok):
            # 开头的裸数字当「选第几个」；出现在片名后面就当清晰度忽略（1080 之类）
            if not name_parts and out.get("pick") is None:
                out["pick"] = int(tok)
                out["pick_end"] = int(tok)
            continue
        name_parts.append(tok)

    out["name"] = _clean(" ".join(name_parts))
    return out


def parse_episodes(addr: str) -> tuple[int | None, int | None]:
    """「1」「3-5」→ (起始, 结束)；不是这种形态返回 (None, None)。"""
    hit = _RANGE_RE.match(_clean(addr))
    if not hit:
        return None, None
    start = int(hit.group(1))
    end = int(hit.group(2)) if hit.group(2) else start
    if start > end:
        start, end = end, start
    return start, end


def pick_rows(addr: str, total: int, *, multi: bool = False) -> list[int] | None:
    """把用户的回复翻成序号列表（1 起）。

    multi=False 只接受单个序号（选订阅候选）；multi=True 支持「1,3」「1-4」（挑资源）。
    非法 / 越界返回 None，由调用方决定怎么提示。
    """
    text = _clean(addr)
    if not text or total <= 0:
        return None
    nums: list[int] = []
    for piece in re.split(r"[,、/+\s]+", text):
        if not piece:
            continue
        start, end = parse_episodes(piece)
        if start is None:
            return None
        if not multi and start != end:
            return None
        for n in range(start, end + 1):
            if n < 1 or n > total:
                return None
            if n not in nums:
                nums.append(n)
    return nums or None


def _short(text: str, limit: int = 70) -> str:
    """简介压成一行：去换行、超长截断加省略号。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if not s:
        return ""
    s = re.sub(r"^(?:又名|别名)[:：]?", "", s)
    return s if len(s) <= limit else s[: limit - 1].rstrip("，,。.、 ") + "…"


def render_candidates(
    query: str,
    cands: Iterable[dict],
    *,
    limit: int = MAX_CANDIDATES,
    with_hint: bool = True,
) -> str:
    """列候选：每部两行（年份 · 剧集/电影 · 集数，加一行简介），末尾给下一步指令。"""
    items = list(cands)[:limit]
    lines = [f"「{query}」找到 {len(items)} 个同名作品，回序号选："]
    for i, c in enumerate(items, 1):
        kind = "剧集" if str(c.get("kind")) == "series" else "电影"
        bits = [str(c.get("year") or "年份未知"), kind]
        if c.get("is_anim"):
            bits.append("动画")
        eps = int(c.get("eps") or c.get("total_eps") or 0)
        if str(c.get("kind")) != "movie" and eps:
            bits.append(f"{eps}集")
        title = c.get("title") or c.get("meta_title") or ""
        lines.append(f"{i}. {title}（{' · '.join(bits)}）")
        brief = _short(str(c.get("overview") or ""))
        if brief:
            lines.append(f"   {brief}")
    if with_hint:
        lines.append("回复序号直接订阅（如「1」）；带参数改：「1 从5集 站点馒头 立即」。")
    return "\n".join(lines)


def render_resources(
    query: str,
    rows: Iterable[dict],
    *,
    limit: int = MAX_RESOURCES,
    show_site: bool = True,
) -> str:
    """列资源：序号 + 站点/清晰度/集数/体积/做种，一行一条，方便回「1-3」直接下。"""
    all_rows = list(rows)
    items = all_rows[:limit]
    if not items:
        return (f"「{query}」没搜到符合条件的资源。\n"
                "换个片名重试，或去插件页把规则放宽（清晰度/体积/做种）。")
    lines = [f"「{query}」找到 {len(items)} 条资源，回序号直接下："]
    for i, r in enumerate(items, 1):
        bits: list[str] = []
        if show_site and (r.get("site") or r.get("domain")):
            bits.append(str(r.get("site") or r.get("domain")))
        res = r.get("resolution") or ""
        if res:
            bits.append(str(res))
        ep = r.get("episode")
        if ep:
            bits.append(f"第{ep}集")
        size = r.get("size_gb")
        if size:
            try:
                bits.append(f"{float(size):.1f}G")
            except (TypeError, ValueError):
                pass
        seed = r.get("seeders")
        if seed is not None:
            bits.append(f"{seed}种")
        if r.get("free") or "FREE" in (r.get("badges") or []):
            bits.append("免费")
        title = str(r.get("display_title") or r.get("title") or "")
        head = f"{i}. [{' · '.join(bits)}] " if bits else f"{i}. "
        lines.append(head + _short(title, 58))
    if len(all_rows) > len(items):
        lines.append(f"（只列前 {len(items)} 条，共 {len(all_rows)} 条）")
    lines.append("回「1」下第 1 条；多条回「1-3」或「1,3」；回「订阅」改成定时订阅。")
    return "\n".join(lines)


def render_sub_created(sub: dict, **extra: Any) -> str:
    """订阅成功回执：认准的作品 + 站点 + 执行计划 + 下一步。"""
    name = sub.get("name") or ""
    kind = "剧集" if str(sub.get("kind") or "series") == "series" else "电影"
    bits: list[str] = []
    if sub.get("year"):
        bits.append(str(sub["year"]))
    bits.append(kind)
    if kind == "剧集":
        start, end = sub.get("ep_start"), sub.get("ep_end")
        if start and end and start != end:
            bits.append(f"第{start}-{end}集")
        elif start:
            bits.append(f"从第{start}集起")
        else:
            bits.append("从第1集起")
    sites = extra.get("site_names") or ""
    if not sites:
        n = len(sub.get("site_ids") or [])
        sites = f"{n} 个站点" if n else "未选站点"
    lines = [f"已订阅：{name}（{' · '.join(bits)}）", f"站点：{sites}"]
    plan = extra.get("plan") or ""
    if plan:
        lines.append(f"计划：{plan}")
    if extra.get("ran"):
        lines.append(str(extra["ran"]))
    lines.append("要改集数/站点/时间去插件页；发「/ptlist」看全部订阅。")
    return "\n".join(lines)


def render_help() -> str:
    """第一次用或发错时的用法速查（尽量短）。"""
    return "\n".join([
        "【定时订阅下载】",
        "/ptsub 片名 —— 查同名作品，回序号建订阅",
        "/ptsub 片名 从5集 站点馒头 立即 —— 参数一次带全",
        "/ptfind 片名 —— 只找资源直接下，不订阅",
        "/ptfind 片名 5-8集 —— 只找这几集",
        "回「1」或「1-3」选序号；/ptlist 看订阅；/ptsub help 看这条",
    ])


def render_sub_list(subs: Iterable[dict], *, limit: int = 10) -> str:
    """我的订阅：一行一条，带进度，方便直接看状态。"""
    items = list(subs)
    if not items:
        return "还没有订阅。发「/ptsub 片名」开始。"
    lines = [f"共 {len(items)} 条订阅："]
    for i, s in enumerate(items[:limit], 1):
        name = s.get("name") or ""
        kind = "剧集" if str(s.get("kind") or "series") == "series" else "电影"
        flag = "" if s.get("enabled") else "（停用）"
        done = s.get("done_eps")
        total = s.get("total_eps") or 0
        progress = ""
        if kind == "剧集" and total:
            progress = f" {int(done or 0)}/{int(total)}集"
        elif kind == "剧集" and done:
            progress = f" 已下{int(done)}集"
        lines.append(f"{i}. {name}{flag} · {kind}{progress}")
    if len(items) > limit:
        lines.append(f"…还有 {len(items) - limit} 条，去插件页看全部。")
    return "\n".join(lines)


_WEEK = {"一": "mon", "二": "tue", "三": "wed", "四": "thu",
         "五": "fri", "六": "sat", "日": "sun", "天": "sun"}
