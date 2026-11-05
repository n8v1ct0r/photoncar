"""DBC decoding with cantools: vendored opendbc files, mapped to catalog signal names.

A mapping entry is either ``(message, signal)`` or a ``MappedSignal`` that combines several
signals of one message (the mean of 4 wheel speeds, or a coarse angle plus its fraction).
Values are converted from the DBC unit to SI; an unknown unit is refused at construction,
so nothing is silently mis-scaled.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import cantools
from cantools.database.can import Database
from cantools.database.can import Message as DbcMessage
from cantools.database.errors import DecodeError

from photoncar.records import CanFrame, Sample
from photoncar.signals import SIGNALS
from photoncar.units import KELVIN_OFFSET, KPH

VENDOR_DIR = Path(__file__).resolve().parent / "vendor" / "opendbc"

STRICT_LOAD: dict[str, bool] = {
    "bmw_e9x_e8x.dbc": True,
    # Overlapping signals in PCS_HUD fail strict loading; see vendor/opendbc/SOURCE.
    "toyota_new_mc_pt_generated.dbc": False,
}

Combine = Literal["one", "mean", "sum"]

_DEG = math.pi / 180.0
UNIT_TO_SI: dict[str, tuple[float, float, str]] = {
    # DBC unit: (scale, offset, SI unit), si = value * scale + offset
    "km/h": (KPH, 0.0, "m/s"),
    "kph": (KPH, 0.0, "m/s"),
    "m/s": (1.0, 0.0, "m/s"),
    "deg": (_DEG, 0.0, "rad"),
    "degree": (_DEG, 0.0, "rad"),
    "C": (1.0, KELVIN_OFFSET, "K"),
    "degC": (1.0, KELVIN_OFFSET, "K"),
    "rpm": (1.0, 0.0, "rpm"),
    "1/min": (1.0, 0.0, "rpm"),
}


@dataclass(frozen=True, slots=True)
class MappedSignal:
    """Signals of one message combined into one catalog value."""

    message: str
    signals: tuple[str, ...]
    combine: Combine = "one"


MappingSpec = tuple[str, str] | MappedSignal

COMMA2K19_TOYOTA_DBC = "toyota_new_mc_pt_generated.dbc"
COMMA2K19_TOYOTA_MAPPING: dict[str, MappingSpec] = {
    "can.engine_rpm": ("ENGINE_RPM", "RPM"),
    "can.wheel_speed": MappedSignal(
        "WHEEL_SPEEDS",
        ("WHEEL_SPEED_FL", "WHEEL_SPEED_FR", "WHEEL_SPEED_RL", "WHEEL_SPEED_RR"),
        "mean",
    ),
    # openpilot reads the Toyota angle as STEER_ANGLE + STEER_FRACTION; positive is left.
    "can.steer_angle": MappedSignal("STEER_ANGLE_SENSOR", ("STEER_ANGLE", "STEER_FRACTION"), "sum"),
}

E90_PTCAN_DBC = "bmw_e9x_e8x.dbc"
E90_PTCAN_MAPPING: dict[str, MappingSpec] = {
    # 0xAA: raw / 4 rpm. Not clamped to the DBC's 8000 maximum.
    "can.engine_rpm": ("AccPedal", "EngineSpeed"),
    # 0x1D0: raw - 48 degC.
    "can.coolant_temp": ("EngineData", "TEMP_ENG"),
    # 0xCE: 0.0625 km/h per bit.
    "can.wheel_speed": MappedSignal(
        "WheelSpeeds", ("Wheel_FL", "Wheel_FR", "Wheel_RL", "Wheel_RR"), "mean"
    ),
    # 0xC4.
    "can.steer_angle": ("SteeringWheelAngle", "SteeringPosition"),
}
"""For a listen-only PT-CAN tap (not in the MVP: the OBD port is gatewayed D-CAN)."""


def load_dbc(name: str) -> Database:
    """Load a vendored opendbc file by name, with the strictness recorded in SOURCE."""
    if name not in STRICT_LOAD:
        raise ValueError(f"not a vendored DBC: {name}")
    db = cantools.database.load_file(VENDOR_DIR / name, strict=STRICT_LOAD[name])
    if not isinstance(db, Database):
        raise TypeError(f"{name} is not a CAN database")
    return db


@dataclass(frozen=True, slots=True)
class _Plan:
    name: str
    signals: tuple[str, ...]
    combine: Combine
    scale: float
    offset: float


class DbcDecoder:
    """Turns CanFrames into catalog Samples for the mapped messages. Other frames yield none."""

    def __init__(
        self, db: Database, mapping: Mapping[str, MappingSpec], *, bus: int | None = None
    ) -> None:
        self._bus = bus
        self._by_id: dict[tuple[int, bool], tuple[DbcMessage, list[_Plan]]] = {}
        self.decode_errors = 0
        for name, spec in mapping.items():
            ms = spec if isinstance(spec, MappedSignal) else MappedSignal(spec[0], (spec[1],))
            if name not in SIGNALS:
                raise ValueError(f"{name} is not in the signal catalog")
            if not ms.signals or (ms.combine == "one" and len(ms.signals) != 1):
                raise ValueError(f"{name}: 'one' needs exactly 1 signal")
            msg = db.get_message_by_name(ms.message)
            units = {msg.get_signal_by_name(s).unit or "" for s in ms.signals}
            if len(units) != 1:
                raise ValueError(f"{name}: signals disagree on units {sorted(units)}")
            unit = units.pop()
            if unit not in UNIT_TO_SI:
                raise ValueError(f"{name}: no SI conversion for DBC unit {unit!r}")
            scale, offset, si_unit = UNIT_TO_SI[unit]
            if SIGNALS[name].unit != si_unit:
                raise ValueError(f"{name}: catalog unit {SIGNALS[name].unit}, DBC gives {si_unit}")
            if ms.combine == "sum" and offset != 0.0:
                raise ValueError(f"{name}: cannot sum values with an offset unit")
            key = (msg.frame_id, bool(msg.is_extended_frame))
            plans = self._by_id.setdefault(key, (msg, []))[1]
            plans.append(_Plan(name, ms.signals, ms.combine, scale, offset))

    @property
    def frame_ids(self) -> frozenset[int]:
        return frozenset(k[0] for k in self._by_id)

    def decode(self, frame: CanFrame) -> list[Sample]:
        if self._bus is not None and frame.bus != self._bus:
            return []
        entry = self._by_id.get((frame.arb_id, frame.is_ext))
        if entry is None:
            return []
        msg, plans = entry
        try:
            values = msg.decode(frame.data, decode_choices=False, scaling=True)
        except (DecodeError, ValueError):
            self.decode_errors += 1
            return []
        if not isinstance(values, dict):
            self.decode_errors += 1
            return []
        out: list[Sample] = []
        for p in plans:
            nums: list[float] = []
            for s in p.signals:
                v = values.get(s)
                if not isinstance(v, int | float):
                    self.decode_errors += 1
                    break
                nums.append(float(v))
            else:
                if p.combine == "mean":
                    combined = sum(nums) / len(nums)
                elif p.combine == "sum":
                    combined = sum(nums)
                else:
                    combined = nums[0]
                out.append(Sample(frame.t_ns, p.name, combined * p.scale + p.offset))
        return out
