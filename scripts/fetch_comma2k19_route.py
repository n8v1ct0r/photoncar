"""Fetch every segment of 1 comma2k19 route (arrays only) without downloading a multi-GB chunk.

The Hugging Face dataset commaai/comma2k19 (pinned revision) stores the full dataset as 10 zip
files of 8.7 to 9.9 GB. A zip keeps its file list in a central directory at the end, so this script
reads that directory with HTTP range requests (about 1.8 MB), then downloads only the
processed_log and global_pose members of the route's segments. It never fetches video.hevc,
raw_log.bz2, or preview.png.

Default route: Chunk_2 ``b0c9d2329ad1606b|2018-11-11--13-08-44`` (2017 Toyota RAV4), segments 9
to 23 with none missing: 15 minutes, 555 files, 27,695,255 bytes to download, 108,971,808 bytes on
disk. Segment 14 holds a standing start to 60 mph. Files land in
``<public root>/comma2k19/Chunk_2/<route>/<segment>/`` (gitignored), the layout
``Comma2k19Source`` reads. The public root is ``PHOTONCAR_PUBLIC_DIR``, else ``data/public``.

Checks: every member's size and CRC-32 against the zip's central directory, and its SHA-256
against the "route" section of scripts/comma2k19_manifest.json (``--write-manifest`` pins it after
a download that passed the CRC checks). Refuses a plan over ``--max-bytes`` (400 MB by default).
Idempotent: files already present with the pinned hash are skipped.

    uv run python scripts/fetch_comma2k19_route.py
    uv run python scripts/fetch_comma2k19_route.py --list Chunk_1.zip
    uv run python scripts/fetch_comma2k19_route.py --chunk Chunk_1.zip \\
        --route 'b0c9d2329ad1606b|2018-08-01--21-13-49'
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fetch_comma2k19_sample import (
    MANIFEST,
    TIMEOUT_S,
    USER_AGENT,
    FetchError,
    default_dest,
    dump_manifest,
    sha256_file,
    ssl_context,
    write_atomic,
)

HF_DATASET = "commaai/comma2k19"
HF_REVISION = "4bff77c7254c654c28d4c2726186b4e825adccee"
DEFAULT_CHUNK = "Chunk_2.zip"
DEFAULT_ROUTE = "b0c9d2329ad1606b|2018-11-11--13-08-44"
KEEP_DIRS = ("processed_log/", "global_pose/")
MAX_BYTES_DEFAULT = 400_000_000
EOCD_SEARCH = 65_536 + 22
RETRIES = 8
WORKERS = 4
ROUTE_NAME = re.compile(r"^[0-9a-f]{16}\|\d{4}-\d{2}-\d{2}--\d{2}-\d{2}-\d{2}$")


@dataclass(frozen=True, slots=True)
class ZipEntry:
    name: str
    method: int
    crc32: int
    csize: int
    usize: int
    offset: int


def chunk_url(chunk: str) -> str:
    path = urllib.parse.quote(f"raw_data/{chunk}")
    return f"https://huggingface.co/datasets/{HF_DATASET}/resolve/{HF_REVISION}/{path}"


def _open(req: urllib.request.Request) -> Any:
    return urllib.request.urlopen(req, timeout=TIMEOUT_S, context=ssl_context())


def remote_size(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    with _open(req) as resp:
        return int(resp.headers["Content-Length"])


def get_range(url: str, start: int, end: int) -> bytes:
    """Bytes ``start..end`` inclusive. Refuses any reply that is not a 206 partial response."""
    headers = {"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"}
    for attempt in range(RETRIES):
        try:
            with _open(urllib.request.Request(url, headers=headers)) as resp:
                if resp.status != 206:
                    raise FetchError(f"server ignored the range request ({resp.status}) for {url}")
                body: bytes = resp.read()
            if len(body) != end - start + 1:
                raise FetchError(f"short range read at {start} from {url}")
            return body
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504) or attempt == RETRIES - 1:
                raise FetchError(f"HTTP {e.code} for {url}") from e
        except urllib.error.URLError as e:
            if attempt == RETRIES - 1:
                raise FetchError(f"cannot reach {url}: {e.reason}") from e
        time.sleep(2.0 * (attempt + 1))
    raise FetchError(f"gave up on {url}")


def parse_central_directory(cd: bytes) -> list[ZipEntry]:
    """Entries of a zip central directory, with ZIP64 sizes and offsets resolved."""
    out: list[ZipEntry] = []
    p = 0
    while p + 46 <= len(cd) and cd[p : p + 4] == b"PK\x01\x02":
        f = struct.unpack("<IHHHHHHIIIHHHHHII", cd[p : p + 46])
        method, crc, csize, usize = f[4], f[7], f[8], f[9]
        nlen, elen, clen, offset = f[10], f[11], f[12], f[16]
        name = cd[p + 46 : p + 46 + nlen].decode("utf-8")
        extra = cd[p + 46 + nlen : p + 46 + nlen + elen]
        q = 0
        while q + 4 <= len(extra):
            hid, hsize = struct.unpack("<HH", extra[q : q + 4])
            data = extra[q + 4 : q + 4 + hsize]
            if hid == 0x0001:
                r = 0
                if usize == 0xFFFFFFFF:
                    usize = struct.unpack("<Q", data[r : r + 8])[0]
                    r += 8
                if csize == 0xFFFFFFFF:
                    csize = struct.unpack("<Q", data[r : r + 8])[0]
                    r += 8
                if offset == 0xFFFFFFFF:
                    offset = struct.unpack("<Q", data[r : r + 8])[0]
            q += 4 + hsize
        out.append(ZipEntry(name, method, crc, csize, usize, offset))
        p += 46 + nlen + elen + clen
    return out


def central_directory(url: str) -> list[ZipEntry]:
    size = remote_size(url)
    tail = get_range(url, max(0, size - EOCD_SEARCH), size - 1)
    loc = tail.rfind(b"PK\x06\x06")
    if loc >= 0:
        n_total, cd_size, cd_off = struct.unpack("<QQQ", tail[loc + 32 : loc + 56])
    else:
        eocd = tail.rfind(b"PK\x05\x06")
        if eocd < 0:
            raise FetchError(f"no zip end record in {url}")
        n_total, cd_size, cd_off = struct.unpack("<HII", tail[eocd + 10 : eocd + 20])
    entries = parse_central_directory(get_range(url, cd_off, cd_off + cd_size - 1))
    if len(entries) != n_total:
        raise FetchError(f"central directory lists {len(entries)} of {n_total} entries")
    return entries


def member_bytes(url: str, e: ZipEntry) -> bytes:
    """One member's contents, inflated and checked against its size and CRC-32."""
    head = get_range(url, e.offset, e.offset + 30 + 1024 + e.csize - 1)
    if head[:4] != b"PK\x03\x04":
        raise FetchError(f"bad local header for {e.name}")
    nlen, elen = struct.unpack("<HH", head[26:30])
    start = 30 + nlen + elen
    data = head[start : start + e.csize]
    if len(data) < e.csize:
        data += get_range(url, e.offset + start + len(data), e.offset + start + e.csize - 1)
    if e.method == 8:
        data = zlib.decompress(data, -15)
    elif e.method != 0:
        raise FetchError(f"unsupported zip method {e.method} for {e.name}")
    if len(data) != e.usize or zlib.crc32(data) != e.crc32:
        raise FetchError(f"{e.name} failed its size or CRC-32 check")
    return data


def route_members(
    entries: list[ZipEntry], route: str, segments: set[int] | None
) -> dict[int, list[ZipEntry]]:
    """Array members of ``route`` grouped by segment number."""
    by_seg: dict[int, list[ZipEntry]] = {}
    for e in entries:
        parts = e.name.split("/")
        if len(parts) < 5 or parts[1] != route or e.name.endswith("/"):
            continue
        rel = "/".join(parts[3:])
        if not parts[2].isdigit() or not rel.startswith(KEEP_DIRS):
            continue
        seg = int(parts[2])
        if segments is None or seg in segments:
            by_seg.setdefault(seg, []).append(e)
    return dict(sorted(by_seg.items()))


def list_routes(entries: list[ZipEntry]) -> list[tuple[str, list[int], int]]:
    """(route, segment numbers, bytes to download) for every route in a chunk."""
    routes: dict[str, tuple[set[int], list[int]]] = {}
    for e in entries:
        parts = e.name.split("/")
        if len(parts) >= 5 and parts[2].isdigit() and "/".join(parts[3:]).startswith(KEEP_DIRS):
            segs, size = routes.setdefault(parts[1], (set(), [0]))
            segs.add(int(parts[2]))
            size[0] += e.csize
    return [(r, sorted(s), n[0]) for r, (s, n) in sorted(routes.items())]


def safe_target(dest: Path, name: str) -> Path:
    target = (dest / name).resolve()
    if not target.is_relative_to(dest.resolve()):
        raise FetchError(f"refusing a path outside the dataset root: {name}")
    return target


def pinned_route(chunk: str, route: str) -> dict[str, tuple[int, str]] | None:
    if not MANIFEST.exists():
        return None
    section = json.loads(MANIFEST.read_text()).get("route")
    if not section or section.get("chunk") != chunk or section.get("route") != route:
        return None
    if section.get("hf_revision") != HF_REVISION:
        raise FetchError("the manifest route section is pinned to another revision")
    return {f["path"]: (int(f["bytes"]), str(f["sha256"])) for f in section["files"]}


def parse_segments(text: str | None) -> set[int] | None:
    if not text:
        return None
    out: set[int] = set()
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        out.update(range(int(lo), int(hi or lo) + 1))
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Fetch one comma2k19 route's arrays by range reads.")
    ap.add_argument("--chunk", default=DEFAULT_CHUNK)
    ap.add_argument("--route", default=DEFAULT_ROUTE)
    ap.add_argument("--segments", help="for example 9-23 or 13,14 (default: every segment)")
    ap.add_argument("--dest", type=Path, default=None)
    ap.add_argument("--max-bytes", type=int, default=MAX_BYTES_DEFAULT)
    ap.add_argument("--list", metavar="CHUNK", help="list the routes in a chunk and exit")
    ap.add_argument("--write-manifest", action="store_true", help="pin SHA-256 of this route")
    args = ap.parse_args(argv)
    dest: Path = args.dest or default_dest()
    try:
        if args.list:
            for route, segs, nbytes in list_routes(central_directory(chunk_url(args.list))):
                gaps = "" if segs[-1] - segs[0] + 1 == len(segs) else " (gaps)"
                print(f"{route}  segments {segs[0]}..{segs[-1]} n={len(segs)}{gaps}  {nbytes:,} B")
            return 0
        if not ROUTE_NAME.match(args.route):
            raise FetchError(f"not a route id: {args.route}")
        url = chunk_url(args.chunk)
        members = route_members(central_directory(url), args.route, parse_segments(args.segments))
        if not members:
            raise FetchError(f"route {args.route} not found in {args.chunk}")
        entries = [e for seg in members.values() for e in seg]
        plan_bytes = sum(e.csize for e in entries)
        if plan_bytes > args.max_bytes:
            raise FetchError(f"plan is {plan_bytes:,} bytes, over --max-bytes {args.max_bytes:,}")
        pins = None if args.write_manifest else pinned_route(args.chunk, args.route)

        def ensure(e: ZipEntry) -> tuple[ZipEntry, str, bool]:
            target = safe_target(dest, e.name)
            if target.is_file() and target.stat().st_size == e.usize:
                digest = sha256_file(target)
                if (pins is not None and pins.get(e.name, (0, ""))[1] == digest) or (
                    pins is None and zlib.crc32(target.read_bytes()) == e.crc32
                ):
                    return e, digest, False
            data = member_bytes(url, e)
            digest = hashlib.sha256(data).hexdigest()
            if pins is not None and pins.get(e.name) != (e.usize, digest):
                raise FetchError(f"{e.name} does not match its pinned SHA-256")
            write_atomic(target, data)
            return e, digest, True

        with ThreadPoolExecutor(WORKERS) as pool:
            results = list(pool.map(ensure, entries))
    except FetchError as err:
        print(f"fetch failed: {err}", file=sys.stderr)
        return 1

    fetched = [e for e, _, new in results if new]
    if args.write_manifest:
        doc: dict[str, Any] = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
        doc["route"] = {
            "source": f"https://huggingface.co/datasets/{HF_DATASET}",
            "hf_revision": HF_REVISION,
            "chunk": args.chunk,
            "route": args.route,
            "segments": sorted(members),
            "download_bytes": plan_bytes,
            "total_bytes": sum(e.usize for e in entries),
            "files": [
                {"path": e.name, "bytes": e.usize, "sha256": d}
                for e, d, _ in sorted(results, key=lambda r: r[0].name)
            ],
        }
        MANIFEST.write_text(dump_manifest(doc))
        print(f"Pinned {len(results)} SHA-256 values in {MANIFEST.name} (route section).")
    segs = sorted(members)
    print(f"comma2k19 {args.chunk} route {args.route} at {HF_REVISION[:12]} in {dest}")
    print(f"Segments {segs[0]}..{segs[-1]} ({len(segs)}), {len(entries)} files: ", end="")
    print(f"{len(fetched)} downloaded, {len(entries) - len(fetched)} already present and verified.")
    print(f"Downloaded {sum(e.csize for e in fetched):,} bytes (plan {plan_bytes:,} bytes).")
    print(f"On disk: {sum(e.usize for e in entries):,} bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
