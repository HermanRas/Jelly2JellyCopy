"""The one source connection: connect, disconnect, and authenticated requests.

The token is the only credential kept, encrypted. Disconnect and a 401 from
the source both destroy it and pause the queue, so nothing keeps hammering a
server that has stopped accepting us.
"""
from __future__ import annotations

import httpx

from app import auth, db, jellyfin


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
    db.set_setting("queue_paused", "0")
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


async def request(method: str, path: str, **kwargs) -> httpx.Response:
    """An authenticated call to the source. A 401 destroys the stored token
    and raises TokenRejected, so the UI can show "Reconnect"."""
    row = db.get_connection()
    token = auth.decrypt_token(row["token_enc"]) if row else None
    if not token:
        if row:
            _forget()
        raise jellyfin.TokenRejected("Not connected to a source server.")
    client = jellyfin.make_client(row["url"], db.device_id(), token)
    async with client:
        try:
            r = await client.request(method, path, **kwargs)
        except httpx.HTTPError as e:
            raise jellyfin.Unreachable(f"Couldn't reach the source server ({type(e).__name__}).")
    if r.status_code == 401:
        _forget()
        raise jellyfin.TokenRejected("The source server no longer accepts our login. Connect again.")
    return r
