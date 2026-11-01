import respx

from app import config, db, enqueue, queue
from fakes import DEST, virtual_folders


def test_defaults(logged_in):
    page = logged_in.get("/settings").text
    assert '<option value="1" selected>' in page
    assert 'name="subtitles" value="1" checked' in page
    assert 'name="include_specials" value="1" >' in page or 'name="include_specials" value="1"  >' in page
    assert queue.concurrency() == 1
    assert enqueue.include_subtitles() is True
    assert enqueue.include_specials() is False


def test_save(logged_in):
    r = logged_in.post("/settings", data={"concurrency": "3", "bandwidth_kbps": "500",
                                          "free_space_reserve_gb": "10", "include_specials": "1"})
    assert r.status_code == 303
    assert queue.concurrency() == 3
    assert queue.bandwidth_bytes() == 500 * 1024
    assert queue.reserve_bytes() == 10 * 1_000_000_000
    assert enqueue.include_specials() is True
    # Not ticked = not sent = off.
    assert enqueue.include_subtitles() is False
    assert "Saved." in logged_in.get("/settings?saved=1").text


def test_invalid_values_are_refused(logged_in):
    r = logged_in.post("/settings", data={"concurrency": "9", "bandwidth_kbps": "-1",
                                          "free_space_reserve_gb": "abc"})
    assert r.status_code == 400
    assert "between 1 and 4" in r.text
    assert "whole number" in r.text
    assert db.get_setting("concurrency") is None


def test_shows_folders_but_never_the_key(logged_in, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "DEST_URL", DEST)
    monkeypatch.setattr(config, "DEST_API_KEY", "super-secret-key")
    with respx.mock() as mock:
        mock.get(f"{DEST}/Library/VirtualFolders").respond(json=virtual_folders(movies=str(tmp_path)))
        page = logged_in.get("/settings").text
    assert "super-secret-key" not in page
    assert "Kids Movies" in page
    assert "not mounted in Jelly2JellyCopy" in page
    assert "Scan library now" in page


def test_scan_now(logged_in, monkeypatch):
    monkeypatch.setattr(config, "DEST_URL", DEST)
    monkeypatch.setattr(config, "DEST_API_KEY", "key")
    with respx.mock() as mock:
        route = mock.post(f"{DEST}/Library/Refresh").respond(204)
        r = logged_in.post("/settings/scan")
    assert route.called
    assert r.headers["location"] == "/settings?saved=scan"


def test_settings_need_login(client):
    assert client.post("/settings", data={"concurrency": "4"}).status_code == 303
    assert db.get_setting("concurrency") is None
