#!/usr/bin/env sh
# Fetch the icon set and fonts into $VENDOR_DIR (default /vendor).
# Runs during `docker build` only - nothing lands in the Drive-synced project
# root, and the running app never loads anything from a CDN.
set -eu
V="${VENDOR_DIR:-/vendor}"

BS_ICONS=1.11.3

mkdir -p "$V/bootstrap-icons/fonts" "$V/fonts"
J=https://cdn.jsdelivr.net/npm

curl -sSfL -o "$V/bootstrap-icons/bootstrap-icons.min.css"     "$J/bootstrap-icons@$BS_ICONS/font/bootstrap-icons.min.css"
curl -sSfL -o "$V/bootstrap-icons/fonts/bootstrap-icons.woff2" "$J/bootstrap-icons@$BS_ICONS/font/fonts/bootstrap-icons.woff2"
curl -sSfL -o "$V/bootstrap-icons/fonts/bootstrap-icons.woff"  "$J/bootstrap-icons@$BS_ICONS/font/fonts/bootstrap-icons.woff"

# Orbitron for headings (the neon look), Inter for everything else. Pull the
# Google Fonts CSS, then rewrite it to point at local files.
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36'
curl -sSfL -A "$UA" -o "$V/fonts/fonts.css" \
  "https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Orbitron:wght@600;800&display=swap"
i=0
for u in $(grep -oE 'https://fonts\.gstatic\.com/[^)]+\.woff2' "$V/fonts/fonts.css" | sort -u); do
  curl -sSfL -o "$V/fonts/font-$i.woff2" "$u"
  sed -i "s|$u|font-$i.woff2|g" "$V/fonts/fonts.css"
  i=$((i + 1))
done

echo "vendored into $V:"
find "$V" -type f | sort
