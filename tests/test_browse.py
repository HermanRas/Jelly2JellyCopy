import pytest
import respx

from app import config, db
from fakes import DEST, MIXED, MOVIES, PLAYLISTS, SHOWS, SRC, USER_ID, mock_source, views, virtual_folders


@pytest.fixture
def source():
    with respx.mock(assert_all_called=False) as mock:
        mock_source(mock)
        mock.get(f"{SRC}/UserViews", params={"userId": USER_ID}).respond(json=views())
        mock.get(f"{SRC}/Items", params={"Limit": "0"}).respond(json={"Items": [], "TotalRecordCount": 7})
        yield mock


@pytest.fixture
def connected(logged_in, source):
    r = logged_in.post("/api/connect", json={"url": SRC, "username": "alice", "password": "pw"})
    assert r.status_code == 200
    return logged_in


@pytest.fixture
def dest(monkeypatch, tmp_path, source):
    """Your Jellyfin, with /movies really writable (a tmp dir) and the rest not mounted."""
    movies = tmp_path / "movies"
    movies.mkdir()
    monkeypatch.setattr(config, "DEST_URL", DEST)
    monkeypatch.setattr(config, "DEST_API_KEY", "key")
    source.get(f"{DEST}/Library/VirtualFolders").respond(json=virtual_folders(movies=str(movies)))
    return str(movies)


def test_browse_needs_a_connection(logged_in):
    r = logged_in.get("/browse")
    assert r.status_code == 303
    assert r.headers["location"] == "/"


def test_nav_shows_download_only_when_connected(logged_in, source):
    assert 'href="/browse"' not in logged_in.get("/").text
    logged_in.post("/api/connect", json={"url": SRC, "username": "alice", "password": "pw"})
    assert 'href="/browse"' in logged_in.get("/").text


def test_libraries_page(connected):
    page = connected.get("/browse").text
    assert f"/browse/{MOVIES}" in page
    assert f"/browse/{SHOWS}" in page
    assert "Standup Comedy" in page
    assert "Playlists" not in page
    assert "7 items" in page


def test_library_page_offers_matching_folders(connected, dest):
    page = connected.get(f"/browse/{MOVIES}").text
    assert f"Movies — {dest}" in page
    assert "Kids Movies — /kids-movies (not mounted in Jelly2JellyCopy)" in page
    assert "Shows — /tv" not in page
    assert "Collections" not in page
    assert "your Jellyfin&#39;s libraries" in page


def test_mixed_library_offers_every_folder(connected, dest):
    page = connected.get(f"/browse/{MIXED}").text
    assert "Shows — /tv" in page
    assert "Kids Movies — /kids-movies" in page


def test_unknown_or_malformed_library_goes_back(connected):
    assert connected.get("/browse/" + "f" * 32).headers["location"] == "/browse"
    assert connected.get("/browse/..%2F..%2Fetc").status_code in (303, 404)


def test_choose_destination_and_preselect(connected, dest):
    r = connected.post(f"/api/libraries/{MOVIES}/dest", json={"path": dest})
    assert r.status_code == 200
    assert db.get_setting(f"dest:{MOVIES}") == dest
    page = connected.get(f"/browse/{MOVIES}").text
    assert f'value="{dest}" selected' in page


@pytest.mark.parametrize("path", ["/kids-movies", "/tv", "/etc", "/"])
def test_destination_must_be_offered_and_writable(connected, dest, path):
    r = connected.post(f"/api/libraries/{MOVIES}/dest", json={"path": path})
    assert r.status_code == 400
    assert db.get_setting(f"dest:{MOVIES}") is None


def test_no_preselect_when_last_folder_is_gone(connected, dest):
    db.set_setting(f"dest:{MOVIES}", "/kids-movies")
    page = connected.get(f"/browse/{MOVIES}").text
    assert 'value="" selected' in page


def test_items_passes_filters(connected, source):
    route = source.get(f"{SRC}/Items", params={"ParentId": MOVIES, "StartIndex": "60"}).respond(json={
        "TotalRecordCount": 61,
        "Items": [{"Id": "b" * 32, "Name": "Heat", "Type": "Movie", "ProductionYear": 1995,
                   "ImageTags": {"Primary": "x"}}]})
    body = connected.get(f"/api/libraries/{MOVIES}/items?start=60&search=hea&letter=h").json()
    assert body["total"] == 61
    assert body["items"] == [{"id": "b" * 32, "name": "Heat", "type": "Movie", "year": 1995,
                              "child_count": None, "has_image": True, "on_server": False}]
    params = route.calls.last.request.url.params
    assert params["SearchTerm"] == "hea"
    assert params["NameStartsWith"] == "H"
    assert params["IncludeItemTypes"] == "Series,Movie,MusicAlbum"


def test_items_hash_letter_means_before_a(connected, source):
    route = source.get(f"{SRC}/Items", params={"ParentId": MOVIES}).respond(
        json={"TotalRecordCount": 0, "Items": []})
    connected.get(f"/api/libraries/{MOVIES}/items?letter=%23")
    params = route.calls.last.request.url.params
    assert params["NameLessThan"] == "A"
    assert "NameStartsWith" not in params


def test_items_rejects_bad_id(connected):
    assert connected.get("/api/libraries/notanid/items").status_code == 400


def test_poster_is_proxied_without_attachment_header(connected, source):
    item = "c" * 32
    source.get(f"{SRC}/Items/{item}/Images/Primary").respond(
        content=b"\xff\xd8jpeg", headers={"Content-Type": "image/jpeg",
                                          "Content-Disposition": "attachment"})
    r = connected.get(f"/api/items/{item}/poster")
    assert r.status_code == 200
    assert r.content == b"\xff\xd8jpeg"
    assert r.headers["content-type"] == "image/jpeg"
    assert "content-disposition" not in r.headers
    assert "private" in r.headers["cache-control"]


def test_poster_rejects_bad_id(connected):
    assert connected.get("/api/items/..%2Fsecret/poster").status_code == 404


def test_source_401_while_browsing_means_reconnect(connected, source):
    source.get(f"{SRC}/Items", params={"ParentId": MOVIES}).respond(401)
    r = connected.get(f"/api/libraries/{MOVIES}/items")
    assert r.status_code == 409
    assert r.json()["error"] == "reconnect"
    assert db.get_connection() is None
    # Pages bounce back to the status card.
    assert connected.get("/browse").headers["location"] == "/"


def test_source_error_page(connected, source):
    source.get(f"{SRC}/UserViews", params={"userId": USER_ID}).respond(500)
    r = connected.get("/browse", headers={"accept": "text/html"})
    assert r.status_code == 502
    assert "HTTP 500" in r.text
