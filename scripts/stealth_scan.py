"""Scan tracked files and the full git history for stealth terms (publish rule 4).

The terms live in a file OUTSIDE this repo, named by PHOTONCAR_STEALTH_TERMS_FILE, one per line.
The terms are never written into the repo. Exit 1 on any match or on a bad setup.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def load_terms() -> list[str]:
    env = os.environ.get("PHOTONCAR_STEALTH_TERMS_FILE")
    if not env:
        sys.exit("PHOTONCAR_STEALTH_TERMS_FILE is not set. Point it at a file outside this repo.")
    path = Path(env).expanduser().resolve()
    if path.is_relative_to(REPO):
        sys.exit("The stealth terms file must live outside this repo.")
    if not path.exists():
        sys.exit(f"Stealth terms file not found: {path}")
    terms = [
        t.strip() for t in path.read_text().splitlines() if t.strip() and not t.startswith("#")
    ]
    if not terms:
        sys.exit("The stealth terms file is empty.")
    return terms


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=REPO, check=True, capture_output=True, text=True, errors="replace"
    ).stdout


def main() -> int:
    terms = load_terms()
    pattern = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
    hits = 0
    for rel in git("ls-files").splitlines():
        p = REPO / rel
        if not p.is_file():
            continue
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if pattern.search(line):
                hits += 1
                print(f"tracked {rel}:{n}")
    history = (
        git("log", "-p", "--all") if git("rev-list", "--all", "--count").strip() != "0" else ""
    )
    for n, line in enumerate(history.splitlines(), 1):
        if pattern.search(line):
            hits += 1
            print(f"history line {n}")
    print(f"stealth scan: {hits} matches across tracked files and full history")
    return 1 if hits else 0


if __name__ == "__main__":
    raise SystemExit(main())
