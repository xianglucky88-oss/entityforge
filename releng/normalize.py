#!/usr/bin/env python3
"""Explicit archive normalization step.

Raw build output is not assumed reproducible. Measured fact for this pinned
toolchain (setuptools 68.1.2):

* wheel zip entries already honor SOURCE_DATE_EPOCH, but normalization is
  still applied so the property does not silently depend on one tool version;
* sdist (.tar.gz) ignores SOURCE_DATE_EPOCH: generated directories carry
  wall-clock mtimes with sub-second PAX headers, source files keep their own
  mtimes, uid/gid/uname/gname are the builder's, and gzip metadata leaks.

Normalization rebuilds each archive deterministically:

  wheel -> rewrite every zip entry sorted by name, fixed epoch, fixed
           unix mode, no extra field, deflate; then verify the RECORD file
           still validates every shipped payload;
  sdist -> rewrite as gzip(USTAR tar), entries sorted by name, fixed epoch,
           uid/gid = 0, no uname/gname, default GNU gzip header with zeroed
           mtime and no original-name field.

Both rewrites are content-preserving: per-member payload digests before and
after are compared, so normalization cannot hide a code or metadata change.
Differences found in code, METADATA or RECORD are reported separately by the
build pipeline rather than papered over by identical archive bytes.
"""
from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import io
import os
import sys
import tarfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ReleaseError, sha256_bytes, sha256_file  # noqa: E402

EPOCH = 1700000000
WHEEL_DIR_MODE = 0o755
FILE_MODE = 0o644


def _zip_member_payloads(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {info.filename: archive.read(info.filename)
                for info in archive.infolist()}


def normalize_wheel(src: Path, dst: Path) -> dict:
    before = _zip_member_payloads(src)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=6) as archive:
        for name in sorted(before):
            info = zipfile.ZipInfo(filename=name,
                                   date_time=_zip_time(EPOCH))
            info.create_system = 3  # unix
            info.external_attr = (
                (WHEEL_DIR_MODE if name.endswith("/") else FILE_MODE)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            info.extra = b""
            info.comment = b""
            archive.writestr(info, before[name])
    dst.write_bytes(buffer.getvalue())
    after = _zip_member_payloads(dst)
    if after != before:
        raise ReleaseError(f"wheel normalization changed payloads: {src}")
    record_issues = verify_record(after)
    if record_issues:
        raise ReleaseError(
            f"RECORD validation failed after normalizing {src}:\n"
            + "\n".join(f"  - {i}" for i in record_issues))
    return {"kind": "wheel", "members": len(before), "record_issues": 0}


def _zip_time(epoch: int) -> tuple[int, int, int, int, int, int]:
    import datetime
    dt = datetime.datetime.fromtimestamp(epoch, datetime.timezone.utc)
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)


def verify_record(members: dict[str, bytes]) -> list[str]:
    record_name = next((n for n in members if n.endswith(".dist-info/RECORD")),
                       None)
    if record_name is None:
        return ["RECORD file missing"]
    issues: list[str] = []
    listed = set()
    for row in csv.reader(io.StringIO(
            members[record_name].decode("utf-8"))):
        if not row:
            continue
        name, hash_field, size_field = (row + ["", ""])[:3]
        listed.add(name)
        if not hash_field:  # RECORD itself is recorded without a hash
            continue
        if name not in members:
            issues.append(f"RECORD references missing member {name}")
            continue
        expected = hash_field.split("=", 1)[1]
        actual = base64.urlsafe_b64encode(
            hashlib.sha256(members[name]).digest()).rstrip(b"=").decode()
        if expected != actual:
            raise ReleaseError(
                f"RECORD hash mismatch for {name}: archive payload was altered")
        if size_field and int(size_field) != len(members[name]):
            issues.append(f"RECORD size mismatch for {name}")
    missing = set(members) - listed
    if missing:
        issues.append(f"members absent from RECORD: {sorted(missing)}")
    return issues


def _tar_members(path: Path) -> list[tuple[str, bytes, int]]:
    with gzip.open(path, "rb") as gz:
        with tarfile.open(fileobj=gz, mode="r:") as archive:
            out = []
            for member in archive.getmembers():
                if member.isdir():
                    out.append((member.name.rstrip("/") + "/", b"",
                                WHEEL_DIR_MODE))
                elif member.isfile():
                    extracted = archive.extractfile(member)
                    out.append((member.name,
                                extracted.read() if extracted else b"",
                                FILE_MODE))
                else:
                    raise ReleaseError(
                        f"unsupported tar member type in sdist: {member.name}")
    return out


def normalize_sdist(src: Path, dst: Path) -> dict:
    members = _tar_members(src)
    # setuptools emits entries in traversal order (directories interleaved with
    # their children); normalization establishes one canonical sorted order.
    members.sort(key=lambda item: item[0])
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w:", format=tarfile.USTAR_FORMAT) as archive:
        for name, payload, mode in members:
            is_dir = name.endswith("/")
            info = tarfile.TarInfo(name=name)
            info.type = tarfile.DIRTYPE if is_dir else tarfile.REGTYPE
            info.size = 0 if is_dir else len(payload)
            info.mtime = EPOCH
            info.mode = mode
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, None if is_dir else io.BytesIO(payload))
    raw_tar = tar_buffer.getvalue()
    # Deterministic gzip wrapper: fixed mtime=0, no original name, XFL/OS 0.
    gz_buffer = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=gz_buffer,
                       mtime=0, compresslevel=9) as gz:
        gz.write(raw_tar)
    dst.write_bytes(gz_buffer.getvalue())

    rebuilt = _tar_members(dst)
    if [(n, p) for n, p, _ in rebuilt] != [(n, p) for n, p, _ in members]:
        raise ReleaseError(f"sdist normalization changed payloads: {src}")
    return {"kind": "sdist", "members": len(members)}


def archive_payload_manifest(path: Path) -> dict[str, str]:
    """Content map used for cross-archive diffs (direct wheel vs rebuilt)."""
    path = Path(path)
    if path.suffix == ".whl":
        return {name: sha256_bytes(payload)
                for name, payload in _zip_member_payloads(path).items()}
    if path.name.endswith(".tar.gz"):
        return {name: sha256_bytes(payload)
                for name, payload, _ in _tar_members(path)}
    raise ReleaseError(f"unknown archive type: {path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("wheel", "sdist"))
    parser.add_argument("src", type=Path)
    parser.add_argument("dst", type=Path)
    args = parser.parse_args()
    args.dst.parent.mkdir(parents=True, exist_ok=True)
    if args.kind == "wheel":
        info = normalize_wheel(args.src, args.dst)
    else:
        info = normalize_sdist(args.src, args.dst)
    info["src"] = str(args.src)
    info["dst"] = str(args.dst)
    info["src_sha256"] = sha256_file(args.src)
    info["dst_sha256"] = sha256_file(args.dst)
    import json
    print(json.dumps(info, indent=2))


if __name__ == "__main__":
    main()
