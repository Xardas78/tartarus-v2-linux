"""
tartarus_macros.py
Shared macro data model + storage for the Tartarus tooling. A macro is a
sequence of (key code, press/release, delay before this step) triplets, bound
to one of the 30 macro slots the driver already understands (CTRL_MACRO's
data byte 1-30 -> KEY_MACRO1..KEY_MACRO30, see resolve_event_kbd() in
tartarus.c). Slots are global (not per-profile) - any profile's key can point
at macro slot N and gets the same recorded content.

Used by both tartarus_gui.py (recording/editing - see tartarus_macro_gui.py)
and tartarus_macro_daemon.py (playback), so both always agree on the file
format without importing GUI code into the daemon or vice versa.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

MACRO_SLOTS = 30
MACROS_PATH = Path.home() / ".config" / "tartarus" / "macros.json"


@dataclass
class MacroStep:
    code: int          # Linux keycode (KEY_* from input-event-codes.h)
    press: bool         # True = key down, False = key up
    delay_ms: int = 0   # wait this long before performing this step

    def to_dict(self) -> dict:
        return {"code": self.code, "press": self.press, "delay_ms": self.delay_ms}

    @classmethod
    def from_dict(cls, d: dict) -> "MacroStep":
        return cls(code=int(d["code"]), press=bool(d["press"]), delay_ms=int(d.get("delay_ms", 0)))


@dataclass
class Macro:
    name: str = ""
    steps: list[MacroStep] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"name": self.name, "steps": [s.to_dict() for s in self.steps]}

    @classmethod
    def from_dict(cls, d: dict) -> "Macro":
        return cls(name=d.get("name", ""), steps=[MacroStep.from_dict(s) for s in d.get("steps", [])])


def load_macros() -> dict[int, Macro]:
    """Returns {slot_number: Macro} for every slot that has one recorded.
    Missing/unreadable file -> no macros yet, not an error."""
    if not MACROS_PATH.exists():
        return {}
    try:
        raw = json.loads(MACROS_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    macros = {}
    for slot_str, macro_dict in raw.items():
        try:
            macros[int(slot_str)] = Macro.from_dict(macro_dict)
        except (ValueError, KeyError, TypeError):
            continue
    return macros


def save_macros(macros: dict[int, Macro]) -> None:
    MACROS_PATH.parent.mkdir(parents=True, exist_ok=True)
    data = {str(slot): macro.to_dict() for slot, macro in macros.items()}
    MACROS_PATH.write_text(json.dumps(data, indent=2))


def save_macro(slot: int, macro: Macro) -> None:
    """Read-modify-write a single slot, so callers don't need the whole set."""
    macros = load_macros()
    macros[slot] = macro
    save_macros(macros)


def delete_macro(slot: int) -> None:
    macros = load_macros()
    if slot in macros:
        del macros[slot]
        save_macros(macros)
