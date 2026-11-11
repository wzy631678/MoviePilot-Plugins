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


_WEEK_CN = {"mon": "一", "tue": "二", "wed": "三", "thu": "四",
            "fri": "五", "sat": "六", "sun": "日"}

# 「就这样吧」这类确认词：用户不想再改参数时一句话建订阅
_CONFIRM_WORDS = ("确定", "确认", "就这样", "就这个", "默认", "都行", "随便", "可以",
                  "好了", "开始", "ok", "OK", "Ok", "yes", "嗯")


def is_confirm(text: str) -> bool:
    """判断这句话是不是「按当前设置建吧」。"""
    s = _clean(text).strip("。.!！ ")
    if not s:
        return False
    if s in _CONFIRM_WORDS:
        return True
    # 「就这样吧」「那就确定」「默认就行」这类也不刁难用户
    return any(w in s for w in ("就这样", "就按这个", "默认", "都行", "照这个", "开始吧"))


def schedule_text(opts: dict[str, Any] | None) -> str:
    """把计划字段翻成一句人话；没设就返回空串（由调用方补默认）。"""
    opts = dict(opts or {})
    mode = opts.get("schedule_mode")
    if mode == "interval":
        minutes = int(opts.get("schedule_interval") or 0)
        if minutes:
            return f"每 {minutes // 60} 小时" if minutes % 60 == 0 and minutes >= 60 else f"每 {minutes} 分钟"
    if mode == "weekly":
        day = _WEEK_CN.get(str(opts.get("schedule_weekday") or "mon"), "一")
        return f"每周{day} {opts.get('schedule_time') or '03:00'}"
    if mode == "daily" and opts.get("schedule_time"):
        return f"每天 {opts['schedule_time']}"
    return ""


def opts_summary(
    opts: dict[str, Any] | None,
    *,
    site_total: int = 0,
    plan_text: str = "",
) -> str:
    """一行摘要：集数范围 · 站点 · 刷新计划。"""
    opts = dict(opts or {})
    bits: list[str] = []
    start = int(opts.get("ep_start") or 0)
    end = int(opts.get("ep_end") or 0)
    if start and end and end != start:
        bits.append(f"只下第{start}-{end}集")
    elif start:
        bits.append(f"从第{start}集起")
    else:
        bits.append("从第1集起")
    if opts.get("site_names"):
        bits.append(str(opts["site_names"]))
    elif opts.get("site_ids"):
        bits.append(f"{len(opts['site_ids'])} 个站点")
    elif site_total:
        bits.append(f"全部 {site_total} 个可搜站")
    else:
        bits.append("全部可搜站")
    plan = schedule_text(opts) or plan_text
    if plan:
        bits.append(plan)
    if opts.get("run_now"):
        bits.append("建完立即搜一轮")
    return " · ".join(bits)


def _cand_title(cand: dict) -> str:
    """候选标题 + 年份 / 类型 / 集数，一行说清是哪一部。"""
    kind = "剧集" if str(cand.get("kind")) == "series" else "电影"
    bits = [str(cand.get("year") or "年份未知"), kind]
    eps = int(cand.get("eps") or 0)
    if kind == "剧集" and eps:
        bits.append(f"{eps}集")
    if cand.get("is_anim"):
        bits.append("动画")
    return f"{cand.get('title') or ''}（{' · '.join(bits)}）"


def render_pick_menu(
    cand: dict,
    *,
    opts: dict[str, Any] | None = None,
    site_total: int = 0,
    plan_text: str = "",
) -> str:
    """选完候选后的第二屏：让用户定集数 / 站点 / 刷新时间。"""
    return "\n".join([
        f"已选：{_cand_title(cand)}",
        "",
        f"当前：{opts_summary(opts, site_total=site_total, plan_text=plan_text)}",
        "",
        "要改就回一句，一次改一样（也可以一条全带）：",
        "· 集数：「从5集」或「5-8集」",
        "· 站点：「站点馒头」或「站点馒头,岛」",
        "· 刷新：「每天3:00」「每30分钟」「每周三21:30」，想先下一轮加「立即」",
        "",
        "不改就回「确定」，我按上面的设置建订阅并立刻补搜一轮。",
    ])


def render_opts_state(
    cand: dict,
    *,
    opts: dict[str, Any] | None = None,
    site_total: int = 0,
    plan_text: str = "",
) -> str:
    """改完一项后的第三屏：回显当前设置 + 提示怎么收尾。"""
    return "\n".join([
        f"已记下：{_cand_title(cand)}",
        f"当前：{opts_summary(opts, site_total=site_total, plan_text=plan_text)}",
        "回「确定」开始；还能接着改集数 / 站点 / 刷新时间。",
    ])


def render_no_choice(key: str = "sub") -> str:
    """没有进行中的选择时的提示。"""
    if key == "dl":
        return "没有待选资源。先发「找资源 片名」，我列资源给你选。"
    return "没有进行中的选择。先发「订阅 片名」，我列同名作品给你选。"


def render_intent_hint(kind: str) -> str:
    """用户只发了动词（订阅 / 找资源）没带片名时怎么用。"""
    if kind == "find":
        return ("用法：找资源 片名 —— 只搜一轮直接下，不建订阅。\n"
                "例：找资源 仙逆 / 找资源 仙逆 5-8集 / 找资源 仙逆 站点馒头")
    return ("用法：订阅 片名 —— 列同名作品让你选，选完再定集数/站点/刷新时间。\n"
            "例：订阅 天才女友 / 订阅 仙逆 从5集 站点馒头 每天3:00")


def split_intent(text: str) -> tuple[str, str]:
    """识别「订阅 X」「找资源 X」这类自然语言入口，返回 (类型, 片名)。

    类型：sub（建订阅）/ find（只找资源）/ 空串（不是本插件的入口）。
    用户在 QQ 里不带斜杠发消息时，宿主的原生交互链会抢先吃掉，
    插件要靠这个函数把「订阅 XXX」这类句子认出来并接管。
    """
    s = _clean(text).strip()
    if not s:
        return "", ""
    # 先去掉可能的命令前缀和礼貌词
    s = re.sub(r"^/\s*[A-Za-z]+\s*", "", s).strip()
    s = re.sub(r"^(?:帮我|帮忙|麻烦|请|给我|我要|我想|想)\s*", "", s).strip()
    for verbs, kind in (
        (("找资源", "找片", "搜资源", "找一下资源", "搜索资源"), "find"),
        (("直接下载", "直接下", "下载这个", "只下载"), "find"),
        (("订阅", "追剧", "追番", "追更", "添加订阅", "订上"), "sub"),
    ):
        for verb in verbs:
            if s.startswith(verb):
                rest = s[len(verb):].strip(" ,，、:：")
                return kind, rest
    return "", ""


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
        lines.append("回序号选（如「1」），选完再定：从第几集开始 / 用哪些站 / 多久刷一次。")
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
    lines.append("回「1」下第 1 条；多条回「1-3」或「1,3」；回「订阅」改成定时追更。")
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
        "直接说：订阅 天才女友 —— 不用打斜杠，回序号后选集数/站点/刷新时间",
        "只下一次：找资源 仙逆 —— 列资源后回 1 或 1-3 直接下，不建订阅",
        "带参数一条搞定：/ptsub 片名 从5集 站点馒头 每天3:00",
        "/ptlist 看订阅进度；/ptsub help 看这条",
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
