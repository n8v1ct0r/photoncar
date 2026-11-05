"""Fetch the pinned comma2k19 example segment into the public dataset root (gitignored).

The root is ``PHOTONCAR_PUBLIC_DIR``, else ``data/public``, the same helper the replay sources
use (``photoncar.sources.comma2k19.default_dataset_root``); files land in ``<root>/comma2k19/``.

Downloads only the processed_log and global_pose numpy arrays of one 60 s segment, plus the
dataset LICENSE, from github.com/commaai/comma2k19 at a pinned commit. Skips video.hevc,
raw_log.bz2, and preview.png (real plates and scenes). Every file is checked against the SHA-256
in scripts/comma2k19_manifest.json. Idempotent: files already present with the right hash are
not downloaded again.

    uv run python scripts/fetch_comma2k19_sample.py                   # make fetch-dataset
    uv run python scripts/fetch_comma2k19_sample.py --write-manifest  # once, to pin hashes

``--write-manifest`` lists the pinned tree with the GitHub API, downloads each file, verifies it
against the git blob SHA-1 from that tree, and records path, bytes, and SHA-256.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from photoncar.sources.comma2k19 import default_dataset_root

OWNER_REPO = "commaai/comma2k19"
COMMIT = "4c7f1a6e1957745beadc1def0e7225f559b09a2a"
SEGMENT = "Example_1/b0c9d2329ad1606b|2018-08-02--08-34-47/40"
KEEP_DIRS = ("processed_log/", "global_pose/")
EXTRA_FILES = ("LICENSE",)
MANIFEST = Path(__file__).resolve().with_name("comma2k19_manifest.json")
LFS_PREFIX = b"version https://git-lfs.github.com/spec/v1\n"
TIMEOUT_S = 60.0
USER_AGENT = "photoncar-fetch/0.1"
# Some Python builds ship without a CA bundle (python.org macOS installs). Verification stays on:
# when the default store is empty, the first system bundle that exists is loaded as well.
SYSTEM_CA_FILES = (
    "/etc/ssl/cert.pem",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
)


class FetchError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LfsPointer:
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class ManifestFile:
    path: str
    bytes: int
    sha256: str


def default_dest() -> Path:
    """Where the replay sources read it: ``comma2k19`` under the public dataset root."""
    return default_dataset_root()


def quote_path(path: str) -> str:
    """URL-encode a repo path segment by segment. The route id's ``|`` becomes ``%7C``."""
    return urllib.parse.quote(path, safe="/")


def raw_url(path: str) -> str:
    return f"https://raw.githubusercontent.com/{OWNER_REPO}/{COMMIT}/{quote_path(path)}"


def media_url(path: str) -> str:
    return f"https://media.githubusercontent.com/media/{OWNER_REPO}/{COMMIT}/{quote_path(path)}"


def parse_lfs_pointer(data: bytes) -> LfsPointer | None:
    """Return the pointer if ``data`` is a Git LFS pointer file, else None."""
    if not data.startswith(LFS_PREFIX) or len(data) > 1024:
        return None
    oid: str | None = None
    size: int | None = None
    for line in data.decode("ascii", errors="replace").splitlines()[1:]:
        key, _, value = line.partition(" ")
        if key == "oid" and value.startswith("sha256:"):
            oid = value.removeprefix("sha256:").strip()
        elif key == "size" and value.strip().isdigit():
            size = int(value.strip())
    if oid is None or size is None or len(oid) != 64:
        raise FetchError("malformed Git LFS pointer")
    return LfsPointer(oid, size)


def git_blob_sha1(data: bytes) -> str:
    """The object id git gives a blob: sha1(b"blob <len>\\0" + data)."""
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats()["x509_ca"] == 0:
        for ca in SYSTEM_CA_FILES:
            if Path(ca).is_file():
                ctx.load_verify_locations(cafile=ca)
                break
    return ctx


def http_get(url: str, *, accept: str | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S, context=ssl_context()) as resp:
            body: bytes = resp.read()
            return body
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code} for {url}") from e
    except urllib.error.URLError as e:
        raise FetchError(f"cannot reach {url}: {e.reason}") from e


def download(path: str) -> bytes:
    """Download one repo file at the pinned commit, following a Git LFS pointer if present."""
    data = http_get(raw_url(path))
    pointer = parse_lfs_pointer(data)
    if pointer is None:
        return data
    data = http_get(media_url(path))
    if len(data) != pointer.size or hashlib.sha256(data).hexdigest() != pointer.sha256:
        raise FetchError(f"Git LFS object for {path} does not match its pointer")
    return data


def list_pinned_files() -> list[tuple[str, int, str]]:
    """(path, size, git blob sha1) for the segment arrays and extra files at the pinned commit."""
    url = f"https://api.github.com/repos/{OWNER_REPO}/git/trees/{COMMIT}?recursive=1"
    tree: dict[str, Any] = json.loads(http_get(url, accept="application/vnd.github+json"))
    if tree.get("truncated"):
        raise FetchError("GitHub returned a truncated tree listing")
    prefix = SEGMENT + "/"
    out: list[tuple[str, int, str]] = []
    for entry in tree["tree"]:
        if entry.get("type") != "blob":
            continue
        p = str(entry["path"])
        rel = p.removeprefix(prefix)
        if (p.startswith(prefix) and rel.startswith(KEEP_DIRS)) or p in EXTRA_FILES:
            out.append((p, int(entry["size"]), str(entry["sha"])))
    if not any(p.startswith(prefix) for p, _, _ in out):
        raise FetchError(f"segment {SEGMENT} not found at commit {COMMIT}")
    return sorted(out)


def load_manifest(path: Path = MANIFEST) -> list[ManifestFile]:
    doc = json.loads(path.read_text())
    if doc.get("commit") != COMMIT or doc.get("segment") != SEGMENT:
        raise FetchError(f"{path.name} is pinned to a different commit or segment")
    return [ManifestFile(str(f["path"]), int(f["bytes"]), str(f["sha256"])) for f in doc["files"]]


def dump_manifest(doc: dict[str, Any]) -> str:
    """JSON with 2-space indent and 1 line per file entry."""
    text = json.dumps(doc, indent=2)
    entry = re.compile(
        r'\{\n\s+"path": (".*?"),\n\s+"bytes": (\d+),\n\s+"sha256": ("[0-9a-f]{64}")\n\s+\}'
    )
    return entry.sub(r'{"path": \1, "bytes": \2, "sha256": \3}', text) + "\n"


def write_atomic(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    part.write_bytes(data)
    os.replace(part, target)


def write_manifest(dest: Path, path: Path = MANIFEST) -> list[ManifestFile]:
    """First verified download: check every file against its git blob SHA-1, then pin SHA-256."""
    files: list[ManifestFile] = []
    for repo_path, size, blob_sha1 in list_pinned_files():
        data = download(repo_path)
        if len(data) != size or git_blob_sha1(data) != blob_sha1:
            raise FetchError(f"{repo_path} does not match the pinned git tree")
        write_atomic(dest / repo_path, data)
        files.append(ManifestFile(repo_path, size, hashlib.sha256(data).hexdigest()))
    previous: dict[str, Any] = json.loads(path.read_text()) if path.exists() else {}
    doc: dict[str, Any] = {
        "dataset": "comma2k19",
        "source": f"https://github.com/{OWNER_REPO}",
        "commit": COMMIT,
        "segment": SEGMENT,
        "license": "MIT",
        "skipped": ["video.hevc", "raw_log.bz2", "preview.png"],
        "total_bytes": sum(f.bytes for f in files),
        "files": [{"path": f.path, "bytes": f.bytes, "sha256": f.sha256} for f in files],
    }
    doc.update({k: v for k, v in previous.items() if k not in doc})  # keeps the "route" section
    path.write_text(dump_manifest(doc))
    return files


def fetch(files: list[ManifestFile], dest: Path) -> tuple[int, int]:
    """Make ``dest`` hold every manifest file with the right hash. Returns (downloaded, skipped)."""
    downloaded = skipped = 0
    for f in files:
        target = dest / f.path
        if (
            target.is_file()
            and target.stat().st_size == f.bytes
            and sha256_file(target) == f.sha256
        ):
            skipped += 1
            continue
        data = download(f.path)
        if len(data) != f.bytes or hashlib.sha256(data).hexdigest() != f.sha256:
            raise FetchError(f"{f.path} failed its SHA-256 check")
        write_atomic(target, data)
        downloaded += 1
    return downloaded, skipped


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    ap.add_argument(
        "--dest",
        type=Path,
        default=None,
        help="dataset folder (default: comma2k19 under PHOTONCAR_PUBLIC_DIR or data/public)",
    )
    ap.add_argument("--write-manifest", action="store_true", help="pin hashes from the git tree")
    args = ap.parse_args(argv)
    dest: Path = args.dest or default_dest()
    try:
        if args.write_manifest:
            files = write_manifest(dest)
            print(f"Wrote {MANIFEST.name}: {len(files)} files verified against the git tree.")
            downloaded, skipped = len(files), 0
        else:
            if not MANIFEST.exists():
                raise FetchError(f"{MANIFEST.name} is missing. Run with --write-manifest once.")
            files = load_manifest()
            downloaded, skipped = fetch(files, dest)
    except FetchError as e:
        print(f"fetch failed: {e}", file=sys.stderr)
        return 1
    seg_bytes = sum(f.bytes for f in files if f.path.startswith(SEGMENT + "/"))
    total = sum(f.bytes for f in files)
    print(f"comma2k19 segment {SEGMENT} at {COMMIT[:12]} in {dest}")
    print(f"{len(files)} files: {downloaded} downloaded, {skipped} already present and verified.")
    print(f"Segment arrays: {seg_bytes:,} bytes. Total with LICENSE: {total:,} bytes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
