"""
tartarus_macro_gui.py
Dialog for recording/editing one macro slot (see tartarus_macros.py for the
storage format). Two ways to build the same step list:
 - Live recording: grabs a chosen keyboard device via evdev and appends
   (code, press, delay_ms) steps with real timing while recording.
 - Manual editing: add/remove individual steps by hand.
Both write into the same self.steps list, so a recording can be manually
touched up afterwards (or a macro built entirely by hand, or entirely by
recording - whichever the user prefers, matching that this is a "have both,
your choice" feature).

Kept in its own module (rather than folded into tartarus_gui.py, which is
already large) and imports nothing from there, so tartarus_macro_daemon.py's
non-GUI code never has to pull in PySide6 or this file's Qt widgets.
"""

from __future__ import annotations

import select

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPushButton, QSpinBox, QVBoxLayout,
)

from tartarus_backend import KEY_CODE_TO_NAME, KEY_NAME_TO_CODE
from tartarus_macros import Macro, MacroStep, load_macros, save_macro

try:
    import evdev
except ImportError:
    evdev = None


def parse_key_text(text: str) -> int | None:
    """Resolve a typed key name or raw hex/decimal value to a Linux keycode."""
    text = text.strip()
    if not text:
        return None
    upper = text.upper()
    if upper in KEY_NAME_TO_CODE:
        return KEY_NAME_TO_CODE[upper]
    try:
        return int(text, 0)
    except ValueError:
        return None


def list_keyboard_devices() -> list[tuple[str, str]]:
    """(device_path, display_name) for each evdev device that looks like a
    keyboard (has a letter-key capability) - crude but avoids offering mice/
    joysticks/etc. as a recording source. Excludes the Tartarus itself: it's
    what's being configured, recording from it while doing so is more
    confusing than useful."""
    if evdev is None:
        return []
    devices = []
    for path in evdev.list_devices():
        try:
            dev = evdev.InputDevice(path)
        except OSError:
            continue
        caps = dev.capabilities().get(evdev.ecodes.EV_KEY, [])
        if evdev.ecodes.KEY_A in caps and "tartarus" not in dev.name.lower():
            devices.append((path, dev.name))
        dev.close()
    return devices


class RecorderThread(QThread):
    """Grabs one evdev device exclusively (so keys don't leak through to the
    rest of the desktop while recording) and emits each press/release with
    the real delay since the previous step. Mirrors KeyMonitorThread's
    select()-based loop in tartarus_gui.py so stop() takes effect promptly
    instead of blocking in evdev's own read_loop()."""

    step_recorded = Signal(int, bool, int)  # code, press, delay_ms

    def __init__(self, device_path: str, parent=None):
        super().__init__(parent)
        self.device_path = device_path
        self._stop = False

    def run(self) -> None:
        try:
            dev = evdev.InputDevice(self.device_path)
        except OSError:
            return
        try:
            dev.grab()
        except OSError:
            pass  # still record even if another process already grabbed it
        last_time: float | None = None
        try:
            while not self._stop:
                r, _, _ = select.select([dev.fd], [], [], 0.2)
                if not r:
                    continue
                try:
                    for event in dev.read():
                        if event.type != evdev.ecodes.EV_KEY or event.value not in (0, 1):
                            continue
                        now = event.timestamp()
                        delay_ms = 0 if last_time is None else max(0, round((now - last_time) * 1000))
                        last_time = now
                        self.step_recorded.emit(event.code, bool(event.value), delay_ms)
                except OSError:
                    break  # device unplugged
        finally:
            try:
                dev.ungrab()
            except OSError:
                pass
            dev.close()

    def stop(self) -> None:
        self._stop = True


class MacroEditDialog(QDialog):
    """Popup to record/edit macro slot `slot`. Saves directly to
    tartarus_macros.MACROS_PATH on accept (there's no separate "write to
    device" step - macros live in a plain file the playback daemon reads,
    not on the Tartarus itself)."""

    def __init__(self, parent, slot: int):
        super().__init__(parent)
        self.slot = slot
        self.setWindowTitle(f"Makro M{slot} bearbeiten")
        self.resize(440, 440)

        existing = load_macros().get(slot)
        self.steps: list[MacroStep] = list(existing.steps) if existing else []
        self.recorder: RecorderThread | None = None

        layout = QVBoxLayout(self)

        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Name:"))
        self.name_edit = QLineEdit(existing.name if existing else f"M{slot}")
        name_row.addWidget(self.name_edit)
        layout.addLayout(name_row)

        self.step_list = QListWidget()
        layout.addWidget(self.step_list)

        record_row = QHBoxLayout()
        record_row.addWidget(QLabel("Gerät:"))
        self.device_combo = QComboBox()
        self._devices = list_keyboard_devices()
        for path, dev_name in self._devices:
            self.device_combo.addItem(dev_name, path)
        record_row.addWidget(self.device_combo, stretch=1)
        self.record_btn = QPushButton("● Aufnahme starten")
        self.record_btn.setEnabled(bool(self._devices))
        self.record_btn.clicked.connect(self._toggle_recording)
        record_row.addWidget(self.record_btn)
        layout.addLayout(record_row)

        if evdev is None:
            layout.addWidget(QLabel("Live-Aufnahme benötigt das Python-Paket 'evdev' (pip install evdev)."))
        elif not self._devices:
            layout.addWidget(QLabel("Keine Tastatur für die Aufnahme gefunden."))

        manual_row = QHBoxLayout()
        self.key_combo = QComboBox()
        self.key_combo.setEditable(True)
        self.key_combo.addItems(sorted(KEY_NAME_TO_CODE.keys()))
        manual_row.addWidget(self.key_combo, stretch=1)
        self.press_check = QCheckBox("Drücken")
        self.press_check.setChecked(True)
        manual_row.addWidget(self.press_check)
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 60000)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setToolTip("Wartezeit vor diesem Schritt")
        manual_row.addWidget(self.delay_spin)
        add_btn = QPushButton("+ Schritt")
        add_btn.clicked.connect(self._add_manual_step)
        manual_row.addWidget(add_btn)
        layout.addLayout(manual_row)

        edit_row = QHBoxLayout()
        remove_btn = QPushButton("Ausgewählten Schritt löschen")
        remove_btn.clicked.connect(self._remove_selected_step)
        edit_row.addWidget(remove_btn)
        clear_btn = QPushButton("Alle löschen")
        clear_btn.clicked.connect(self._clear_steps)
        edit_row.addWidget(clear_btn)
        layout.addLayout(edit_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save_and_close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_step_list()

    def _refresh_step_list(self) -> None:
        self.step_list.clear()
        for i, step in enumerate(self.steps, start=1):
            arrow = "↓" if step.press else "↑"  # down/up
            name = KEY_CODE_TO_NAME.get(step.code, f"KC{step.code}")
            self.step_list.addItem(f"{i}.  +{step.delay_ms}ms  {arrow}  {name}")

    def _toggle_recording(self) -> None:
        if self.recorder is not None:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        path = self.device_combo.currentData()
        if not path:
            return
        self.recorder = RecorderThread(path, self)
        self.recorder.step_recorded.connect(self._on_step_recorded)
        self.recorder.start()
        self.record_btn.setText("■ Aufnahme stoppen")
        self.device_combo.setEnabled(False)

    def _stop_recording(self) -> None:
        if self.recorder is not None:
            self.recorder.stop()
            self.recorder.wait(1000)
            self.recorder = None
        self.record_btn.setText("● Aufnahme starten")
        self.device_combo.setEnabled(True)

    def _on_step_recorded(self, code: int, press: bool, delay_ms: int) -> None:
        self.steps.append(MacroStep(code, press, delay_ms))
        self._refresh_step_list()
        self.step_list.scrollToBottom()

    def _add_manual_step(self) -> None:
        code = parse_key_text(self.key_combo.currentText())
        if code is None or not (0 < code < 0x2ff):
            QMessageBox.warning(self, "Ungültige Taste",
                                 "Tastenname nicht erkannt (z.B. 'A', 'F1', 'SPACE').")
            return
        self.steps.append(MacroStep(code, self.press_check.isChecked(), self.delay_spin.value()))
        self._refresh_step_list()
        self.step_list.scrollToBottom()

    def _remove_selected_step(self) -> None:
        row = self.step_list.currentRow()
        if row >= 0:
            del self.steps[row]
            self._refresh_step_list()

    def _clear_steps(self) -> None:
        if not self.steps:
            return
        choice = QMessageBox.question(
            self, "Alle Schritte löschen", "Wirklich alle Schritte dieses Makros entfernen?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if choice == QMessageBox.Yes:
            self.steps = []
            self._refresh_step_list()

    def _save_and_close(self) -> None:
        self._stop_recording()
        macro = Macro(name=self.name_edit.text().strip() or f"M{self.slot}", steps=self.steps)
        save_macro(self.slot, macro)
        self.accept()

    def reject(self) -> None:
        self._stop_recording()
        super().reject()

    def closeEvent(self, event) -> None:
        self._stop_recording()
        super().closeEvent(event)
