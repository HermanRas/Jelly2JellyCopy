"""FastAPI entrypoint: routes, the login gate and /healthz."""
from __future__ import annotations

from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app import auth, browse, config, connection, db, dest_folders, jellyfin


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init()
    yield


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


@app.post("/api/libraries/{library_id}/dest")
async def api_set_dest(library_id: str, body: DestBody):
    if not browse.valid_id(library_id):
        return JSONResponse({"error": "bad_id"}, status_code=400)
    lib = await browse.library(library_id)
    if lib is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    folders = dest_folders.offered(await dest_folders.detect(), lib["type"])
    # Only a folder we offered and can write to: this path later decides
    # where files land on disk, so it is never taken on trust from the browser.
    if not any(f.path == body.path and f.available for f in folders):
        return JSONResponse({"error": "bad_folder", "detail": "That folder isn't on offer."},
                            status_code=400)
    db.set_setting(_dest_key(library_id), body.path)
    return {"path": body.path}


@app.get("/api/items/{item_id}/poster")
async def api_poster(item_id: str):
    if not browse.valid_id(item_id):
        return Response(status_code=404)
    return await browse.poster(item_id)
