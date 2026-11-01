"""Reading the source server's libraries and items for the browse page."""
from __future__ import annotations

import asyncio
import re

from fastapi import Response

from app import connection, db, jellyfin

# Jellyfin ids are 32 hex chars. Checked before they go into a source URL
# path, so a crafted id can't steer the request somewhere else.
ID_RE = re.compile(r"^[0-9a-f]{32}$")


def valid_id(value: str) -> bool:
    return bool(ID_RE.match(value or ""))

# What a browsable "item" is in each library. Episodes and tracks are reached
# through their series or album, never listed directly.
ITEM_TYPES = "Series,Movie,MusicAlbum"
PAGE_SIZE = 60


def _check(r):
    if not r.is_success:
        raise jellyfin.SourceError(f"The source server answered HTTP {r.status_code}.")
    return r.json()


async def _views() -> list[dict]:
    conn = db.get_connection()
    user_id = conn["user_id"] if conn else ""
    views = _check(await connection.request("GET", f"/UserViews?userId={user_id}"))["Items"]
    # Only real media libraries; "playlists", "livetv" and the like have
    # nothing to download.
    return [{"id": v["Id"], "name": v.get("Name") or "Library", "type": v.get("CollectionType"),
             "has_image": bool((v.get("ImageTags") or {}).get("Primary"))}
            for v in views
            if v.get("Type") == "CollectionFolder"
            and (v.get("CollectionType") or "") not in ("playlists", "livetv", "boxsets")]


async def libraries() -> list[dict]:
    views = await _views()

    async def count(view):
        body = _check(await connection.request(
            "GET", f"/Items?ParentId={view['id']}&IncludeItemTypes={ITEM_TYPES}"
                   "&Recursive=true&Limit=0"))
        return body.get("TotalRecordCount", 0)

    counts = await asyncio.gather(*(count(v) for v in views))
    return [{**v, "count": n} for v, n in zip(views, counts)]


async def library(library_id: str) -> dict | None:
    return next((v for v in await _views() if v["id"] == library_id), None)


async def items(library_id: str, start: int = 0, search: str = "", letter: str = "") -> dict:
    params = {
        "ParentId": library_id,
        "IncludeItemTypes": ITEM_TYPES,
        "Recursive": "true",
        "Fields": "ProductionYear,ChildCount",
        "SortBy": "SortName",
        "SortOrder": "Ascending",
        "StartIndex": str(max(start, 0)),
        "Limit": str(PAGE_SIZE),
        "EnableImageTypes": "Primary",
        "ImageTypeLimit": "1",
    }
    if search:
        params["SearchTerm"] = search
    if letter == "#":
        # Titles that sort before "A": digits and symbols.
        params["NameLessThan"] = "A"
    elif letter:
        params["NameStartsWith"] = letter
    body = _check(await connection.request("GET", "/Items", params=params))
    return {
        "total": body.get("TotalRecordCount", 0),
        "start": start,
        "items": [{"id": i["Id"], "name": i.get("Name") or "?", "type": i.get("Type"),
                   "year": i.get("ProductionYear"), "child_count": i.get("ChildCount"),
                   "has_image": bool((i.get("ImageTags") or {}).get("Primary"))}
                  for i in body.get("Items", [])],
    }


async def poster(item_id: str, height: int = 300) -> Response:
    r = await connection.request("GET", f"/Items/{item_id}/Images/Primary",
                                 params={"maxHeight": str(height), "quality": "85"})
    if not r.is_success:
        return Response(status_code=404)
    # Jellyfin sends Content-Disposition: attachment on images, which can make
    # a browser download the poster instead of showing it - so only the type
    # is passed on. Private caching: posters sit behind the app login.
    return Response(r.content, media_type=r.headers.get("content-type", "image/jpeg"),
                    headers={"Cache-Control": "private, max-age=86400"})
