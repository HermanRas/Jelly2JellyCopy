"""Queue buttons: expanding a series / season / movie / album into jobs."""
import pytest
import respx

from app import config, db
from fakes import DEST, MOVIES, SHOWS, SRC, USER_ID, mock_source, views, virtual_folders

SERIES = "5" * 32
MOVIE = "6" * 32
ALBUM = "7" * 32
S0, S1, S2 = "a0" * 16, "a1" * 16, "a2" * 16


def ep(n, season, season_id, size=1_000_000, name=None, virtual=False, path=None):
    item = {"Id": f"{season}{n:031x}", "Name": name or f"Episode {n}", "IndexNumber": n,
            "ParentIndexNumber": season, "SeasonId": season_id, "Type": "Episode",
            "LocationType": "Virtual" if virtual else "FileSystem"}
    if not virtual:
        item["Path"] = path or f"/media/Show/S{season}/e{n}.mkv"
        item["MediaSources"] = [{"Size": size, "Container": "matroska,webm", "Path": item["Path"]}]
    return item


EPISODES = [
    ep(1, 0, S0, name="Behind the Scenes"),
    ep(1, 1, S1, name="Pilot"), ep(2, 1, S1, name="Second"),
    ep(1, 2, S2, size=2_000_000), ep(2, 2, S2, virtual=True),
]


@pytest.fixture
def source():
    with respx.mock(assert_all_called=False) as mock:
        mock_source(mock)
        mock.get(f"{SRC}/UserViews", params={"userId": USER_ID}).respond(json=views())
        mock.get(f"{SRC}/Items/{SERIES}").respond(json={"Id": SERIES, "Name": "The Show", "ProductionYear": 2011})
        mock.get(f"{SRC}/Shows/{SERIES}/Episodes").respond(json={"Items": EPISODES})
        mock.get(f"{SRC}/Shows/{SERIES}/Seasons").respond(json={"Items": [
            {"Id": S0, "Name": "Specials", "IndexNumber": 0, "ChildCount": None},
            {"Id": S1, "Name": "Season 1", "IndexNumber": 1, "ChildCount": None},
            {"Id": S2, "Name": "Season 2", "IndexNumber": 2, "ChildCount": None}]})
        mock.get(f"{SRC}/Items/{MOVIE}").respond(json={
            "Id": MOVIE, "Name": "Heat", "ProductionYear": 1995, "Path": "/m/Heat.mp4",
            "Container": "mov,mp4,m4a,3gp,3g2,mj2",
            "MediaSources": [{"Size": 5_000_000, "Container": "mov,mp4,m4a,3gp,3g2,mj2", "Path": "/m/Heat.mp4"}]})
        mock.get(f"{SRC}/Items/{ALBUM}").respond(json={"Id": ALBUM, "Name": "Album", "AlbumArtist": "Band"})
        mock.get(f"{SRC}/Items", params={"ParentId": ALBUM}).respond(json={"Items": [
            {"Id": f"{d}{i:031x}", "Name": f"Song {i}", "IndexNumber": i, "ParentIndexNumber": d,
             "Album": "Album", "AlbumArtist": "Band", "Path": f"/music/{d}-{i}.flac",
             "MediaSources": [{"Size": 100, "Path": f"/music/{d}-{i}.flac"}]}
            for i, d in ((1, 1), (2, 1), (1, 2))]})
        yield mock


@pytest.fixture
def connected(logged_in, source, tmp_path, monkeypatch):
    for name in ("movies", "tv", "kids"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(config, "DEST_URL", DEST)
    monkeypatch.setattr(config, "DEST_API_KEY", "key")
    source.get(f"{DEST}/Library/VirtualFolders").respond(json=virtual_folders(
        movies=str(tmp_path / "movies"), kids=str(tmp_path / "kids"), tv=str(tmp_path / "tv")))
    r = logged_in.post("/api/connect", json={"url": SRC, "username": "alice", "password": "pw"})
    assert r.status_code == 200
    logged_in.tmp = tmp_path
    return logged_in


def queue(client, **body):
    return client.post("/api/queue", json=body)


def jobs():
    return sorted(db.list_jobs(), key=lambda r: r["id"])


def test_queue_all_skips_specials_by_default_and_missing_files(connected):
    tv = str(connected.tmp / "tv")
    r = queue(connected, library_id=SHOWS, item_id=SERIES, type="Series", dest=tv, which="all")
    body = r.json()
    assert (body["added"], body["unavailable"], body["bytes"]) == (3, 1, 4_000_000)
    assert [j["target_path"].removeprefix(tv) for j in jobs()] == [
        "/The Show (2011)/Season 01/The Show - S01E01 - Pilot.mkv",
        "/The Show (2011)/Season 01/The Show - S01E02 - Second.mkv",
        "/The Show (2011)/Season 02/The Show - S02E01 - Episode 1.mkv",
    ]
    j = jobs()[0]
    assert (j["kind"], j["display_name"], j["server_id"], j["size"]) == \
        ("episode", "The Show - S01E01 - Pilot", "srv123", 1_000_000)
    assert j["download_path"] == f"/Items/{j['source_item_id']}/Download"
    # The choice is remembered for next time.
    assert db.get_setting(f"dest:{SHOWS}") == tv


def test_queue_all_includes_specials_when_the_setting_is_on(connected):
    db.set_setting("include_specials", "1")
    queue(connected, library_id=SHOWS, item_id=SERIES, type="Series", dest=str(connected.tmp / "tv"))
    assert any("Season 00/The Show - S00E01 - Behind the Scenes.mkv" in j["target_path"] for j in jobs())


def test_queue_first_season(connected):
    body = queue(connected, library_id=SHOWS, item_id=SERIES, type="Series",
                 dest=str(connected.tmp / "tv"), which="first").json()
    assert body["added"] == 2
    assert all("Season 01" in j["target_path"] for j in jobs())


def test_manual_seasons_can_include_specials(connected):
    body = queue(connected, library_id=SHOWS, item_id=SERIES, type="Series", dest=str(connected.tmp / "tv"),
                 which="seasons", season_ids=[S0, S2]).json()
    assert body["added"] == 2
    assert {j["target_path"].split("/")[-2] for j in jobs()} == {"Season 00", "Season 02"}


def test_queueing_twice_reports_duplicates(connected):
    args = dict(library_id=SHOWS, item_id=SERIES, type="Series", dest=str(connected.tmp / "tv"), which="first")
    queue(connected, **args)
    body = queue(connected, **args).json()
    assert (body["added"], body["already_queued"]) == (0, 2)


def test_seasons_api_counts_episodes_with_files(connected):
    seasons = connected.get(f"/api/series/{SERIES}/seasons").json()["seasons"]
    assert [(s["name"], s["episodes"], s["bytes"]) for s in seasons] == [
        ("Specials", 1, 1_000_000), ("Season 1", 2, 2_000_000), ("Season 2", 1, 2_000_000)]


def test_movie_uses_path_extension_not_container(connected):
    body = queue(connected, library_id=MOVIES, item_id=MOVIE, type="Movie", dest=str(connected.tmp / "movies")).json()
    assert body["added"] == 1
    assert jobs()[0]["target_path"] == str(connected.tmp / "movies" / "Heat (1995)" / "Heat (1995).mp4")
    assert jobs()[0]["display_name"] == "Heat (1995)"


def test_album_with_two_discs(connected, monkeypatch):
    # The fake has no music library, so offer this folder to every type.
    music = connected.tmp / "music"
    music.mkdir()
    monkeypatch.setattr(config, "DEST_URL", "")
    monkeypatch.setattr(config, "DEST_FOLDERS", f"music={music}")
    from app import dest_folders
    dest_folders._cache = None
    body = queue(connected, library_id="3" * 32, item_id=ALBUM, type="MusicAlbum", dest=str(music)).json()
    assert body["added"] == 3
    assert [j["target_path"].removeprefix(str(music)) for j in jobs()] == [
        "/Band/Album/1-01 - Song 1.flac", "/Band/Album/1-02 - Song 2.flac", "/Band/Album/2-01 - Song 1.flac"]


def test_destination_must_match_library_type(connected):
    r = queue(connected, library_id=MOVIES, item_id=MOVIE, type="Movie", dest=str(connected.tmp / "tv"))
    assert r.status_code == 400
    assert jobs() == []


def test_destination_must_be_chosen(connected):
    r = queue(connected, library_id=MOVIES, item_id=MOVIE, type="Movie", dest="")
    assert r.status_code == 400
    assert "Choose a destination" in r.json()["detail"]


def test_bad_ids_are_refused(connected):
    tv = str(connected.tmp / "tv")
    assert queue(connected, library_id=SHOWS, item_id="../x", type="Series", dest=tv).status_code == 400
    assert queue(connected, library_id=SHOWS, item_id=SERIES, type="Series", dest=tv,
                 which="seasons", season_ids=["nope"]).status_code == 400
    assert queue(connected, library_id=SHOWS, item_id=SERIES, type="Episode", dest=tv).status_code == 422


def test_series_without_season_one(connected, source):
    source.get(f"{SRC}/Shows/{SERIES}/Episodes").respond(json={"Items": [ep(1, 2, S2)]})
    r = queue(connected, library_id=SHOWS, item_id=SERIES, type="Series",
              dest=str(connected.tmp / "tv"), which="first")
    assert "no season 1" in r.json()["detail"]


def test_hostile_metadata_stays_inside_root(connected, source):
    source.get(f"{SRC}/Items/{SERIES}").respond(json={"Id": SERIES, "Name": "../../../etc", "ProductionYear": None})
    tv = connected.tmp / "tv"
    queue(connected, library_id=SHOWS, item_id=SERIES, type="Series", dest=str(tv), which="first")
    for j in jobs():
        assert j["target_path"].startswith(str(tv) + "/")


def test_queue_counts(connected):
    queue(connected, library_id=SHOWS, item_id=SERIES, type="Series", dest=str(connected.tmp / "tv"), which="first")
    assert connected.get("/api/queue/counts").json() == {"counts": {"queued": 2}, "paused": False}


def test_library_page_has_season_picker(connected):
    page = connected.get(f"/browse/{SHOWS}").text
    assert 'id="seasons-modal"' in page
    assert "dest-missing" in page


def with_subs(item, streams):
    item = {**item, "MediaSources": [{**item["MediaSources"][0], "Id": "ms" + item["Id"][2:], "MediaStreams": streams}]}
    return item


SUBS = [
    {"Type": "Subtitle", "Index": 2, "Codec": "subrip", "Language": "eng", "IsExternal": True},
    {"Type": "Subtitle", "Index": 3, "Codec": "subrip", "Language": "eng", "IsExternal": True, "IsForced": True},
    {"Type": "Subtitle", "Index": 4, "Codec": "ass", "Language": "fre", "IsExternal": True},
    {"Type": "Subtitle", "Index": 5, "Codec": "pgssub", "Language": "ger", "IsExternal": True},   # image: skipped
    {"Type": "Subtitle", "Index": 6, "Codec": "subrip", "Language": "spa", "IsExternal": False},  # embedded: in the file
    {"Type": "Audio", "Index": 1, "Codec": "aac"},
]


def test_external_subtitles_ride_along(connected, source):
    pilot = with_subs(ep(1, 1, S1, name="Pilot"), SUBS)
    source.get(f"{SRC}/Shows/{SERIES}/Episodes").respond(json={"Items": [pilot]})
    tv = str(connected.tmp / "tv")
    body = queue(connected, library_id=SHOWS, item_id=SERIES, type="Series", dest=tv, which="first").json()
    assert (body["added"], body["subtitles"]) == (1, 3)
    subs = [j for j in jobs() if j["kind"] == "subtitle"]
    base = f"{tv}/The Show (2011)/Season 01/The Show - S01E01 - Pilot"
    assert [j["target_path"] for j in subs] == [f"{base}.eng.2.srt", f"{base}.eng.3.forced.srt", f"{base}.fre.ass"]
    ms = pilot["MediaSources"][0]["Id"]
    assert subs[0]["download_path"] == f"/Videos/{pilot['Id']}/{ms}/Subtitles/2/Stream.srt"
    assert subs[0]["size"] is None
    # The video is queued ahead of its subtitles.
    assert jobs()[0]["kind"] == "episode"


def test_subtitles_setting_off(connected, source):
    db.set_setting("subtitles", "0")
    source.get(f"{SRC}/Shows/{SERIES}/Episodes").respond(json={"Items": [with_subs(ep(1, 1, S1), SUBS)]})
    body = queue(connected, library_id=SHOWS, item_id=SERIES, type="Series",
                 dest=str(connected.tmp / "tv"), which="first").json()
    assert body["subtitles"] == 0
    assert all(j["kind"] == "episode" for j in jobs())
