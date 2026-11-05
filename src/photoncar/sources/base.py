"""The source contract. Live hardware, the public dataset, and synthetic drives all look alike."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Literal, Protocol

from photoncar.records import Record


@dataclass(frozen=True, slots=True)
class SourceMeta:
    name: str
    kind: Literal["live", "replay", "synthetic"]
    dataset_ref: str | None = None
    synthetic_signals: tuple[str, ...] = ()


class Source(Protocol):
    """A time-ordered stream of records. ``records()`` yields in non-decreasing record time."""

    @property
    def meta(self) -> SourceMeta: ...

    def records(self) -> Iterator[Record]: ...
