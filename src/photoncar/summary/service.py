"""The drive summary route's logic, independent of HTTP.

Order of work for 1 request (docs/phase-1-plan.md section 9, model route):

1. Build the input from computed stats only (``summary_input``). Fewer usable stats than the
   minimum sentence count: 422, since not even the template could cite them.
2. Cache first. A hit returns at once: no model call, no spend, no rate-limit use. It works
   offline too, since it sends nothing.
3. No API key: 503 "Needs connection." Nothing is queued.
4. Per-IP sliding window: 429 with ``Retry-After``.
5. Reserve the worst-case cost under the spend cap: 429 with ``Retry-After`` until the next
   UTC midnight when the cap would be passed.
6. Call the model, then correct the ledger from the reported usage.
7. Validate. A rejected reply gets 1 retry, with the problems appended to the system prompt.
   A second rejection, or a failure during the retry, falls back to the deterministic
   template, flagged ``source: "template"``. Only validated model output is cached.

Failures of the first call: unreachable service (no network) is 503 "Needs connection.", as
the contract's offline answer; any other model service error is 502.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from photoncar.clock import Clock
from photoncar.config import Config
from photoncar.records import DriveStat
from photoncar.summary.cache import SummaryCache, cache_key
from photoncar.summary.limits import (
    USD_MICROS,
    SlidingWindowLimiter,
    SpendLedger,
    cost_usd_micros,
    seconds_to_utc_midnight,
    worst_case_usd_micros,
)
from photoncar.summary.model_client import ModelCallFailed, ModelOffline, SummaryClient
from photoncar.summary.prompt import USER_PREFIX, retry_note, system_prompt
from photoncar.summary.stats import StatItem, stats_json, summary_input
from photoncar.summary.validate import (
    Summary,
    normalize,
    parse_output,
    template_summary,
    validate,
)

log = logging.getLogger(__name__)

NEEDS_CONNECTION = "Needs connection."
MAX_ATTEMPTS = 2


class SummaryError(Exception):
    """A summary request that cannot be answered with a summary."""

    def __init__(self, status: int, message: str, *, retry_after_s: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.retry_after_s = retry_after_s


@dataclass(frozen=True, slots=True)
class SummaryResult:
    summary: Summary
    source: Literal["model", "template"]
    cached: bool
    model: str | None

    def to_json(self) -> dict[str, Any]:
        return {
            "sentences": [
                {"text": s["text"], "stat_ids": list(s["stat_ids"])}
                for s in self.summary["sentences"]
            ],
            "source": self.source,
            "cached": self.cached,
            "model": self.model,
        }


def _retry_after(seconds: float) -> int:
    return max(1, math.ceil(seconds))


class SummaryService:
    def __init__(self, cfg: Config, index_path: Path, client: SummaryClient, clock: Clock) -> None:
        self.client = client
        self.prompt_version = int(cfg.get("summary.prompt_version"))
        self.max_tokens = int(cfg.get("summary.max_tokens"))
        self.min_sentences = int(cfg.get("summary.min_sentences"))
        self.max_sentences = int(cfg.get("summary.max_sentences"))
        self.in_price = float(cfg.get("summary.input_usd_per_mtok"))
        self.out_price = float(cfg.get("summary.output_usd_per_mtok"))
        cap = round(float(cfg.get("summary.daily_cap_usd")) * USD_MICROS)
        self.cache = SummaryCache(index_path)
        self.ledger = SpendLedger(index_path, cap, clock)
        self.limiter = SlidingWindowLimiter(
            [
                (60.0, int(cfg.get("summary.per_ip_per_minute"))),
                (86_400.0, int(cfg.get("summary.per_ip_per_day"))),
            ],
            clock,
        )
        self._clock = clock
        self.system = system_prompt(self.min_sentences, self.max_sentences)

    def _too_few(self) -> SummaryError:
        return SummaryError(
            422, f"This drive needs at least {self.min_sentences} stats for a summary."
        )

    def _template(self, items: Sequence[StatItem]) -> SummaryResult:
        try:
            summary = template_summary(
                items, min_sentences=self.min_sentences, max_sentences=self.max_sentences
            )
        except ValueError:
            raise self._too_few() from None
        return SummaryResult(summary, "template", cached=False, model=None)

    def summarize(self, stats: Sequence[DriveStat], client_ip: str) -> SummaryResult:
        items = summary_input(stats)
        if len(items) < self.min_sentences:
            raise self._too_few()
        payload = stats_json(items)
        model = self.client.model
        key = cache_key(
            model=model,
            prompt_version=self.prompt_version,
            system=self.system,
            stats=items,
            max_tokens=self.max_tokens,
        )
        hit = self.cache.get(key)
        if hit is not None:
            return SummaryResult(hit.summary, "model", cached=True, model=hit.model)
        if not self.client.ready():
            raise SummaryError(503, NEEDS_CONNECTION)
        wait = self.limiter.check(client_ip)
        if wait is not None:
            raise SummaryError(
                429,
                "Too many summary requests. Try again soon.",
                retry_after_s=_retry_after(wait),
            )

        in_tokens = out_tokens = spent = 0
        problems: list[str] = []
        for attempt in range(MAX_ATTEMPTS):
            system = self.system + (retry_note(problems) if problems else "")
            worst = worst_case_usd_micros(
                (system, USER_PREFIX, payload), self.max_tokens, self.in_price, self.out_price
            )
            reservation = self.ledger.reserve(worst)
            if reservation is None:
                if attempt == 0:
                    raise SummaryError(
                        429,
                        "The daily summary budget is spent. Try again tomorrow.",
                        retry_after_s=_retry_after(seconds_to_utc_midnight(self._clock.now_ns())),
                    )
                log.info("Summary retry skipped: the daily budget is spent.")
                break
            try:
                result = self.client.complete(system, payload, self.max_tokens)
            except ModelOffline:
                self.ledger.release(reservation)
                if attempt == 0:
                    raise SummaryError(503, NEEDS_CONNECTION) from None
                break
            except ModelCallFailed as e:
                if not e.maybe_billed:
                    self.ledger.release(reservation)
                if attempt == 0:
                    raise SummaryError(
                        502, "The summary service did not answer. Try again later."
                    ) from None
                break
            cost = cost_usd_micros(
                result.in_tokens, result.out_tokens, self.in_price, self.out_price
            )
            self.ledger.settle(reservation, cost)
            in_tokens += result.in_tokens
            out_tokens += result.out_tokens
            spent += cost
            parsed, problems = parse_output(result.text)
            if not problems:
                problems = validate(
                    parsed,
                    items,
                    min_sentences=self.min_sentences,
                    max_sentences=self.max_sentences,
                )
            if not problems:
                summary = normalize(parsed)
                self.cache.put(
                    key,
                    model=model,
                    prompt_version=self.prompt_version,
                    stats_json=payload,
                    summary=summary,
                    in_tokens=in_tokens,
                    out_tokens=out_tokens,
                    cost_usd_micros=spent,
                )
                return SummaryResult(summary, "model", cached=False, model=model)
            log.info("Summary attempt %d rejected: %s", attempt + 1, "; ".join(problems))
        return self._template(items)
