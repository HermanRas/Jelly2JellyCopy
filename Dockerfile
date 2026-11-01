# Three stages: a base that both others share, a test stage carrying pytest and
# the suite, and the runtime image that gets published. Runtime is last, so a
# plain `docker build .` produces the shippable image and never the test one.

# ---- base ------------------------------------------------------------------
FROM python:3.12-slim AS base

# curl is only used to vendor the frontend assets at build time.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Keep Python from writing .pyc files into any bind-mounted source.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    J2J_VENDOR_DIR=/vendor \
    J2J_DATA_DIR=/data

# The default app password. Compose `environment:` or `docker run -e` overrides
# it; while it is still this value every page shows a red banner saying so.
ENV J2J_APP_PASSWORD=jelly2jelly

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Fonts and icons are fetched at build time into /vendor, inside the image -
# never into the project root, which is synced to Google Drive.
COPY scripts/fetch-vendor.sh /usr/local/bin/fetch-vendor.sh
RUN chmod +x /usr/local/bin/fetch-vendor.sh && /usr/local/bin/fetch-vendor.sh

COPY app ./app

# ---- test ------------------------------------------------------------------
# Built and run in CI, and usable locally with:
#   docker build --target test -t jelly2jelly-test . && docker run --rm jelly2jelly-test
FROM base AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY tests ./tests

# The suite must not touch /data - that volume holds a real token and queue.
ENV J2J_DATA_DIR=/tmp/j2j-test

CMD ["python", "-m", "pytest", "tests", "-q", "-p", "no:warnings", "-p", "no:cacheprovider"]

# ---- runtime ---------------------------------------------------------------
FROM base AS runtime

# Runs as 1000:1000, the usual PUID/PGID of a linuxserver media stack, so files
# it writes into the media folders belong to the same user as Jellyfin's. A
# named volume copies this ownership on first use; a bind mount must already
# be writable by whatever `user:` compose sets.
RUN mkdir -p /data && chown 1000:1000 /data
USER 1000:1000
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/healthz').read()" || exit 1

# --proxy-headers: behind a reverse proxy the session cookie must be marked
# Secure when the browser is on https, and login rate-limiting must see the
# real client address rather than the proxy's.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
