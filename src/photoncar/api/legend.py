"""The legend (``GET /api/legend``): every alert rule with its condition, built from config.

The legend page shows these strings as they are, so it never hard-codes a threshold. Every
number in them is a config value; changing ``config/photoncar.toml`` changes the legend.
"""

from __future__ import annotations

from typing import Any

from photoncar.config import Config
from photoncar.detect import baseline, rules
from photoncar.units import duration_text

BATTERY = {"agm": "AGM", "flooded": "flooded"}
SYNTHETIC_NOTE = (
    "Synthetic marks a value no car measured. The public dataset has no OBD-II data, so its "
    "OBD values, and any fault on them, are generated."
)


def _n(x: Any) -> str:
    """A config number as written: 105.0 reads 105, 12.5 reads 12.5."""
    return f"{float(x):g}"


def _s(x: Any) -> str:
    """A config duration in seconds: "10 s", or from 60 whole seconds as the web shows
    durations ("1 min 0 s")."""
    v = float(x)
    return duration_text(v) if v >= 60.0 and v.is_integer() else f"{_n(v)} s"


def _rule(
    rule_id: str, title: str, condition: str, severity: int, *, advisory: bool = False
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "title": title,
        "condition": condition,
        "severity": severity,
        "advisory": advisory,
    }


def legend(cfg: Config) -> dict[str, Any]:
    r = cfg.section("rules")
    ch, tw, tc = r["coolant_high"], r["trim_warn"], r["trim_critical"]
    vr, vz, sh = r["voltage_low_running"], r["voltage_low_rested"], r["shared"]
    b = cfg.section("baseline")
    battery = str(vz["battery_type"])
    rest_v = float(vz["threshold_agm_v"] if battery == "agm" else vz["threshold_flooded_v"])
    sev = rules.SEVERITY
    return {
        "rules": [
            _rule(
                "coolant_high",
                "Coolant high",
                f"Coolant over {_n(ch['threshold_c'])} °C for {_s(ch['on_s'])}. It clears "
                f"after {_s(ch['clear_s'])} under {_n(ch['clear_below_c'])} °C.",
                sev["coolant_high"],
            ),
            _rule(
                "trim_warn",
                "Fuel trim",
                f"A bank's short term plus long term fuel trim outside ±{_n(tw['limit_pct'])}% "
                f"for {_s(tw['on_s'])}. Judged only in closed loop, with coolant at "
                f"{_n(tw['min_coolant_c'])} °C or more.",
                sev["trim_warn"],
            ),
            _rule(
                "trim_critical",
                "Fuel trim critical",
                f"Either fuel trim of a bank outside ±{_n(tc['limit_pct'])}% for "
                f"{_s(tc['on_s'])}, judged the same way.",
                sev["trim_critical"],
            ),
            _rule(
                "voltage_low_running",
                "Module voltage low",
                f"Module voltage (OBD 0x42) under {_n(vr['threshold_v'])} V for "
                f"{_s(vr['on_s'])} with the engine running. The first "
                f"{_s(vr['ignore_after_start_s'])} after a start do not count.",
                sev["voltage_low_running"],
            ),
            _rule(
                "voltage_low_rested",
                "Rested module voltage low",
                f"Module voltage at key-on under {rest_v:.2f} V ({BATTERY[battery]} battery) "
                f"after the car rested {_n(vz['min_rest_h'])} h or more. OBD 0x42 reads "
                "under load, so it reads low.",
                sev["voltage_low_rested"],
                advisory=bool(vz["advisory"]),
            ),
            _rule(
                "stale",
                "Signal stale",
                f"An OBD value with no update for more than {int(sh['stale_after_polls'])} "
                "poll periods. Missing data never counts as normal.",
                sev["stale"],
            ),
            _rule(
                "baseline",
                "Off baseline",
                f"A value more than {_n(b['z_flag'])} sigma from this car's own baseline for "
                f"{_s(b['sustain_s'])}. Each operating state arms after "
                f"{int(b['arm_drives'])} drives and {int(b['arm_windows'])} windows of "
                f"{_s(b['window_s'])}.",
                baseline.SEVERITY,
            ),
        ],
        "privacy_radius_km": float(cfg.get("privacy.radius_km")),
        "max_request_hz": float(cfg.get("obd.max_request_hz")),
        "synthetic_note": SYNTHETIC_NOTE,
    }
