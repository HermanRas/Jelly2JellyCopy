"""The download worker: one asyncio task in the app process.

Each job streams one file from the source's /Items/{id}/Download (the
original, no transcode) into `<target>.j2jpart` *in the target folder* - a
staging dir elsewhere would make the final rename cross bind mounts, which
fails with EXDEV even on the same disk. A dropped connection resumes with
Range from the part file's size; the byte count is checked against the
source's size; only then is the part renamed into place. Jellyfin ignores
.j2jpart, so half-written files never show up in a library.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from app import config, connection, db, dest_folders, jellyfin, naming

log = logging.getLogger("j2j.queue")

PART = ".j2jpart"
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (30, 120, 300)
IDLE_SCAN_SECONDS = 120
PROGRESS_SAVE_SECONDS = 2.0


class Fatal(Exception):
    """Retrying won't help (no permission, file gone, disk full, name clash)."""


def concurrency() -> int:
    try:
        return min(max(int(db.get_setting("concurrency", "1")), 1), 4)
    except ValueError:
        return 1


def bandwidth_kbit() -> int:
    """Cap in kilobits/s, as the settings page shows it; 0 = unlimited."""
    try:
        kbit = db.get_setting("bandwidth_kbit")
        if kbit is not None:
            return max(int(kbit), 0)
        # Before 1.0.2 the cap was stored in KB/s (1024 bytes).
        return round(max(int(db.get_setting("bandwidth_kbps", "0")), 0) * 1024 * 8 / 1000)
    except ValueError:
        return 0


def bandwidth_bytes() -> int:
    """Cap in bytes/s; 0 = unlimited."""
    return bandwidth_kbit() * 1000 // 8


def reserve_bytes() -> int:
    try:
        gb = int(db.get_setting("free_space_reserve_gb", str(config.FREE_SPACE_RESERVE_GB)))
    except ValueError:
        gb = config.FREE_SPACE_RESERVE_GB
    return max(gb, 0) * 1_000_000_000


def is_paused() -> bool:
    return db.get_setting("queue_paused", "0") == "1"


def _unlink(path: str) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


class Worker:
    def __init__(self) -> None:
        self.running: dict[int, asyncio.Task] = {}
        # Live numbers for the UI; the db copy of bytes_done is only saved
        # every couple of seconds.
        self.progress: dict[int, dict] = {}
        self._stop_reason: dict[int, str] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._scan_due = False
        self._idle_since: float | None = None

    # ---- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        # Survives restarts: anything mid-download when the container stopped
        # goes back in the queue and resumes from its .j2jpart.
        db.set_status_where("queued", ("downloading",))
        self._task = asyncio.create_task(self._loop(), name="j2j-worker")

    async def stop(self) -> None:
        for job_id in list(self.running):
            self.interrupt(job_id, "shutdown")
        if self.running:
            await asyncio.gather(*self.running.values(), return_exceptions=True)
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    def poke(self) -> None:
        self._wake.set()

    def interrupt(self, job_id: int, reason: str) -> bool:
        task = self.running.get(job_id)
        if task is None:
            return False
        self._stop_reason[job_id] = reason
        task.cancel()
        return True

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("queue tick failed")
            try:
                await asyncio.wait_for(self._wake.wait(), 1.0)
            except TimeoutError:
                pass
            self._wake.clear()

    async def tick(self) -> None:
        conn = connection.current()
        if is_paused() or conn is None:
            for job_id in list(self.running):
                self.interrupt(job_id, "paused")
        else:
            while len(self.running) < concurrency():
                job = db.next_job(conn["server_id"], set(self.running), time.time())
                if job is None:
                    break
                self.running[job["id"]] = asyncio.create_task(self.run_job(job["id"]))
        await self._maybe_scan()

    async def _maybe_scan(self) -> None:
        """One library scan once the queue has been quiet for a while, rather
        than one per file - a scan per episode would keep Jellyfin busy for
        the whole of a long season."""
        if not self._scan_due:
            return
        if self.running:
            self._idle_since = None
            return
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
        elif now - self._idle_since >= IDLE_SCAN_SECONDS:
            self._scan_due = False
            self._idle_since = None
            ok = await dest_folders.refresh_library()
            log.info("library scan %s", "requested" if ok else "skipped (no API key or Jellyfin down)")

    # ---- one job --------------------------------------------------------------

    async def run_job(self, job_id: int) -> None:
        job = db.get_job(job_id)
        db.update_job(job_id, status="downloading", error="")
        self.progress[job_id] = {"bytes": job["bytes_done"], "speed": 0, "received": 0}
        try:
            result = await self.download(job)
            db.update_job(job_id, status=result, error="",
                          bytes_done=job["size"] or self.progress[job_id]["bytes"])
            if result == "done":
                self._scan_due = True
        except asyncio.CancelledError:
            reason = self._stop_reason.pop(job_id, "shutdown")
            if reason == "cancelled":
                _unlink(job["target_path"] + PART)
                db.update_job(job_id, status="cancelled", error="")
            elif reason == "paused":
                db.update_job(job_id, status="paused", bytes_done=self._bytes(job_id, job))
            else:
                db.update_job(job_id, status="queued", bytes_done=self._bytes(job_id, job))
        except jellyfin.TokenRejected as e:
            # The token is already gone and the queue paused (connection.stream);
            # this job just waits for the next connect.
            db.update_job(job_id, status="paused", error=e.message, bytes_done=self._bytes(job_id, job))
        except (Fatal, naming.UnsafePath) as e:
            db.update_job(job_id, status="failed", error=str(e), bytes_done=self._bytes(job_id, job))
        except Exception as e:  # network trouble, short reads, odd answers: retry
            message = str(e) or type(e).__name__
            # A multi-GB file over a flaky link may drop several times while
            # still making progress; only attempts that got nothing count.
            received = self.progress.get(job_id, {}).get("received", 0)
            attempts = job["attempts"] + (0 if received else 1)
            if attempts >= MAX_ATTEMPTS:
                db.update_job(job_id, status="failed", attempts=attempts, error=message,
                              bytes_done=self._bytes(job_id, job))
            else:
                delay = BACKOFF_SECONDS[min(attempts, len(BACKOFF_SECONDS) - 1)]
                db.update_job(job_id, status="queued", attempts=attempts,
                              error=f"Retrying in {delay}s: {message}",
                              not_before=time.time() + delay, bytes_done=self._bytes(job_id, job))
            log.warning("job %s: %s", job_id, message)
        finally:
            self.running.pop(job_id, None)
            self.progress.pop(job_id, None)
            self._stop_reason.pop(job_id, None)
            self._wake.set()

    def _bytes(self, job_id: int, job) -> int:
        return self.progress.get(job_id, {}).get("bytes", job["bytes_done"])

    async def download(self, job) -> str:
        """Returns "done" or "skipped"."""
        root, target, size = job["dest_root"], job["target_path"], job["size"]
        if not Path(root).is_dir():
            raise Fatal(f"The destination folder {root} isn't mounted in Jelly2JellyCopy.")
        naming.check_inside(root, target)

        if os.path.exists(target):
            if size is None or os.path.getsize(target) == size:
                return "skipped"
            raise Fatal("A different file with this name is already there; it was left alone.")

        part = target + PART
        os.makedirs(os.path.dirname(target), exist_ok=True)
        # Again now the folders exist, so any symlink among them is followed.
        naming.check_inside(root, target)
        offset = os.path.getsize(part) if os.path.exists(part) else 0
        if size is not None and offset > size:
            _unlink(part)
            offset = 0

        if size is not None:
            need = size - offset
            free = shutil.disk_usage(root).free
            if need > 0 and free - need < reserve_bytes():
                raise Fatal(f"Not enough free space in {root}: needs {need / 1e9:.1f} GB "
                            f"plus the {reserve_bytes() / 1e9:.0f} GB reserve.")

        if size is None or offset < size:
            await self._fetch(job, part, offset)

        final = os.path.getsize(part)
        if size is not None and final != size:
            if final > size:
                _unlink(part)
                raise RuntimeError(f"Got {final} bytes but the source said {size}; starting over.")
            raise RuntimeError(f"The connection ended at {final} of {size} bytes.")
        os.replace(part, target)
        return "done"

    async def _fetch(self, job, part: str, offset: int) -> None:
        headers = {"Range": f"bytes={offset}-"} if offset else None
        async with connection.stream(job["download_path"], headers) as r:
            if r.status_code == 416 and offset:
                return            # nothing left to send; the size check decides
            if r.status_code == 403:
                raise Fatal("The source refused the download (403). The user may have lost "
                            "\"Allow media downloading\".")
            if r.status_code == 404:
                raise Fatal("This item is no longer on the source server.")
            if not r.is_success:
                raise RuntimeError(f"The source answered HTTP {r.status_code}.")
            if offset and r.status_code == 200:
                offset = 0        # the server ignored Range and sent the whole file
            elif offset and not r.headers.get("content-range", "").startswith(f"bytes {offset}-"):
                raise RuntimeError("The source resumed from the wrong place.")

            job_id = job["id"]
            progress = self.progress[job_id]
            done, received = offset, 0
            cap = bandwidth_bytes()
            started = last_save = last_tick = time.monotonic()
            last_bytes = done
            with open(part, "ab" if offset else "wb") as f:
                # No chunk_size: httpx would hold data back until it had a
                # full chunk, and a dropped connection would lose that part.
                async for chunk in r.aiter_bytes():
                    f.write(chunk)
                    done += len(chunk)
                    received += len(chunk)
                    progress["received"] = received
                    now = time.monotonic()
                    if now - last_tick >= 1.0:
                        speed = (done - last_bytes) / (now - last_tick)
                        old = progress["speed"]
                        progress["speed"] = speed if not old else 0.7 * old + 0.3 * speed
                        last_tick, last_bytes = now, done
                    progress["bytes"] = done
                    if now - last_save >= PROGRESS_SAVE_SECONDS:
                        db.update_job(job_id, bytes_done=done)
                        last_save = now
                    if cap:
                        ahead = received / cap - (now - started)
                        if ahead > 0:
                            await asyncio.sleep(ahead)


worker = Worker()


# ---- what the UI and API see ----------------------------------------------------

def job_view(row) -> dict:
    live = worker.progress.get(row["id"])
    return {
        "id": row["id"], "kind": row["kind"], "name": row["display_name"],
        "status": row["status"], "size": row["size"],
        "bytes_done": live["bytes"] if live else row["bytes_done"],
        "speed": round(live["speed"]) if live else 0,
        "error": row["error"], "dest_root": row["dest_root"],
        "target": os.path.relpath(row["target_path"], row["dest_root"]),
    }


def snapshot() -> dict:
    counts = db.job_counts()
    free = {}
    for root in db.active_roots():
        try:
            free[root] = shutil.disk_usage(root).free
        except OSError:
            free[root] = None
    return {
        "paused": is_paused(),
        "connected": connection.current() is not None,
        "counts": counts,
        "free": free,
        "jobs": [job_view(r) for r in db.list_jobs()],
    }


def cancel(job_id: int) -> bool:
    job = db.get_job(job_id)
    if job is None or job["status"] not in db.ACTIVE:
        return False
    if not worker.interrupt(job_id, "cancelled"):
        _unlink(job["target_path"] + PART)
        db.update_job(job_id, status="cancelled", error="")
    return True


def retry(job_id: int) -> bool:
    job = db.get_job(job_id)
    if job is None or job["status"] != "failed":
        return False
    db.update_job(job_id, status="queued", attempts=0, error="", not_before=0)
    worker.poke()
    return True


def retry_failed() -> int:
    n = db.set_status_where("queued", ("failed",), attempts=0, error="", not_before=0)
    worker.poke()
    return n


def pause() -> None:
    db.set_setting("queue_paused", "1")
    worker.poke()


def resume() -> None:
    db.set_status_where("queued", ("paused",), error="")
    db.set_setting("queue_paused", "0")
    worker.poke()
