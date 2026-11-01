"""FastAPI entrypoint: routes, the login gate and /healthz."""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Literal
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import (FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response,
                               StreamingResponse)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import auth, browse, config, connection, db, dest_folders, enqueue, jellyfin, mine, queue


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    if config.WORKER_ENABLED:
        queue.worker.start()
    yield
    if config.WORKER_ENABLED:
        await queue.worker.stop()


app = FastAPI(title=config.APP_NAME, docs_url=None, redoc_url=None, openapi_url=None,
              lifespan=lifespan)

# Two static mounts: our own files from the repo, vendored assets from the image.
app.mount("/static", StaticFiles(directory=config.BASE_DIR / "static"), name="static")
if config.VENDOR_DIR.is_dir():
    app.mount("/vendor", StaticFiles(directory=config.VENDOR_DIR), name="vendor")

templates = Jinja2Templates(directory=config.BASE_DIR / "templates")
templates.env.globals["app_name"] = config.APP_NAME
templates.env.globals["version"] = config.VERSION
# A function, not a value: tests (and a future settings reload) change the
# password without re-importing this module.
templates.env.globals["default_password"] = auth.using_default_password
templates.env.globals["connected"] = lambda: connection.current() is not None

# Reachable without a session. Static files carry nothing sensitive, and the
# login page needs its stylesheet and icon.
PUBLIC_PREFIXES = ("/login", "/healthz", "/favicon.ico", "/static/", "/vendor/")


@app.middleware("http")
async def require_login(request: Request, call_next):
    path = request.url.path
    if path.startswith(PUBLIC_PREFIXES) or auth.valid_session(request.cookies.get(config.SESSION_COOKIE)):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "login required"}, status_code=401)
    target = path + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(f"/login?next={quote(target)}", status_code=303)


@app.exception_handler(jellyfin.SourceError)
async def source_error(request: Request, exc: jellyfin.SourceError):
    """Problems with the source server, in plain words.

    A rejected token has already been forgotten by connection.request, so the
    answer is always "connect again": pages go back to the status card, API
    callers get 409 (not 401, which the browser code reads as "app login
    expired").
    """
    api = request.url.path.startswith("/api/")
    if isinstance(exc, jellyfin.TokenRejected):
        if api:
            return JSONResponse({"error": "reconnect", "detail": exc.message}, status_code=409)
        return RedirectResponse("/", status_code=303)
    if api:
        return JSONResponse({"error": exc.code, "detail": exc.message}, status_code=502)
    return templates.TemplateResponse(request, "error.html",
                                      {"title": "Source server problem", "detail": exc.message},
                                      status_code=502)


def _safe_next(next_url: str) -> str:
    # Only same-site paths; "//evil.example" is a scheme-relative URL to
    # another host, so it is refused too.
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return "/"


def _client_addr(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@app.get("/healthz")
async def healthz() -> JSONResponse:
    # "vendor" lets CI fail an image whose fonts and icons never made it in,
    # which a bare "is uvicorn listening" check would call healthy.
    return JSONResponse({"status": "ok", "app": config.APP_NAME, "version": config.VERSION,
                         "vendor": config.VENDOR_DIR.is_dir()})


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> FileResponse:
    # Browsers ask for this at the root regardless of the <link> tags.
    return FileResponse(config.BASE_DIR / "static" / "img" / "favicon.ico", media_type="image/x-icon")


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = "/"):
    if auth.valid_session(request.cookies.get(config.SESSION_COOKIE)):
        return RedirectResponse(_safe_next(next), status_code=303)
    return templates.TemplateResponse(request, "login.html", {"title": "Log in", "next": next})


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request, password: str = Form(""), next: str = Form("/")):
    addr = _client_addr(request)
    if auth.is_rate_limited(addr):
        return templates.TemplateResponse(
            request, "login.html",
            {"title": "Log in", "next": next, "error": "Too many attempts. Wait a few minutes and try again."},
            status_code=429)
    if not auth.check_password(password):
        auth.record_failure(addr)
        return templates.TemplateResponse(
            request, "login.html",
            {"title": "Log in", "next": next, "error": "Wrong password."},
            status_code=401)

    auth.clear_failures(addr)
    resp = RedirectResponse(_safe_next(next), status_code=303)
    resp.set_cookie(
        config.SESSION_COOKIE, auth.make_session(),
        max_age=config.SESSION_MAX_AGE, httponly=True, samesite="strict",
        # Plain http is allowed for a LAN-only setup; behind the proxy,
        # --proxy-headers makes the scheme https and the cookie Secure.
        secure=request.url.scheme == "https",
    )
    return resp


@app.post("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(config.SESSION_COOKIE)
    return resp


@app.get("/", response_class=HTMLResponse)
async def status_page(request: Request):
    return templates.TemplateResponse(request, "status.html",
                                      {"title": "Status", "conn": connection.current()})


class ConnectBody(BaseModel):
    url: str
    username: str
    password: str


@app.get("/api/connection")
async def api_connection():
    return {"connected": (conn := connection.current()) is not None, "connection": conn}


@app.post("/api/connect")
async def api_connect(body: ConnectBody):
    try:
        conn = await connection.connect(body.url, body.username, body.password)
    except jellyfin.SourceError as e:
        # 400 for "your input is wrong", 502 for "the other server is the problem".
        status = 502 if isinstance(e, jellyfin.Unreachable) else 400
        return JSONResponse({"error": e.code, "detail": e.message}, status_code=status)
    return {"connected": True, "connection": conn}


@app.post("/api/disconnect")
async def api_disconnect():
    await connection.disconnect()
    queue.worker.poke()   # stop running downloads now, not on the next tick
    return {"connected": False, "connection": None}


# ---- browse ----------------------------------------------------------------

def _dest_key(library_id: str) -> str:
    return f"dest:{library_id}"


@app.get("/browse", response_class=HTMLResponse)
async def browse_page(request: Request):
    if connection.current() is None:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "browse.html", {
        "title": "Download", "conn": connection.current(), "libraries": await browse.libraries()})


@app.get("/browse/{library_id}", response_class=HTMLResponse)
async def library_page(request: Request, library_id: str):
    if connection.current() is None:
        return RedirectResponse("/", status_code=303)
    lib = await browse.library(library_id) if browse.valid_id(library_id) else None
    if lib is None:
        return RedirectResponse("/browse", status_code=303)

    detection = await dest_folders.detect()
    folders = dest_folders.offered(detection, lib["type"])
    # Pre-select the last folder used for this library, but only if it is
    # still on offer and writable; otherwise the picker starts empty and the
    # user has to choose.
    last = db.get_setting(_dest_key(library_id))
    selected = last if any(f.path == last and f.available for f in folders) else ""
    return templates.TemplateResponse(request, "library.html", {
        "title": lib["name"], "lib": lib, "folders": folders, "selected": selected,
        "detection": detection})


@app.get("/api/libraries/{library_id}/items")
async def api_items(library_id: str, start: int = 0, search: str = "", letter: str = ""):
    if not browse.valid_id(library_id):
        return JSONResponse({"error": "bad_id"}, status_code=400)
    letter = letter[:1].upper() if letter != "#" else "#"
    return await browse.items(library_id, start=start, search=search.strip()[:100], letter=letter)


class DestBody(BaseModel):
    path: str


async def _check_dest(library_id: str, path: str) -> JSONResponse | None:
    """None if `path` is a folder we offer for this library and can write to.

    This path decides where files land on disk, so it is never taken on trust
    from the browser."""
    if not browse.valid_id(library_id):
        return JSONResponse({"error": "bad_id"}, status_code=400)
    lib = await browse.library(library_id)
    if lib is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    folders = dest_folders.offered(await dest_folders.detect(), lib["type"])
    if not any(f.path == path and f.available for f in folders):
        return JSONResponse({"error": "bad_folder",
                             "detail": "Choose a destination folder at the top of the page first."},
                            status_code=400)
    return None


@app.post("/api/libraries/{library_id}/dest")
async def api_set_dest(library_id: str, body: DestBody):
    if (error := await _check_dest(library_id, body.path)) is not None:
        return error
    db.set_setting(_dest_key(library_id), body.path)
    return {"path": body.path}


@app.get("/api/series/{series_id}/seasons")
async def api_seasons(series_id: str):
    if not browse.valid_id(series_id):
        return JSONResponse({"error": "bad_id"}, status_code=400)
    return {"seasons": await enqueue.seasons(series_id)}


class QueueBody(BaseModel):
    library_id: str
    item_id: str
    type: Literal["Series", "Movie", "MusicAlbum"]
    dest: str
    which: Literal["all", "first", "seasons"] = "all"
    season_ids: list[str] = []


@app.post("/api/queue")
async def api_enqueue(body: QueueBody):
    if not browse.valid_id(body.item_id) or not all(browse.valid_id(s) for s in body.season_ids):
        return JSONResponse({"error": "bad_id"}, status_code=400)
    if (error := await _check_dest(body.library_id, body.dest)) is not None:
        return error

    if body.type == "Series":
        plan = await enqueue.plan_series(body.item_id, body.dest, body.which, body.season_ids)
    elif body.type == "Movie":
        plan = await enqueue.plan_movie(body.item_id, body.dest)
    else:
        plan = await enqueue.plan_album(body.item_id, body.dest)

    # Videos first, so each one is queued ahead of its own subtitles.
    added, dupes = db.add_jobs(plan.main_jobs)
    subtitles, _ = db.add_jobs(plan.subtitle_jobs)
    db.set_setting(_dest_key(body.library_id), body.dest)
    queue.worker.poke()
    return {"added": added, "already_queued": dupes, "subtitles": subtitles, "bytes": plan.bytes,
            "unavailable": plan.unavailable, "unsafe": plan.unsafe, "dest": body.dest,
            "kind": {"Series": "episode", "Movie": "movie", "MusicAlbum": "track"}[body.type]}


@app.get("/api/queue/counts")
async def api_queue_counts():
    # Cheap enough to poll from the header on every page.
    return {"counts": db.job_counts(), "paused": queue.is_paused()}


@app.get("/api/items/{item_id}/poster")
async def api_poster(item_id: str):
    if not browse.valid_id(item_id):
        return Response(status_code=404)
    return await browse.poster(item_id)


# ---- queue -----------------------------------------------------------------

@app.get("/api/queue")
async def api_queue():
    return queue.snapshot()


@app.post("/api/queue/pause")
async def api_queue_pause():
    queue.pause()
    return queue.snapshot()


@app.post("/api/queue/resume")
async def api_queue_resume():
    if connection.current() is None:
        return JSONResponse({"error": "reconnect", "detail": "Connect to the source server first."},
                            status_code=409)
    queue.resume()
    return queue.snapshot()


@app.post("/api/queue/retry-failed")
async def api_queue_retry_failed():
    queue.retry_failed()
    return queue.snapshot()


@app.post("/api/queue/clear-finished")
async def api_queue_clear_finished():
    db.clear_finished()
    return queue.snapshot()


@app.post("/api/queue/jobs/{job_id}/cancel")
async def api_job_cancel(job_id: int):
    if not queue.cancel(job_id):
        return JSONResponse({"error": "not_active"}, status_code=400)
    return {"ok": True}


@app.post("/api/queue/jobs/{job_id}/retry")
async def api_job_retry(job_id: int):
    if not queue.retry(job_id):
        return JSONResponse({"error": "not_failed"}, status_code=400)
    return {"ok": True}


# ---- settings ----------------------------------------------------------------

def _settings() -> dict:
    return {
        "concurrency": queue.concurrency(),
        "bandwidth_kbps": queue.bandwidth_bytes() // 1024,
        "free_space_reserve_gb": queue.reserve_bytes() // 1_000_000_000,
        "include_specials": enqueue.include_specials(),
        "subtitles": enqueue.include_subtitles(),
    }


async def _settings_page(request: Request, saved: str = "", errors: list[str] | None = None,
                         status_code: int = 200):
    return templates.TemplateResponse(request, "settings.html", {
        "title": "Settings", "s": _settings(), "saved": saved, "errors": errors or [],
        "detection": await dest_folders.detect(),
        "dest_url": config.DEST_URL, "dest_key": bool(config.DEST_API_KEY),
        "dest_configured": bool(config.DEST_URL and config.DEST_API_KEY),
    }, status_code=status_code)


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, saved: str = ""):
    messages = {"1": "Saved.", "folders": "Looked for destination folders again.",
                "scan": "Asked your Jellyfin to scan its libraries.",
                "scan-failed": "Your Jellyfin didn't accept the scan request."}
    return await _settings_page(request, saved=messages.get(saved, ""))


def _int_in(raw: str, low: int, high: int, label: str, errors: list[str]) -> int | None:
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{label} must be a whole number.")
        return None
    if not low <= value <= high:
        errors.append(f"{label} must be between {low} and {high}.")
        return None
    return value


@app.post("/settings", response_class=HTMLResponse)
async def save_settings(request: Request, concurrency: str = Form("1"), bandwidth_kbps: str = Form("0"),
                        free_space_reserve_gb: str = Form("50"), include_specials: str = Form(""),
                        subtitles: str = Form("")):
    errors: list[str] = []
    values = {
        "concurrency": _int_in(concurrency, 1, 4, "Downloads at a time", errors),
        "bandwidth_kbps": _int_in(bandwidth_kbps, 0, 10_000_000, "Bandwidth cap", errors),
        "free_space_reserve_gb": _int_in(free_space_reserve_gb, 0, 1_000_000, "Keep free", errors),
    }
    if errors:
        return await _settings_page(request, errors=errors, status_code=400)
    for key, value in values.items():
        db.set_setting(key, str(value))
    # Unticked checkboxes aren't sent at all, hence "" as the default.
    db.set_setting("include_specials", "1" if include_specials == "1" else "0")
    db.set_setting("subtitles", "1" if subtitles == "1" else "0")
    queue.worker.poke()
    return RedirectResponse("/settings?saved=1", status_code=303)


@app.post("/settings/redetect")
async def redetect_folders():
    await dest_folders.detect(force=True)
    mine.forget()
    return RedirectResponse("/settings?saved=folders", status_code=303)


@app.post("/settings/scan")
async def scan_library():
    ok = await dest_folders.refresh_library()
    return RedirectResponse(f"/settings?saved={'scan' if ok else 'scan-failed'}", status_code=303)


SSE_INTERVAL = 1.0
SSE_HEARTBEAT = 15.0


@app.get("/api/events")
async def api_events(request: Request):
    """Live queue state as Server-Sent Events: a full snapshot whenever it
    changes (checked every second), and a comment line now and then so
    proxies don't close an idle stream."""
    async def events():
        last, quiet = None, 0.0
        while not await request.is_disconnected():
            data = json.dumps(queue.snapshot(), separators=(",", ":"))
            if data != last:
                last, quiet = data, 0.0
                yield f"event: queue\ndata: {data}\n\n"
            elif quiet >= SSE_HEARTBEAT:
                quiet = 0.0
                yield ": keep-alive\n\n"
            await asyncio.sleep(SSE_INTERVAL)
            quiet += SSE_INTERVAL

    # X-Accel-Buffering: nginx (and so nginx-proxy-manager) would otherwise
    # buffer the stream and deliver progress in lumps.
    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
