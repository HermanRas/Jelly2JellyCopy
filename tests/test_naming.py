import os

import pytest

from app import naming


@pytest.mark.parametrize("raw, expected", [
    ("Normal Title", "Normal Title"),
    ("What If...?", "What If"),
    ("AC/DC: Live", "AC DC Live"),
    ("  lots   of\tspace ", "lots of space"),
    ("..", "Unknown"),
    ("", "Unknown"),
    (None, "Unknown"),
    ("con<>tro\x00l", "con tro l"),
    (".hidden", "hidden"),
])
def test_sanitise(raw, expected):
    assert naming.sanitise(raw) == expected


def test_sanitise_limits_bytes_without_breaking_utf8():
    name = naming.sanitise("é" * 300)
    assert len(name.encode()) <= naming.MAX_COMPONENT_BYTES
    name.encode().decode()  # still valid UTF-8


@pytest.mark.parametrize("path, container, expected", [
    ("/media/Heat (1995)/Heat.mkv", "matroska,webm", "mkv"),
    # The spike: an MP4 reported Container "mov,mp4,m4a,3gp,3g2,mj2".
    ("/media/x/Movie.mp4", "mov,mp4,m4a,3gp,3g2,mj2", "mp4"),
    (None, "mov,mp4,m4a,3gp,3g2,mj2", "mov"),
    ("D:\\Media\\Show\\ep.AVI", None, "avi"),
    ("/no/extension", "avi", "avi"),
    (None, None, "mkv"),
    ("/weird/file.thisistoolong", None, "mkv"),
])
def test_pick_ext(path, container, expected):
    assert naming.pick_ext(path, container) == expected


def test_episode():
    assert str(naming.episode_relpath("2 Fast", 2011, 1, 3, None, "The Pilot", "avi")) == \
        "2 Fast (2011)/Season 01/2 Fast - S01E03 - The Pilot.avi"


def test_episode_specials_and_double():
    assert str(naming.episode_relpath("Show", None, 0, 1, None, "Extra", "mkv")) == \
        "Show/Season 00/Show - S00E01 - Extra.mkv"
    assert str(naming.episode_relpath("Show", 2020, 2, 1, 2, None, "mkv")) == \
        "Show (2020)/Season 02/Show - S02E01-E02.mkv"
    assert str(naming.episode_relpath("Show", 2020, None, None, None, "?", "mkv")).startswith(
        "Show (2020)/Season 00/Show - S00E00")


def test_movie():
    assert str(naming.movie_relpath("Heat", 1995, "mkv")) == "Heat (1995)/Heat (1995).mkv"
    assert str(naming.movie_relpath("Untitled", None, "mp4")) == "Untitled/Untitled.mp4"


def test_track():
    assert str(naming.track_relpath("Artist", "Album", 1, 5, "Song", "flac", multi_disc=False)) == \
        "Artist/Album/05 - Song.flac"
    assert str(naming.track_relpath("Artist", "Album", 2, 5, "Song", "flac", multi_disc=True)) == \
        "Artist/Album/2-05 - Song.flac"
    assert str(naming.track_relpath(None, None, None, None, "Song", "mp3", multi_disc=False)) == \
        "Unknown Artist/Unknown Album/00 - Song.mp3"


def test_subtitle():
    video = "/tv/Show/Season 01/Show - S01E01.mkv"
    assert naming.subtitle_path(video, "eng", "subrip") == "/tv/Show/Season 01/Show - S01E01.eng.srt"
    assert naming.subtitle_path(video, None, "ass", forced=True) == \
        "/tv/Show/Season 01/Show - S01E01.und.forced.ass"
    assert naming.subtitle_path(video, "ENG", "webvtt", index=2) == \
        "/tv/Show/Season 01/Show - S01E01.eng.2.vtt"


def test_metadata_cannot_escape_root(tmp_path):
    # Sanitising removes slashes, so "../" in a title stays one harmless name.
    rel = naming.movie_relpath("../../etc/passwd", None, "mkv")
    target = naming.resolve_inside(str(tmp_path), rel)
    assert target.startswith(str(tmp_path) + os.sep)


def test_resolve_inside_rejects_escapes(tmp_path):
    with pytest.raises(naming.UnsafePath):
        naming.resolve_inside(str(tmp_path), "../outside.mkv")
    with pytest.raises(naming.UnsafePath):
        naming.resolve_inside(str(tmp_path), "/etc/passwd")
    with pytest.raises(naming.UnsafePath):
        naming.resolve_inside(str(tmp_path), ".")


def test_check_inside_follows_symlinks(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "Show").symlink_to(outside)
    with pytest.raises(naming.UnsafePath):
        naming.check_inside(str(root), str(root / "Show" / "ep.mkv"))
    naming.check_inside(str(root), str(root / "Other" / "ep.mkv"))
