"""Settings from the environment. Nothing host-specific is hardcoded: the repo
is public and friends run it against their own servers, so every hostname,
path, key and password comes from compose."""
from pathlib import Path
import os

APP_NAME = "Jelly2JellyCopy"
VERSION = "1.0.0"
BASE_DIR = Path(__file__).resolve().parent

# The project root is synced to Google Drive, so nothing bulky or
# machine-generated lives under it: fonts and icons are baked into the image
# at /vendor, and SQLite and the secret live on the /data volume.
VENDOR_DIR = Path(os.getenv("J2J_VENDOR_DIR", "/vendor"))
DATA_DIR = Path(os.getenv("J2J_DATA_DIR", "/data"))

DEFAULT_APP_PASSWORD = "jelly2jelly"
# An empty value counts as unset, so a blank `J2J_APP_PASSWORD:` line in
# compose can't lock everyone out - it falls back to the default and the
# banner says so.
APP_PASSWORD = os.getenv("J2J_APP_PASSWORD") or DEFAULT_APP_PASSWORD

# Optional; generated into DATA_DIR/secret.key on first start when unset.
SECRET = os.getenv("J2J_SECRET", "")

DEST_URL = os.getenv("J2J_DEST_URL", "").rstrip("/")
DEST_API_KEY = os.getenv("J2J_DEST_API_KEY", "")
DEST_FOLDERS = os.getenv("J2J_DEST_FOLDERS", "")
FREE_SPACE_RESERVE_GB = int(os.getenv("J2J_FREE_SPACE_RESERVE_GB", "50"))

# The test suite turns the background download worker off and drives it by hand.
WORKER_ENABLED = os.getenv("J2J_WORKER", "1") != "0"

SESSION_COOKIE = "j2j_session"
SESSION_MAX_AGE = 30 * 24 * 3600

# Login rate limit: this many failures per address inside the window, then 429.
LOGIN_MAX_FAILURES = 5
LOGIN_WINDOW_SECONDS = 300
