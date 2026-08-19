"""
tartarus_layout.py
Shared device-artwork layout data for tartarus_gui.py.

The device view renders tartarus_v2.svg as a static background, then overlays
one clickable region per physical key on top of it. tartarus_gui.py prefers to
read each region's exact position/size straight off the SVG's own named
elements (Key_1..Key_20, Circle, Cross, Scroll) via
QSvgRenderer.boundsOnElement()/transformForElement() - editing the shapes in
Inkscape is what "calibrates" the hitboxes, no separate tool needed.

DEFAULT_KEY_HITBOXES / DEFAULT_MOUSE_CLICK_HITBOX here are only the fallback
used when a named element is missing from the SVG (rotated rectangle: cx, cy,
w, h, angle_degrees, in the SVG's own viewBox units - see SVG_VIEWBOX). A
tartarus_hitboxes.json, if present, overrides these defaults per-region;
load_hitboxes() prefers it and falls back to the hardcoded values below.
"""

from __future__ import annotations

import json
from pathlib import Path

SVG_PATH = Path(__file__).parent / "tartarus_v2.svg"
SVG_VIEWBOX = (912, 1170)
HITBOX_FILE = Path(__file__).parent / "tartarus_hitboxes.json"

HitBox = tuple[float, float, float, float, float]  # cx, cy, w, h, angle_degrees

# index -> hitbox, order matches KEY_LABELS/PHYSICAL_KEYS in tartarus_backend.py.
# Converted from the original hand-read (left, top, right, bottom) estimates;
# angle 0 until calibrated.
DEFAULT_KEY_HITBOXES: dict[int, HitBox] = {
    0: (165, 232.5, 140, 115, 0),    # 01
    1: (267.5, 196.5, 135, 117, 0),  # 02
    2: (370, 187.5, 130, 115, 0),    # 03
    3: (475, 186.5, 130, 117, 0),    # 04
    4: (585, 187.5, 130, 115, 0),    # 05
    5: (192.5, 342.5, 135, 115, 0),  # 06
    6: (310, 307.5, 130, 115, 0),    # 07
    7: (425, 299, 130, 112, 0),      # 08
    8: (540, 294, 130, 112, 0),      # 09
    9: (657.5, 286, 125, 108, 0),    # 10
    10: (217.5, 435, 135, 120, 0),   # 11
    11: (310, 402.5, 130, 115, 0),   # 12
    12: (417.5, 397.5, 125, 115, 0), # 13
    13: (525, 392.5, 130, 115, 0),   # 14
    14: (650, 375, 130, 110, 0),     # 15
    15: (237.5, 525, 135, 120, 0),   # 16
    16: (335, 492.5, 130, 115, 0),   # 17
    17: (430, 486.5, 130, 117, 0),   # 18
    18: (525, 477.5, 130, 115, 0),   # 19
    19: (737.5, 792.5, 95, 95, 0),   # 20
    20: (697.5, 585, 95, 80, 0),     # Circle
    21: (753, 607.5, 65, 65, 0),     # Thumb U
    22: (785.5, 640, 65, 80, 0),     # Thumb R
    23: (753, 672.5, 65, 65, 0),     # Thumb D
    24: (720.5, 640, 65, 80, 0),     # Thumb L
}
DEFAULT_MOUSE_CLICK_HITBOX: HitBox = (582.5, 510, 75, 170, 0)


def load_hitboxes() -> tuple[dict[int, HitBox], HitBox]:
    """Returns (key_hitboxes, mouse_click_hitbox) - only used as a fallback for
    SVG elements tartarus_gui.py can't find by id. Prefers tartarus_hitboxes.json
    if present and falls back to the hardcoded defaults above, per-region."""
    key_hitboxes = dict(DEFAULT_KEY_HITBOXES)
    mouse_hitbox = DEFAULT_MOUSE_CLICK_HITBOX

    if HITBOX_FILE.exists():
        try:
            data = json.loads(HITBOX_FILE.read_text())
            for k, v in data.get("keys", {}).items():
                key_hitboxes[int(k)] = tuple(v)
            if "mouse_click" in data:
                mouse_hitbox = tuple(data["mouse_click"])
        except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
            pass  # fall back to defaults silently - calibration file is optional

    return key_hitboxes, mouse_hitbox


def save_hitboxes(key_hitboxes: dict[int, HitBox], mouse_hitbox: HitBox) -> None:
    data = {
        "keys": {str(k): list(v) for k, v in key_hitboxes.items()},
        "mouse_click": list(mouse_hitbox),
    }
    HITBOX_FILE.write_text(json.dumps(data, indent=2))
