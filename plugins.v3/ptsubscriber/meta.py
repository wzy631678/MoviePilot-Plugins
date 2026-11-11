"""TMDB 元数据：海报、年份、总集数。

只做「查一下、拿回来」这一件事，任何一步失败都返回空字典，
绝不影响订阅本身的保存和补搜——没有海报照样能跑。
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("PtSubscriber")

# TMDB 图片地址前缀；w342 足够卡片清晰度，又不至于太大
IMAGE_BASE = "https://image.tmdb.org/t/p/w500"

# 落库的元数据字段（upsert 时沿用旧值，避免编辑一次就丢海报）
META_KEYS = ("tmdb_id", "meta_title", "year", "poster_path",
             "total_eps", "season_eps", "seasons", "meta_at")


def clean_seasons(raw: Any) -> list[dict]:
    """TMDB 的 seasons → [{'season': 1, 'eps': 200}]，跳过特典季（第 0 季）。"""
    out: list[dict] = []
    for item in raw or []:
        try:
            num = int(item.get("season_number"))
            eps = int(item.get("episode_count") or 0)
        except (AttributeError, TypeError, ValueError):
            continue
        if num <= 0 or eps <= 0:
            continue
        out.append({"season": num, "eps": eps})
    return sorted(out, key=lambda x: x["season"])


def pick_total(seasons: list[dict], season: int | None = None) -> int:
    """要补的总集数：优先那一季；只有一季正片就直接用；否则整部加起来。

    仙逆这种「一季 200 集」的，卡片上就显示 166/200，和 MP 订阅页一致。
    """
    if not seasons:
        return 0
    if season:
        for item in seasons:
            if item["season"] == int(season):
                return int(item["eps"])
        return sum(int(x["eps"]) for x in seasons)
    if len(seasons) == 1:
        return int(seasons[0]["eps"])
    return sum(int(x["eps"]) for x in seasons)


def poster_url(path: str | None) -> str:
    text = str(path or "").strip()
    if not text.startswith("/"):
        return ""
    return IMAGE_BASE + text


def genre_info(data: Any) -> tuple[list[str], bool]:
    """从 TMDB 详情里取题材名，并判断是不是动画（类型 16）。

    同名作品最容易混的就是「动漫版」和「真人版」，例如《凡人修仙传》2020 是动画、
    2025 是真人剧，光看年份和集数不好分，标出动画/真人一眼就能选对。
    """
    names: list[str] = []
    anim = False
    for item in (data or {}).get("genres") or []:
        try:
            gid = int((item or {}).get("id") or 0)
        except (AttributeError, TypeError, ValueError):
            gid = 0
        name = str((item or {}).get("name") or "").strip()
        if name:
            names.append(name)
        if gid == 16:
            anim = True
    return names, anim


def resolve(name: str, kind: str = "series", season: int | None = None,
            tmdb_id: int | None = None) -> dict:
    """按剧名去 TMDB 找条目，返回海报/年份/总集数等元数据。

    tmdb_id 有值时直接按 ID 取详情——用户在前端点选过同名候选，不能再按名字猜。
    在 MP 进程内才可用（依赖 app.modules.themoviedb）；单独跑单测时直接返回空。
    """
    title = str(name or "").strip()
    if not tmdb_id and not title:
        return {}
    if tmdb_id:
        try:
            meta = _details(int(tmdb_id), kind, season)
        except Exception as exc:
            logger.warning(f"[PtSubscriber] TMDB 详情失败：{type(exc).__name__}: {str(exc)[:120]}")
            return {}
        if not meta:
            return {}
        meta["tmdb_id"] = int(tmdb_id)
        return meta
    try:
        hit = _search(title, kind)
    except Exception as exc:  # 宿主没装 TMDB / 网络不通 / 接口变更
        logger.warning(f"[PtSubscriber] TMDB 匹配失败：{type(exc).__name__}: {str(exc)[:120]}")
        return {}
    tmdb_id = hit.get("id")
    if not tmdb_id:
        return {}
    try:
        meta = _details(int(tmdb_id), kind, season)
    except Exception as exc:
        logger.warning(f"[PtSubscriber] TMDB 详情失败：{type(exc).__name__}: {str(exc)[:120]}")
        return {}
    if not meta:
        return {}
    meta["tmdb_id"] = int(tmdb_id)
    return meta


def search(name: str, kind: str = "series", limit: int = 4) -> list[dict]:
    """按名字列出同名候选（含年份、动画/真人、季数、总集数），给前端点选用。

    同名作品很容易认错（《天下第一》既有 2005 剧集也有 2021 电影，
    《凡人修仙传》2020 是动画、2025 是真人剧），所以把候选都摆出来，
    用户点哪个就用哪个的 TMDB ID。
    """
    title = str(name or "").strip()
    if len(title) < 1:
        return []
    try:
        from app.modules.themoviedb.tmdbapi import TmdbApi

        api = TmdbApi()
    except Exception as exc:
        logger.warning(f"[PtSubscriber] TMDB 不可用：{type(exc).__name__}: {str(exc)[:120]}")
        return []
    try:
        tv_rows = api.search_tvs(title, "") or []
    except Exception:
        tv_rows = []
    try:
        movie_rows = api.search_movies(title, "") or []
    except Exception:
        movie_rows = []
    order = (("tv", tv_rows), ("movie", movie_rows))
    if str(kind) == "movie":
        order = (("movie", movie_rows), ("tv", tv_rows))
    out: list[dict] = []
    for mtype, rows in order:
        for row in list(rows)[:limit]:
            tid = row.get("id")
            if not tid or any(x["tmdb_id"] == int(tid) for x in out):
                continue
            try:
                detail = _tv_details(int(tid), None) if mtype == "tv" else _movie_details(int(tid))
            except Exception:
                continue
            seasons = detail.get("seasons") or []
            out.append({
                "tmdb_id": int(tid),
                "kind": "series" if mtype == "tv" else "movie",
                "meta_title": detail.get("meta_title") or "",
                "year": detail.get("year") or "",
                "poster_path": detail.get("poster_path") or "",
                "poster_url": poster_url(detail.get("poster_path")),
                "total_eps": int(detail.get("total_eps") or 0),
                "season_count": len(seasons),
                "seasons": seasons,
                "genres": detail.get("genres") or [],
                "is_anim": bool(detail.get("is_anim")),
            })
    return out


def _search(title: str, kind: str) -> dict:
    """按订阅类型精确搜：剧集只搜电视剧，电影只搜电影。

    match_multi 是「电影+剧集混搜」，同名作品时会串（例如《天下第一》会被
    匹配成 2021 年的同名电影），所以先按类型搜，搜不到再退回混搜。
    """
    from app.modules.themoviedb.tmdbapi import TmdbApi

    api = TmdbApi()
    try:
        rows = (api.search_movies(title, "") if str(kind) == "movie"
                else api.search_tvs(title, "")) or []
    except Exception:
        rows = []
    if rows:
        return rows[0] or {}
    return api.match_multi(title) or {}


def _details(tmdb_id: int, kind: str, season: int | None) -> dict:
    """取详情；电影只关心海报和年份，剧集还要算出总集数。"""
    if str(kind) == "movie":
        return _movie_details(tmdb_id)
    try:
        return _tv_details(tmdb_id, season)
    except Exception:
        # 名字可能是电影：退一步按电影查，总比什么都不显示好
        try:
            return _movie_details(tmdb_id)
        except Exception:
            return {}


def _movie_details(tmdb_id: int) -> dict:
    from app.modules.themoviedb.tmdbv3api import Movie

    data = Movie().details(tmdb_id) or {}
    genres, anim = genre_info(data)
    return {
        "meta_title": data.get("title") or data.get("original_title") or "",
        "year": str(data.get("release_date") or "")[:4],
        "poster_path": data.get("poster_path") or "",
        "total_eps": 1,
        "season_eps": 1,
        "seasons": [],
        "genres": genres,
        "is_anim": anim,
    }


def _tv_details(tmdb_id: int, season: int | None) -> dict:
    from app.modules.themoviedb.tmdbv3api import TV

    data = TV().details(tmdb_id) or {}
    seasons = clean_seasons(data.get("seasons"))
    total = pick_total(seasons, season)
    genres, anim = genre_info(data)
    return {
        "meta_title": data.get("name") or data.get("original_name") or "",
        "year": str(data.get("first_air_date") or "")[:4],
        "poster_path": data.get("poster_path") or "",
        "total_eps": total or int(data.get("number_of_episodes") or 0),
        "season_eps": total or int(data.get("number_of_episodes") or 0),
        "seasons": seasons,
        "genres": genres,
        "is_anim": anim,
    }
