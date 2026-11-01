"""FastAPI entrypoint: routes, the login gate and /healthz."""
from __future__ import annotations

from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app import auth, config

app = FastAPI(title=config.APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)

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
    return templates.TemplateResponse(request, "status.html", {"title": "Status"})
