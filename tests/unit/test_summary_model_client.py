"""The real Anthropic client, driven through the SDK against a loopback stub of the Messages API.

No request leaves the machine: ``ANTHROPIC_BASE_URL`` points at a local HTTP server, and the
API key is a synthetic test string.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from photoncar.config import load_config
from photoncar.summary.model_client import (
    OUTPUT_SCHEMA,
    AnthropicSummaryClient,
    ModelCallFailed,
    ModelOffline,
)
from photoncar.summary.prompt import USER_PREFIX

TEST_KEY = "sk-ant-test-" + "0" * 24
REPLY = '{"sentences":[{"text":"Peak lateral: 0.46 g.","stat_ids":["perf.peak_lateral_g"]}]}'
STATS_JSON = '[{"id":"perf.peak_lateral_g","label":"Peak lateral","value":0.46,"unit":"g",' \
    '"display":"0.46 g"}]'  # fmt: skip


@dataclass
class Stub:
    status: int = 200
    delay_s: float = 0.0
    requests: list[tuple[str, dict[str, str], dict[str, Any]]] = field(default_factory=list)
    url: str = ""


def _message() -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-haiku-4-5-20251001",
        "content": [{"type": "text", "text": REPLY}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": 812,
            "output_tokens": 97,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> Iterator[Stub]:
    state = Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.requests.append((self.path, headers, body))
            if state.delay_s:
                time.sleep(state.delay_s)
            if state.status == 200:
                payload = _message()
            else:
                payload = {"type": "error", "error": {"type": "api_error", "message": "stub"}}
            data = json.dumps(payload).encode()
            try:
                self.send_response(state.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_address[1]}"
    monkeypatch.setenv("ANTHROPIC_BASE_URL", state.url)
    monkeypatch.setenv("ANTHROPIC_API_KEY", TEST_KEY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    yield state
    server.shutdown()
    server.server_close()


def test_request_shape_and_usage(stub: Stub) -> None:
    client = AnthropicSummaryClient(load_config())
    assert client.ready()
    result = client.complete("SYSTEM TEXT", STATS_JSON, 400)
    assert (result.text, result.in_tokens, result.out_tokens) == (REPLY, 812, 97)
    assert len(stub.requests) == 1
    path, headers, body = stub.requests[0]
    assert path == "/v1/messages"
    assert headers["x-api-key"] == TEST_KEY
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["max_tokens"] == 400
    assert body["system"] == "SYSTEM TEXT"
    assert body["messages"] == [{"role": "user", "content": USER_PREFIX + STATS_JSON}]
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    assert "thinking" not in body


@pytest.mark.parametrize("status", [400, 429, 500, 529])
def test_service_errors_fail_once_without_retry_or_billing(stub: Stub, status: int) -> None:
    stub.status = status
    client = AnthropicSummaryClient(load_config())
    with pytest.raises(ModelCallFailed) as err:
        client.complete("s", STATS_JSON, 400)
    assert err.value.maybe_billed is False
    assert len(stub.requests) == 1  # max_retries=0: 1 billed-or-not call per reservation


def test_timeout_may_have_been_billed(stub: Stub) -> None:
    stub.delay_s = 1.0
    client = AnthropicSummaryClient(load_config(), timeout_s=0.2)
    with pytest.raises(ModelCallFailed) as err:
        client.complete("s", STATS_JSON, 400)
    assert err.value.maybe_billed is True
    assert len(stub.requests) == 1


def test_unreachable_service_is_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{port}")
    monkeypatch.setenv("ANTHROPIC_API_KEY", TEST_KEY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    with pytest.raises(ModelOffline):
        AnthropicSummaryClient(load_config()).complete("s", STATS_JSON, 400)


def test_without_a_key_it_is_not_ready_and_never_calls_out(
    stub: Stub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    client = AnthropicSummaryClient(load_config())
    assert not client.ready()
    with pytest.raises(ModelOffline):
        client.complete("s", STATS_JSON, 400)
    assert stub.requests == []
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    assert not client.ready()


def test_the_key_never_reaches_logs_or_errors(stub: Stub, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    client = AnthropicSummaryClient(load_config())
    client.complete("s", STATS_JSON, 400)
    stub.status = 500
    with pytest.raises(ModelCallFailed) as err:
        client.complete("s", STATS_JSON, 400)
    assert TEST_KEY not in caplog.text
    assert TEST_KEY not in str(err.value)
    assert err.value.__cause__ is None and err.value.__suppress_context__
