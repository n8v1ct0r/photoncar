"""The summary output cache: table ``model_summaries`` in ``data/index.sqlite``.

The key is the SHA-256 of the canonical JSON of ``{model, prompt_version, system, stats,
max_tokens}``: the same stats under the same prompt and model never cost a second call. Only
validated model output is stored; the template fallback never is, so a later request can still
get a model summary.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from photoncar.store.meta import index_schema_sql
from photoncar.summary.stats import StatItem
from photoncar.summary.validate import Sentence, Summary

BUSY_TIMEOUT_S = 5.0


def cache_key(
    *, model: str, prompt_version: int, system: str, stats: Sequence[StatItem], max_tokens: int
) -> str:
    doc = {
        "model": model,
        "prompt_version": prompt_version,
        "system": system,
        "stats": list(stats),
        "max_tokens": max_tokens,
    }
    text = json.dumps(
        doc, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CachedSummary:
    summary: Summary
    model: str
    in_tokens: int
    out_tokens: int
    cost_usd_micros: int


def connect(path: Path) -> sqlite3.Connection:
    """A connection to the index file with the full index schema applied."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
    conn.executescript(index_schema_sql())
    return conn


class SummaryCache:
    def __init__(self, index_path: Path) -> None:
        self.path = Path(index_path)

    def get(self, key: str) -> CachedSummary | None:
        with closing(connect(self.path)) as conn:
            row = conn.execute(
                "SELECT output_json, model, in_tokens, out_tokens, cost_usd_micros "
                "FROM model_summaries WHERE input_sha256 = ?",
                (key,),
            ).fetchone()
        if row is None:
            return None
        doc = json.loads(row[0])
        summary = Summary(
            sentences=[
                Sentence(text=str(s["text"]), stat_ids=[str(i) for i in s["stat_ids"]])
                for s in doc["sentences"]
            ]
        )
        return CachedSummary(summary, str(row[1]), int(row[2]), int(row[3]), int(row[4]))

    def put(
        self,
        key: str,
        *,
        model: str,
        prompt_version: int,
        stats_json: str,
        summary: Summary,
        in_tokens: int,
        out_tokens: int,
        cost_usd_micros: int,
    ) -> None:
        """Store a validated summary. The first write for a key wins."""
        output = json.dumps(summary, ensure_ascii=False, separators=(",", ":"))
        with closing(connect(self.path)) as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO model_summaries (input_sha256, model, prompt_version, "
                    "stats_json, output_json, in_tokens, out_tokens, cost_usd_micros, "
                    "created_seq) VALUES (?, ?, ?, ?, ?, ?, ?, ?, "
                    "(SELECT COALESCE(MAX(created_seq), 0) + 1 FROM model_summaries))",
                    (
                        key,
                        model,
                        prompt_version,
                        stats_json,
                        output,
                        in_tokens,
                        out_tokens,
                        cost_usd_micros,
                    ),
                )
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
