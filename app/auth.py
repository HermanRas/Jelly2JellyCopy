"""App login: one shared password, a signed session cookie, and a rate limit.

The app holds a working token for someone else's server and can write into
the media folders, so every page except /login and /healthz sits behind this.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app import config

_secret: str | None = None


def secret() -> str:
    """J2J_SECRET if set, otherwise a random one persisted in the data dir.

    Persisted rather than generated per start, so sessions (and, later, the
    encrypted source token) survive a container restart.
    """
    global _secret
    if _secret is None:
        if config.SECRET:
            _secret = config.SECRET
        else:
            path = config.DATA_DIR / "secret.key"
            if not path.exists():
                config.DATA_DIR.mkdir(parents=True, exist_ok=True)
                path.write_text(secrets.token_urlsafe(48))
                path.chmod(0o600)
            _secret = path.read_text().strip()
    return _secret


def using_default_password() -> bool:
    return config.APP_PASSWORD == config.DEFAULT_APP_PASSWORD


def check_password(given: str) -> bool:
    return hmac.compare_digest(given.encode(), config.APP_PASSWORD.encode())


def _password_fingerprint() -> str:
    # Baked into the session, so changing J2J_APP_PASSWORD logs everyone out
    # without needing a sessions table.
    return hmac.new(secret().encode(), config.APP_PASSWORD.encode(), hashlib.sha256).hexdigest()[:16]


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(secret(), salt="j2j-session")


def make_session() -> str:
    return _serializer().dumps({"pw": _password_fingerprint()})


def valid_session(cookie: str | None) -> bool:
    if not cookie:
        return False
    try:
        data = _serializer().loads(cookie, max_age=config.SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return False
    return hmac.compare_digest(str(data.get("pw", "")), _password_fingerprint())


# In memory on purpose: a restart clearing the counter is fine for one
# shared password behind a reverse proxy, and it keeps the limiter free of db
# writes on every bad guess.
_failures: dict[str, deque[float]] = defaultdict(deque)


def _recent(addr: str) -> deque[float]:
    q = _failures[addr]
    cutoff = time.monotonic() - config.LOGIN_WINDOW_SECONDS
    while q and q[0] < cutoff:
        q.popleft()
    return q


def is_rate_limited(addr: str) -> bool:
    return len(_recent(addr)) >= config.LOGIN_MAX_FAILURES


def record_failure(addr: str) -> None:
    _recent(addr).append(time.monotonic())


def clear_failures(addr: str) -> None:
    _failures.pop(addr, None)
