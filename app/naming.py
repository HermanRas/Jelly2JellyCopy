"""Target paths for downloaded files, in the layout Jellyfin, Sonarr and
Radarr expect, plus filesystem sanitising and the traversal check.

Every name comes from the *source* server's metadata, which we don't control,
so every component is sanitised and the joined path must still resolve inside
the chosen destination root.
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path, PurePosixPath

# Windows-hostile characters too: people share these folders over SMB.
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_SPACES = re.compile(r"\s+")
MAX_COMPONENT_BYTES = 200   # ext4 allows 255; leave room for ".j2jpart" and suffixes

SUBTITLE_EXT = {"subrip": "srt", "srt": "srt", "ass": "ass", "ssa": "ssa", "webvtt": "vtt",
                "vtt": "vtt", "pgssub": "sup", "pgs": "sup", "dvdsub": "sub", "sub": "sub",
                "mov_text": "srt", "ttml": "ttml", "smi": "smi"}


class UnsafePath(ValueError):
    pass


def sanitise(name: str | None, fallback: str = "Unknown") -> str:
    name = unicodedata.normalize("NFC", name or "")
    name = _BAD_CHARS.sub(" ", name)
    name = _SPACES.sub(" ", name).strip()
    # Trailing dots and spaces break Windows/SMB; a leading dot hides the file.
    name = name.strip(". ")
    if name in ("", ".", ".."):
        name = fallback
    encoded = name.encode()
    if len(encoded) > MAX_COMPONENT_BYTES:
        name = encoded[:MAX_COMPONENT_BYTES].decode(errors="ignore").rstrip(". ")
    return name


def pick_ext(path: str | None, container: str | None, default: str = "mkv") -> str:
    """The file extension, from the source file's own path first.

    `Container` is not reliable: for MP4s Jellyfin reports ffprobe's whole
    format list ("mov,mp4,m4a,3gp,3g2,mj2"), so it's only the fallback.
    """
    if path:
        suffix = PurePosixPath(path.replace("\\", "/")).suffix.lstrip(".").lower()
        if suffix and re.fullmatch(r"[a-z0-9]{1,6}", suffix):
            return suffix
    if container:
        first = container.split(",")[0].strip().lower()
        if re.fullmatch(r"[a-z0-9]{1,6}", first):
            return first
    return default


def _with_year(title: str, year: int | None) -> str:
    title = sanitise(title)
    return f"{title} ({year})" if year else title


def episode_relpath(series: str, year: int | None, season: int | None, episode: int | None,
                    episode_end: int | None, title: str | None, ext: str) -> PurePosixPath:
    folder = _with_year(series, year)
    season = season or 0          # unknown season counts as specials
    ep = f"E{episode or 0:02d}"
    if episode_end and episode and episode_end > episode:
        ep += f"-E{episode_end:02d}"
    name = f"{sanitise(series)} - S{season:02d}{ep}"
    if title:
        name += f" - {sanitise(title)}"
    return PurePosixPath(folder, f"Season {season:02d}", f"{sanitise(name)}.{ext}")


def movie_relpath(title: str, year: int | None, ext: str) -> PurePosixPath:
    name = _with_year(title, year)
    return PurePosixPath(name, f"{name}.{ext}")


def track_relpath(album_artist: str | None, album: str | None, disc: int | None,
                  track: int | None, title: str | None, ext: str, multi_disc: bool) -> PurePosixPath:
    prefix = f"{disc}-" if multi_disc and disc else ""
    name = f"{prefix}{track or 0:02d} - {sanitise(title)}"
    return PurePosixPath(sanitise(album_artist, "Unknown Artist"), sanitise(album, "Unknown Album"),
                         f"{sanitise(name)}.{ext}")


def subtitle_path(video_target: str, language: str | None, codec: str | None,
                  forced: bool = False, index: int | None = None) -> str:
    """Next to the video: `<basename>.<lang>[.forced].<ext>`, which Jellyfin
    picks up as an external subtitle. `index` disambiguates two subtitles in
    the same language."""
    base = video_target.rsplit(".", 1)[0]
    lang = sanitise((language or "und").lower(), "und")
    ext = SUBTITLE_EXT.get((codec or "").lower(), "srt")
    parts = [base, lang]
    if index is not None:
        parts.append(str(index))
    if forced:
        parts.append("forced")
    return ".".join(parts) + f".{ext}"


def resolve_inside(root: str, rel: PurePosixPath | str) -> str:
    """Join and check the result stays inside root, following symlinks that
    already exist on disk. Raises UnsafePath otherwise."""
    root_path = Path(root).resolve()
    target = (root_path / str(rel)).resolve()
    if not target.is_relative_to(root_path) or target == root_path:
        raise UnsafePath(f"{rel} would land outside {root}")
    return str(target)


def check_inside(root: str, target: str) -> None:
    """Re-checked right before writing: a folder may have become a symlink
    since the job was queued."""
    root_path = Path(root).resolve()
    resolved = Path(target).resolve()
    if not resolved.is_relative_to(root_path) or resolved == root_path:
        raise UnsafePath(f"{target} would land outside {root}")
