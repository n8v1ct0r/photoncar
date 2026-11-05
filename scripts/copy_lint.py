"""Copy lint: 0 em dashes, 0 en dashes, 0 banned words in UI text, the README, and the portfolio.

Scans README.md, portfolio/, web/src/, web/index.html, and the Python sources whose strings reach
the UI (src/photoncar). CSS uses of the word "transform" (text-transform, transform:) are code,
not copy, and are skipped. Exit 1 on any finding.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
BANNED = [
    "leverage",
    "leverages",
    "leveraged",
    "leveraging",
    "delve",
    "delves",
    "landscape",
    "landscapes",
    "showcase",
    "showcases",
    "pivotal",
    "seamless",
    "seamlessly",
    "robust",
    "empower",
    "empowers",
    "unlock",
    "unlocks",
    "transform",
    "transforms",
    "transformed",
]
DASHES = re.compile("[–—]")
WORDS = re.compile(r"\b(" + "|".join(BANNED) + r")\b", re.IGNORECASE)
CSS_TRANSFORM = re.compile(r"(text-transform|transform\s*:|transform-origin|\.transform\b)")
TARGETS = ["README.md", "portfolio", "web/src", "web/index.html", "src/photoncar"]
SUFFIXES = {".md", ".mdx", ".json", ".ts", ".tsx", ".css", ".html", ".py", ".txt"}


def files() -> list[Path]:
    out: list[Path] = []
    for t in TARGETS:
        p = REPO / t
        if p.is_file():
            out.append(p)
        elif p.is_dir():
            out.extend(f for f in sorted(p.rglob("*")) if f.is_file() and f.suffix in SUFFIXES)
    return out


def main() -> int:
    findings = 0
    for f in files():
        for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
            if DASHES.search(line):
                findings += 1
                print(f"{f.relative_to(REPO)}:{n}: em or en dash")
            cleaned = CSS_TRANSFORM.sub("", line)
            for m in WORDS.finditer(cleaned):
                findings += 1
                print(f"{f.relative_to(REPO)}:{n}: banned word '{m.group(1)}'")
    print(f"copy lint: {findings} findings in {len(files())} files")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
