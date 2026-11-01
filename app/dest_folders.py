"""Where downloads may go: the destination folders and whether we can write them.

Found in this order, first one that yields anything wins:
  1. "api"    - your Jellyfin's libraries (/Library/VirtualFolders), so the
                options are exactly the folders Jellyfin already watches;
  2. "env"    - J2J_DEST_FOLDERS, for people without an API key;
  3. "mounts" - top-level directories mounted into this container.

Jellyfin reports library paths as seen inside *its* container, which is why
the compose rule is to mount media at the same container paths in both.
"""
from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import httpx

from app import config, db, jellyfin

# Library types that aren't media folders: Jellyfin keeps them under its own
# config dir, which is a real absolute path, so they must be skipped by type.
SKIP_TYPES = {"boxsets", "playlists"}

# Top-level paths that are ours or the system's, never a media mount.
NOT_MEDIA = {"/data", "/vendor", "/srv", "/proc", "/sys", "/dev", "/etc", "/tmp",
             "/run", "/usr", "/var", "/bin", "/sbin", "/lib", "/lib64", "/opt",
             "/home", "/root", "/boot", "/mnt", "/media", "/snap"}

CACHE_SECONDS = 300


@dataclass
class Folder:
    path: str
    library: str               # label: the Jellyfin library name, or the type for env/mounts
    collection_type: str | None  # None = fits any source library
    available: bool = True
    reason: str = ""


@dataclass
class Detection:
    method: str                # "api" | "env" | "mounts"
    folders: list[Folder]
    warning: str = ""


_cache: tuple[float, Detection] | None = None


def check_writable(path: str) -> tuple[bool, str]:
    p = Path(path)
    if not p.is_dir():
        return False, "not mounted in Jelly2JellyCopy"
    if not os.access(p, os.W_OK | os.X_OK):
        return False, "read-only"
    return True, ""


def _from_api_payload(payload: list[dict]) -> list[Folder]:
    folders = []
    for lib in payload:
        ctype = (lib.get("CollectionType") or "").lower() or None
        if ctype in SKIP_TYPES:
            continue
        for loc in lib.get("Locations") or []:
            if loc.startswith("/"):
                folders.append(Folder(path=loc.rstrip("/") or "/", library=lib.get("Name") or loc,
                                      collection_type=ctype))
    return folders


async def _from_api() -> list[Folder]:
    headers = {"Authorization": jellyfin.auth_header(db.device_id(), config.DEST_API_KEY)}
    async with httpx.AsyncClient(base_url=config.DEST_URL, timeout=jellyfin.TIMEOUT,
                                 headers=headers, follow_redirects=True) as c:
        r = await c.get("/Library/VirtualFolders")
    if r.status_code in (401, 403):
        raise jellyfin.SourceError("Your Jellyfin refused the API key (J2J_DEST_API_KEY).")
    r.raise_for_status()
    return _from_api_payload(r.json())


def parse_env(spec: str) -> list[Folder]:
    """`movies=/movies,/kids-movies;tvshows=/tv;music=/music`"""
    folders = []
    for group in spec.split(";"):
        if "=" not in group:
            continue
        ctype, paths = group.split("=", 1)
        ctype = ctype.strip().lower() or None
        for path in paths.split(","):
            path = path.strip().rstrip("/")
            if path.startswith("/"):
                folders.append(Folder(path=path, library=ctype or "any", collection_type=ctype))
    return folders


def from_mounts(root: str = "/") -> list[Folder]:
    folders = []
    for entry in sorted(Path(root).iterdir()):
        path = str(entry)
        if path in NOT_MEDIA or not entry.is_dir() or not os.path.ismount(entry):
            continue
        folders.append(Folder(path=path, library=entry.name, collection_type=None))
    return folders


async def detect(force: bool = False) -> Detection:
    global _cache
    if not force and _cache and time.monotonic() - _cache[0] < CACHE_SECONDS:
        return _cache[1]

    warning = ""
    result: Detection | None = None
    if config.DEST_URL and config.DEST_API_KEY:
        try:
            folders = await _from_api()
            if folders:
                result = Detection("api", folders)
        except jellyfin.SourceError as e:
            warning = e.message
        except (httpx.HTTPError, ValueError) as e:
            warning = f"Couldn't read your Jellyfin's libraries ({type(e).__name__})."
    if result is None and config.DEST_FOLDERS:
        folders = parse_env(config.DEST_FOLDERS)
        if folders:
            result = Detection("env", folders, warning)
    if result is None:
        result = Detection("mounts", from_mounts(), warning)

    # Jellyfin returns libraries in its own internal order; people scan the
    # picker by name.
    result.folders.sort(key=lambda f: (f.library.lower(), f.path))
    for f in result.folders:
        f.available, f.reason = check_writable(f.path)
    _cache = (time.monotonic(), result)
    return result


def offered(detection: Detection, source_type: str | None) -> list[Folder]:
    """Folders of the matching kind. A source library with no type ("mixed"
    or unset) gets every folder; a folder with no type fits every library."""
    source_type = (source_type or "").lower() or None
    if source_type not in ("movies", "tvshows", "music"):
        return list(detection.folders)
    return [f for f in detection.folders if f.collection_type in (source_type, None)]


def as_dicts(folders: list[Folder]) -> list[dict]:
    return [asdict(f) for f in folders]
