"""Simple text slate input."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Slip:
    name: str
    stake: float
    legs: tuple[str, ...]


def read_slips(path: Path) -> list[Slip]:
    slips = []
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = re.fullmatch(r"slip\s+(.+?)\s+(\d+(?:\.\d{1,2})?)\s*:\s*(.+)", line, re.I)
        if not match:
            raise ValueError(f"Line {number}: expected slip NAME STAKE: Leg, Leg")
        legs = tuple(leg.strip() for leg in match.group(3).split(","))
        if any(not leg for leg in legs) or len(set(leg.casefold() for leg in legs)) != len(legs):
            raise ValueError(f"Line {number}: legs must be named and unique within a slip")
        stake = float(match.group(2))
        if stake <= 0:
            raise ValueError(f"Line {number}: stake must be positive")
        slips.append(Slip(match.group(1), stake, legs))
    if not slips:
        raise ValueError("The slate file has no slips")
    if len({slip.name.casefold() for slip in slips}) != len(slips):
        raise ValueError("Slip names must be unique")
    return slips
