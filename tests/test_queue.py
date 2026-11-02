"""The download worker against a fake source that speaks Range, drops
connections and refuses things on demand."""
import asyncio
import os
import re

import httpx
import pytest
import respx

from app import auth, db, queue
from fakes import SRC, TOKEN

ITEM = "d" * 32
DATA = bytes(range(256)) * 400          # 102,400 bytes
DOWNLOAD = f"{SRC}/Items/{ITEM}/Download"


def connect_fake(server_id="srv123"):
    db.save_connection(url=SRC, server_name="Friend's Jelly", server_id=server_id, version="12.3.0",
                       user_name="alice", user_id="u1", token_enc=auth.encrypt_token(TOKEN))


def add_job(root, rel="Heat (1995)/Heat (1995).mkv", size=len(DATA), server_id="srv123", item=ITEM):
    db.add_jobs([{"server_id": server_id, "source_item_id": item, "kind": "movie",
                  "display_name": "Heat (1995)", "dest_root": str(root),
                  "target_path": os.path.join(str(root), rel),
                  "download_path": f"/Items/{item}/Download", "size": size}])
    return max(r["id"] for r in db.list_jobs())


def ranged(data=DATA, honour_range=True, cut_at=None):
    """A download handler: answers Range with 206, optionally ignores it, or
    stops sending after `cut_at` bytes as if the connection dropped."""
    def handler(request):
        start = 0
        m = re.match(r"bytes=(\d+)-", request.headers.get("range", ""))
        if m and honour_range:
            start = int(m.group(1))
        body = data[start:]
        headers = {"Content-Type": "video/x-matroska", "Accept-Ranges": "bytes"}
        status = 200
        if m and honour_range:
            status = 206
            headers["Content-Range"] = f"bytes {start}-{len(data) - 1}/{len(data)}"
        if cut_at is not None:
            async def partial():
                yield body[:cut_at]
                raise httpx.ReadError("connection reset")
            return httpx.Response(status, headers=headers, content=partial())
        return httpx.Response(status, headers=headers, content=body)
    return handler


@pytest.fixture
def root(tmp_path):
    r = tmp_path / "movies"
    r.mkdir()
    return r


@pytest.fixture
def source():
    with respx.mock(assert_all_called=False) as mock:
        yield mock


def run(job_id, worker=None):
    w = worker or queue.Worker()
    asyncio.run(w.run_job(job_id))
    return db.get_job(job_id), w


def target(root):
    return root / "Heat (1995)" / "Heat (1995).mkv"


def test_full_download(root, source):
    connect_fake()
    source.get(DOWNLOAD).mock(side_effect=ranged())
    job, w = run(add_job(root))
    assert job["status"] == "done"
    assert target(root).read_bytes() == DATA
    assert not (root / "Heat (1995)" / "Heat (1995).mkv.j2jpart").exists()
    assert job["bytes_done"] == len(DATA)
    assert w._scan_due


def test_resumes_from_part_file(root, source):
    connect_fake()
    route = source.get(DOWNLOAD).mock(side_effect=ranged())
    part = root / "Heat (1995)" / "Heat (1995).mkv.j2jpart"
    part.parent.mkdir()
    part.write_bytes(DATA[:30000])
    job, _ = run(add_job(root))
    assert route.calls.last.request.headers["range"] == "bytes=30000-"
    assert job["status"] == "done"
    assert target(root).read_bytes() == DATA


def test_server_ignoring_range_starts_over(root, source):
    connect_fake()
    source.get(DOWNLOAD).mock(side_effect=ranged(honour_range=False))
    part = root / "Heat (1995)" / "Heat (1995).mkv.j2jpart"
    part.parent.mkdir()
    part.write_bytes(b"x" * 500)
    job, _ = run(add_job(root))
    assert job["status"] == "done"
    assert target(root).read_bytes() == DATA


def test_dropped_connection_keeps_part_and_resumes(root, source):
    connect_fake()
    source.get(DOWNLOAD).mock(side_effect=ranged(cut_at=40000))
    job_id = add_job(root)
    job, _ = run(job_id)
    assert job["status"] == "queued"
    assert "Retrying" in job["error"]
    # Progress was made, so the attempt doesn't count against the limit.
    assert job["attempts"] == 0
    assert job["not_before"] > 0
    part = root / "Heat (1995)" / "Heat (1995).mkv.j2jpart"
    assert part.stat().st_size == 40000
    assert not target(root).exists()

    source.get(DOWNLOAD).mock(side_effect=ranged())
    job, _ = run(job_id)
    assert job["status"] == "done"
    assert target(root).read_bytes() == DATA


def test_short_file_is_not_renamed(root, source):
    connect_fake()
    source.get(DOWNLOAD).mock(side_effect=ranged(data=DATA[:1000]))
    job, _ = run(add_job(root))
    assert job["status"] == "queued"
    assert "ended at 1000" in job["error"]
    assert not target(root).exists()


def test_fails_after_three_attempts_without_progress(root, source):
    connect_fake()
    source.get(DOWNLOAD).respond(500)
    job_id = add_job(root)
    for expected in (1, 2):
        job, _ = run(job_id)
        assert (job["status"], job["attempts"]) == ("queued", expected)
    job, _ = run(job_id)
    assert job["status"] == "failed"
    assert job["attempts"] == 3
    assert "HTTP 500" in job["error"]


@pytest.mark.parametrize("status, words", [(403, "Allow media downloading"), (404, "no longer")])
def test_permanent_refusals_fail_at_once(root, source, status, words):
    connect_fake()
    source.get(DOWNLOAD).respond(status)
    job, _ = run(add_job(root))
    assert job["status"] == "failed"
    assert words in job["error"]


def test_401_pauses_and_forgets_token(root, source):
    connect_fake()
    source.get(DOWNLOAD).respond(401)
    job, _ = run(add_job(root))
    assert job["status"] == "paused"
    assert db.get_connection() is None
    assert queue.is_paused()


def test_existing_same_size_is_skipped(root, source):
    connect_fake()
    route = source.get(DOWNLOAD).mock(side_effect=ranged())
    target(root).parent.mkdir()
    target(root).write_bytes(DATA)
    job, w = run(add_job(root))
    assert job["status"] == "skipped"
    assert not route.called
    assert not w._scan_due


def test_existing_different_file_is_left_alone(root, source):
    connect_fake()
    source.get(DOWNLOAD).mock(side_effect=ranged())
    target(root).parent.mkdir()
    target(root).write_bytes(b"my own file")
    job, _ = run(add_job(root))
    assert job["status"] == "failed"
    assert target(root).read_bytes() == b"my own file"


def test_target_outside_root_is_refused(root, source):
    connect_fake()
    route = source.get(DOWNLOAD).mock(side_effect=ranged())
    job, _ = run(add_job(root, rel="../escape.mkv"))
    assert job["status"] == "failed"
    assert not route.called
    assert not (root.parent / "escape.mkv").exists()


def test_unmounted_root_fails(tmp_path, source):
    connect_fake()
    job, _ = run(add_job(tmp_path / "not-there"))
    assert job["status"] == "failed"
    assert "isn't mounted" in job["error"]


def test_not_enough_space(root, source):
    connect_fake()
    source.get(DOWNLOAD).mock(side_effect=ranged())
    db.set_setting("free_space_reserve_gb", str(10 ** 9))
    job, _ = run(add_job(root))
    assert job["status"] == "failed"
    assert "Not enough free space" in job["error"]


def test_bandwidth_cap_sleeps(root, source, monkeypatch):
    connect_fake()
    source.get(DOWNLOAD).mock(side_effect=ranged())
    db.set_setting("bandwidth_kbit", "80")
    slept = []

    async def fake_sleep(s):
        slept.append(s)
    monkeypatch.setattr(queue.asyncio, "sleep", fake_sleep)
    job, _ = run(add_job(root))
    assert job["status"] == "done"
    # 80 Kbps is 10,000 bytes/s, so ~10s of waiting in total for 100 KB.
    assert sum(slept) == pytest.approx(len(DATA) / 10_000, rel=0.2)


def test_cancel_running_job_deletes_part(root, source):
    connect_fake()
    started = None

    async def scenario():
        nonlocal started
        started = asyncio.Event()

        async def slow():
            yield DATA[:5000]
            started.set()
            await asyncio.sleep(3600)
            yield b""

        source.get(DOWNLOAD).mock(return_value=httpx.Response(200, content=slow()))
        job_id = add_job(root)
        w = queue.Worker()
        w.running[job_id] = asyncio.create_task(w.run_job(job_id))
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.sleep(0.05)
        assert (root / "Heat (1995)" / "Heat (1995).mkv.j2jpart").exists()
        w.interrupt(job_id, "cancelled")
        await asyncio.gather(*w.running.values(), return_exceptions=True)
        return job_id

    job_id = asyncio.run(scenario())
    assert db.get_job(job_id)["status"] == "cancelled"
    assert not (root / "Heat (1995)" / "Heat (1995).mkv.j2jpart").exists()


def test_cancel_queued_job_deletes_part(root):
    job_id = add_job(root)
    part = root / "Heat (1995)" / "Heat (1995).mkv.j2jpart"
    part.parent.mkdir()
    part.write_bytes(b"partial")
    assert queue.cancel(job_id)
    assert db.get_job(job_id)["status"] == "cancelled"
    assert not part.exists()
    assert not queue.cancel(job_id)


def test_duplicates_are_not_queued_twice(root):
    add_job(root)
    added, dupes = db.add_jobs([{"server_id": "srv123", "source_item_id": ITEM, "kind": "movie",
                                 "display_name": "x", "dest_root": str(root),
                                 "target_path": str(root / "other.mkv"),
                                 "download_path": "/x", "size": 1}])
    assert (added, dupes) == (0, 1)
    # Same target from a different item is a duplicate too.
    added, dupes = db.add_jobs([{"server_id": "srv123", "source_item_id": "e" * 32, "kind": "movie",
                                 "display_name": "x", "dest_root": str(root),
                                 "target_path": str(target(root)), "download_path": "/x", "size": 1}])
    assert (added, dupes) == (0, 1)


def test_tick_respects_server_concurrency_and_pause(root, source, monkeypatch):
    connect_fake(server_id="srv123")
    mine = [add_job(root, rel=f"m{i}.mkv", item=f"{i:032x}") for i in range(3)]
    other = add_job(root, rel="other.mkv", server_id="someone-else", item="f" * 32)
    started = []

    async def fake_run_job(self, job_id):
        started.append(job_id)
        await asyncio.sleep(3600)

    monkeypatch.setattr(queue.Worker, "run_job", fake_run_job)

    async def scenario():
        w = queue.Worker()
        db.set_setting("concurrency", "2")
        await w.tick()
        await asyncio.sleep(0)
        assert started == mine[:2]
        db.set_setting("queue_paused", "1")
        await w.tick()
        await asyncio.gather(*w.running.values(), return_exceptions=True)

    asyncio.run(scenario())
    assert other not in started


def test_restart_requeues_interrupted_jobs(root):
    job_id = add_job(root)
    db.update_job(job_id, status="downloading")

    async def scenario():
        w = queue.Worker()
        w.start()
        await w.stop()

    asyncio.run(scenario())
    assert db.get_job(job_id)["status"] == "queued"


# ---- API ---------------------------------------------------------------------

def test_queue_api(logged_in, root):
    connect_fake()
    job_id = add_job(root)
    snap = logged_in.get("/api/queue").json()
    assert snap["counts"] == {"queued": 1}
    assert snap["jobs"][0]["target"] == "Heat (1995)/Heat (1995).mkv"
    assert str(root) in snap["free"]

    assert logged_in.post("/api/queue/pause").json()["paused"] is True
    assert logged_in.post("/api/queue/resume").json()["paused"] is False

    db.update_job(job_id, status="failed", error="boom", attempts=3)
    snap = logged_in.post("/api/queue/retry-failed").json()
    assert snap["jobs"][0]["status"] == "queued"
    assert snap["jobs"][0]["error"] == ""

    assert logged_in.post(f"/api/queue/jobs/{job_id}/cancel").status_code == 200
    assert logged_in.post(f"/api/queue/jobs/{job_id}/cancel").status_code == 400
    snap = logged_in.post("/api/queue/clear-finished").json()
    assert snap["jobs"] == []


def test_resume_needs_a_connection(logged_in):
    db.set_setting("queue_paused", "1")
    r = logged_in.post("/api/queue/resume")
    assert r.status_code == 409
    assert db.get_setting("queue_paused") == "1"


def test_paused_jobs_resume_together(logged_in, root):
    connect_fake()
    job_id = add_job(root)
    db.update_job(job_id, status="paused")
    db.set_setting("queue_paused", "1")
    logged_in.post("/api/queue/resume")
    assert db.get_job(job_id)["status"] == "queued"


def test_status_page_has_queue(logged_in):
    page = logged_in.get("/").text
    assert 'id="queue-list"' in page
    assert "/static/js/queue.js" in page
