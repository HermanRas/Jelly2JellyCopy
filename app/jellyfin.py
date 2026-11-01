"""Async Jellyfin client for the source server (user token).

Every call carries the MediaBrowser Authorization header. That is the form
Jellyfin 10.9+ and 12.x accept; the old X-Emby-Token header is not relied on.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app import config

TIMEOUT = httpx.Timeout(20.0, connect=10.0)


class SourceError(Exception):
    """A failure worth showing on the connection card, in plain words."""

    code = "error"

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class BadUrl(SourceError):
    code = "bad_url"


class Unreachable(SourceError):
    code = "unreachable"


class NotJellyfin(SourceError):
    code = "not_jellyfin"


class WrongPassword(SourceError):
    code = "wrong_password"


class NoDownloadPermission(SourceError):
    code = "no_download"


class TokenRejected(SourceError):
    """The stored token stopped working (logged out elsewhere, user removed…)."""

    code = "token_rejected"


def auth_header(device_id: str, token: str | None = None) -> str:
    parts = (f'Client="{config.APP_NAME}", Device="{config.APP_NAME}", '
             f'DeviceId="{device_id}", Version="{config.VERSION}"')
    if token:
        parts += f', Token="{token}"'
    return f"MediaBrowser {parts}"


def normalise_url(raw: str) -> str:
    """Accept what people actually paste: a bare host, a trailing slash, or a
    link copied from the web client (https://host/web/#/home)."""
    url = raw.strip()
    if not url:
        raise BadUrl("Enter the server's address.")
    # Only a bare host gets https:// added; "ftp://x" must be refused, not
    # turned into "https://ftp://x".
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        raise BadUrl("That doesn't look like a web address.")
    path = parts.path
    # Everything from /web on belongs to the web client, not the API base.
    path = re.split(r"/web(?:/|$)", path, maxsplit=1)[0]
    return f"{parts.scheme.lower()}://{parts.netloc}{path}".rstrip("/")


@dataclass
class Login:
    url: str
    server_name: str
    server_id: str
    version: str
    user_name: str
    user_id: str
    token: str


def make_client(base_url: str, device_id: str, token: str | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT, follow_redirects=True,
                             headers={"Authorization": auth_header(device_id, token)})


async def public_info(client: httpx.AsyncClient) -> dict:
    try:
        r = await client.get("/System/Info/Public")
    except httpx.InvalidURL:
        raise BadUrl("That doesn't look like a web address.")
    except httpx.HTTPError as e:
        raise Unreachable(f"Couldn't reach the server ({type(e).__name__}).")
    try:
        info = r.json() if r.is_success else None
    except ValueError:
        info = None
    if not info or not info.get("Id") or "Version" not in info:
        raise NotJellyfin("Something answered at that address, but it isn't a Jellyfin server.")
    return info


async def logout(base_url: str, device_id: str, token: str) -> None:
    """Best effort: if the source is down, the token is still forgotten locally."""
    try:
        async with make_client(base_url, device_id, token) as c:
            await c.post("/Sessions/Logout")
    except httpx.HTTPError:
        pass


async def login(raw_url: str, username: str, password: str, device_id: str) -> Login:
    url = normalise_url(raw_url)
    async with make_client(url, device_id) as c:
        info = await public_info(c)
        try:
            r = await c.post("/Users/AuthenticateByName", json={"Username": username, "Pw": password})
        except httpx.HTTPError as e:
            raise Unreachable(f"The server stopped answering during login ({type(e).__name__}).")
    if r.status_code in (401, 403):
        raise WrongPassword("Wrong username or password.")
    if not r.is_success:
        raise SourceError(f"The server refused the login (HTTP {r.status_code}).")

    body = r.json()
    token = body["AccessToken"]
    user = body["User"]
    if not user.get("Policy", {}).get("EnableContentDownloading"):
        # Don't leave a session behind on someone else's server for a login
        # that is of no use to us.
        await logout(url, device_id, token)
        raise NoDownloadPermission(
            "This user isn't allowed to download. Ask the server's admin to tick "
            "\"Allow media downloading\" for this user.")

    return Login(url=url, server_name=info.get("ServerName") or url, server_id=info["Id"],
                 version=info["Version"], user_name=user.get("Name") or username,
                 user_id=user["Id"], token=token)
