#!/usr/bin/env python3
"""Regenerate vendor/MANIFEST.json from the wheels present on disk.

The manifest is the offline dependency artifact digest list: it binds wheel
file name, size and sha256, and records the pinned tool versions. Run it after
fetch-vendor.sh. It never touches the network.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT / "releng"))
from common import load_pins, sha256_file  # noqa: E402


def main() -> None:
    pins = load_pins()
    entries = []
    for tool in pins["build_tools"]:
        wheel = HERE / "wheels" / tool["wheel"]
        if not wheel.is_file():
            raise SystemExit(f"missing wheel: {wheel}")
        digest = sha256_file(wheel)
        if digest != tool["sha256"]:
            raise SystemExit(
                f"hash drift for {tool['wheel']}: {digest} != {tool['sha256']}")
        entries.append({
            "name": tool["name"],
            "version": tool["version"],
            "file": f"wheels/{tool['wheel']}",
            "size": wheel.stat().st_size,
            "sha256": digest,
        })
    manifest = {
        "schema": "entityforge-offline-vendor/1",
        "interpreter_pinned": pins["interpreter"]["version_exact"],
        "source_date_epoch": pins["source_date_epoch"],
        "wheels": entries,
    }
    out = HERE / "MANIFEST.json"
    out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    print(f"wrote {out} with {len(entries)} verified wheels")


if __name__ == "__main__":
    main()
