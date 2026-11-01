from app import config


def test_healthz_is_public(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_pages_redirect_to_login(client):
    r = client.get("/?x=1")
    assert r.status_code == 303
    assert r.headers["location"] == "/login?next=/%3Fx%3D1"


def test_api_answers_401_instead_of_redirecting(client):
    assert client.get("/api/anything").status_code == 401


def test_login_page_and_static_are_public(client):
    assert client.get("/login").status_code == 200
    assert client.get("/static/css/theme.css").status_code == 200
    r = client.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/x-icon"


def test_wrong_password(client):
    r = client.post("/login", data={"password": "nope"})
    assert r.status_code == 401
    assert "Wrong password" in r.text
    assert config.SESSION_COOKIE not in r.cookies


def test_login_sets_strict_httponly_cookie(client):
    r = client.post("/login", data={"password": config.DEFAULT_APP_PASSWORD, "next": "/"})
    assert r.status_code == 303
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie
    assert "samesite=strict" in cookie
    # TestClient talks plain http, so no Secure flag here.
    assert "secure" not in cookie


def test_logged_in_sees_status(logged_in):
    r = logged_in.get("/")
    assert r.status_code == 200
    assert "Connection status" in r.text


def test_next_cannot_leave_the_site(client):
    r = client.post("/login", data={"password": config.DEFAULT_APP_PASSWORD, "next": "//evil.example/x"})
    assert r.headers["location"] == "/"
    r = client.post("/login", data={"password": config.DEFAULT_APP_PASSWORD, "next": "https://evil.example"})
    assert r.headers["location"] == "/"


def test_default_password_banner(logged_in, monkeypatch):
    assert "default app password is in use" in logged_in.get("/").text


def test_no_banner_with_own_password(client, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "something-else")
    client.post("/login", data={"password": "something-else"})
    r = client.get("/")
    assert r.status_code == 200
    assert "default app password is in use" not in r.text


def test_changing_password_ends_sessions(logged_in, monkeypatch):
    monkeypatch.setattr(config, "APP_PASSWORD", "rotated")
    assert logged_in.get("/").status_code == 303


def test_tampered_cookie_is_rejected(client):
    client.cookies.set(config.SESSION_COOKIE, "forged.value.here")
    assert client.get("/").status_code == 303


def test_logout_clears_session(logged_in):
    r = logged_in.post("/logout")
    assert r.status_code == 303
    assert logged_in.get("/").status_code == 303


def test_login_is_rate_limited(client):
    for _ in range(config.LOGIN_MAX_FAILURES):
        assert client.post("/login", data={"password": "bad"}).status_code == 401
    # Even the right password is refused while the limit holds.
    r = client.post("/login", data={"password": config.DEFAULT_APP_PASSWORD})
    assert r.status_code == 429
