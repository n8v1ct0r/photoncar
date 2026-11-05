"""Privacy scan of tracked files (publish rules 5 and 12).

Flags: phone numbers, email addresses (except known public placeholders), VIN-shaped strings,
high-precision latitude/longitude pairs, drive-log and location file types, and SQLite files.
Exit 1 on any finding.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PHONE = re.compile(r"(?<![\w.])(\+?1[ .-]?)?\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\w.])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
VIN = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
HEX_ONLY = re.compile(r"[0-9A-F]{17}")
LATLON = re.compile(r"(-?\d{1,2}\.\d{5,})\s*,\s*(-?\d{1,3}\.\d{5,})")
ALLOWED_EMAILS = {"noreply@anthropic.com"}
BAD_SUFFIXES = {".gpx", ".kml", ".geojson", ".csv", ".parquet", ".nmea", ".ubx", ".asc", ".blf"}
BAD_SUFFIXES |= {".sqlite", ".db", ".log", ".trc"}
SKIP_SUFFIXES = {".woff2", ".png", ".jpg", ".webp", ".mp4", ".webm", ".lock"}
SKIP_FILES = {"web/pnpm-lock.yaml", "uv.lock"}


def main() -> int:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=REPO, check=True, capture_output=True, text=True
    ).stdout.splitlines()
    findings = 0
    for rel in tracked:
        p = REPO / rel
        if p.suffix in BAD_SUFFIXES:
            findings += 1
            print(f"{rel}: drive-log or location file type is tracked")
            continue
        if p.suffix in SKIP_SUFFIXES or rel in SKIP_FILES or not p.is_file():
            continue
        text = p.read_text(errors="ignore")
        for n, line in enumerate(text.splitlines(), 1):
            for m in EMAIL.finditer(line):
                if m.group(0).lower() not in ALLOWED_EMAILS:
                    findings += 1
                    print(f"{rel}:{n}: email address")
            if PHONE.search(line):
                findings += 1
                print(f"{rel}:{n}: phone-number shape")
            # Pure-hex runs are CAN payloads or hashes (candump "ID#DATA"); a VIN almost always
            # carries letters outside A-F.
            if any(not HEX_ONLY.fullmatch(m.group(0)) for m in VIN.finditer(line)):
                findings += 1
                print(f"{rel}:{n}: VIN-shaped string")
            if LATLON.search(line):
                findings += 1
                print(f"{rel}:{n}: high-precision lat,lon pair")
    print(f"privacy scan: {findings} findings in {len(tracked)} tracked files")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
