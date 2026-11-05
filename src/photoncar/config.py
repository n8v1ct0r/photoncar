"""Configuration loading: config/photoncar.toml, the vehicle profile, and the local privacy file."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Config:
    raw: Mapping[str, Any]

    def section(self, dotted: str) -> Mapping[str, Any]:
        node: Any = self.raw
        for part in dotted.split("."):
            if not isinstance(node, Mapping) or part not in node:
                raise ConfigError(f"missing config section [{dotted}]")
            node = node[part]
        if not isinstance(node, Mapping):
            raise ConfigError(f"[{dotted}] is not a table")
        return node

    def get(self, dotted: str) -> Any:
        section, _, key = dotted.rpartition(".")
        table = self.section(section) if section else self.raw
        if key not in table:
            raise ConfigError(f"missing config key {dotted}")
        return table[key]


@dataclass(frozen=True, slots=True)
class HomeZone:
    """The privacy zone. The center comes only from the gitignored local file."""

    lat_deg: float
    lon_deg: float
    radius_m: float


def load_config(path: Path | None = None) -> Config:
    p = path or CONFIG_DIR / "photoncar.toml"
    with p.open("rb") as f:
        return Config(tomllib.load(f))


def load_vehicle(profile: str, config_dir: Path | None = None) -> Config:
    p = (config_dir or CONFIG_DIR) / "vehicles" / f"{profile}.toml"
    with p.open("rb") as f:
        return Config(tomllib.load(f))


def data_dir() -> Path:
    env = os.environ.get("PHOTONCAR_DATA_DIR")
    return Path(env).expanduser().resolve() if env else REPO_ROOT / "data"


def load_home_zone(cfg: Config, path: Path | None = None) -> HomeZone:
    """Load the privacy zone. Raises ConfigError if the local file is missing or invalid.

    Export and capture must call this and refuse to run on error (hard rule 7).
    """
    p = path or CONFIG_DIR / "privacy.local.toml"
    if not p.exists():
        raise ConfigError(f"privacy file missing: {p}. Copy config/privacy.example.toml.")
    with p.open("rb") as f:
        doc = tomllib.load(f)
    home = doc.get("home")
    if not isinstance(home, Mapping):
        raise ConfigError("privacy file needs a [home] table")
    lat, lon = home.get("lat_deg"), home.get("lon_deg")
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        raise ConfigError("[home] lat_deg and lon_deg must be numbers")
    if not (-90.0 <= float(lat) <= 90.0 and -180.0 <= float(lon) <= 180.0):
        raise ConfigError("[home] coordinates out of range")
    radius_km = cfg.get("privacy.radius_km")
    if not isinstance(radius_km, int | float) or radius_km <= 0:
        raise ConfigError("privacy.radius_km must be a positive number")
    return HomeZone(float(lat), float(lon), float(radius_km) * 1000.0)
