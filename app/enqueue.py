"""Turning "queue this series / season / movie / album" into one job per file.

Expansion happens at queue time, so every job carries its own fixed target
path and size; changing the destination picker later never moves a job.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from app import connection, db, jellyfin, naming

EPISODE_FIELDS = "Path,MediaSources"


@dataclass
class Plan:
    jobs: list[dict] = field(default_factory=list)
    unsafe: int = 0            # files whose name would land outside the root
    unavailable: int = 0       # listed by the source but with no file (missing episodes)

    @property
    def bytes(self) -> int:
        return sum(j["size"] or 0 for j in self.jobs)

    @property
    def main_jobs(self) -> list[dict]:
        return [j for j in self.jobs if j["kind"] != "subtitle"]

    @property
    def subtitle_jobs(self) -> list[dict]:
        return [j for j in self.jobs if j["kind"] == "subtitle"]


def include_specials() -> bool:
    return db.get_setting("include_specials", "0") == "1"


async def _get(path: str, **params) -> dict:
    r = await connection.request("GET", path, params=params or None)
    if not r.is_success:
        raise jellyfin.SourceError(f"The source server answered HTTP {r.status_code}.")
    return r.json()


def _media(item: dict) -> tuple[int | None, str]:
    """(size, extension) of the file /Items/{id}/Download will send: the
    first media source, which is what Jellyfin serves for that call."""
    sources = item.get("MediaSources") or []
    first = sources[0] if sources else {}
    size = first.get("Size")
    ext = naming.pick_ext(first.get("Path") or item.get("Path"), first.get("Container") or item.get("Container"))
    return (int(size) if size else None), ext


def _has_file(item: dict) -> bool:
    # "Virtual" items are episodes Jellyfin knows about from metadata but
    # has no file for ("missing episodes").
    return item.get("LocationType") != "Virtual" and bool(item.get("MediaSources") or item.get("Path"))


def _job(plan: Plan, *, server_id: str, root: str, item: dict, kind: str, name: str, rel) -> None:
    if not _has_file(item):
        plan.unavailable += 1
        return
    try:
        target = naming.resolve_inside(root, rel)
    except naming.UnsafePath:
        plan.unsafe += 1
        return
    size, _ = _media(item)
    plan.jobs.append({"server_id": server_id, "source_item_id": item["Id"], "kind": kind,
                      "display_name": name, "dest_root": root, "target_path": target,
                      "download_path": f"/Items/{item['Id']}/Download", "size": size})
    if kind in ("episode", "movie") and include_subtitles():
        _subtitle_jobs(plan, server_id=server_id, root=root, item=item, name=name, video_target=target)


# Text formats only: Jellyfin serves these as files. Image-based subtitles
# (PGS, VobSub) can't be fetched through the stream endpoint as a file.
TEXT_SUBTITLES = {"srt", "subrip", "ass", "ssa", "vtt", "webvtt"}


def include_subtitles() -> bool:
    return db.get_setting("subtitles", "1") == "1"


def _subtitle_jobs(plan: Plan, *, server_id: str, root: str, item: dict, name: str, video_target: str) -> None:
    """External subtitle files sit next to the video on the source, not
    inside it, so /Items/{id}/Download doesn't bring them along."""
    sources = item.get("MediaSources") or []
    if not sources:
        return
    source = sources[0]
    subs = [s for s in source.get("MediaStreams") or []
            if s.get("Type") == "Subtitle" and s.get("IsExternal")
            and (s.get("Codec") or "").lower() in TEXT_SUBTITLES]
    per_language = Counter((s.get("Language") or "und").lower() for s in subs)
    for s in subs:
        lang = (s.get("Language") or "und").lower()
        ext = naming.SUBTITLE_EXT[(s.get("Codec") or "").lower()]
        # Two subtitles in one language need telling apart in the file name.
        index = s.get("Index") if per_language[lang] > 1 else None
        target = naming.subtitle_path(video_target, lang, s.get("Codec"), bool(s.get("IsForced")), index)
        try:
            naming.check_inside(root, target)
        except naming.UnsafePath:
            plan.unsafe += 1
            continue
        plan.jobs.append({
            "server_id": server_id,
            # One item can carry several subtitles; each needs its own
            # identity for the duplicate check.
            "source_item_id": f"{item['Id']}:{s.get('Index')}",
            "kind": "subtitle", "display_name": f"{name} (subtitles, {lang})",
            "dest_root": root, "target_path": target,
            "download_path": f"/Videos/{item['Id']}/{source.get('Id') or item['Id']}"
                             f"/Subtitles/{s.get('Index')}/Stream.{ext}",
            "size": None})


# ---- series ----------------------------------------------------------------------

async def series_episodes(series_id: str) -> tuple[dict, list[dict]]:
    conn = connection.current()
    series = await _get(f"/Items/{series_id}", Fields="ProductionYear")
    body = await _get(f"/Shows/{series_id}/Episodes", userId=conn["user_id"], Fields=EPISODE_FIELDS)
    return series, body.get("Items", [])


async def seasons(series_id: str) -> list[dict]:
    """For the Manual picker. Seasons don't report an episode count on 12.x
    (ChildCount is null), so the episodes are fetched once and grouped."""
    conn = connection.current()
    body = await _get(f"/Shows/{series_id}/Seasons", userId=conn["user_id"])
    _, episodes = await series_episodes(series_id)
    out = []
    for s in body.get("Items", []):
        eps = [e for e in episodes if e.get("SeasonId") == s["Id"] and _has_file(e)]
        out.append({"id": s["Id"], "name": s.get("Name") or f"Season {s.get('IndexNumber')}",
                    "index": s.get("IndexNumber"), "episodes": len(eps),
                    "bytes": sum(_media(e)[0] or 0 for e in eps)})
    return out


async def plan_series(series_id: str, root: str, which: str, season_ids: list[str] | None = None) -> Plan:
    """which: "all" (specials only if the setting is on), "first" (season 1),
    or "seasons" (exactly season_ids, specials included if ticked)."""
    server_id = connection.current()["server_id"]
    series, episodes = await series_episodes(series_id)
    name, year = series.get("Name") or "Unknown", series.get("ProductionYear")

    if which == "first":
        chosen = [e for e in episodes if e.get("ParentIndexNumber") == 1]
        if not chosen:
            raise jellyfin.SourceError("This series has no season 1 on the source server.")
    elif which == "seasons":
        wanted = set(season_ids or [])
        chosen = [e for e in episodes if e.get("SeasonId") in wanted]
    else:
        specials = include_specials()
        chosen = [e for e in episodes if specials or (e.get("ParentIndexNumber") or 0) != 0]

    plan = Plan()
    for e in chosen:
        season, number = e.get("ParentIndexNumber"), e.get("IndexNumber")
        _, ext = _media(e)
        rel = naming.episode_relpath(name, year, season, number, e.get("IndexNumberEnd"), e.get("Name"), ext)
        label = f"{name} - S{season or 0:02d}E{number or 0:02d}" + (f" - {e['Name']}" if e.get("Name") else "")
        _job(plan, server_id=server_id, root=root, item=e, kind="episode", name=label, rel=rel)
    return plan


# ---- movie and album ----------------------------------------------------------

async def plan_movie(movie_id: str, root: str) -> Plan:
    server_id = connection.current()["server_id"]
    movie = await _get(f"/Items/{movie_id}", Fields="Path,MediaSources,ProductionYear")
    _, ext = _media(movie)
    name, year = movie.get("Name") or "Unknown", movie.get("ProductionYear")
    plan = Plan()
    _job(plan, server_id=server_id, root=root, item=movie, kind="movie",
         name=f"{name} ({year})" if year else name, rel=naming.movie_relpath(name, year, ext))
    return plan


async def plan_album(album_id: str, root: str) -> Plan:
    server_id = connection.current()["server_id"]
    album = await _get(f"/Items/{album_id}")
    body = await _get("/Items", ParentId=album_id, IncludeItemTypes="Audio", Recursive="true",
                      Fields=EPISODE_FIELDS, SortBy="ParentIndexNumber,IndexNumber")
    tracks = body.get("Items", [])
    multi_disc = len({t.get("ParentIndexNumber") for t in tracks if t.get("ParentIndexNumber")}) > 1
    album_artist = album.get("AlbumArtist") or (album.get("AlbumArtists") or [{}])[0].get("Name")
    plan = Plan()
    for t in tracks:
        _, ext = _media(t)
        rel = naming.track_relpath(t.get("AlbumArtist") or album_artist, t.get("Album") or album.get("Name"),
                                   t.get("ParentIndexNumber"), t.get("IndexNumber"), t.get("Name"),
                                   ext, multi_disc)
        _job(plan, server_id=server_id, root=root, item=t, kind="track",
             name=f"{album.get('Name') or 'Album'} - {t.get('Name') or 'Track'}", rel=rel)
    return plan
