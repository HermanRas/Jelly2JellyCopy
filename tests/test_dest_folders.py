import asyncio
import os

import httpx
import pytest
import respx

from app import config, dest_folders
from app.dest_folders import Detection, Folder
from fakes import DEST, virtual_folders


def test_api_payload_skips_collections_by_type():
    folders = dest_folders._from_api_payload(virtual_folders())
    assert [(f.library, f.path, f.collection_type) for f in folders] == [
        ("Movies", "/movies", "movies"),
        ("Kids Movies", "/kids-movies", "movies"),
        ("Shows", "/tv", "tvshows"),
    ]


def test_parse_env():
    folders = dest_folders.parse_env("movies=/movies, /kids-movies/;tvshows=/tv;junk;music=relative")
    assert [(f.path, f.collection_type) for f in folders] == [
        ("/movies", "movies"), ("/kids-movies", "movies"), ("/tv", "tvshows")]


def detection():
    return Detection("api", [Folder("/movies", "Movies", "movies"),
                             Folder("/kids-movies", "Kids Movies", "movies"),
                             Folder("/tv", "Shows", "tvshows"),
                             Folder("/anything", "anything", None)])


@pytest.mark.parametrize("source_type, expected", [
    ("movies", ["/movies", "/kids-movies", "/anything"]),
    ("tvshows", ["/tv", "/anything"]),
    ("music", ["/anything"]),
    (None, ["/movies", "/kids-movies", "/tv", "/anything"]),
    ("mixed", ["/movies", "/kids-movies", "/tv", "/anything"]),
])
def test_offered_matches_type(source_type, expected):
    assert [f.path for f in dest_folders.offered(detection(), source_type)] == expected


def test_check_writable(tmp_path):
    assert dest_folders.check_writable(str(tmp_path)) == (True, "")
    assert dest_folders.check_writable(str(tmp_path / "nope")) == (False, "not mounted in Jelly2JellyCopy")


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_check_writable_read_only(tmp_path):
    tmp_path.chmod(0o555)
    try:
        assert dest_folders.check_writable(str(tmp_path)) == (False, "read-only")
    finally:
        tmp_path.chmod(0o755)


def test_from_mounts_ignores_plain_dirs_and_system_paths(tmp_path):
    (tmp_path / "plain").mkdir()
    # Nothing under a tmp dir is a mount point.
    assert dest_folders.from_mounts(str(tmp_path)) == []


def test_detect_via_api(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEST_URL", DEST)
    monkeypatch.setattr(config, "DEST_API_KEY", "key123")
    with respx.mock() as mock:
        route = mock.get(f"{DEST}/Library/VirtualFolders").respond(
            json=virtual_folders(movies=str(tmp_path)))
        result = asyncio.run(dest_folders.detect())
    assert result.method == "api"
    assert 'Token="key123"' in route.calls.last.request.headers["Authorization"]
    assert [f.library for f in result.folders] == ["Kids Movies", "Movies", "Shows"]
    by_path = {f.path: f for f in result.folders}
    assert by_path[str(tmp_path)].available
    assert by_path["/kids-movies"].reason == "not mounted in Jelly2JellyCopy"


def test_detect_falls_back_to_env_when_key_is_refused(monkeypatch):
    monkeypatch.setattr(config, "DEST_URL", DEST)
    monkeypatch.setattr(config, "DEST_API_KEY", "wrong")
    monkeypatch.setattr(config, "DEST_FOLDERS", "movies=/movies")
    with respx.mock() as mock:
        mock.get(f"{DEST}/Library/VirtualFolders").respond(401)
        result = asyncio.run(dest_folders.detect())
    assert result.method == "env"
    assert "API key" in result.warning


def test_detect_falls_back_when_dest_is_down(monkeypatch):
    monkeypatch.setattr(config, "DEST_URL", DEST)
    monkeypatch.setattr(config, "DEST_API_KEY", "key")
    with respx.mock() as mock:
        mock.get(f"{DEST}/Library/VirtualFolders").mock(side_effect=httpx.ConnectError("down"))
        result = asyncio.run(dest_folders.detect())
    assert result.method == "mounts"
    assert "ConnectError" in result.warning


def test_detect_is_cached(monkeypatch):
    monkeypatch.setattr(config, "DEST_URL", DEST)
    monkeypatch.setattr(config, "DEST_API_KEY", "key")
    with respx.mock() as mock:
        route = mock.get(f"{DEST}/Library/VirtualFolders").respond(json=virtual_folders())
        asyncio.run(dest_folders.detect())
        asyncio.run(dest_folders.detect())
        assert route.call_count == 1
        asyncio.run(dest_folders.detect(force=True))
        assert route.call_count == 2
