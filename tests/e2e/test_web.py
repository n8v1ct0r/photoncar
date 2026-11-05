"""The dashboard in a real browser (Playwright Chromium) against `photoncar serve`.

Setup, once per module: seed the demo drives into a temporary folder (`seed-demo`, public dataset
and synthetic only), build the web app and the embed (`pnpm build`, `pnpm build:embed`), start
`photoncar serve` on a free port over that folder, and export the embed of 1 public-dataset
drive into a temporary folder served by a plain static file server.

Needs the public dataset sample (make fetch-dataset), pnpm, and the Playwright browser
(`uv run playwright install chromium`). Run with `uv run pytest -m e2e`; add `-s` to print the
measured live text sizes. With PHOTONCAR_SHOTS set to a folder, every checked route and state is
also saved there as a screenshot.
"""

from __future__ import annotations

import functools
import http.server
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import (
    Browser,
    ConsoleMessage,
    Page,
    Playwright,
    Route,
    ViewportSize,
    sync_playwright,
)

from photoncar import SAFETY_LINE, cli
from photoncar.config import REPO_ROOT, load_config
from photoncar.export.embed import export_embed
from photoncar.sources.comma2k19 import default_dataset_root

pytestmark = pytest.mark.e2e

WEB = REPO_ROOT / "web"
DESKTOP: ViewportSize = {"width": 1440, "height": 900}
PHONE: ViewportSize = {"width": 390, "height": 844}
VIEWPORTS = {"1440": DESKTOP, "390": PHONE}
# Plan 8.4 device assumptions: a 13.3 in laptop at 1440x900 and a 6.1 in phone at 390x844.
MM_PER_PX = {"1440": 0.1987, "390": 0.1657}
MIN_TEXT_PX = {"1440": 41.8, "390": 50.2}
MIN_DIGIT_PX_390 = 70.2
VIEW_DISTANCE_MM = 1000.0
MIN_TEXT_ARCMIN = 20.0
MIN_DIGIT_ARCMIN = 40.0
INK = "rgb(17, 20, 23)"
YELLOW = "rgb(255, 230, 0)"
LABEL_SAMPLE = "Replay: public dataset sample"
LABEL_ROUTE = "Replay: public dataset route"
LABEL_TRACK = "Synthetic track day"
EMBED_SUBDIR = "replay"
TOKEN_KEY = "photoncar.device_token"
DEVICE_TOKEN = "review-token-123"
REFUSED = "The device refused that token. Check PHOTONCAR_API_TOKEN on the device."
CREDIT = "Replay of public data (comma2k19, MIT)"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


def get_json(url: str) -> Any:
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


@dataclass(frozen=True)
class Stack:
    base: str
    """The device app, served by `photoncar serve`."""
    data_dir: Path
    ids: dict[str, str]
    """Drive label to drive ID."""
    embed_base: str
    """The embed's folder on the static server, ending in /."""
    embed_origin: str
    embed_dir: Path
    embed_id: str


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        return


def run_pnpm(script: str) -> None:
    pnpm = shutil.which("pnpm")
    if pnpm is None:
        pytest.fail("pnpm is not installed.")
    subprocess.run([pnpm, script], cwd=WEB, check=True, capture_output=True, timeout=300)


def start_serve(data: Path, token: str | None = None) -> tuple[subprocess.Popen[bytes], str]:
    """Start `photoncar serve` over `data` on a free port; with `token`, the API asks for it."""
    port = free_port()
    env = {
        k: v for k, v in os.environ.items() if k not in ("PHOTONCAR_API_TOKEN", "ANTHROPIC_API_KEY")
    }
    if token is not None:
        env["PHOTONCAR_API_TOKEN"] = token
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "photoncar.cli",
            "serve",
            "--data-dir",
            str(data),
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 60
    while True:
        try:
            with urllib.request.urlopen(f"{base}/api/health", timeout=10):
                break
        except urllib.error.HTTPError:
            break  # the server answers (a 401 when it asks for a token)
        except (urllib.error.URLError, ConnectionError):
            if time.monotonic() > deadline or proc.poll() is not None:
                proc.kill()
                pytest.fail("photoncar serve did not start.")
            time.sleep(0.2)
    return proc, base


def stop_serve(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(scope="module")
def stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Stack]:
    root = default_dataset_root()
    if not (root / cli.SAMPLE_REF).is_dir():
        pytest.fail("The comma2k19 sample is missing. Run make fetch-dataset.")
    run_pnpm("build")
    run_pnpm("build:embed")

    data = tmp_path_factory.mktemp("demo")
    assert cli.main(["seed-demo", "--data-dir", str(data)]) == 0

    proc, base = start_serve(data)
    ids = {d["label"]: d["drive_id"] for d in get_json(f"{base}/api/drives")["drives"]}
    embed_id = ids.get(LABEL_ROUTE, ids[LABEL_SAMPLE])

    site = tmp_path_factory.mktemp("site")
    embed_dir = site / EMBED_SUBDIR
    privacy = site.parent / f"{site.name}-privacy.toml"
    privacy.write_text("[home]\nlat_deg = -45.0\nlon_deg = 170.0\n", encoding="utf-8")
    export_embed(
        data,
        embed_id,
        embed_dir,
        cfg=load_config(),
        web_dist_embed=WEB / "dist-embed",
        privacy_path=privacy,
    )
    static = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(site))
    )
    threading.Thread(target=static.serve_forever, daemon=True).start()
    origin = f"http://127.0.0.1:{static.server_address[1]}"
    try:
        yield Stack(
            base=base,
            data_dir=data,
            ids=ids,
            embed_base=f"{origin}/{EMBED_SUBDIR}/",
            embed_origin=origin,
            embed_dir=embed_dir,
            embed_id=embed_id,
        )
    finally:
        static.shutdown()
        stop_serve(proc)


@pytest.fixture(scope="module")
def pw() -> Iterator[Playwright]:
    with sync_playwright() as p:
        yield p


@pytest.fixture(scope="module")
def browser(pw: Playwright) -> Iterator[Browser]:
    b = pw.chromium.launch()
    yield b
    b.close()


def new_page(browser: Browser, viewport: ViewportSize, *, reduced: bool = False) -> Page:
    ctx = browser.new_context(
        viewport=viewport,
        device_scale_factor=1,
        reduced_motion="reduce" if reduced else "no-preference",
    )
    return ctx.new_page()


def shot(page: Page, name: str) -> None:
    folder = os.environ.get("PHOTONCAR_SHOTS")
    if folder:
        Path(folder).mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(Path(folder) / f"{name}.png"))


def _safety_in_view(page: Page, where: str) -> None:
    line = page.locator(".safety-line")
    box = line.bounding_box()
    vp = page.viewport_size
    assert box is not None and vp is not None, where
    assert box["y"] >= 0 and box["y"] + box["height"] <= vp["height"] + 0.5, (where, box)
    assert box["x"] >= 0 and box["x"] + box["width"] <= vp["width"] + 0.5, (where, box)
    uncovered = page.evaluate(
        """() => {
          const el = document.querySelector('.safety-line');
          const r = el.getBoundingClientRect();
          const x = r.left + Math.min(20, r.width / 2);
          const hit = document.elementFromPoint(x, r.top + r.height / 2);
          return hit === el || el.contains(hit);
        }"""
    )
    assert uncovered, where


def assert_no_sideways_scroll(page: Page, where: str) -> None:
    widths = page.evaluate(
        "() => [document.documentElement.scrollWidth, document.documentElement.clientWidth]"
    )
    assert widths[0] <= widths[1], (where, widths)


def assert_safety_visible(page: Page, where: str) -> None:
    """The safety line is on screen, uncovered, where the page lands, and at the top of the
    page too (a deep link to a box scrolls a phone page to that box)."""
    line = page.locator(".safety-line")
    line.wait_for(state="visible", timeout=10_000)
    assert line.count() == 1, where
    assert line.inner_text().strip() == SAFETY_LINE, where
    _safety_in_view(page, where)
    if page.evaluate("() => window.scrollY") != 0:
        page.evaluate("() => window.scrollTo(0, 0)")
        _safety_in_view(page, f"{where}, scrolled to the top")


def json_route(
    status: int, body: Any, headers: dict[str, str] | None = None
) -> Callable[[Route], None]:
    def handle(route: Route) -> None:
        route.fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(body),
            headers=headers or {},
        )

    return handle


def hold(held: list[Route]) -> Callable[[Route], None]:
    """Keep the request open, so the page shows its loading state."""

    def handle(route: Route) -> None:
        held.append(route)

    return handle


# -- the safety line on every route and state --------------------------------------------------


Setup = Callable[[Page, Stack], None]


@dataclass(frozen=True)
class Case:
    name: str
    path: Callable[[Stack], str]
    wait: str
    """A selector, or text=... to wait for before checking."""
    setup: Setup | None = None
    action: Callable[[Page], None] | None = None


def _nothing(page: Page, s: Stack) -> None:
    return


def route_id(s: Stack) -> str:
    return s.ids.get(LABEL_ROUTE, s.ids[LABEL_SAMPLE])


def _no_drives(page: Page, s: Stack) -> None:
    page.route("**/api/drives", json_route(200, {"drives": []}))


def _events_empty(page: Page, s: Stack) -> None:
    page.route("**/api/drives/*/events", json_route(200, {"events": []}))


def _report_empty(page: Page, s: Stack) -> None:
    page.route(
        "**/api/drives/*/report",
        json_route(
            200,
            {
                "behavior": {
                    "hard_brakes": 0, "hard_accels": 0, "corners": 0, "jerk_long_mean": None,
                    "jerk_long_p95": None, "jerk_lat_mean": None, "jerk_lat_p95": None,
                    "rpm_bands": [], "shifts": [],
                },
                "performance": {"zero_to_sixty": [], "sixty_to_zero": [], "peak_lateral_g": None},
                "laps": [],
            },
        ),
    )  # fmt: skip


def _held(pattern: str) -> Setup:
    def setup(page: Page, s: Stack) -> None:
        held: list[Route] = []
        page.route(pattern, hold(held))

    return setup


def _failing(pattern: str, status: int = 500) -> Setup:
    def setup(page: Page, s: Stack) -> None:
        page.route(pattern, json_route(status, {"error": "The device hit an error. Try again."}))

    return setup


def _token_needed(page: Page, s: Stack) -> None:
    page.route("**/api/**", json_route(401, {"error": "A valid device token is needed."}))


def _token_refused(page: Page, s: Stack) -> None:
    """A token is stored and the device answers 401 to it."""
    page.add_init_script(f"window.localStorage.setItem({TOKEN_KEY!r}, 'wrong-token')")
    _token_needed(page, s)


HEALTH_RECORDING = {"ok": True, "version": "0.1.0", "mode": "live", "moving": False}


def _recording_no_frames_yet(page: Page, s: Stack) -> None:
    """The device records, and its live feed has not sent a frame yet."""
    page.route("**/api/health", json_route(200, HEALTH_RECORDING))
    held: list[Route] = []
    page.route("**/api/live/stream", hold(held))


def _moving(page: Page, s: Stack) -> None:
    page.route(
        "**/api/health",
        json_route(200, {"ok": True, "version": "0.1.0", "mode": "live", "moving": True}),
    )
    page.route(
        "**/api/drives**", json_route(423, {"error": "Parked views open when the car stops."})
    )
    held: list[Route] = []
    page.route("**/api/live/stream", hold(held))


def _summary_429(page: Page, s: Stack) -> None:
    page.route(
        "**/api/drives/*/summary",
        json_route(429, {"error": "Too many summaries from this device."}, {"Retry-After": "30"}),
    )


def _summary_template(page: Page, s: Stack) -> None:
    page.route(
        "**/api/drives/*/summary",
        json_route(
            200,
            {
                "sentences": [
                    {
                        "text": "The drive covered the distance shown.",
                        "stat_ids": ["drive.distance_mi"],
                    },
                    {
                        "text": "Top speed is listed with its stat.",
                        "stat_ids": ["drive.max_speed_mph"],
                    },
                    {"text": "Alerts are counted.", "stat_ids": ["alerts.count"]},
                ],
                "source": "template",
                "cached": False,
                "model": None,
            },
        ),
    )


def click(label: str) -> Callable[[Page], None]:
    def act(page: Page) -> None:
        page.get_by_role("button", name=label).click()

    return act


CASES: list[Case] = [
    Case("stages", lambda s: "/", ".stage"),
    Case("stages-loading", lambda s: "/", "text=Loading drives.", _held("**/api/drives")),
    Case("stages-empty", lambda s: "/", "text=No drives yet.", _no_drives),
    Case("stages-error", lambda s: "/", ".state-error", _failing("**/api/drives")),
    Case("review", lambda s: f"/drives/{route_id(s)}", ".box.is-selected"),
    Case("review-box", lambda s: f"/drives/{route_id(s)}?box=3", ".box.is-selected"),
    Case("review-track", lambda s: f"/drives/{s.ids[LABEL_TRACK]}", ".box.is-selected"),
    Case(
        "review-loading",
        lambda s: f"/drives/{route_id(s)}",
        "text=Loading the roadbook.",
        _held("**/api/drives/*/timeline*"),
    ),
    Case(
        "review-empty",
        lambda s: f"/drives/{route_id(s)}",
        "text=No boxes on this drive.",
        _events_empty,
    ),
    Case("review-error", lambda s: "/drives/no-such-drive", ".state-error"),
    Case(
        "summary-offline",
        lambda s: f"/drives/{route_id(s)}",
        "text=Needs connection.",
        None,
        click("Write summary"),
    ),
    Case(
        "summary-429",
        lambda s: f"/drives/{route_id(s)}",
        "text=Try again in 30 s.",
        _summary_429,
        click("Write summary"),
    ),
    Case(
        "summary-template",
        lambda s: f"/drives/{route_id(s)}",
        ".summary .chip",
        _summary_template,
        click("Write summary"),
    ),
    Case(
        "export",
        lambda s: f"/drives/{route_id(s)}",
        ".outputs .state-error, .outputs .exported, .outputs .state-empty",
        None,
        click("Export laps"),
    ),
    Case("report", lambda s: f"/drives/{s.ids[LABEL_TRACK]}/report", ".run"),
    Case(
        "report-loading",
        lambda s: f"/drives/{route_id(s)}/report",
        "text=Loading the report.",
        _held("**/api/drives/*/report"),
    ),
    Case(
        "report-empty",
        lambda s: f"/drives/{route_id(s)}/report",
        "text=No performance runs on this drive.",
        _report_empty,
    ),
    Case("report-error", lambda s: "/drives/no-such-drive/report", ".state-error"),
    Case("legend", lambda s: "/legend", ".rules"),
    Case(
        "legend-loading",
        lambda s: "/legend",
        "text=Loading the thresholds.",
        _held("**/api/legend"),
    ),
    Case("legend-error", lambda s: "/legend", ".state-error", _failing("**/api/legend")),
    Case("live-frozen", lambda s: f"/live?replay={route_id(s)}&t=555", ".live-head"),
    Case("live-replay", lambda s: f"/live?replay={s.ids[LABEL_SAMPLE]}", ".live-line, .live-head"),
    Case(
        "live-waiting",
        lambda s: "/live",
        "text=Waiting for live frames.",
        _recording_no_frames_yet,
    ),
    Case("live-idle", lambda s: "/live", "text=Nothing is recording."),
    Case("live-error", lambda s: "/live", ".live-line", _failing("**/api/live/stream")),
    Case("token", lambda s: "/", "#token-input", _token_needed),
    Case("token-refused", lambda s: "/", "#token-problem", _token_refused),
    Case("token-live", lambda s: "/live", ".live-line", _token_needed),
    Case("moving", lambda s: f"/drives/{route_id(s)}", ".banks", _moving),
    Case("missing", lambda s: "/nowhere", "text=Not on this roadbook"),
]

EMBED_CASES: list[tuple[str, Callable[[Stack], str], str]] = [
    ("embed-home", lambda s: "", ".box.is-selected"),
    ("embed-review", lambda s: f"#/drives/{s.embed_id}?box=2", ".box.is-selected"),
    ("embed-report", lambda s: f"#/drives/{s.embed_id}/report", ".report-part"),
    ("embed-live", lambda s: "#/live", ".live-head, .live-line"),
    ("embed-legend", lambda s: "#/legend", ".rules"),
]


@pytest.mark.parametrize("vp", ["1440", "390"])
def test_safety_line_on_every_route_and_state(browser: Browser, stack: Stack, vp: str) -> None:
    for case in CASES:
        page = new_page(browser, VIEWPORTS[vp])
        try:
            if case.setup:
                case.setup(page, stack)
            page.goto(stack.base + case.path(stack))
            if case.action:
                page.locator(".box.is-selected").wait_for()
                case.action(page)
            page.locator(case.wait).first.wait_for(timeout=15_000)
            where = f"{case.name} at {vp}"
            assert_safety_visible(page, where)
            assert_no_sideways_scroll(page, where)
            if case.name in ("moving", "token-live", "live-error", "live-waiting", "live-idle"):
                assert page.locator("input, button, select, textarea, canvas").count() == 0, where
            if case.name == "moving":
                page.wait_for_url(re.compile(r"/live$"))
            if case.name == "summary-template":
                assert "is not on this drive" not in page.locator(".summary").inner_text(), where
            shot(page, f"{case.name}-{vp}")
        finally:
            page.context.close()
    for name, path, wait in EMBED_CASES:
        page = new_page(browser, VIEWPORTS[vp])
        try:
            page.goto(stack.embed_base + "index.html" + path(stack))
            page.locator(wait).first.wait_for(timeout=15_000)
            assert_safety_visible(page, f"{name} at {vp}")
            assert_no_sideways_scroll(page, f"{name} at {vp}")
            shot(page, f"{name}-{vp}")
        finally:
            page.context.close()


# -- console, keyboard, swipe -----------------------------------------------------------------


def watch_console(page: Page) -> list[str]:
    problems: list[str] = []

    def on_console(m: ConsoleMessage) -> None:
        if m.type in ("error", "warning"):
            problems.append(f"{m.type}: {m.text}")

    page.on("console", on_console)
    page.on("pageerror", lambda e: problems.append(f"pageerror: {e}"))
    return problems


@pytest.mark.parametrize("vp", ["1440", "390"])
def test_main_flows_log_no_console_errors_or_warnings(
    browser: Browser, stack: Stack, vp: str
) -> None:
    page = new_page(browser, VIEWPORTS[vp])
    problems = watch_console(page)
    try:
        page.goto(stack.base + "/")
        page.get_by_role("link", name=LABEL_TRACK).click()
        page.locator(".box.is-selected").wait_for()
        page.locator(".box.is-selected").focus()
        for key in ("j", "j", "k", "ArrowDown"):
            page.keyboard.press(key)
        page.locator(".map-swipe").first.wait_for(state="attached")
        page.get_by_role("link", name="Report").first.click()
        page.locator(".run").first.wait_for()
        page.get_by_role("link", name="Legend").click()
        page.locator(".rules").wait_for()
        page.goto(stack.base + f"/live?replay={stack.ids[LABEL_SAMPLE]}")
        page.locator(".live-line, .live-head").first.wait_for()
        page.wait_for_timeout(1500)
        page.goto(stack.embed_base + "index.html")
        page.locator(".box.is-selected").wait_for()
        page.goto(stack.embed_base + "index.html#/live")
        page.locator(".live-head, .live-line").first.wait_for()
        page.wait_for_timeout(800)
    finally:
        page.context.close()
    assert problems == []


def selected_box(page: Page) -> int:
    text = page.locator(".box.is-selected .box-no .n").inner_text()
    return int(text)


def yellow_pixels(page: Page) -> list[int]:
    """Marker-yellow pixels in each strip canvas."""
    counts: list[int] = page.evaluate(
        """() => [...document.querySelectorAll('.strip-plot canvas')].map((c) => {
            const copy = document.createElement('canvas');
            copy.width = c.width;
            copy.height = c.height;
            const ctx = copy.getContext('2d', { willReadFrequently: true });
            ctx.drawImage(c, 0, 0);
            const d = ctx.getImageData(0, 0, c.width, c.height).data;
            let n = 0;
            for (let i = 0; i < d.length; i += 4) {
              if (d[i] === 255 && d[i + 1] === 230 && d[i + 2] === 0) n++;
            }
            return n;
        })"""
    )
    return counts


def test_keyboard_selection_focus_ring_and_cited_swipe(browser: Browser, stack: Stack) -> None:
    page = new_page(browser, DESKTOP)
    problems = watch_console(page)
    track = stack.ids[LABEL_TRACK]
    try:
        page.goto(stack.base + f"/drives/{track}?box=2")
        page.locator(".box.is-selected").wait_for()
        assert selected_box(page) == 2
        # Reach the roadbook by keyboard alone: the selected box is the one tab stop in it.
        for _ in range(30):
            page.keyboard.press("Tab")
            if page.evaluate("() => document.activeElement.classList.contains('box')"):
                break
        else:
            pytest.fail("Tab never reached the roadbook.")
        ring = page.evaluate(
            """() => { const s = getComputedStyle(document.activeElement);
                       return [s.outlineStyle, s.outlineWidth, s.outlineColor]; }"""
        )
        assert ring == ["solid", "3px", INK]
        page.locator(".strip-plot canvas").first.wait_for()
        page.wait_for_timeout(300)
        before = yellow_pixels(page)
        assert len(before) == 5

        page.keyboard.press("j")
        page.wait_for_function(
            "() => document.activeElement.getAttribute('aria-current') === 'true'"
        )
        assert selected_box(page) == 3
        assert page.url.endswith("?box=3")
        focused = page.evaluate("() => document.activeElement.id")
        assert focused == "box-3"
        swipe = page.evaluate(
            """() => getComputedStyle(
                document.querySelector('.box.is-selected'), '::before').backgroundColor"""
        )
        assert swipe == YELLOW
        page.wait_for_timeout(300)
        after = yellow_pixels(page)
        assert all(n > 0 for n in after), after  # the cited window lands on every strip
        assert after != before
        d = page.locator(".map-swipe").first.get_attribute("d")
        assert d is not None and d.startswith("M") and "L" in d  # and on the map

        page.keyboard.press("k")
        page.wait_for_function("() => document.activeElement.id === 'box-2'")
        page.keyboard.press("ArrowDown")
        page.wait_for_function("() => document.activeElement.id === 'box-3'")
        page.keyboard.press("Enter")
        assert selected_box(page) == 3
    finally:
        page.context.close()
    assert problems == []


# -- strip labels on short windows ---------------------------------------------------------------

LABEL_FIT = """() => {
  const out = [];
  for (const strip of document.querySelectorAll('.strips .strip')) {
    const lab = strip.querySelector('.strip-lab');
    for (const part of lab.children) {
      if (part.classList.contains('visually-hidden')) continue;
      part.scrollIntoView({ block: 'nearest', inline: 'nearest' });
      const r = part.getBoundingClientRect();
      const s = strip.getBoundingClientRect();
      const clips = [];
      for (let a = part.parentElement; a; a = a.parentElement) {
        const cs = getComputedStyle(a);
        if (cs.overflowX === 'visible' && cs.overflowY === 'visible') continue;
        const ar = a.getBoundingClientRect();
        if (r.top < ar.top - 0.5 || r.bottom > ar.bottom + 0.5 ||
            r.left < ar.left - 0.5 || r.right > ar.right + 0.5) {
          clips.push(String(a.className || a.tagName));
        }
      }
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      out.push({
        text: part.textContent.trim(),
        tag: part.classList.contains('tag'),
        inStrip: r.top >= s.top - 0.5 && r.bottom <= s.bottom + 0.5 && r.right <= s.right + 0.5,
        clips,
        uncovered: hit !== null && (hit === part || part.contains(hit)),
      });
    }
  }
  return out;
}"""

OUTPUT_DONE = ".outputs .state-error, .outputs .state-empty, .outputs .summary, .outputs .exported"


@pytest.mark.parametrize(
    ("width", "height", "action"),
    [
        (1280, 720, None),
        (1280, 720, "Write summary"),
        (1440, 900, "Write summary"),
        (1440, 900, "Export laps"),
    ],
)
def test_strip_labels_and_synthetic_tags_are_never_clipped(
    browser: Browser, stack: Stack, width: int, height: int, action: str | None
) -> None:
    """Every strip of the synthetic track day is synthetic: each shows its name, value, and
    Synthetic tag whole, on a short window and with the summary or export output open."""
    page = new_page(browser, {"width": width, "height": height})
    try:
        page.goto(stack.base + f"/drives/{stack.ids[LABEL_TRACK]}")
        page.locator(".box.is-selected").wait_for()
        page.locator(".strip-plot canvas").first.wait_for()
        if action:
            page.get_by_role("button", name=action).click()
            page.locator(OUTPUT_DONE).first.wait_for(timeout=15_000)
        page.evaluate("() => document.fonts.ready.then(() => true)")
        page.wait_for_timeout(200)
        parts = page.evaluate(LABEL_FIT)
        assert_safety_visible(page, f"strips at {width}x{height}, {action}")
        shot(page, f"strips-{width}x{height}-{(action or 'closed').replace(' ', '-').lower()}")
    finally:
        page.context.close()
    tags = [p for p in parts if p["tag"]]
    assert [t["text"] for t in tags] == ["Synthetic"] * 5
    assert len(parts) == 15  # name, value, and tag in each of the 5 strips
    bad = [p for p in parts if p["clips"] or not p["inStrip"] or not p["uncovered"]]
    assert bad == []


# -- the device token ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def token_base(stack: Stack) -> Iterator[str]:
    """A second `photoncar serve` over the same drives that asks for DEVICE_TOKEN."""
    proc, base = start_serve(stack.data_dir, token=DEVICE_TOKEN)
    try:
        yield base
    finally:
        stop_serve(proc)


@pytest.mark.parametrize("vp", ["1440", "390"])
def test_a_refused_token_says_so_and_is_forgotten(
    browser: Browser, token_base: str, vp: str
) -> None:
    page = new_page(browser, VIEWPORTS[vp])
    stored = f"() => window.localStorage.getItem({TOKEN_KEY!r})"
    try:
        page.goto(token_base + "/")
        field = page.locator("#token-input")
        field.wait_for(timeout=10_000)
        assert page.locator("#token-problem").count() == 0
        field.fill("wrong")
        page.get_by_role("button", name="Save token").click()
        problem = page.locator("#token-problem")
        problem.wait_for(timeout=10_000)
        assert problem.inner_text().strip() == REFUSED
        assert page.evaluate(stored) is None
        assert field.get_attribute("aria-invalid") == "true"
        assert field.get_attribute("aria-describedby") == "token-problem"
        page.wait_for_function("() => document.activeElement.id === 'token-input'", timeout=2000)
        f = field.bounding_box()
        pb = problem.bounding_box()
        b = page.get_by_role("button", name="Save token").bounding_box()
        assert f is not None and pb is not None and b is not None
        assert f["y"] + f["height"] <= pb["y"] and pb["y"] + pb["height"] <= b["y"]  # under it
        assert_safety_visible(page, f"token refused at {vp}")
        shot(page, f"token-wrong-{vp}")

        field.fill(DEVICE_TOKEN)
        page.get_by_role("button", name="Save token").click()
        page.locator(".stage").first.wait_for(timeout=10_000)
        assert page.locator("#token-problem").count() == 0
        assert page.evaluate(stored) == DEVICE_TOKEN
        page.reload()
        page.locator(".stage").first.wait_for(timeout=10_000)
        assert page.locator("#token-input").count() == 0
    finally:
        page.context.close()


# -- live text at 1 m -----------------------------------------------------------------------------


def arcmin(mm: float) -> float:
    return math.degrees(2 * math.atan(mm / 2 / VIEW_DISTANCE_MM)) * 60


MEASURE_LIVE = """() => {
  const footer = document.querySelector('.safety-live').getBoundingClientRect();
  const out = [];
  const seen = new Set();
  const live = document.querySelector('.frame-live');
  const walker = document.createTreeWalker(live, NodeFilter.SHOW_TEXT);
  const ctx = document.createElement('canvas').getContext('2d');
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!node.textContent.trim()) continue;
    const el = node.parentElement;
    if (seen.has(el) || el.closest('.visually-hidden')) continue;
    seen.add(el);
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.right <= 0 || r.left >= innerWidth) continue;
    const cs = getComputedStyle(el);
    const seg = cs.fontFamily.includes('DSEG7');
    ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
    const m = ctx.measureText(seg ? '8' : 'H');
    out.push({
      text: node.textContent.trim().slice(0, 40), seg, size: parseFloat(cs.fontSize),
      glyph: m.actualBoundingBoxAscent + (seg ? m.actualBoundingBoxDescent : 0),
      bottom: r.bottom, inFooter: el.closest('.safety-live') !== null,
    });
  }
  return {
    items: out, footerTop: footer.top, footerBottom: footer.bottom,
    scroll: document.documentElement.scrollHeight, fonts: document.fonts.check('50px DSEG7'),
  };
}"""


def assert_glance_sizes(m: Any, vp: str, where: str) -> None:
    """Every tripmeter text and digit meets the 1 m glance sizes and sits above the safety
    line, which sits inside the viewport. Prints the measured sizes."""
    height = VIEWPORTS[vp]["height"]
    items = m["items"]
    text = [i for i in items if not i["seg"]]
    digits = [i for i in items if i["seg"]]
    assert text and digits and m["fonts"]
    assert any(i["text"] == "Synthetic" for i in text)  # the synthetic coolant field
    mm = MM_PER_PX[vp]
    report = {
        "viewport": vp,
        "text_px": (min(i["size"] for i in text), max(i["size"] for i in text)),
        "text_cap_px": (min(i["glyph"] for i in text), max(i["glyph"] for i in text)),
        "digit_px": (min(i["size"] for i in digits), max(i["size"] for i in digits)),
        "digit_height_px": (min(i["glyph"] for i in digits), max(i["glyph"] for i in digits)),
    }
    for key in ("text_cap_px", "digit_height_px"):
        lo, hi = report[key]
        report[key.replace("_px", "_mm")] = (round(lo * mm, 3), round(hi * mm, 3))
        report[key.replace("_px", "_arcmin")] = (
            round(arcmin(lo * mm), 2),
            round(arcmin(hi * mm), 2),
        )
    print(f"\nlive text sizes, {where}: {json.dumps(report)}")
    for i in text:
        assert i["size"] >= MIN_TEXT_PX[vp], i
        assert arcmin(i["glyph"] * mm) >= MIN_TEXT_ARCMIN, i
    for i in digits:
        assert i["size"] >= (MIN_DIGIT_PX_390 if vp == "390" else MIN_DIGIT_PX_390 + 1), i
        assert arcmin(i["glyph"] * mm) >= MIN_DIGIT_ARCMIN, i
    # Everything fits above the safety line, which sits inside the viewport.
    for i in items:
        if not i["inFooter"]:
            assert i["bottom"] <= m["footerTop"] + 0.5, i
    assert m["footerBottom"] <= height + 0.5
    assert m["scroll"] <= height


@pytest.mark.parametrize("vp", ["390", "1440"])
def test_live_text_meets_the_1_m_glance_sizes(browser: Browser, stack: Stack, vp: str) -> None:
    page = new_page(browser, VIEWPORTS[vp])
    try:
        # The fullest frame: an active synthetic alert and a synthetic coolant field.
        page.goto(stack.base + f"/live?replay={route_id(stack)}&t=555")
        page.locator(".live-head").wait_for()
        page.evaluate("() => document.fonts.ready.then(() => true)")
        m = page.evaluate(MEASURE_LIVE)
    finally:
        page.context.close()
    assert_glance_sizes(m, vp, f"device at {vp}")


# -- the embed ------------------------------------------------------------------------------------


def test_embed_stays_in_its_folder_and_matches_the_api(browser: Browser, stack: Stack) -> None:
    page = new_page(browser, DESKTOP)
    requests: list[str] = []
    page.on("request", lambda r: requests.append(r.url))
    problems = watch_console(page)
    try:
        page.goto(stack.embed_base + "index.html")
        page.locator(".box.is-selected").wait_for()
        assert re.search(rf"#/drives/{stack.embed_id}$", page.url)  # 1 drive: straight to review
        assert page.get_by_text(CREDIT).first.is_visible()
        assert page.get_by_role("button", name="Write summary").count() == 0
        assert page.get_by_role("button", name="Export laps").count() == 0
        embed_boxes = page.locator(".box").count()
        embed_stats = page.locator(".stat-rail").inner_text()
        for path in (f"#/drives/{stack.embed_id}/report", "#/legend", "#/live"):
            page.goto(stack.embed_base + "index.html" + path)
            page.locator(".report-part, .rules, .live-head, .live-line").first.wait_for()
        page.wait_for_timeout(500)

        app = new_page(browser, DESKTOP)
        try:
            app.goto(stack.base + f"/drives/{stack.embed_id}")
            app.locator(".box.is-selected").wait_for()
            assert app.locator(".box").count() == embed_boxes
            assert app.locator(".stat-rail").inner_text() == embed_stats
        finally:
            app.context.close()
    finally:
        page.context.close()
    assert problems == []
    assert requests and all(u.startswith(stack.embed_base) for u in requests), [
        u for u in requests if not u.startswith(stack.embed_base)
    ]

    def embed_file(name: str) -> Any:
        return get_json(f"{stack.embed_base}data/{name}")

    api = f"{stack.base}/api/drives/{stack.embed_id}"
    listed = [
        d for d in get_json(f"{stack.base}/api/drives")["drives"] if d["drive_id"] == stack.embed_id
    ]
    assert embed_file("drives.json") == {"drives": listed}
    detail = get_json(api)
    assert detail["drive"]["dataset_ref"]  # a public-dataset replay: its ref names the route
    detail["drive"]["dataset_ref"] = "comma2k19"  # the embed drops the route's date and dongle ID
    assert embed_file("drive.json") == detail
    assert embed_file("timeline.json") == get_json(f"{api}/timeline?max_points=1500")
    assert embed_file("events.json") == get_json(f"{api}/events")
    assert embed_file("report.json") == get_json(f"{api}/report")
    assert embed_file("legend.json") == get_json(f"{stack.base}/api/legend")
    frames = embed_file("live_frames.json")
    assert 0 < len(frames) <= 1201
    for f in (frames[0], frames[len(frames) // 2], frames[-1]):
        assert f == get_json(f"{api}/live_frame?t_s={f['t_s']!r}")
    assert embed_file("trim.json") == {"windows_s": []}


def test_embed_live_loops_and_holds_still_for_reduced_motion(
    browser: Browser, stack: Stack
) -> None:
    def readings(reduced: bool) -> list[str]:
        page = new_page(browser, PHONE, reduced=reduced)
        try:
            page.goto(stack.embed_base + "index.html#/live")
            page.locator(".live-head, .live-line").first.wait_for()
            seen = []
            for _ in range(4):
                seen.append(page.locator(".banks").inner_text())
                page.wait_for_timeout(400)
            assert page.locator("input, button, select, textarea, canvas").count() == 0
            return seen
        finally:
            page.context.close()

    moving = readings(False)
    assert len(set(moving)) > 1
    still = readings(True)
    assert len(set(still)) == 1


def _embed_live_fullest(page: Page, s: Stack) -> None:
    """Serve the embed only its fullest frames: an active synthetic alert and a synthetic
    coolant field, so the glance text fills the most lines."""
    frames = json.loads((s.embed_dir / "data" / "live_frames.json").read_text(encoding="utf-8"))
    fullest = [f for f in frames if f["alert"] and "coolant_c" in f.get("synthetic_fields", [])]
    assert fullest, "The embed window holds no frame with an alert."
    page.route("**/data/live_frames.json", json_route(200, fullest))


@pytest.mark.parametrize("vp", ["1440", "390"])
def test_embed_live_prints_the_credit_and_a_way_back(
    browser: Browser, stack: Stack, vp: str
) -> None:
    """The embed's live view: the dataset credit and a visible link back to the roadbook, in a
    bar outside the tripmeter, while the tripmeter keeps its glance sizes and 1 viewport."""
    page = new_page(browser, VIEWPORTS[vp])
    where = f"embed live at {vp}"
    try:
        _embed_live_fullest(page, stack)
        page.goto(stack.embed_base + "index.html#/live")
        page.locator(".live-head").wait_for(timeout=15_000)
        page.evaluate("() => document.fonts.ready.then(() => true)")
        credit = page.get_by_text(CREDIT, exact=True)
        credit.wait_for(timeout=10_000)
        back = page.get_by_role("link", name="Roadbook")
        assert back.is_visible(), where  # without keyboard focus
        assert page.locator(".frame-live").get_by_text(CREDIT).count() == 0, where
        assert page.locator(".frame-live").get_by_role("link", name="Roadbook").count() == 0
        vw, vh = VIEWPORTS[vp]["width"], VIEWPORTS[vp]["height"]
        for loc in (credit, back):
            b = loc.bounding_box()
            assert b is not None and b["x"] >= 0 and b["x"] + b["width"] <= vw, where
            assert b["y"] >= 0 and b["y"] + b["height"] <= vh, where
        target = back.bounding_box()
        assert target is not None and target["height"] >= 40, (where, target)  # a touch target
        assert_glance_sizes(page.evaluate(MEASURE_LIVE), vp, where)
        assert_safety_visible(page, where)
        assert_no_sideways_scroll(page, where)
        assert page.locator("input, button, select, textarea, canvas").count() == 0, where
        shot(page, f"embed-live-fullest-{vp}")

        back.click()  # a mouse
        page.wait_for_url(re.compile(rf"#/drives/{stack.embed_id}$"))
        page.locator(".box.is-selected").wait_for()
    finally:
        page.context.close()

    ctx = browser.new_context(
        viewport=VIEWPORTS[vp], device_scale_factor=1, has_touch=True, is_mobile=vp == "390"
    )
    try:
        touch = ctx.new_page()
        touch.goto(stack.embed_base + "index.html#/live")
        touch.locator(".live-head, .live-line").first.wait_for(timeout=15_000)
        touch.get_by_role("link", name="Roadbook").tap()  # a finger
        touch.wait_for_url(re.compile(rf"#/drives/{stack.embed_id}$"))
        touch.locator(".box.is-selected").wait_for()
    finally:
        ctx.close()


@pytest.mark.parametrize("vp", ["1440", "390"])
def test_embed_legend_prints_the_credit(browser: Browser, stack: Stack, vp: str) -> None:
    page = new_page(browser, VIEWPORTS[vp])
    try:
        page.goto(stack.embed_base + "index.html#/legend")
        page.locator(".rules").wait_for(timeout=15_000)
        assert page.get_by_text(CREDIT, exact=True).is_visible()
    finally:
        page.context.close()
    device = new_page(browser, VIEWPORTS[vp])
    try:  # the device legend covers every drive, so it names no dataset
        device.goto(stack.base + "/legend")
        device.locator(".rules").wait_for()
        assert device.get_by_text(CREDIT, exact=True).count() == 0
    finally:
        device.context.close()


# -- the live feed: reconnect and idle ------------------------------------------------------------

SEG_TEXT = "() => [...document.querySelectorAll('.seg-lit')].map((e) => e.textContent)"
DASHES = ["---", "----", "---"]


def test_live_feed_reconnects_with_backoff_and_shows_dashes_while_lost(
    browser: Browser, stack: Stack
) -> None:
    """The device's live feed drops (the stream closes), then fails once (503), then comes
    back. While it is lost the tripmeter shows dashes and 1 plain line, asks for nothing, and
    tries again after 1 s, then 2 s; once frames flow, the line is gone."""
    page = new_page(browser, PHONE)
    calls: list[float] = []
    frame = {
        "t_s": 1.0, "speed_mph": 42.0, "rpm": 2100.0, "coolant_c": 90.0,
        "synthetic_fields": [], "alert": None,
    }  # fmt: skip
    closes = (
        f'event: state\ndata: {{"moving": false}}\n\nevent: frame\ndata: {json.dumps(frame)}\n\n'
    )
    # A feed that stays open: the device replays a stored drive as its live feed.
    open_feed = f"{stack.base}/api/live/stream?replay={route_id(stack)}&speed=1"

    def feed(route: Route) -> None:
        calls.append(time.monotonic())
        if len(calls) == 1:
            route.fulfill(status=200, content_type="text/event-stream", body=closes)
        elif len(calls) == 2:
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"error": "The device hit an error. Try again."}),
            )
        else:
            route.continue_(url=open_feed)

    lost_lines: set[str] = set()
    try:
        page.route("**/api/health", json_route(200, HEALTH_RECORDING))
        page.route("**/api/live/stream", feed)
        page.goto(stack.base + "/live")
        lost = page.locator(".live-line", has_text="Live feed lost.")
        lost.wait_for(timeout=10_000)
        while len(calls) < 3:
            if lost.count():
                text = lost.inner_text().replace(" ", " ").strip()
                lost_lines.add(text)
                assert page.evaluate(SEG_TEXT) == DASHES, text
                assert page.locator("input, button, select, textarea").count() == 0
                assert page.locator(".live-head").count() == 0
            assert time.monotonic() - calls[0] < 20, "No third try."
            page.wait_for_timeout(100)
        assert_safety_visible(page, "live feed lost at 390")
        page.wait_for_function(
            f"() => ({SEG_TEXT})()[0] !== '---' && !document.body.innerText.includes('lost')",
            timeout=15_000,
        )
        assert page.locator(".live-line, .live-head").first.is_visible()
        shot(page, "live-reconnected-390")
    finally:
        page.context.close()
    assert len(calls) >= 3
    assert calls[1] - calls[0] >= 0.9, calls  # 1 s after the drop
    assert calls[2] - calls[1] >= 1.9, calls  # then 2 s
    assert lost_lines, "The lost line never showed."
    pattern = re.compile(r"^Live feed lost\. Trying again (in [1-8] s|now)\.$")
    assert all(pattern.match(t) for t in lost_lines), lost_lines


def test_live_says_nothing_is_recording_while_the_device_is_idle(
    browser: Browser, stack: Stack
) -> None:
    assert get_json(f"{stack.base}/api/health")["mode"] == "idle"
    page = new_page(browser, PHONE)
    try:
        page.goto(stack.base + "/live")
        page.get_by_text("Nothing is recording.", exact=True).wait_for(timeout=10_000)
        page.wait_for_timeout(500)
        assert page.get_by_text("Nothing is recording.", exact=True).is_visible()
        assert page.get_by_text("Waiting for live frames.").count() == 0
        assert page.evaluate(SEG_TEXT) == DASHES
        assert page.locator("input, button, select, textarea").count() == 0
        assert_safety_visible(page, "live idle at 390")
    finally:
        page.context.close()


# -- the legend: units stay with their numbers ---------------------------------------------------

UNIT_BREAKS = r"""() => {
  const unit = /(\d)([  ])(°C|s|min|h|V|mi|km|%|sigma)(?![\p{L}\d])/gu;
  const out = [];
  let pairs = 0;
  const cells = document.querySelectorAll('.rules tbody td, .facts dd');
  for (const cell of cells) {
    const walker = document.createTreeWalker(cell, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      for (const m of node.textContent.matchAll(unit)) {
        pairs += 1;
        const range = document.createRange();
        range.setStart(node, m.index);
        range.setEnd(node, m.index + m[0].length);
        const tops = new Set([...range.getClientRects()].map((r) => Math.round(r.top)));
        if (m[2] !== ' ' || tops.size !== 1) {
          out.push({ text: m[0], nbsp: m[2] === ' ', lines: tops.size });
        }
      }
    }
  }
  return { pairs, bad: out };
}"""


@pytest.mark.parametrize("vp", ["390", "1440"])
def test_legend_keeps_every_number_with_its_unit(browser: Browser, stack: Stack, vp: str) -> None:
    page = new_page(browser, VIEWPORTS[vp])
    try:
        page.goto(stack.base + "/legend")
        page.locator(".rules").wait_for()
        page.evaluate("() => document.fonts.ready.then(() => true)")
        found = page.evaluate(UNIT_BREAKS)
        assert_no_sideways_scroll(page, f"legend at {vp}")
    finally:
        page.context.close()
    assert found["pairs"] >= 10, found  # the thresholds and the privacy radius
    assert found["bad"] == [], found


# -- the map: no label over another ---------------------------------------------------------------

MAP_OVERLAPS = """() => {
  const els = [...document.querySelectorAll(
    '.map-box circle, .map-label, .map-start, .map-finish, .map-scale text')];
  const svg = document.querySelector('.map-canvas svg').getBoundingClientRect();
  const boxes = els.map((e) => {
    const r = e.getBoundingClientRect();
    const g = e.closest('.map-box');
    const name = g ? `box ${g.textContent.trim()}` : e.textContent.trim() || e.classList[0];
    return { name, l: r.left, t: r.top, r: r.right, b: r.bottom };
  });
  const bad = [];
  for (let i = 0; i < boxes.length; i++) {
    const a = boxes[i];
    const out = a.l < svg.left - 0.5 || a.r > svg.right + 0.5;
    if (out || a.t < svg.top - 0.5 || a.b > svg.bottom + 0.5) bad.push([a.name, 'outside the map']);
    for (let j = i + 1; j < boxes.length; j++) {
      const b = boxes[j];
      if (a.l < b.r && b.l < a.r && a.t < b.b && b.t < a.b) bad.push([a.name, b.name]);
    }
  }
  return { markers: document.querySelectorAll('.map-box').length, labels: boxes.length, bad };
}"""


@pytest.mark.parametrize("vp", ["1440", "390"])
def test_map_box_markers_and_end_labels_never_overlap(
    browser: Browser, stack: Stack, vp: str
) -> None:
    """Bounding boxes of every box marker, the Start and Finish marks and labels, and the scale
    label: none overlaps another, and all sit inside the map. The track day repeats corners on
    each lap and the route ends near 2 boxes, so both crowd their markers."""
    for label in (LABEL_ROUTE, LABEL_TRACK):
        drive = stack.ids.get(label)
        if drive is None:
            continue
        page = new_page(browser, VIEWPORTS[vp])
        try:
            page.goto(stack.base + f"/drives/{drive}")
            page.locator(".map-box").first.wait_for()
            page.evaluate("() => document.fonts.ready.then(() => true)")
            page.wait_for_timeout(300)
            found = page.evaluate(MAP_OVERLAPS)
            events = get_json(f"{stack.base}/api/drives/{drive}/events")["events"]
            boxes = [e for e in events if e["box_no"] is not None]
            assert found["bad"] == [], (label, vp, found)
            assert found["markers"] == len(boxes), (label, vp, found)
            leaders = page.locator(".map-leader").count()
            assert leaders < found["markers"], (label, vp)  # most markers sit on the trace
            shot(page, f"map-{label.split()[-1]}-{vp}")
        finally:
            page.context.close()


# -- gear shifts stay out of the roadbook ---------------------------------------------------------


def test_events_without_a_box_number_stay_out_of_the_roadbook(
    browser: Browser, stack: Stack
) -> None:
    """Gear shifts carry box_no null and belong to the report only; box numbers run 1 to n
    over the rest, and ?box=N selects by box number."""
    track = stack.ids[LABEL_TRACK]
    events = get_json(f"{stack.base}/api/drives/{track}/events")["events"]
    out: list[dict[str, Any]] = []
    n = 0
    for e in events:
        if e["kind"] == "behavior.shift":
            out.append({**e, "box_no": None})
        else:
            n += 1
            out.append({**e, "box_no": n})
    assert 0 < n < len(out), "The track day should hold shifts and boxes."
    last = (
        out[-1]
        if out[-1]["box_no"] is not None
        else next(e for e in reversed(out) if e["box_no"] is not None)
    )
    page = new_page(browser, DESKTOP)
    problems = watch_console(page)
    try:
        page.route("**/api/drives/*/events", json_route(200, {"events": out}))
        page.goto(stack.base + f"/drives/{track}?box={n}")
        page.locator(".box.is-selected").wait_for()
        numbers = [int(t) for t in page.locator(".box .box-no .n").all_inner_texts()]
        assert numbers == list(range(1, n + 1))
        assert selected_box(page) == n
        assert page.locator(".box.is-selected").get_attribute("aria-label") == (
            f"Box {n}. {last['message']}"
        )
        page.locator(".map-box").first.wait_for()  # the map draws once it has a size
        assert page.locator(".map-box").count() == n
        assert page.locator(".safety-extra").inner_text().startswith(f"Box {n} of {n} ")
        assert f"{n} boxes" in page.locator(".drive-meta").inner_text()
        page.locator(".box.is-selected").focus()
        page.keyboard.press("k")
        page.wait_for_function(f"() => document.activeElement.id === 'box-{n - 1}'")

        page.goto(stack.base + "/")
        page.locator(".stage").first.wait_for()
        row = page.locator(".stage", has_text=LABEL_TRACK)
        boxes_cell = row.locator(".stage-facts div", has_text="Boxes").locator("dd")
        assert boxes_cell.inner_text() == str(n)

        page.goto(stack.base + f"/drives/{track}/report")
        page.locator(".shifts").wait_for()
        report = get_json(f"{stack.base}/api/drives/{track}/report")
        assert page.locator(".shifts tbody tr").count() == min(
            12, len(report["behavior"]["shifts"])
        )

        # The device's own answer: every gear shift comes without a box number, so the real
        # roadbook holds no shift.
        shifts = [e for e in events if e["kind"] == "behavior.shift"]
        assert shifts and all(e["box_no"] is None for e in shifts), [e["box_no"] for e in shifts]
        page.unroute("**/api/drives/*/events")
        page.goto(stack.base + f"/drives/{track}")
        page.locator(".box.is-selected").wait_for()
        labels = page.locator(".box").evaluate_all("(els) => els.map((e) => e.ariaLabel)")
        assert len(labels) == sum(1 for e in events if e["box_no"] is not None)
        shift_notes = {e["message"] for e in shifts}
        assert not [x for x in labels if x.split(". ", 1)[1] in shift_notes]
    finally:
        page.context.close()
    assert problems == []
