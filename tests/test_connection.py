"""Connect / disconnect against a fake Jellyfin (respx), shaped like the
responses a real 12.x server gave during the compatibility spike."""
import asyncio

import httpx
import pytest
import respx

from app import connection, db, jellyfin

SRC = "https://src.example.com"
TOKEN = "tok-0123456789abcdef"
PUBLIC_INFO = {"ServerName": "Friend's Jelly", "Version": "12.3.0",
               "ProductName": "Jellyfin Server", "Id": "srv123"}


def auth_ok(downloads=True):
    return {"AccessToken": TOKEN, "ServerId": "srv123", "SessionInfo": {},
            "User": {"Name": "alice", "Id": "user1",
                     "Policy": {"EnableContentDownloading": downloads, "IsAdministrator": False}}}


@pytest.fixture
def source():
    with respx.mock(base_url=SRC, assert_all_called=False) as mock:
        mock.get("/System/Info/Public").respond(json=PUBLIC_INFO)
        mock.post("/Users/AuthenticateByName").respond(json=auth_ok())
        mock.post("/Sessions/Logout").respond(204)
        yield mock


def connect(client, url=SRC, pw="secret"):
    return client.post("/api/connect", json={"url": url, "username": "alice", "password": pw})


@pytest.mark.parametrize("raw, expected", [
    ("src.example.com", "https://src.example.com"),
    ("https://src.example.com/", "https://src.example.com"),
    ("https://src.example.com/web/#/home.html", "https://src.example.com"),
    ("http://10.0.0.5:8096/web/index.html", "http://10.0.0.5:8096"),
    ("https://example.com/jellyfin/web/", "https://example.com/jellyfin"),
])
def test_normalise_url(raw, expected):
    assert jellyfin.normalise_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ftp://x.example.com", "https://"])
def test_normalise_url_rejects(raw):
    with pytest.raises(jellyfin.BadUrl):
        jellyfin.normalise_url(raw)


def test_connect_stores_encrypted_token_only(logged_in, source):
    r = connect(logged_in)
    assert r.status_code == 200
    body = r.json()
    assert body["connection"]["server_name"] == "Friend's Jelly"
    assert "token" not in str(body).lower()

    # Neither the token nor the password is anywhere in the db file.
    raw = db.db_path().read_bytes()
    assert TOKEN.encode() not in raw
    assert b"secret" not in raw

    sent = source.calls[1].request
    assert b'"Pw":"secret"' in sent.content.replace(b" ", b"")
    assert sent.headers["Authorization"].startswith('MediaBrowser Client="Jelly2JellyCopy"')


def test_status_page_shows_connection(logged_in, source):
    connect(logged_in)
    page = logged_in.get("/").text
    assert "Friend&#39;s Jelly" in page
    assert 'href="/browse"' in page
    assert TOKEN not in page


def test_wrong_password(logged_in, source):
    source.post("/Users/AuthenticateByName").respond(401)
    r = connect(logged_in, pw="nope")
    assert r.status_code == 400
    assert r.json()["error"] == "wrong_password"
    assert db.get_connection() is None


def test_no_download_permission_logs_out_again(logged_in, source):
    source.post("/Users/AuthenticateByName").respond(json=auth_ok(downloads=False))
    r = connect(logged_in)
    assert r.json()["error"] == "no_download"
    assert db.get_connection() is None
    # The useless session was closed on the source, with that session's token.
    logout = source.routes[2]
    assert logout.called
    assert TOKEN in logout.calls.last.request.headers["Authorization"]


def test_not_jellyfin(logged_in, source):
    source.get("/System/Info/Public").respond(200, html="<html>nginx</html>")
    assert connect(logged_in).json()["error"] == "not_jellyfin"


def test_unreachable(logged_in, source):
    source.get("/System/Info/Public").mock(side_effect=httpx.ConnectError("refused"))
    r = connect(logged_in)
    assert r.status_code == 502
    assert r.json()["error"] == "unreachable"


def test_bad_url(logged_in):
    r = connect(logged_in, url="ftp://nope")
    assert r.json()["error"] == "bad_url"


def test_disconnect_logs_out_and_destroys_token(logged_in, source):
    connect(logged_in)
    r = logged_in.post("/api/disconnect")
    assert r.json()["connected"] is False
    assert db.get_connection() is None
    assert db.get_setting("queue_paused") == "1"
    assert source.routes[2].called


def test_disconnect_survives_source_being_down(logged_in, source):
    connect(logged_in)
    source.post("/Sessions/Logout").mock(side_effect=httpx.ConnectError("down"))
    assert logged_in.post("/api/disconnect").status_code == 200
    assert db.get_connection() is None


def test_401_from_source_forgets_token_and_pauses(logged_in, source):
    connect(logged_in)
    source.get("/Users/Me").respond(401)
    with pytest.raises(jellyfin.TokenRejected):
        asyncio.run(connection.request("GET", "/Users/Me"))
    assert db.get_connection() is None
    assert db.get_setting("queue_paused") == "1"


def test_authenticated_request_sends_token(logged_in, source):
    connect(logged_in)
    me = source.get("/Users/Me").respond(json={"Name": "alice"})
    r = asyncio.run(connection.request("GET", "/Users/Me"))
    assert r.json()["Name"] == "alice"
    assert f'Token="{TOKEN}"' in me.calls.last.request.headers["Authorization"]


def test_reconnect_ends_previous_session(logged_in, source):
    connect(logged_in)
    connect(logged_in)
    assert source.routes[2].call_count == 1
    assert db.get_connection() is not None


def test_api_requires_login(client):
    assert client.post("/api/connect", json={"url": SRC, "username": "a", "password": "b"}).status_code == 401
