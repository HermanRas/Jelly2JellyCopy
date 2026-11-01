"""Fake Jellyfin responses, shaped like the ones a real 12.x server gave
during the compatibility spike."""

SRC = "https://src.example.com"
DEST = "http://dest.example.com:8096"
TOKEN = "tok-0123456789abcdef"
USER_ID = "a" * 32

PUBLIC_INFO = {"ServerName": "Friend's Jelly", "Version": "12.3.0",
               "ProductName": "Jellyfin Server", "Id": "srv123"}

MOVIES = "1" * 32
SHOWS = "2" * 32
MIXED = "3" * 32
PLAYLISTS = "4" * 32


def auth_ok(downloads=True):
    return {"AccessToken": TOKEN, "ServerId": "srv123", "SessionInfo": {},
            "User": {"Name": "alice", "Id": USER_ID,
                     "Policy": {"EnableContentDownloading": downloads, "IsAdministrator": False}}}


def views():
    return {"Items": [
        {"Id": MOVIES, "Name": "Movies", "CollectionType": "movies", "Type": "CollectionFolder"},
        {"Id": SHOWS, "Name": "Series", "CollectionType": "tvshows", "Type": "CollectionFolder"},
        {"Id": MIXED, "Name": "Standup Comedy", "CollectionType": None, "Type": "CollectionFolder"},
        {"Id": PLAYLISTS, "Name": "Playlists", "CollectionType": "playlists", "Type": "CollectionFolder"},
    ]}


def virtual_folders(movies="/movies", kids="/kids-movies", tv="/tv"):
    return [
        {"Name": "Movies", "CollectionType": "movies", "Locations": [movies]},
        {"Name": "Kids Movies", "CollectionType": "movies", "Locations": [kids]},
        {"Name": "Shows", "CollectionType": "tvshows", "Locations": [tv]},
        # Jellyfin keeps collections under its own config dir: a real
        # absolute path, so it must be skipped by type.
        {"Name": "Collections", "CollectionType": "boxsets",
         "Locations": ["/config/data/data/collections"]},
    ]


def mock_source(mock):
    mock.get(f"{SRC}/System/Info/Public").respond(json=PUBLIC_INFO)
    mock.post(f"{SRC}/Users/AuthenticateByName").respond(json=auth_ok())
    mock.post(f"{SRC}/Sessions/Logout").respond(204)
    return mock
