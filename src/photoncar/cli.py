"""The ``photoncar`` command.

- ``replay --dataset comma2k19 [--segment PATH | --route [DIR]]``: the pinned sample by default,
  1 segment, or a whole route (1 ``Comma2k19Source`` per segment, joined in order). CAN frames
  decode through the vendored Toyota DBC. comma2k19 has no OBD-II, so a ``SyntheticObdSource``
  overlay adds service 01 values, and every signal it makes is listed as synthetic. A replay of
  the dataset's RAV4 or Civic runs under that car's own vehicle profile (``dataset_vehicle``),
  not the configured one; a comma segment from any other car runs under the configured one.
- ``replay --synthetic track_day|commute --seed N``: a scripted drive (all synthetic).
- ``serve [--host 127.0.0.1] [--port 8000] [--live]``: the dashboard and API. The API token
  comes from ``PHOTONCAR_API_TOKEN``; without it the API answers only this device. Nothing moves
  in idle mode, so the parked views are open. ``--live`` is for the in-car device while another
  process records: this process has no speed source, so it starts in live mode, locked, and
  stays locked (plan H16).
- ``seed-demo --data-dir DIR``: the demo data set, from the public dataset and synthetic drives
  only. It refuses a folder that holds live drives.
- ``export --drive ID --kind laps``: lap v0 files under ``<data dir>/exports``. Refuses without
  ``config/privacy.local.toml``.
- ``export-embed --drive ID --out DIR``: the static portfolio replay of 1 public-dataset drive
  (``photoncar.export.embed.export_embed``). It refuses live drives and a missing privacy file.
- ``record``: a live drive from the car, with the dashboard served from the same process so the
  moving lock sees the live speed. Needs Linux, listen-only ``can0``, and OBD polling turned on;
  anywhere else it stops with a plain message and a non-zero exit. The GPS and IMU join when
  they are there (``open_live_sensors``); without them it records CAN and OBD only and says so.
  The sources merge on the clock they stamp records on, so a GPS without a fix never holds up
  the rest, and the IMU mount rotation is found from the drive's own data (pipeline runner).

The data folder (drives, index, exports) is ``--data-dir``, else ``PHOTONCAR_DATA_DIR``, else
``data/``. The public dataset has its own root: ``--public-dir``, else ``PHOTONCAR_PUBLIC_DIR``,
else ``data/public``, so a scratch data folder still finds it.

Exit codes: 0 done, 2 refused or not possible here (the message says why).
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import logging
import os
import signal
import sqlite3
import sys
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

import uvicorn

from photoncar import SAFETY_LINE
from photoncar.api.live import LiveState
from photoncar.clock import Clock, MonotonicClock
from photoncar.config import REPO_ROOT, Config, ConfigError, data_dir, load_config, load_vehicle
from photoncar.decode.dbc import (
    COMMA2K19_TOYOTA_DBC,
    COMMA2K19_TOYOTA_MAPPING,
    DbcDecoder,
    load_dbc,
)
from photoncar.detect.laps import StartFinishLine
from photoncar.pipeline.runner import (
    LapLine,
    RunReport,
    SegmentChain,
    records_table,
    run_to_data_dir,
)
from photoncar.records import Record
from photoncar.sources.base import Source, SourceMeta
from photoncar.sources.comma2k19 import (
    DATASET_FOLDER,
    Comma2k19Source,
    default_dataset_root,
    segment_profile,
)
from photoncar.sources.synthetic import (
    COOLANT_HOLD_S,
    COOLANT_PEAK_C,
    SCENARIOS,
    CoolantProfile,
    SyntheticDriveSource,
    SyntheticObdSource,
    synthetic_drive,
)
from photoncar.store.meta import DriveSource
from photoncar.store.reader import DriveReader

REFUSED = 2
log = logging.getLogger(__name__)

SAMPLE_REF = "Example_1/b0c9d2329ad1606b|2018-08-02--08-34-47/40"
ROUTE_REF = "Chunk_2/b0c9d2329ad1606b|2018-11-11--13-08-44"
TOYOTA_BUS = 0
"""The comma2k19 RAV4 carries ENGINE_RPM, WHEEL_SPEEDS and STEER_ANGLE_SENSOR on bus 0 only
(measured on the pinned sample: 0 of those frames on bus 1)."""

LABEL_SAMPLE = "Replay: public dataset sample"
LABEL_ROUTE = "Replay: public dataset route"
LABEL_SEGMENT = "Replay: public dataset segment"
LABEL_SYNTHETIC = {"track_day": "Synthetic track day", "commute": "Synthetic commute"}
LABEL_LIVE = "Live drive"

DEMO_SEED = 0
FAULT_START_S = 540.0
"""The demo route's scripted coolant fault starts 540 s (9 min) into the 900 s route."""
FAULT_RAMP_S = 10.0
FAULT_COOL_S = 20.0
NORMAL_COOLANT_C = 90.0

LAP_MIN_DIST_FRACTION = 0.5
"""A lap counts only after half the known track length since the last crossing."""

NO_ROUTE = "The public route is not fetched. Run make fetch-route."
NO_SAMPLE = "The public dataset sample is missing. Run make fetch-dataset."
PRIVACY_MISSING = (
    "Exports need the privacy file. Copy config/privacy.example.toml to "
    "config/privacy.local.toml and set the home center."
)
NO_SENSORS = "Recording CAN and OBD only. {reason}"
SENSOR_STOPPED = "{name} stopped: {reason} Recording goes on without it."
WEB_DIST_EMBED = REPO_ROOT / "web" / "dist-embed"


class Refused(Exception):
    """A plain reason the command cannot go ahead."""


# -- sources ---------------------------------------------------------------------------------


def segment_dirs(route_dir: Path) -> list[Path]:
    """Numbered segment folders of a route, in segment order."""
    if not route_dir.is_dir():
        raise Refused(NO_ROUTE)
    dirs = sorted((p for p in route_dir.iterdir() if p.is_dir() and p.name.isdigit()), key=_num)
    if not dirs:
        raise Refused(f"No segment folders in {route_dir}.")
    return dirs


def _num(p: Path) -> int:
    return int(p.name)


def comma2k19_sources(
    segments: Sequence[Path],
    cfg: Config,
    vehicle: Config,
    *,
    seed: int,
    coolant_c: CoolantProfile | None = None,
    dataset_root: Path | None = None,
) -> list[Source]:
    """The joined segments plus the synthetic OBD overlay built from their speed trace."""
    parts = [Comma2k19Source(p, dataset_root=dataset_root) for p in segments]
    chain = SegmentChain(
        parts,
        name=f"comma2k19:{parts[0].meta.dataset_ref}",
        dataset_ref=_route_ref(parts),
    )
    obd = SyntheticObdSource(chain, seed, cfg=cfg, vehicle=vehicle, coolant_c=coolant_c)
    return [chain, obd]


def _route_ref(parts: Sequence[Comma2k19Source]) -> str | None:
    refs = [p.meta.dataset_ref for p in parts]
    first, last = refs[0], refs[-1]
    if first is None or last is None or len(parts) == 1:
        return first
    return f"{first.rsplit('/', 1)[0]}/{first.rsplit('/', 1)[-1]}..{last.rsplit('/', 1)[-1]}"


def toyota_decoder() -> DbcDecoder:
    return DbcDecoder(load_dbc(COMMA2K19_TOYOTA_DBC), COMMA2K19_TOYOTA_MAPPING, bus=TOYOTA_BUS)


def lap_lines(drive: SyntheticDriveSource) -> list[LapLine]:
    sf = drive.start_finish
    if sf is None or drive.track_length_m is None:
        return []
    line = StartFinishLine(sf.line_id, sf.x1, sf.y1, sf.x2, sf.y2)
    return [LapLine(line, 0.0, LAP_MIN_DIST_FRACTION * drive.track_length_m)]


def coolant_fault(start_s: float) -> CoolantProfile:
    """A scripted coolant excursion (synthetic): 90 °C, a 10 s rise to 104 °C, 106 °C for
    20 s, then a 20 s fall from 101 °C back to 90 °C. The same shape as the track day's."""
    hot_end = start_s + COOLANT_HOLD_S

    def coolant(t_s: float) -> float:
        if start_s <= t_s < hot_end:
            return COOLANT_PEAK_C
        if start_s - FAULT_RAMP_S <= t_s < start_s:
            return NORMAL_COOLANT_C + 14.0 * (t_s - (start_s - FAULT_RAMP_S)) / FAULT_RAMP_S
        if hot_end <= t_s < hot_end + FAULT_COOL_S:
            return 101.0 - 11.0 * (t_s - hot_end) / FAULT_COOL_S
        return NORMAL_COOLANT_C

    return coolant


# -- drives ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Context:
    cfg: Config
    vehicle: Config
    profile: str
    data_dir: Path
    dataset_root: Path


MIXED_CARS = "Those segments come from more than 1 car. Replay 1 car at a time."


def dataset_vehicle(ctx: Context, segments: Sequence[Path]) -> tuple[str, Config]:
    """(profile name, profile) for a replay: the dataset car's own profile (RAV4 or Civic),
    else the configured one. Refuses segments from more than 1 car."""
    found = {segment_profile(seg) for seg in segments}
    if len(found) > 1:
        raise Refused(MIXED_CARS)
    profile = next(iter(found), None)
    if profile is None:
        return ctx.profile, ctx.vehicle
    return profile, load_vehicle(profile)


def replay_dataset(
    ctx: Context,
    segments: Sequence[Path],
    label: str,
    *,
    seed: int,
    coolant_c: CoolantProfile | None = None,
) -> RunReport:
    for seg in segments:
        if not seg.is_dir():
            raise Refused(f"No comma2k19 segment at {seg}.")
    profile, vehicle = dataset_vehicle(ctx, segments)
    sources = comma2k19_sources(
        segments,
        ctx.cfg,
        vehicle,
        seed=seed,
        coolant_c=coolant_c,
        dataset_root=ctx.dataset_root,
    )
    return run_to_data_dir(
        sources,
        ctx.cfg,
        vehicle,
        ctx.data_dir,
        vehicle_profile=profile,
        label=label,
        dbc_decoder=toyota_decoder(),
        dbc_name=COMMA2K19_TOYOTA_DBC,
    )


def replay_synthetic(ctx: Context, scenario: str, seed: int) -> RunReport:
    drive = synthetic_drive(seed, scenario, cfg=ctx.cfg, vehicle=ctx.vehicle)
    return run_to_data_dir(
        [drive],
        ctx.cfg,
        ctx.vehicle,
        ctx.data_dir,
        vehicle_profile=ctx.profile,
        label=LABEL_SYNTHETIC[scenario],
        lines=lap_lines(drive),
    )


def stage_table(stage_s: dict[str, float]) -> str:
    """``stage 1.234 s, stage 0.040 s`` in the order the run first timed each stage."""
    return ", ".join(f"{k} {v:.3f} s" for k, v in stage_s.items())


def print_report(rep: RunReport, out: Callable[[str], None] = print) -> None:
    out(f"{rep.label}: drive {rep.drive_id}")
    out(f"  Records in: {records_table(rep.records_in)}")
    out(f"  Records made: {records_table(rep.records_out)}")
    out(
        f"  Data {rep.data_span_s:.1f} s in {rep.wall_s:.2f} s of wall time: "
        f"{rep.realtime_factor:.1f}x real time, {rep.records_per_s:,.0f} records/s."
    )
    out(f"  Time by stage: {stage_table(rep.stage_s)}")
    out(f"  Errors: {rep.n_errors}.")
    for note in rep.notes:
        out(f"  {note}")
    for stat_id, display in rep.stats:
        out(f"  {stat_id}: {display}")


# -- seed-demo ---------------------------------------------------------------------------------


def _drive_source(path: Path) -> DriveSource:
    try:
        with DriveReader(path) as reader:
            return reader.meta().source
    except (sqlite3.Error, ValueError) as e:
        raise Refused(f"Cannot read {path.name}, so it stays. Use an empty folder.") from e


def _index_sources(index_path: Path) -> list[str]:
    uri = index_path.resolve().as_uri() + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True)
        try:
            return [r[0] for r in con.execute("SELECT source FROM drives_index")]
        finally:
            con.close()
    except sqlite3.Error as e:
        raise Refused(f"Cannot read {index_path.name}. Use an empty folder.") from e


LIVE_HELD = "This folder holds live drives. Seed the demo into another folder."


def check_demo_dir(root: Path) -> list[Path]:
    """The earlier demo drive files in ``root``. Refuses if it holds a live drive or anything
    in ``drives/`` that is not a Photoncar drive file."""
    index_path = root / "index.sqlite"
    if index_path.is_file() and "live" in _index_sources(index_path):
        raise Refused(LIVE_HELD)
    files: list[Path] = []
    drives = root / "drives"
    if drives.is_dir():
        for p in sorted(drives.iterdir()):
            if p.suffix == ".sqlite" and p.is_file():
                if _drive_source(p) == "live":
                    raise Refused(LIVE_HELD)
                files.append(p)
            elif not (p.name.endswith((".sqlite-wal", ".sqlite-shm")) and p.is_file()):
                raise Refused(f"{p} was not written by Photoncar. Use an empty folder.")
    return files


def clear_demo_dir(root: Path, files: Sequence[Path]) -> None:
    """Remove the checked demo drive files and their WAL files, and empty the index's drive
    list and baselines. The index keeps its model cache and spend ledger."""
    for p in files:
        for f in (p, p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
            f.unlink(missing_ok=True)
    index_path = root / "index.sqlite"
    if index_path.is_file():
        con = sqlite3.connect(index_path)
        try:
            con.execute("DELETE FROM drives_index")
            con.execute("DELETE FROM baselines")
            con.commit()
        finally:
            con.close()


def seed_demo(ctx: Context, out: Callable[[str], None] = print) -> list[RunReport]:
    """The demo drives: the public sample, the public route (when fetched) with a scripted
    synthetic coolant fault on its OBD overlay, and the synthetic track day."""
    earlier = check_demo_dir(ctx.data_dir)
    sample = ctx.dataset_root / SAMPLE_REF
    if not sample.is_dir():
        raise Refused(NO_SAMPLE)
    route = ctx.dataset_root / ROUTE_REF
    segments = segment_dirs(route) if route.is_dir() else None
    clear_demo_dir(ctx.data_dir, earlier)
    if earlier:
        out(f"Removed {len(earlier)} earlier demo drives.")
    reports = [replay_dataset(ctx, [sample], LABEL_SAMPLE, seed=DEMO_SEED)]
    if segments is not None:
        reports.append(
            replay_dataset(
                ctx,
                segments,
                LABEL_ROUTE,
                seed=DEMO_SEED,
                coolant_c=coolant_fault(FAULT_START_S),
            )
        )
    else:
        out(f"{NO_ROUTE} Seeding without it.")
    reports.append(replay_synthetic(ctx, "track_day", DEMO_SEED))
    return reports


# -- commands ------------------------------------------------------------------------------------


def _context(args: argparse.Namespace) -> Context:
    cfg = load_config()
    profile = str(cfg.get("vehicle.profile"))
    root = Path(args.data_dir).expanduser().resolve() if args.data_dir else data_dir()
    public = getattr(args, "public_dir", None)
    dataset = (
        Path(public).expanduser().resolve() / DATASET_FOLDER if public else default_dataset_root()
    )
    return Context(cfg, load_vehicle(profile), profile, root, dataset)


def cmd_replay(args: argparse.Namespace) -> int:
    ctx = _context(args)
    if args.synthetic is not None:
        rep = replay_synthetic(ctx, args.synthetic, args.seed)
    elif args.segment is not None:
        rep = replay_dataset(ctx, [Path(args.segment)], LABEL_SEGMENT, seed=args.seed)
    elif args.route is not None:
        route = Path(args.route) if args.route else ctx.dataset_root / ROUTE_REF
        rep = replay_dataset(ctx, segment_dirs(route), LABEL_ROUTE, seed=args.seed)
    else:
        sample = ctx.dataset_root / SAMPLE_REF
        if not sample.is_dir():
            raise Refused(NO_SAMPLE)
        rep = replay_dataset(ctx, [sample], LABEL_SAMPLE, seed=args.seed)
    print_report(rep)
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from photoncar.api.app import create_app
    from photoncar.api.guard import TOKEN_ENV

    ctx = _context(args)
    app = create_app(ctx.cfg, ctx.data_dir)
    if args.live:
        lock_live(app.state.live)
    print(SAFETY_LINE)
    if args.live:
        print("Live mode with no speed source here: parked views stay locked.")
    if not _token_set(TOKEN_ENV):
        print(f"No {TOKEN_ENV} set. The API answers only this device.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def lock_live(live: object) -> LiveState:
    """Live mode, locked: nothing here knows the speed yet, so it counts as moving."""
    if not isinstance(live, LiveState):
        raise TypeError("the app has no live state")
    live.set_mode("live")
    live.set_moving(True)
    return live


def _token_set(name: str) -> bool:
    return bool(os.environ.get(name, "").strip())


def cmd_seed_demo(args: argparse.Namespace) -> int:
    ctx = _context(args)
    t0 = time.perf_counter()
    reports = seed_demo(ctx)
    for rep in reports:
        print_report(rep)
    print(f"Seeded {len(reports)} drives in {time.perf_counter() - t0:.1f} s.")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    from photoncar.export.laps_v0 import export_drive_laps

    ctx = _context(args)
    if args.kind != "laps":
        raise Refused("Only lap exports exist. Use --kind laps.")
    path = ctx.data_dir / "drives" / f"{args.drive}.sqlite"
    if "/" in args.drive or not path.is_file():
        raise Refused("No drive with that ID.")
    with DriveReader(path) as reader:
        try:
            files = export_drive_laps(reader, ctx.cfg, base=ctx.data_dir)
        except ConfigError:
            raise Refused(PRIVACY_MISSING) from None
    if not files:
        print("No laps to export.")
    for f in files:
        print(f.resolve().relative_to(ctx.data_dir).as_posix())
    return 0


class StoppableSensor:
    """A live sensor source the recording can stop, and outlive.

    ``stop`` ends the stream at its next record (and calls the sensor's own ``stop`` when it
    has one), so Ctrl+C ends the whole drive. A sensor that fails mid-drive ends its own stream
    with a plain message; the CAN recording goes on.
    """

    def __init__(self, source: Source, say: Callable[[str], None] = print) -> None:
        self._source = source
        self._say = say
        self._stop = threading.Event()

    @property
    def meta(self) -> SourceMeta:
        return self._source.meta

    def records(self) -> Iterator[Record]:
        stream = self._source.records()
        try:
            for rec in stream:
                if self._stop.is_set():
                    return
                yield rec
        except Exception as e:  # a sensor fault must not end the CAN recording
            log.error("live sensor %s failed", self._source.meta.name, exc_info=e)
            self._say(SENSOR_STOPPED.format(name=self._source.meta.name, reason=_sentence(e)))
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()

    def stop(self) -> None:
        """End the stream at its next record. Safe from a signal handler."""
        self._stop.set()
        fn = getattr(self._source, "stop", None)
        if callable(fn):
            fn()

    def close(self) -> None:
        """Release the device, once the drive has ended."""
        fn = getattr(self._source, "close", None)
        if callable(fn):
            fn()


def _sentence(e: BaseException) -> str:
    text = str(e).strip() or type(e).__name__
    return text if text.endswith(".") else f"{text}."


def open_sensors(
    cfg: Config, clock: Clock, say: Callable[[str], None] = print
) -> list[StoppableSensor]:
    """The GPS and IMU sources, or none (said plainly) when they are not there."""
    from photoncar.sources import live_sensors

    try:
        sources = live_sensors.open_live_sensors(cfg, clock)
    except live_sensors.LiveSensorsUnavailable as e:
        say(NO_SENSORS.format(reason=_sentence(e)))
        return []
    return [StoppableSensor(s, say) for s in sources]


def cmd_record(args: argparse.Namespace) -> int:
    from photoncar.sources import obd

    ctx = _context(args)
    clock = MonotonicClock()
    try:
        session: obd.LiveCanSession = obd.open_live_can_session(ctx.cfg, clock)
    except (obd.ObdPollingOff, obd.LiveCanUnavailable) as e:
        raise Refused(f"Live recording cannot start here. {e}") from None
    print(SAFETY_LINE)
    try:
        sensors = open_sensors(ctx.cfg, clock)
    except BaseException:
        session.stop()
        raise
    sources: list[Source] = [session, *sensors]

    def stop_all() -> None:
        session.stop()
        for s in sensors:
            s.stop()

    served = None if args.no_serve else _serve_in_thread(ctx, args.host, args.port)
    live = LiveState() if served is None else served[1]
    stopper = _stop_on_signal(stop_all)
    try:
        print("Recording. Press Ctrl+C to stop.")
        rep = run_to_data_dir(
            sources,
            ctx.cfg,
            ctx.vehicle,
            ctx.data_dir,
            vehicle_profile=ctx.profile,
            label=LABEL_LIVE,
            live=live,
            live_clock=clock,
        )
    finally:
        stopper()
        stop_all()
        for s in sensors:
            s.close()
        if served is not None:
            served[0].should_exit = True
    print_report(rep)
    return 0


def _serve_in_thread(ctx: Context, host: str, port: int) -> tuple[uvicorn.Server, LiveState]:
    """The dashboard on a background thread. The pipeline publishes to the app's own live
    state, which the API guard reads for the moving lock."""
    from photoncar.api.app import create_app

    app = create_app(ctx.cfg, ctx.data_dir)
    live = lock_live(app.state.live)  # locked from the first request, before the recording
    server = uvicorn.Server(uvicorn.Config(app, host=host, port=port, log_level="warning"))
    threading.Thread(target=server.run, name="photoncar-serve", daemon=True).start()
    return server, live


def cmd_export_embed(args: argparse.Namespace) -> int:
    # The web build owns this module; importing it by name keeps the CLI independent of it.
    embed = importlib.import_module("photoncar.export.embed")
    ctx = _context(args)
    out = Path(args.out).expanduser().resolve()
    try:
        report = embed.export_embed(
            ctx.data_dir, args.drive, out, cfg=ctx.cfg, web_dist_embed=WEB_DIST_EMBED
        )
    except embed.EmbedRefused as e:
        raise Refused(str(e)) from None
    print(f"Embed written to {out}.")
    if dataclasses.is_dataclass(report) and not isinstance(report, type):
        for f in dataclasses.fields(report):  # the report's plain numbers and names
            value = getattr(report, f.name)
            if f.name != "out_dir" and isinstance(value, int | float | str):
                print(f"  {f.name}: {value}")
    return 0


def _stop_on_signal(stop: Callable[[], None]) -> Callable[[], None]:
    """Route Ctrl+C and SIGTERM to ``stop`` so the drive closes cleanly. Returns the undo."""

    def handler(signum: int, frame: FrameType | None) -> None:
        stop()

    saved = {s: signal.signal(s, handler) for s in (signal.SIGINT, signal.SIGTERM)}

    def undo() -> None:
        for s, h in saved.items():
            signal.signal(s, h)

    return undo


# -- parser ------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="photoncar",
        description=f"Read-only car telemetry. {SAFETY_LINE}",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def data_opt(sp: argparse.ArgumentParser, *, required: bool = False) -> None:
        sp.add_argument(
            "--data-dir",
            required=required,
            help="Data folder (default: PHOTONCAR_DATA_DIR or data/).",
        )

    def public_opt(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "--public-dir",
            help="Public dataset root (default: PHOTONCAR_PUBLIC_DIR or data/public).",
        )

    r = sub.add_parser("replay", help="Replay the public dataset or a synthetic drive.")
    what = r.add_mutually_exclusive_group(required=True)
    what.add_argument("--dataset", choices=["comma2k19"], help="Replay the public dataset.")
    what.add_argument("--synthetic", choices=list(SCENARIOS), help="Replay a synthetic drive.")
    where = r.add_mutually_exclusive_group()
    where.add_argument("--segment", help="1 comma2k19 segment folder.")
    where.add_argument(
        "--route",
        nargs="?",
        const="",
        help="A whole comma2k19 route folder (default: the fetched default route).",
    )
    r.add_argument("--seed", type=int, default=0, help="Seed for synthetic values.")
    data_opt(r)
    public_opt(r)
    r.set_defaults(func=cmd_replay)

    s = sub.add_parser("serve", help="Serve the dashboard and API.")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument(
        "--live",
        action="store_true",
        help="On the car while another process records: stay locked (no speed source here).",
    )
    data_opt(s)
    s.set_defaults(func=cmd_serve)

    d = sub.add_parser("seed-demo", help="Write the demo drives into an empty data folder.")
    data_opt(d, required=True)
    public_opt(d)
    d.set_defaults(func=cmd_seed_demo)

    e = sub.add_parser("export", help="Export laps in the lap v0 format.")
    e.add_argument("--drive", required=True, help="Drive ID.")
    e.add_argument("--kind", required=True, choices=["laps"])
    data_opt(e)
    e.set_defaults(func=cmd_export)

    x = sub.add_parser("export-embed", help="Export the static portfolio replay of 1 drive.")
    x.add_argument("--drive", required=True, help="Drive ID (a public-dataset drive).")
    x.add_argument("--out", required=True, help="Output folder.")
    data_opt(x)
    x.set_defaults(func=cmd_export_embed)

    c = sub.add_parser("record", help="Record a live drive from the car.")
    c.add_argument("--host", default="127.0.0.1")
    c.add_argument("--port", type=int, default=8000)
    c.add_argument("--no-serve", action="store_true", help="Record without the dashboard.")
    data_opt(c)
    c.set_defaults(func=cmd_record)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    where = (getattr(args, "segment", None), getattr(args, "route", None))
    if getattr(args, "synthetic", None) is not None and where != (None, None):
        parser.error("--segment and --route go with --dataset comma2k19.")
    func: Callable[[argparse.Namespace], int] = args.func
    try:
        return func(args)
    except Refused as e:
        print(str(e), file=sys.stderr)
        return REFUSED


if __name__ == "__main__":
    sys.exit(main())
