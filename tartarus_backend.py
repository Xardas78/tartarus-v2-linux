"""
tartarus_backend.py
Backend library for the hid-tartarus kernel driver (Drayux/Tartarus) controlling a
Razer Tartarus V2. Wraps the sysfs interface (profile / profile_num / profile_count)
and the raw 512-byte profile format into a clean, GUI-agnostic Python API.

Reference (from tartarus.c / module.h):
  - A profile is 256 entries x 2 bytes (bind type + data) = 512 bytes.
  - profile_show()/profile_store() always operate on the CURRENTLY ACTIVE profile,
    so reading/writing a different profile requires switching profile_num first
    (this briefly makes that profile active on the device - LEDs will change too).
  - Bind types SCRIPT, SWKEY, MOUSE_MOVE and MOUSE_WHEEL are accepted/stored by the
    driver but not yet acted upon in resolve_event_kbd() - treat them as reserved.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

DRIVER_ROOT = Path("/sys/bus/hid/drivers/hid-tartarus")
PROFILE_SIZE = 512   # bytes
KEYMAP_LEN = 256      # bind entries per profile


class BindType(IntEnum):
    NOP = 0x00
    KEY = 0x01
    HYPERSHIFT = 0x02
    PROFILE = 0x03
    MACRO = 0x04
    SCRIPT = 0x05       # reserved - not yet handled by the kernel driver
    SWKEY = 0x06         # reserved - not yet handled by the kernel driver
    MOUSE_MOVE = 0x07    # reserved - not yet handled by the kernel driver
    MOUSE_WHEEL = 0x08   # reserved - not yet handled by the kernel driver
    DEBUG = 0xFF


# Bind types resolve_event_kbd() in tartarus.c currently implements.
IMPLEMENTED_BIND_TYPES = {
    BindType.NOP, BindType.KEY, BindType.HYPERSHIFT,
    BindType.PROFILE, BindType.MACRO, BindType.DEBUG,
}

# Physical key codes in on-device order (5 columns x 5 rows incl. thumb cluster),
# taken from the RZKEY_* defines / debug overlay diagram in keymap.h.
PHYSICAL_KEYS = [
    0x1E, 0x1F, 0x20, 0x21, 0x22,   # row 1
    0x2B, 0x14, 0x1A, 0x08, 0x15,   # row 2
    0x39, 0x04, 0x16, 0x07, 0x09,   # row 3
    0x42, 0x1D, 0x1B, 0x06, 0x2C,   # row 4 (0x42 = Shift, modkey-masked)
    0x44, 0x50, 0x52, 0x4F, 0x51,   # circle + thumbstick (0x44 = Alt, modkey-masked)
]

KEY_LABELS = [
    "01", "02", "03", "04", "05",
    "06", "07", "08", "09", "10",
    "11", "12", "13", "14", "15",
    "16", "17", "18", "19", "20",
    "Circle", "Thumb U", "Thumb R", "Thumb D", "Thumb L",
]

# Linux keycodes the device can usefully emit (subset of input-event-codes.h)
KEY_NAME_TO_CODE = {
    "ESC": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9, "9": 10, "0": 11,
    "MINUS": 12, "EQUAL": 13, "BACKSPACE": 14, "TAB": 15,
    "Q": 16, "W": 17, "E": 18, "R": 19, "T": 20, "Y": 21, "U": 22, "I": 23, "O": 24, "P": 25,
    "LEFTBRACE": 26, "RIGHTBRACE": 27, "ENTER": 28, "LEFTCTRL": 29,
    "A": 30, "S": 31, "D": 32, "F": 33, "G": 34, "H": 35, "J": 36, "K": 37, "L": 38,
    "SEMICOLON": 39, "APOSTROPHE": 40, "GRAVE": 41, "LEFTSHIFT": 42, "BACKSLASH": 43,
    "Z": 44, "X": 45, "C": 46, "V": 47, "B": 48, "N": 49, "M": 50,
    "COMMA": 51, "DOT": 52, "SLASH": 53, "RIGHTSHIFT": 54, "KPASTERISK": 55,
    "LEFTALT": 56, "SPACE": 57, "CAPSLOCK": 58,
    "F1": 59, "F2": 60, "F3": 61, "F4": 62, "F5": 63, "F6": 64, "F7": 65, "F8": 66, "F9": 67, "F10": 68,
    "NUMLOCK": 69, "SCROLLLOCK": 70,
    "KP7": 71, "KP8": 72, "KP9": 73, "KPMINUS": 74, "KP4": 75, "KP5": 76, "KP6": 77,
    "KPPLUS": 78, "KP1": 79, "KP2": 80, "KP3": 81, "KP0": 82, "KPDOT": 83,
    "F11": 87, "F12": 88,
    "KPENTER": 96, "RIGHTCTRL": 97, "KPSLASH": 98,
    "RIGHTALT": 100,
    "HOME": 102, "UP": 103, "PAGEUP": 104, "LEFT": 105, "RIGHT": 106,
    "END": 107, "DOWN": 108, "PAGEDOWN": 109, "INSERT": 110, "DELETE": 111,
    "MUTE": 113, "VOLUMEDOWN": 114, "VOLUMEUP": 115,
    "LEFTMETA": 125, "RIGHTMETA": 126, "MENU": 127,
}
KEY_CODE_TO_NAME = {v: k for k, v in KEY_NAME_TO_CODE.items()}

# KEY_MACRO1 (0x290) .. KEY_MACRO30 (0x2AD) - data byte 1-30 maps to this offset in
# resolve_event_kbd(): input_report_key(data->input, action.data + 0x28F, ev->state)
MACRO_NAMES = [f"M{i}" for i in range(1, 31)]


def find_keyboard_interface() -> Path:
    """Locate the sysfs directory of the KBD interface (inum 0) of the connected device."""
    if not DRIVER_ROOT.exists():
        raise FileNotFoundError(
            "hid-tartarus driver not found under /sys/bus/hid/drivers/. "
            "Is the kernel module loaded (lsmod | grep tartarus)?"
        )
    for entry in DRIVER_ROOT.iterdir():
        if not entry.is_symlink():
            continue
        intf_file = entry / "intf_type"
        if not intf_file.exists():
            continue
        try:
            if intf_file.read_text().strip() == "KBD":
                return entry
        except OSError:
            continue
    raise FileNotFoundError("No KBD interface found. Is the Tartarus V2 plugged in?")


@dataclass
class Bind:
    type: BindType = BindType.NOP
    data: int = 0

    def to_bytes(self) -> bytes:
        return bytes([int(self.type) & 0xFF, self.data & 0xFF])

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Bind":
        try:
            t = BindType(raw[0])
        except ValueError:
            t = BindType.NOP
        return cls(type=t, data=raw[1])

    def describe(self) -> str:
        if self.type == BindType.NOP:
            return "\u2014"
        if self.type == BindType.KEY:
            return KEY_CODE_TO_NAME.get(self.data, f"KC 0x{self.data:02x}")
        if self.type == BindType.MACRO:
            try:
                return MACRO_NAMES[self.data - 1]
            except IndexError:
                return f"Macro {self.data}"
        if self.type in (BindType.HYPERSHIFT, BindType.PROFILE):
            return f"Profile {self.data}"
        return f"{self.type.name} ({self.data})"


@dataclass
class Profile:
    keymap: list = field(default_factory=lambda: [Bind() for _ in range(KEYMAP_LEN)])

    def to_bytes(self) -> bytes:
        buf = bytearray(PROFILE_SIZE)
        for i, bind in enumerate(self.keymap):
            buf[i * 2: i * 2 + 2] = bind.to_bytes()
        return bytes(buf)

    @classmethod
    def from_bytes(cls, raw: bytes) -> "Profile":
        raw = raw[:PROFILE_SIZE].ljust(PROFILE_SIZE, b"\x00")
        keymap = [Bind.from_bytes(raw[i * 2: i * 2 + 2]) for i in range(KEYMAP_LEN)]
        return cls(keymap=keymap)

    def get_physical(self, index: int) -> Bind:
        """index: 0-24, matches PHYSICAL_KEYS / KEY_LABELS order."""
        return self.keymap[PHYSICAL_KEYS[index]]

    def set_physical(self, index: int, bind: Bind) -> None:
        self.keymap[PHYSICAL_KEYS[index]] = bind

    def to_dict(self) -> dict:
        return {
            "keys": [
                {
                    "index": i,
                    "label": KEY_LABELS[i],
                    "type": self.get_physical(i).type.name,
                    "data": self.get_physical(i).data,
                }
                for i in range(len(PHYSICAL_KEYS))
            ]
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Profile":
        p = cls()
        for entry in d["keys"]:
            bind = Bind(type=BindType[entry["type"]], data=entry["data"])
            p.set_physical(entry["index"], bind)
        return p


class TartarusDevice:
    def __init__(self):
        self.path = find_keyboard_interface()

    def _read(self, name: str) -> bytes:
        with open(self.path / name, "rb") as f:
            return f.read()

    def _write(self, name: str, data: bytes) -> None:
        try:
            with open(self.path / name, "wb") as f:
                f.write(data)
        except PermissionError as e:
            raise PermissionError(
                f"No write access to {self.path / name}. "
                "Install 99-tartarus.rules and add yourself to the 'tartarus' group, "
                "or run with sudo for now."
            ) from e

    @property
    def profile_count(self) -> int:
        return int(self._read("profile_count").decode().strip())

    @property
    def active_profile(self) -> int:
        return int(self._read("profile_num").decode().strip())

    @active_profile.setter
    def active_profile(self, value: int) -> None:
        if not (1 <= value <= self.profile_count):
            raise ValueError(f"Profile must be between 1 and {self.profile_count}")
        self._write("profile_num", str(value).encode())

    def led_state_for_profile(self, profile_num: int) -> tuple[bool, bool, bool]:
        """Returns the (red, green, blue) on/off state the driver derives automatically
        from the profile number bits - see set_profile() in tartarus.c. Informational
        only; there is currently no way to set this independently of the profile."""
        return (bool(profile_num & 0x04), bool(profile_num & 0x02), bool(profile_num & 0x01))

    def find_event_device(self) -> Path | None:
        """Locates the /dev/input/eventX node registered for this KBD interface
        (input_config() in tartarus.c registers one input_dev per interface).
        Used for live key-press monitoring in the GUI; returns None if the
        input subsystem hasn't created the node (or already gone)."""
        for event_dir in self.path.glob("input/input*/event*"):
            dev_node = Path("/dev/input") / event_dir.name
            if dev_node.exists():
                return dev_node
        return None

    def read_profile(self, profile_num: int | None = None) -> Profile:
        """Reads the keymap of the given profile (switches active profile if needed)."""
        if profile_num is not None and profile_num != self.active_profile:
            self.active_profile = profile_num
        return Profile.from_bytes(self._read("profile"))

    def write_profile(self, profile: Profile, profile_num: int | None = None) -> None:
        """Writes a keymap to the given profile (switches active profile if needed).
        NOTE: this briefly makes the target profile active on the device (LEDs change)."""
        if profile_num is not None and profile_num != self.active_profile:
            self.active_profile = profile_num
        self._write("profile", profile.to_bytes())

    def save_profile_to_file(self, path: str, profile_num: int | None = None) -> None:
        """Saves as readable JSON (preferred format for this toolset)."""
        profile = self.read_profile(profile_num)
        with open(path, "w") as f:
            json.dump(profile.to_dict(), f, indent=2)

    def load_profile_from_file(self, path: str, profile_num: int | None = None) -> None:
        with open(path) as f:
            profile = Profile.from_dict(json.load(f))
        self.write_profile(profile, profile_num)

    def save_profile_raw(self, path: str, profile_num: int | None = None) -> None:
        """Saves the raw 512-byte format, compatible with linapse's .rz files."""
        profile = self.read_profile(profile_num)
        with open(path, "wb") as f:
            f.write(profile.to_bytes())

    def load_profile_raw(self, path: str, profile_num: int | None = None) -> None:
        with open(path, "rb") as f:
            raw = f.read()
        self.write_profile(Profile.from_bytes(raw), profile_num)
