#!/usr/bin/env bash
# Offline dependency preparation — runs ONCE on a connected preparation host.
#
# The delivery environment must never run this script. It downloads the exact
# wheels pinned in releng/pins.json from a PyPI simple mirror, verifies the
# sha256 digest published in the simple index, and places them in
# vendor/wheels/. Subsequent builds only ever use --no-index + --find-links
# against that directory.
#
# Usage:  bash vendor/fetch-vendor.sh [mirror-base-url]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
WHEEL_DIR="$HERE/wheels"
MIRROR="${1:-https://mirrors.aliyun.com/pypi/simple}"
PY="${PYTHON:-python3}"

mkdir -p "$WHEEL_DIR"
cd "$WHEEL_DIR"

# name==version pairs, kept in lockstep with releng/pins.json.
SPECS=(
  "pip==23.3.2"
  "setuptools==68.1.2"
  "wheel==0.41.3"
)

echo "[fetch-vendor] resolving $MIRROR (network allowed only on this host)"
"$PY" - "$MIRROR" "$WHEEL_DIR" "$ROOT/releng/pins.json" <<'PYEOF'
import hashlib
import json
import re
import sys
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

mirror, wheel_dir, pins_path = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
specs = {"pip": "23.3.2", "setuptools": "68.1.2", "wheel": "0.41.3"}
pins = json.loads(pins_path.read_text(encoding="utf-8"))
pinned = {t["name"]: t for t in pins["build_tools"]}


class LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            href = dict(attrs).get("href", "")
            if href:
                self.links.append(href)


for name, version in specs.items():
    wanted = f"{name}-{version}-py3-none-any.whl"
    url = f"{mirror.rstrip('/')}/{name}/"
    with urllib.request.urlopen(url, timeout=60) as response:
        page = response.read().decode("utf-8")
    parser = LinkParser()
    parser.feed(page)
    match = None
    for href in parser.links:
        fragment = href.split("#", 1)
        link = fragment[0]
        if link.rstrip("/").endswith("/" + wanted):
            match = href
            break
    if match is None:
        sys.exit(f"[fetch-vendor] FATAL: {wanted} not found on {url}")
    link, _, fragment = match.partition("#")
    # Mirror simple pages publish the canonical PyPI digest in the URL fragment.
    expected_hash = None
    m = re.search(r"sha256=([A-Za-z0-9_-]+)", fragment)
    if m:
        expected_hash = m.group(1)
    if expected_hash != pinned[name]["sha256"]:
        sys.exit(f"[fetch-vendor] FATAL: index hash for {wanted} does not "
                 f"match releng/pins.json\n  index={expected_hash}\n  "
                 f"pins ={pinned[name]['sha256']}")
    if link.startswith("../../"):
        download = mirror.split("/simple")[0] + "/" + link.replace("../../", "")
    elif link.startswith("http"):
        download = link
    else:
        download = mirror.rstrip("/") + "/" + name + "/" + link
    target = wheel_dir / wanted
    print(f"[fetch-vendor] downloading {wanted}")
    urllib.request.urlretrieve(download, target)
    actual = hashlib.sha256(target.read_bytes()).hexdigest()
    if actual != expected_hash:
        target.unlink(missing_ok=True)
        sys.exit(f"[fetch-vendor] FATAL: downloaded digest mismatch for {wanted}")
    print(f"[fetch-vendor] OK {wanted} sha256={actual}")
PYEOF

echo "[fetch-vendor] all wheels present and hash-verified in $WHEEL_DIR"
"$PY" "$HERE/write-manifest.py"
