"""The "On my server" badges."""
import asyncio

import httpx
import pytest
import respx

from app import config, mine
from fakes import DEST, MOVIES, SRC, USER_ID, mock_source, views

MINE = [
    {"Type": "Movie", "Name": "Heat", "ProductionYear": 1995, "ProviderIds": {"Imdb": "tt0113277"}},
    {"Type": "Series", "Name": "The Office", "ProductionYear": 2005, "ProviderIds": {"Tvdb": "73244"}},
    {"Type": "Movie", "Name": "Amélie", "ProductionYear": 2001, "ProviderIds": {}},
]


@pytest.fixture
def dest(monkeypatch):
    monkeypatch.setattr(config, "DEST_URL", DEST)
    monkeypatch.setattr(config, "DEST_API_KEY", "key")
    with respx.mock(assert_all_called=False) as mock:
        mock.get(f"{DEST}/Items").respond(json={"Items": MINE})
        yield mock


def idx():
    return asyncio.run(mine.index())


@pytest.mark.parametrize("item, expected", [
    # Same IMDb id, different title (a translated name on the source).
    ({"Type": "Movie", "Name": "Fuego contra fuego", "ProductionYear": 1995,
      "ProviderIds": {"Imdb": "TT0113277"}}, True),
    # No ids on one side: title + year, ignoring "The", case and accents.
    ({"Type": "Series", "Name": "Office", "ProductionYear": 2005, "ProviderIds": {}}, True),
    ({"Type": "Movie", "Name": "amelie", "ProductionYear": 2001}, True),
    # Same title, different year: a remake, not the same thing.
    ({"Type": "Movie", "Name": "Amélie", "ProductionYear": 2030}, False),
    # Same TVDB number but a movie: ids are scoped by type.
    ({"Type": "Movie", "Name": "Other", "ProductionYear": 1, "ProviderIds": {"Tvdb": "73244"}}, False),
])
def test_matching(dest, item, expected):
    assert mine.on_server(item, idx()) is expected


def test_uses_api_key_and_caches(dest):
    idx()
    idx()
    route = dest.routes[0]
    assert route.call_count == 1
    assert 'Token="key"' in route.calls.last.request.headers["Authorization"]
    mine.forget()
    idx()
    assert route.call_count == 2


def test_no_api_key_means_no_badges(monkeypatch):
    monkeypatch.setattr(config, "DEST_URL", DEST)
    assert idx() == set()


def test_dest_down_means_no_badges_not_an_error(dest):
    dest.get(f"{DEST}/Items").mock(side_effect=httpx.ConnectError("down"))
    assert idx() == set()


def test_badge_reaches_the_browse_api(logged_in, dest):
    mock_source(dest)
    dest.get(f"{SRC}/UserViews", params={"userId": USER_ID}).respond(json=views())
    dest.get(f"{SRC}/Items", params={"ParentId": MOVIES}).respond(json={"TotalRecordCount": 2, "Items": [
        {"Id": "b" * 32, "Name": "Heat", "Type": "Movie", "ProductionYear": 1995,
         "ProviderIds": {"Imdb": "tt0113277"}},
        {"Id": "c" * 32, "Name": "Ronin", "Type": "Movie", "ProductionYear": 1998}]})
    logged_in.post("/api/connect", json={"url": SRC, "username": "alice", "password": "pw"})
    items = logged_in.get(f"/api/libraries/{MOVIES}/items").json()["items"]
    assert [i["on_server"] for i in items] == [True, False]
