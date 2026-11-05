"""Bench ECU emulator: answers OBD-II service 01 requests like an engine ECU.

This runs on the bench computer (CANable #1 in normal mode), never on the car and never as
part of Photoncar. It answers 0x7DF and 0x7E0 requests on 0x7E8 with ``encode_pid`` values.
Unsupported PIDs get no reply, as ISO 15765-4 ECUs do.

``main`` refuses the car's interfaces (``[can] capture_interface`` and ``request_interface``
in the config) on every python-can interface, even with ``--bench``. Any interface other
than python-can's in-process ``virtual`` bus needs ``--bench``, which states that the
channel is the bench bus and not the car. Tests use the virtual bus.

    uv run python scripts/bench/ecu_emulator.py --bench --interface slcan \\
        --channel /dev/tty.usbmodem1
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path

import can

from photoncar.config import Config, load_config
from photoncar.decode.obd_pids import bitmaps_for, encode_pid
from photoncar.units import c_to_k

FUNCTIONAL_ID = 0x7DF
REPLY_PAD = 0x55
VIRTUAL_INTERFACE = "virtual"
"""python-can's in-process bus: it reaches no wire, so it needs no ``--bench``."""

DEFAULT_VALUES: dict[int, float] = {
    0x03: 2.0,  # closed loop
    0x04: 0.25,
    0x05: c_to_k(90.0),
    0x06: 0.0,
    0x07: 0.03125,
    0x08: 0.0,
    0x09: -0.03125,
    0x0C: 850.0,
    0x0D: 0.0,
    0x0F: c_to_k(25.0),
    0x11: 0.15,
    0x42: 14.1,
}
"""Synthetic idle values. The S65 has no MAF sensor, so 0x10 is left unsupported."""


class EcuEmulator:
    """Pure request -> reply logic plus two loops: ``pump`` (tests) and ``serve`` (bench)."""

    def __init__(
        self,
        values: Mapping[int, float],
        *,
        nrc: Mapping[int, int] | None = None,
        silent: Iterable[int] = (),
        physical_id: int = 0x7E0,
        record: bool = True,
    ) -> None:
        self.values = dict(values)
        self.nrc = dict(nrc or {})
        self.silent = frozenset(silent)
        self.physical_id = physical_id
        self.reply_id = physical_id + 8
        self.bitmaps = bitmaps_for(frozenset(self.values) | frozenset(self.nrc) | self.silent)
        self.record = record
        self.received: list[can.Message] = []
        self.replies = 0

    def respond(self, msg: can.Message) -> can.Message | None:
        if self.record:
            self.received.append(msg)
        if msg.is_extended_id or msg.is_remote_frame or msg.is_error_frame or msg.is_fd:
            return None
        if msg.arbitration_id not in (FUNCTIONAL_ID, self.physical_id):
            return None
        data = bytes(msg.data)
        if len(data) < 3 or data[0] != 0x02 or data[1] != 0x01:
            return None
        pid = data[2]
        if pid in self.bitmaps:
            return self._frame(bytes([0x41, pid]) + self.bitmaps[pid])
        if pid in self.silent:
            return None
        if pid in self.nrc:
            return self._frame(bytes([0x7F, 0x01, self.nrc[pid]]))
        if pid in self.values:
            return self._frame(bytes([0x41, pid]) + encode_pid(pid, self.values[pid]))
        return None

    def _frame(self, payload: bytes) -> can.Message:
        data = bytes([len(payload)]) + payload
        data += bytes([REPLY_PAD] * (8 - len(data)))
        return can.Message(arbitration_id=self.reply_id, is_extended_id=False, data=data, dlc=8)

    def pump(self, bus: can.BusABC) -> int:
        """Answer every frame waiting on ``bus`` without blocking. Returns replies sent."""
        n = 0
        while (msg := bus.recv(timeout=0.0)) is not None:
            reply = self.respond(msg)
            if reply is not None:
                bus.send(reply)
                n += 1
        self.replies += n
        return n

    def serve(self, bus: can.BusABC, stop: threading.Event) -> None:
        """Answer requests until ``stop`` is set."""
        while not stop.is_set():
            msg = bus.recv(timeout=0.1)
            if msg is None:
                continue
            reply = self.respond(msg)
            if reply is not None:
                bus.send(reply)
                self.replies += 1


class EmulatorRefused(ValueError):
    """The emulator will not open this bus. The message says why in plain words."""


def car_interfaces(cfg: Config) -> frozenset[str]:
    """The car's CAN interfaces from ``[can]``: capture (listen-only) and request."""
    section = cfg.section("can")
    return frozenset({str(section["capture_interface"]), str(section["request_interface"])})


def check_target(interface: str, channel: str, car: frozenset[str], *, bench: bool) -> None:
    """Raise EmulatorRefused unless the emulator may open ``channel`` on ``interface``."""
    name = channel.strip()
    if not name:
        raise EmulatorRefused("Name the bench channel with --channel.")
    if name in car:
        raise EmulatorRefused(
            f"{name} is a car interface in the config. The emulator never runs on the car."
        )
    if interface != VIRTUAL_INTERFACE and not bench:
        raise EmulatorRefused(
            f"{name} on {interface or 'the default interface'} may be a real bus. "
            "Add --bench to confirm it is the bench bus, not the car."
        )


def open_bus(interface: str, channel: str, bitrate: int) -> can.BusABC:
    return can.Bus(interface=interface, channel=channel, bitrate=bitrate)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bench ECU emulator for OBD-II service 01.")
    parser.add_argument("--interface", default="socketcan", help="python-can interface")
    parser.add_argument("--channel", required=True, help="python-can channel")
    parser.add_argument("--bitrate", type=int, default=500000)
    parser.add_argument(
        "--bench",
        action="store_true",
        help="confirm the channel is the bench bus, never the car (needed off the virtual bus)",
    )
    parser.add_argument(
        "--config", type=Path, default=None, help="photoncar.toml that names the car interfaces"
    )
    parser.add_argument(
        "--drop",
        type=lambda s: int(s, 0),
        action="append",
        default=[],
        help="PID to leave unsupported (repeatable), for example 0x42",
    )
    args = parser.parse_args(argv)
    try:
        check_target(
            args.interface, args.channel, car_interfaces(load_config(args.config)), bench=args.bench
        )
    except EmulatorRefused as exc:
        print(f"Refused. {exc}", file=sys.stderr)
        return 2
    values = {k: v for k, v in DEFAULT_VALUES.items() if k not in args.drop}
    emulator = EcuEmulator(values, record=False)
    bus = open_bus(args.interface, args.channel.strip(), args.bitrate)
    stop = threading.Event()
    print(f"Emulator answering on 0x{emulator.reply_id:03X}. Press Ctrl+C to stop.")
    try:
        emulator.serve(bus, stop)
    except KeyboardInterrupt:
        pass
    finally:
        bus.shutdown()
    print(f"Replies sent: {emulator.replies}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
