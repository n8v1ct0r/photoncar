"""The signal catalog: the single source of names, units, rates, and citation labels.

Rules, the store, the API, and the UI all refer to signals by these names. A citation's
``source`` string is ``SIGNALS[name].label`` (for example "OBD 0x05").
"""

from __future__ import annotations

from dataclasses import dataclass

from photoncar.records import SourceKind


@dataclass(frozen=True, slots=True)
class SignalDef:
    name: str
    unit: str
    source: SourceKind
    nominal_hz: float | None
    label: str
    title: str
    pid: int | None = None


def _obd(name: str, pid: int, unit: str, title: str, hz: float | None) -> SignalDef:
    return SignalDef(name, unit, "obd", hz, f"OBD 0x{pid:02X}", title, pid)


SIGNALS: dict[str, SignalDef] = {
    s.name: s
    for s in (
        _obd("obd.fuel_system", 0x03, "enum", "Fuel system status", None),
        _obd("obd.load", 0x04, "ratio", "Engine load", None),
        _obd("obd.coolant_temp", 0x05, "K", "Coolant temperature", None),
        _obd("obd.stft_b1", 0x06, "ratio", "Short-term fuel trim, bank 1", None),
        _obd("obd.ltft_b1", 0x07, "ratio", "Long-term fuel trim, bank 1", None),
        _obd("obd.stft_b2", 0x08, "ratio", "Short-term fuel trim, bank 2", None),
        _obd("obd.ltft_b2", 0x09, "ratio", "Long-term fuel trim, bank 2", None),
        _obd("obd.rpm", 0x0C, "rpm", "Engine speed", 2.0),
        _obd("obd.speed", 0x0D, "m/s", "Vehicle speed", 2.0),
        _obd("obd.iat", 0x0F, "K", "Intake air temperature", None),
        _obd("obd.maf", 0x10, "kg/s", "Mass air flow", None),
        _obd("obd.throttle", 0x11, "ratio", "Throttle position", 2.0),
        _obd("obd.module_voltage", 0x42, "V", "Module voltage", None),
        SignalDef("gps.speed", "m/s", "gps", 10.0, "GPS", "GPS speed"),
        SignalDef("imu.accel_x", "m/s^2", "imu", 104.0, "IMU", "Accel x (sensor)"),
        SignalDef("imu.gyro_z", "rad/s", "imu", 104.0, "IMU", "Gyro z (sensor)"),
        SignalDef("state.speed", "m/s", "derived", 104.0, "Estimator", "Speed"),
        SignalDef("state.accel_long", "m/s^2", "derived", 104.0, "Estimator", "Longitudinal accel"),
        SignalDef("state.accel_lat", "m/s^2", "derived", 104.0, "Estimator", "Lateral accel (v·r)"),
        SignalDef("state.yaw_rate", "rad/s", "derived", 104.0, "Estimator", "Yaw rate"),
        SignalDef(
            "behavior.jerk_long", "m/s^3", "derived", 104.0, "Estimator", "Longitudinal jerk"
        ),
        SignalDef("behavior.jerk_lat", "m/s^3", "derived", 104.0, "Estimator", "Lateral jerk"),
        SignalDef("can.wheel_speed", "m/s", "can", None, "CAN", "Wheel speed (mean)"),
        SignalDef("can.engine_rpm", "rpm", "can", None, "CAN", "Engine speed (CAN)"),
        SignalDef("can.steer_angle", "rad", "can", None, "CAN", "Steering angle"),
        SignalDef("can.coolant_temp", "K", "can", None, "CAN", "Coolant temperature (CAN)"),
    )
}

OBD_PID_TO_SIGNAL: dict[int, str] = {s.pid: s.name for s in SIGNALS.values() if s.pid is not None}


def label(name: str) -> str:
    """Citation source label for a signal, e.g. "OBD 0x05". Unknown names pass through."""
    sig = SIGNALS.get(name)
    return sig.label if sig else name
