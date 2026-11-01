"""The one source connection: connect, disconnect, and authenticated requests.

The token is the only credential kept, encrypted. Disconnect and a 401 from
the source both destroy it and pause the queue, so nothing keeps hammering a
server that has stopped accepting us.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx

from app import auth, db, jellyfin

# Downloads: a slow source may pause a while between chunks of a multi-GB file.
STREAM_TIMEOUT = httpx.Timeout(connect=15.0, read=120.0, write=30.0, pool=30.0)


def current() -> dict | None:
    """What the UI may see about the connection: never the token."""
    row = db.get_connection()
    if row is None:
        return None
    return {k: row[k] for k in ("url", "server_name", "server_id", "version",
                                "user_name", "user_id", "connected_at")}


async def connect(url: str, username: str, password: str) -> dict:
    device = db.device_id()
    login = await jellyfin.login(url, username, password, device)
    # Replacing an existing connection: end the old session on its server too.
    await disconnect()
    db.save_connection(url=login.url, server_name=login.server_name, server_id=login.server_id,
                       version=login.version, user_name=login.user_name, user_id=login.user_id,
                       token_enc=auth.encrypt_token(login.token))
    # A queue paused by a disconnect or a rejected token stays paused: after a
    # reconnect (perhaps to a different account) the user resumes it on purpose.
    return current()


def _forget() -> None:
    db.delete_connection()
    db.set_setting("queue_paused", "1")


async def disconnect() -> None:
    row = db.get_connection()
    if row is None:
        return
    token = auth.decrypt_token(row["token_enc"])
    # Forget locally first: even if the logout call hangs or fails, the token
    # must be gone from here.
    _forget()
    if token:
        await jellyfin.logout(row["url"], db.device_id(), token)


def _credentials() -> tuple[str, str]:
    row = db.get_connection()
    token = auth.decrypt_token(row["token_enc"]) if row else None
    if not token:
        if row:
            _forget()
        raise jellyfin.TokenRejected("Not connected to a source server.")
    return row["url"], token


@asynccontextmanager
async def stream(path: str, headers: dict | None = None) -> AsyncIterator[httpx.Response]:
    """A streaming GET for downloads. Same 401 rule as request(); transport
    errors are left to the caller, which retries and resumes."""
    url, token = _credentials()
    async with jellyfin.make_client(url, db.device_id(), token) as client:
        client.timeout = STREAM_TIMEOUT
        async with client.stream("GET", path, headers=headers) as r:
            if r.status_code == 401:
                _forget()
                raise jellyfin.TokenRejected("The source server no longer accepts our login. Connect again.")
            yield r


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    """An authenticated call to the source. A 401 destroys the stored token
    and raises TokenRejected, so the UI can show "Reconnect"."""
    url, token = _credentials()
    client = jellyfin.make_client(url, db.device_id(), token)
    async with client:
        try:
            r = await client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise jellyfin.Unreachable(f"Couldn't reach the source server ({type(e).__name__}).")
    if r.status_code == 401:
        _forget()
        raise jellyfin.TokenRejected("The source server no longer accepts our login. Connect again.")
    return r
