"""What's already on your Jellyfin, for the "✔ On my server" badges.

Items are matched by provider ids first (IMDb, TMDb, TVDB, MusicBrainz),
which survive different titles and languages, then by title + year. Keys
include the item type: TMDb numbers movies and shows separately, so the same
number can mean two different things.
"""
from __future__ import annotations

import re
import time
import unicodedata

import httpx

from app import config, db, jellyfin

CACHE_SECONDS = 300
PROVIDERS = ("imdb", "tmdb", "tvdb", "musicbrainzalbum", "musicbrainzreleasegroup")

_cache: tuple[float, set[str]] | None = None


def _norm(title: str) -> str:
    title = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode().lower()
    title = re.sub(r"^(the|a|an) ", "", title)
    return re.sub(r"[^a-z0-9]+", "", title)


def keys(item: dict) -> set[str]:
    kind = item.get("Type") or ""
    out = set()
    for provider, value in (item.get("ProviderIds") or {}).items():
        if provider.lower() in PROVIDERS and value:
            out.add(f"{kind}:{provider.lower()}:{str(value).lower()}")
    name = _norm(item.get("Name") or "")
    if name:
        out.add(f"{kind}:name:{name}:{item.get('ProductionYear') or ''}")
    return out


async def _fetch() -> set[str]:
    headers = {"Authorization": jellyfin.auth_header(db.device_id(), config.DEST_API_KEY)}
    async with httpx.AsyncClient(base_url=config.DEST_URL, timeout=httpx.Timeout(60.0, connect=10.0),
                                 headers=headers, follow_redirects=True) as c:
        r = await c.get("/Items", params={"Recursive": "true", "IncludeItemTypes": "Series,Movie,MusicAlbum",
                                          "Fields": "ProviderIds,ProductionYear",
                                          "EnableImages": "false", "EnableUserData": "false"})
    r.raise_for_status()
    index: set[str] = set()
    for item in r.json().get("Items", []):
        index |= keys(item)
    return index


async def index() -> set[str]:
    """Empty when there's no API key or your Jellyfin can't be reached: the
    badges are a convenience, never a reason for the browse page to fail."""
    global _cache
    if not (config.DEST_URL and config.DEST_API_KEY):
        return set()
    if _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
        return _cache[1]
    try:
        found = await _fetch()
    except (httpx.HTTPError, ValueError):
        found = _cache[1] if _cache else set()
    _cache = (time.monotonic(), found)
    return found


def forget() -> None:
    """Drop the cache, so the next browse page asks your Jellyfin again."""
    global _cache
    _cache = None


def on_server(item: dict, idx: set[str]) -> bool:
    return bool(idx) and not keys(item).isdisjoint(idx)
