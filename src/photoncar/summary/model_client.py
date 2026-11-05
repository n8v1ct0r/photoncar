"""The model behind the drive summary.

``AnthropicSummaryClient`` is the server default. It reads ``ANTHROPIC_API_KEY`` from the
environment at call time, only on the server, and never logs or returns it. Without the key
it is not ready, and the summary route answers 503 "Needs connection." without calling out.
It asks for structured JSON output (``output_config.format``), sends 1 request per call
(``max_retries=0``, so every billed call passes through the spend ledger once), and reports
token usage for the ledger.

``FakeSummaryClient`` is for tests and local development only. The server never selects it
by itself: it runs only when passed explicitly to ``create_app``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

import anthropic

from photoncar.config import Config
from photoncar.summary.prompt import USER_PREFIX
from photoncar.summary.stats import StatItem
from photoncar.summary.validate import template_summary

log = logging.getLogger(__name__)

API_KEY_ENV = "ANTHROPIC_API_KEY"
REQUEST_TIMEOUT_S = 30.0

OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "stat_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "stat_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sentences"],
    "additionalProperties": False,
}


@dataclass(frozen=True, slots=True)
class ModelResult:
    text: str
    in_tokens: int
    out_tokens: int


class ModelOffline(Exception):
    """No API key, or the model service could not be reached. Nothing was billed."""


class ModelCallFailed(Exception):
    """The model service answered with an error, or the call timed out.

    ``maybe_billed`` is True when tokens may have been spent (a timeout), so the spend
    reservation must stand.
    """

    def __init__(self, reason: str, *, maybe_billed: bool) -> None:
        super().__init__(reason)
        self.maybe_billed = maybe_billed


class SummaryClient(Protocol):
    @property
    def model(self) -> str:
        """The model id: part of the cache key and of the route's answer."""
        ...

    def ready(self) -> bool:
        """True when the client has what it needs to call out. Makes no network call."""
        ...

    def complete(self, system: str, stats_json: str, max_tokens: int) -> ModelResult:
        """1 model call. Raises ``ModelOffline`` or ``ModelCallFailed``."""
        ...


class AnthropicSummaryClient:
    def __init__(self, cfg: Config, *, timeout_s: float = REQUEST_TIMEOUT_S) -> None:
        self._model = str(cfg.get("summary.model"))
        self._timeout_s = timeout_s
        self._lock = threading.Lock()
        self._sdk: anthropic.Anthropic | None = None
        self._sdk_key: str | None = None

    @property
    def model(self) -> str:
        return self._model

    @staticmethod
    def _key() -> str:
        return os.environ.get(API_KEY_ENV, "").strip()

    def ready(self) -> bool:
        return bool(self._key())

    def _client(self) -> anthropic.Anthropic:
        key = self._key()
        if not key:
            raise ModelOffline("no API key")
        with self._lock:
            if self._sdk is None or self._sdk_key != key:
                self._sdk = anthropic.Anthropic(api_key=key, max_retries=0, timeout=self._timeout_s)
                self._sdk_key = key
            return self._sdk

    def complete(self, system: str, stats_json: str, max_tokens: int) -> ModelResult:
        client = self._client()
        try:
            message = client.messages.create(
                model=self._model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": USER_PREFIX + stats_json}],
                output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
            )
        except anthropic.APITimeoutError:
            log.warning("Summary model call timed out.")
            raise ModelCallFailed("the model call timed out", maybe_billed=True) from None
        except anthropic.APIConnectionError:
            log.warning("Summary model service could not be reached.")
            raise ModelOffline("could not reach the model service") from None
        except anthropic.APIStatusError as e:
            log.warning("Summary model service returned HTTP %d.", e.status_code)
            raise ModelCallFailed(f"HTTP {e.status_code}", maybe_billed=False) from None
        if message.stop_reason not in ("end_turn", "stop_sequence"):
            log.warning("Summary model stopped with %s.", message.stop_reason)
        text = "".join(block.text for block in message.content if block.type == "text")
        usage = message.usage
        in_tokens = (
            usage.input_tokens
            + (usage.cache_creation_input_tokens or 0)
            + (usage.cache_read_input_tokens or 0)
        )
        return ModelResult(text=text, in_tokens=in_tokens, out_tokens=usage.output_tokens)


FakeReply = str | Exception


class FakeSummaryClient:
    """A scripted stand-in for the model. Tests and local development only.

    Each call takes the next scripted reply: a string is returned as the model text, an
    exception instance is raised. With no replies left, it returns a valid summary built from
    the stats (3 to 5 sentences, each quoting 1 stat's display). ``calls`` records every call.
    """

    def __init__(
        self,
        replies: Sequence[FakeReply] = (),
        *,
        model: str = "fake-summary-model",
        in_tokens: int = 600,
        out_tokens: int = 120,
        ready: bool = True,
    ) -> None:
        self._replies = list(replies)
        self._model = model
        self._in_tokens = in_tokens
        self._out_tokens = out_tokens
        self._ready = ready
        self._lock = threading.Lock()
        self.calls: list[tuple[str, str, int]] = []

    @property
    def model(self) -> str:
        return self._model

    def ready(self) -> bool:
        return self._ready

    def complete(self, system: str, stats_json: str, max_tokens: int) -> ModelResult:
        with self._lock:
            self.calls.append((system, stats_json, max_tokens))
            reply = self._replies.pop(0) if self._replies else None
        if not self._ready:
            raise ModelOffline("fake client is offline")
        if isinstance(reply, Exception):
            raise reply
        if reply is None:
            items: list[StatItem] = json.loads(stats_json)
            reply = json.dumps(template_summary(items))
        return ModelResult(text=reply, in_tokens=self._in_tokens, out_tokens=self._out_tokens)
