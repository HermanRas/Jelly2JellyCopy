"""Compatibility spike: confirm the Jellyfin API calls this app relies on
behave as expected on a real server (Jellyfin moved routes between 10.x and
12.x). Useful before pointing the app at a server on an unusual version.

Run it interactively (it prompts for credentials, never stores them):

    docker run --rm -it -e PYTHONDONTWRITEBYTECODE=1 \
      -v "$PWD/scripts:/spike:ro" -v /some/dir:/out \
      python:3.12-slim sh -c "pip install -q httpx && python -I /spike/spike.py"

It writes /out/spike-report.json. Every secret (password, token, API key) is
scrubbed from the report before it is written, and the source session is
logged out at the end, so nothing usable is left behind.
"""

import getpass
import json
import sys
import time
import uuid

import httpx

SECRETS: list[str] = []
REPORT: dict = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "source": [], "dest": []}


def auth_header(token: str | None = None) -> str:
    # Jellyfin 10.9+ wants the MediaBrowser scheme in Authorization; the old
    # X-Emby-Token header is what 12.x is most likely to have dropped.
    parts = 'Client="Jelly2JellyCopy", Device="Jelly2JellyCopy", DeviceId="%s", Version="0.0.1"' % DEVICE_ID
    if token:
        parts += ', Token="%s"' % token
    return "MediaBrowser " + parts


DEVICE_ID = "j2j-spike-" + uuid.uuid4().hex[:12]


def scrub(obj):
    if isinstance(obj, str):
        for s in SECRETS:
            # Very short values (e.g. a 2-letter username) would shred the report.
            if s and len(s) >= 4:
                obj = obj.replace(s, "<redacted>")
        return obj
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items()}
    return obj


def shape(data, depth=0):
    """Summarise JSON so the report shows field names without dumping libraries."""
    if isinstance(data, dict):
        if depth >= 2:
            return sorted(data.keys())
        return {k: shape(v, depth + 1) for k, v in data.items()}
    if isinstance(data, list):
        return {"len": len(data), "first": shape(data[0], depth + 1) if data else None}
    return data


def record(section, name, resp=None, *, note=None, data=None, error=None, t0=None):
    entry = {"step": name}
    if resp is not None:
        entry["request"] = f"{resp.request.method} {resp.request.url.raw_path.decode()}"
        entry["status"] = resp.status_code
        keep = ("content-type", "content-length", "content-range", "accept-ranges", "content-disposition")
        entry["headers"] = {k: v for k, v in resp.headers.items() if k.lower() in keep}
    if t0 is not None:
        entry["ms"] = round((time.monotonic() - t0) * 1000)
    if note is not None:
        entry["note"] = note
    if data is not None:
        entry["data"] = data
    if error is not None:
        entry["error"] = error
    REPORT[section].append(entry)
    status = entry.get("status", "-")
    print(f"  [{status}] {name}" + (f"  !! {error}" if error else ""))
    return entry


def call(client, section, name, method, url, **kw):
    t0 = time.monotonic()
    try:
        resp = client.request(method, url, **kw)
    except httpx.HTTPError as e:
        record(section, name, error=f"{type(e).__name__}: {e}", t0=t0)
        return None
    body = None
    if "json" in resp.headers.get("content-type", "") and resp.content:
        try:
            body = resp.json()
        except ValueError:
            pass
    record(section, name, resp, t0=t0, data=shape(body) if body is not None else None,
           error=None if resp.is_success else resp.text[:300])
    return resp if resp.is_success else None


def first(items, pred=lambda _: True):
    return next((i for i in items if pred(i)), None)


def spike_source(base, username, password):
    print(f"\nSource: {base}")
    s = "source"
    c = httpx.Client(base_url=base, timeout=30, headers={"Authorization": auth_header()},
                     follow_redirects=True)

    call(c, s, "public info", "GET", "/System/Info/Public")

    r = call(c, s, "login", "POST", "/Users/AuthenticateByName",
             json={"Username": username, "Pw": password})
    if not r:
        print("Login failed, stopping.")
        return
    auth = r.json()
    token = auth["AccessToken"]
    SECRETS.append(token)
    user = auth["User"]
    uid = user["Id"]
    pol = user.get("Policy", {})
    REPORT["source_user"] = {
        "EnableContentDownloading": pol.get("EnableContentDownloading"),
        "IsAdministrator": pol.get("IsAdministrator"),
        "EnableMediaPlayback": pol.get("EnableMediaPlayback"),
        "policy_keys": sorted(pol.keys()),
        "auth_response_keys": sorted(auth.keys()),
    }
    c.headers["Authorization"] = auth_header(token)

    try:
        views = None
        for path in (f"/UserViews?userId={uid}", f"/Users/{uid}/Views"):
            r = call(c, s, f"libraries via {path.split('?')[0]}", "GET", path)
            if r and views is None:
                views = r.json().get("Items", [])
        views = views or []
        REPORT["source_libraries"] = [
            {"Name": v.get("Name"), "CollectionType": v.get("CollectionType"), "Type": v.get("Type")}
            for v in views
        ]

        samples = {}
        fields = "ProductionYear,ChildCount,ProviderIds"
        for v in views:
            q = (f"/Items?ParentId={v['Id']}&IncludeItemTypes=Series,Movie,MusicAlbum&Recursive=true"
                 f"&Fields={fields}&SortBy=SortName&StartIndex=0&Limit=5")
            r = call(c, s, f"items in '{v.get('Name')}' ({v.get('CollectionType')})", "GET", q)
            if not r:
                r = call(c, s, f"items in '{v.get('Name')}' with userId", "GET", q + f"&userId={uid}")
            if r:
                body = r.json()
                REPORT.setdefault("library_counts", {})[v.get("Name")] = body.get("TotalRecordCount")
                for it in body.get("Items", []):
                    samples.setdefault(it.get("Type"), it)

        # Search + A-Z filter support (browse page needs both).
        if views:
            v0 = views[0]
            call(c, s, "search (SearchTerm=a)", "GET",
                 f"/Items?ParentId={v0['Id']}&IncludeItemTypes=Series,Movie,MusicAlbum&Recursive=true"
                 f"&SearchTerm=a&Limit=2")
            call(c, s, "A-Z filter (NameStartsWith=B)", "GET",
                 f"/Items?ParentId={v0['Id']}&IncludeItemTypes=Series,Movie,MusicAlbum&Recursive=true"
                 f"&NameStartsWith=B&Limit=2")

        series = samples.get("Series")
        episode = None
        if series:
            REPORT["sample_series"] = {k: series.get(k) for k in
                                       ("Name", "ProductionYear", "ChildCount", "ProviderIds", "Type")}
            r = call(c, s, "seasons", "GET", f"/Shows/{series['Id']}/Seasons?userId={uid}")
            seasons = r.json().get("Items", []) if r else []
            REPORT["sample_seasons"] = [
                {"Name": x.get("Name"), "IndexNumber": x.get("IndexNumber"),
                 "ChildCount": x.get("ChildCount")} for x in seasons
            ]
            season = first(seasons, lambda x: x.get("IndexNumber") == 1) or (seasons[0] if seasons else None)
            if season:
                r = call(c, s, "episodes of a season", "GET",
                         f"/Shows/{series['Id']}/Episodes?seasonId={season['Id']}&userId={uid}"
                         f"&Fields=Path,MediaSources")
                eps = r.json().get("Items", []) if r else []
                if eps:
                    episode = eps[0]
                    ms = (episode.get("MediaSources") or [{}])[0]
                    REPORT["sample_episode"] = {
                        "keys": sorted(episode.keys()),
                        "SeriesName": episode.get("SeriesName"),
                        "Name": episode.get("Name"),
                        "ParentIndexNumber": episode.get("ParentIndexNumber"),
                        "IndexNumber": episode.get("IndexNumber"),
                        "IndexNumberEnd": episode.get("IndexNumberEnd"),
                        "Container": episode.get("Container"),
                        "Path_present": bool(episode.get("Path")),
                        "Path_ext": (episode.get("Path") or "").rsplit(".", 1)[-1] if episode.get("Path") else None,
                        "MediaSource_keys": sorted(ms.keys()),
                        "MediaSource_Size": ms.get("Size"),
                        "MediaSource_Container": ms.get("Container"),
                        "MediaSources_count": len(episode.get("MediaSources") or []),
                        "external_subs": [
                            {k: st.get(k) for k in ("Index", "Codec", "Language", "IsExternal", "Title")}
                            for st in ms.get("MediaStreams", []) if st.get("Type") == "Subtitle"
                        ],
                    }
            # Specials: is season 0 listed?
            REPORT["has_specials_season"] = any(x.get("IndexNumber") == 0 for x in seasons)
            # All episodes of the series at once (for "Queue all").
            call(c, s, "all episodes of series", "GET",
                 f"/Shows/{series['Id']}/Episodes?userId={uid}&Fields=MediaSources&Limit=3")

        movie = samples.get("Movie")
        if movie:
            for path in (f"/Items/{movie['Id']}?Fields=Path,MediaSources,ProviderIds",
                         f"/Users/{uid}/Items/{movie['Id']}"):
                r = call(c, s, f"single movie via {path.split('?')[0].replace(movie['Id'], '{id}').replace(uid, '{uid}')}",
                         "GET", path)
                if r and "sample_movie" not in REPORT:
                    m = r.json()
                    ms = (m.get("MediaSources") or [{}])[0]
                    REPORT["sample_movie"] = {
                        "Name": m.get("Name"), "ProductionYear": m.get("ProductionYear"),
                        "Container": m.get("Container"), "MediaSource_Size": ms.get("Size"),
                        "ProviderIds": m.get("ProviderIds"),
                    }
            if not REPORT.get("sample_movie"):
                call(c, s, "single movie via /Items?Ids=", "GET",
                     f"/Items?Ids={movie['Id']}&Fields=Path,MediaSources,ProviderIds")

        album = samples.get("MusicAlbum")
        if album:
            r = call(c, s, "album tracks", "GET",
                     f"/Items?ParentId={album['Id']}&IncludeItemTypes=Audio&Fields=Path,MediaSources"
                     f"&SortBy=ParentIndexNumber,IndexNumber")
            tracks = r.json().get("Items", []) if r else []
            if tracks:
                t = tracks[0]
                REPORT["sample_track"] = {k: t.get(k) for k in
                                          ("Name", "Album", "AlbumArtist", "IndexNumber",
                                           "ParentIndexNumber", "Container")}

        # Download: the core of the app. Probe with small ranges only.
        target = episode or (REPORT.get("sample_movie") and movie)
        if target:
            size = ((target.get("MediaSources") or [{}])[0]).get("Size")
            for label, rng in (("download, first 1 KB", "bytes=0-1023"),
                               ("download, resume from middle", f"bytes={(size or 2048) // 2}-")):
                t0 = time.monotonic()
                try:
                    with c.stream("GET", f"/Items/{target['Id']}/Download",
                                  headers={"Range": rng}) as resp:
                        got = 0
                        for chunk in resp.iter_bytes():
                            got += len(chunk)
                            if got >= 1024 * 1024:
                                break
                        e = record(s, label, resp, t0=t0,
                                   error=None if resp.is_success else resp.read()[:300].decode(errors="replace"))
                        e["bytes_read"] = got
                        e["source_size"] = size
                except httpx.HTTPError as ex:
                    record(s, label, error=str(ex), t0=t0)

            # Subtitles: only if an external one exists.
            ms = (target.get("MediaSources") or [{}])[0]
            sub = first(ms.get("MediaStreams", []),
                        lambda st: st.get("Type") == "Subtitle" and st.get("IsExternal"))
            if sub:
                codec = sub.get("Codec") or "srt"
                call(c, s, "external subtitle", "GET",
                     f"/Videos/{target['Id']}/{ms.get('Id')}/Subtitles/{sub.get('Index')}/Stream.{codec}")
            else:
                REPORT["note_subtitles"] = "no external subtitle on the sampled item"

        poster_item = series or movie or album
        if poster_item:
            r = call(c, s, "poster", "GET", f"/Items/{poster_item['Id']}/Images/Primary?maxHeight=300")
            if REPORT["source"][-1].get("data") is None:
                REPORT["source"][-1].pop("data", None)

        call(c, s, "who am I (/Users/Me)", "GET", "/Users/Me")
    finally:
        call(c, s, "logout", "POST", "/Sessions/Logout")
        # Confirms the token is really dead, and shows what a 401 looks like.
        t0 = time.monotonic()
        try:
            resp = c.get("/Users/Me")
            record(s, "token after logout (expect 401)", resp, t0=t0)
        except httpx.HTTPError as e:
            record(s, "token after logout", error=str(e), t0=t0)


def spike_dest(base, api_key):
    print(f"\nDestination: {base}")
    d = "dest"
    c = httpx.Client(base_url=base, timeout=30, headers={"Authorization": auth_header(api_key)},
                     follow_redirects=True)
    call(c, d, "public info", "GET", "/System/Info/Public")
    r = call(c, d, "virtual folders", "GET", "/Library/VirtualFolders")
    if r:
        REPORT["dest_virtual_folders"] = [
            {"Name": f.get("Name"), "CollectionType": f.get("CollectionType"), "Locations": f.get("Locations")}
            for f in r.json()
        ]
    r = call(c, d, "all items with ProviderIds", "GET",
             "/Items?Recursive=true&IncludeItemTypes=Series,Movie,MusicAlbum&Fields=ProviderIds&Limit=3")
    if r:
        REPORT["dest_item_count"] = r.json().get("TotalRecordCount")
    # /Library/Refresh is not called: it starts a real scan. Phase 6 tests it.


def main():
    src = input("Source URL (e.g. https://jellyfin.example.com): ").strip()
    if not src:
        sys.exit("A source URL is required.")
    user = input("Source username: ").strip()
    pw = getpass.getpass("Source password (hidden): ")
    dest = input("Destination URL (blank to skip): ").strip()
    key = getpass.getpass("Destination API key (hidden, blank to skip): ").strip() if dest else ""
    SECRETS.extend([pw, key, user])

    try:
        spike_source(src.rstrip("/"), user, pw)
        if dest and key:
            spike_dest(dest.rstrip("/"), key)
        else:
            REPORT["note_dest"] = "skipped: no destination URL or API key given"
    finally:
        out = "/out/spike-report.json"
        with open(out, "w") as f:
            json.dump(scrub(REPORT), f, indent=2, default=str)
        print(f"\nReport written to {out} (secrets scrubbed).")


if __name__ == "__main__":
    sys.exit(main())
