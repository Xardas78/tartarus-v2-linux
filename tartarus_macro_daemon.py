"""
tartarus_macro_daemon.py
Standalone background service: watches the Tartarus KBD interface's evdev
node for KEY_MACRO1..KEY_MACRO30 press events - a key bound to a macro slot
reports one of these instead of a normal key, see the CTRL_MACRO case in
resolve_event_kbd() in tartarus.c - and replays that slot's recorded steps
(tartarus_macros.py, edited via tartarus_macro_gui.py) through a uinput
virtual keyboard.

This is intentionally separate from tartarus_gui.py: macros need to fire
whether or not the configuration GUI happens to be open, so this is meant to
run as a systemd --user service (see tartarus-macros.service) rather than a
thread inside the GUI process.

Needs read access to the Tartarus event device (already covered by the
`input`/`tartarus` group setup from the main README) and write access to
/dev/uinput (see the udev rule in 99-tartarus.rules and the README's macro
setup section - without it, UInput() raises PermissionError on start).

Run standalone for testing: python3 tartarus_macro_daemon.py
"""

from __future__ import annotations

import select
import signal
import sys
import time

try:
    import evdev
    from evdev import UInput, ecodes
except ImportError:
    print("tartarus-macros: the 'evdev' package is required (pip install evdev).",
          file=sys.stderr)
    sys.exit(1)

from tartarus_backend import TartarusDevice
from tartarus_macros import MacroStep, MACRO_SLOTS, load_macros

# KEY_MACRO1..KEY_MACRO30 = 0x290..0x2AD - see MACRO_KEYCODE_OFFSET in
# tartarus_gui.py / the CTRL_MACRO case in resolve_event_kbd() in tartarus.c.
MACRO_KEYCODE_BASE = 0x28F

# Every keycode a recorded step could plausibly use (matches the range
# input_config() registers on the KBD interface in tartarus.c).
_ALL_KEYCODES = list(range(1, 249))


def macro_slot_for_code(code: int) -> int | None:
    slot = code - MACRO_KEYCODE_BASE
    return slot if 1 <= slot <= MACRO_SLOTS else None


class MacroPlayer:
    """Owns one uinput virtual keyboard and plays macro steps through it.
    Playback is a plain blocking loop (time.sleep between steps) - simple,
    and since the daemon's own event loop is single-threaded, it also means
    a second trigger arriving mid-playback just waits its turn in the kernel's
    event queue instead of overlapping, which is exactly the "ignore/queue,
    don't interleave two macros' keystrokes" behavior we want anyway."""

    def __init__(self):
        self.ui = UInput({ecodes.EV_KEY: _ALL_KEYCODES}, name="tartarus-macro-player")

    def play(self, steps: list[MacroStep]) -> None:
        for step in steps:
            if step.delay_ms:
                time.sleep(step.delay_ms / 1000)
            self.ui.write(ecodes.EV_KEY, step.code, 1 if step.press else 0)
            self.ui.syn()

    def close(self) -> None:
        self.ui.close()


def find_tartarus_event_device() -> str | None:
    try:
        device = TartarusDevice()
    except FileNotFoundError:
        return None
    path = device.find_event_device()
    return str(path) if path else None


def run() -> None:
    event_path = find_tartarus_event_device()
    if event_path is None:
        print("tartarus-macros: Tartarus KBD event device not found - "
              "is the driver loaded and the device plugged in?", file=sys.stderr)
        sys.exit(1)

    dev = evdev.InputDevice(event_path)

    try:
        player = MacroPlayer()
    except PermissionError:
        print(f"tartarus-macros: no write access to /dev/uinput. "
              f"Install the uinput udev rule from 99-tartarus.rules "
              f"(and 'sudo modprobe uinput' if the module isn't loaded), "
              f"then log out/in so the group membership takes effect.",
              file=sys.stderr)
        dev.close()
        sys.exit(1)

    print(f"tartarus-macros: watching {event_path} ({dev.name})")

    running = True

    def handle_signal(signum, frame) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        while running:
            ready, _, _ = select.select([dev.fd], [], [], 0.5)
            if not ready:
                continue
            try:
                events = list(dev.read())
            except OSError:
                print("tartarus-macros: device disconnected, exiting.", file=sys.stderr)
                break
            for event in events:
                if event.type != ecodes.EV_KEY or event.value != 1:
                    continue
                slot = macro_slot_for_code(event.code)
                if slot is None:
                    continue
                macro = load_macros().get(slot)
                if macro is None or not macro.steps:
                    continue
                player.play(macro.steps)
    finally:
        player.close()
        dev.close()


if __name__ == "__main__":
    run()
