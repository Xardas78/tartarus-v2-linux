"""
tartarus_gui.py
PySide6 desktop GUI for the Razer Tartarus V2 (hid-tartarus driver), built on top of
tartarus_backend.py. Visual 5x5 + thumbstick layout matching the physical device,
click a key to edit its bind, switch between the 8 device profiles, save/load
profiles as JSON or raw .rz files.

Run: python3 tartarus_gui.py
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QSpinBox, QDialog, QDialogButtonBox,
    QMessageBox, QFileDialog, QStatusBar, QFrame, QStackedWidget,
)

from tartarus_backend import (
    TartarusDevice, Profile, Bind, BindType,
    KEY_LABELS, KEY_NAME_TO_CODE, KEY_CODE_TO_NAME, MACRO_NAMES,
)

# Version of this GUI/backend tooling (independent of the kernel module's DKMS
# version, which stays at the stable 0.1 unless the driver itself changes).
VERSION = "v0.1"

# Only expose bind types the kernel driver actually executes today
# (SCRIPT/SWKEY/MOUSE_MOVE/MOUSE_WHEEL are stored but currently no-ops - see
# resolve_event_kbd() in tartarus.c - so we hide them to avoid silent "it does nothing").
EDITABLE_BIND_TYPES = [BindType.NOP, BindType.KEY, BindType.HYPERSHIFT,
                        BindType.PROFILE, BindType.MACRO]
BIND_TYPE_LABELS = {
    BindType.NOP: "Nichts (deaktiviert)",
    BindType.KEY: "Taste",
    BindType.HYPERSHIFT: "Hypershift (halten)",
    BindType.PROFILE: "Profil wechseln",
    BindType.MACRO: "Makro-Slot",
}


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


class KeyEditDialog(QDialog):
    """Popup to edit a single key's bind. Returns the new Bind via .result_bind
    if the user clicked OK, otherwise .result_bind stays None."""

    def __init__(self, parent, key_label: str, current: Bind, profile_count: int):
        super().__init__(parent)
        self.setWindowTitle(f"Taste {key_label} belegen")
        self.profile_count = profile_count
        self.result_bind: Bind | None = None

        layout = QVBoxLayout(self)

        self.type_box = QComboBox()
        for bt in EDITABLE_BIND_TYPES:
            self.type_box.addItem(BIND_TYPE_LABELS[bt], bt)
        layout.addWidget(QLabel("Aktion:"))
        layout.addWidget(self.type_box)

        # Stacked widget: one page per bind type needing extra input
        self.stack = QStackedWidget()

        # NOP - nothing to configure
        self.stack.addWidget(QLabel("Diese Taste tut nichts."))

        # KEY - editable combobox of key names
        self.key_combo = QComboBox()
        self.key_combo.setEditable(True)
        self.key_combo.addItems(sorted(KEY_NAME_TO_CODE.keys()))
        self.stack.addWidget(self._wrap("Taste:", self.key_combo))

        # HYPERSHIFT - target profile
        self.hs_spin = QSpinBox()
        self.hs_spin.setRange(1, profile_count)
        self.stack.addWidget(self._wrap("Ziel-Profil (solange gehalten):", self.hs_spin))

        # PROFILE - target profile
        self.prof_spin = QSpinBox()
        self.prof_spin.setRange(1, profile_count)
        self.stack.addWidget(self._wrap("Ziel-Profil (dauerhaft wechseln):", self.prof_spin))

        # MACRO - macro slot
        self.macro_combo = QComboBox()
        self.macro_combo.addItems(MACRO_NAMES)
        self.stack.addWidget(self._wrap(
            "Makro-Slot (Wiedergabe braucht separates Userspace-Tool):", self.macro_combo))

        layout.addWidget(self.stack)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.type_box.currentIndexChanged.connect(
            lambda i: self.stack.setCurrentIndex(self.type_box.itemData(i)))

        self._load(current)

    @staticmethod
    def _wrap(label_text: str, widget: QWidget) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel(label_text))
        row.addWidget(widget)
        return box

    def _load(self, bind: Bind) -> None:
        idx = EDITABLE_BIND_TYPES.index(bind.type) if bind.type in EDITABLE_BIND_TYPES else 0
        self.type_box.setCurrentIndex(idx)
        self.stack.setCurrentIndex(bind.type if bind.type in EDITABLE_BIND_TYPES else 0)

        if bind.type == BindType.KEY:
            name = KEY_CODE_TO_NAME.get(bind.data, "")
            self.key_combo.setCurrentText(name)
        elif bind.type == BindType.HYPERSHIFT:
            self.hs_spin.setValue(bind.data or 1)
        elif bind.type == BindType.PROFILE:
            self.prof_spin.setValue(bind.data or 1)
        elif bind.type == BindType.MACRO:
            try:
                self.macro_combo.setCurrentIndex(max(0, bind.data - 1))
            except Exception:
                pass

    def _accept(self) -> None:
        bt = self.type_box.currentData()

        if bt == BindType.NOP:
            self.result_bind = Bind(BindType.NOP, 0)

        elif bt == BindType.KEY:
            code = parse_key_text(self.key_combo.currentText())
            if code is None or not (0 < code < 256):
                QMessageBox.warning(self, "Ungültige Taste",
                                     "Tastenname nicht erkannt (z.B. 'A', 'F1', 'SPACE') "
                                     "und auch nicht als Zahl interpretierbar.")
                return
            self.result_bind = Bind(BindType.KEY, code)

        elif bt == BindType.HYPERSHIFT:
            self.result_bind = Bind(BindType.HYPERSHIFT, self.hs_spin.value())

        elif bt == BindType.PROFILE:
            self.result_bind = Bind(BindType.PROFILE, self.prof_spin.value())

        elif bt == BindType.MACRO:
            self.result_bind = Bind(BindType.MACRO, self.macro_combo.currentIndex() + 1)

        self.accept()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"Tartarus V2 Configurator {VERSION}")

        try:
            self.device = TartarusDevice()
        except FileNotFoundError as e:
            QMessageBox.critical(self, "Gerät nicht gefunden", str(e))
            sys.exit(1)

        self.profile_num = self.device.active_profile
        self.profile = self.device.read_profile(self.profile_num)
        self.dirty = False
        self.key_buttons: list[QPushButton] = []

        self._build_ui()
        self._refresh_all()

    # -- UI construction --
    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)

        # Top bar: profile selector + LED preview + actions
        top = QHBoxLayout()
        top.addWidget(QLabel("Profil:"))

        self.profile_box = QComboBox()
        for n in range(1, self.device.profile_count + 1):
            self.profile_box.addItem(f"Profil {n}", n)
        self.profile_box.currentIndexChanged.connect(self._on_profile_selected)
        top.addWidget(self.profile_box)

        self.led_preview = QFrame()
        self.led_preview.setFixedSize(24, 24)
        self.led_preview.setFrameShape(QFrame.Box)
        top.addWidget(QLabel("LED:"))
        top.addWidget(self.led_preview)

        top.addStretch()

        write_btn = QPushButton("Auf Gerät schreiben")
        write_btn.clicked.connect(self._write_to_device)
        top.addWidget(write_btn)

        reload_btn = QPushButton("Neu vom Gerät laden")
        reload_btn.clicked.connect(self._reload_from_device)
        top.addWidget(reload_btn)

        outer.addLayout(top)

        # Key grid: 5 columns x 5 rows, matching PHYSICAL_KEYS / KEY_LABELS order
        grid = QGridLayout()
        grid.setSpacing(8)
        for i, label in enumerate(KEY_LABELS):
            btn = QPushButton()
            btn.setMinimumSize(140, 70)
            btn.clicked.connect(lambda checked=False, idx=i: self._edit_key(idx))
            self.key_buttons.append(btn)
            grid.addWidget(btn, i // 5, i % 5)
        outer.addLayout(grid)

        # Mouse wheel - placeholder only. resolve_event_mouse() in tartarus.c hardcodes
        # BTN_MIDDLE/REL_WHEEL with no profile lookup at all (unlike the keyboard side),
        # so there is currently nothing in the driver to read or write here. Shown
        # disabled as a reminder of planned functionality, not a working control.
        wheel_row = QHBoxLayout()
        for wheel_label in ("Mausrad hoch", "Mausrad runter", "Mausrad-Klick"):
            wheel_btn = QPushButton(f"{wheel_label}\n(im Treiber noch nicht unterstützt)")
            wheel_btn.setMinimumSize(140, 70)
            wheel_btn.setEnabled(False)
            wheel_row.addWidget(wheel_btn)
        outer.addLayout(wheel_row)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        self._build_menu()

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&Datei")

        save_json = menu.addAction("Profil speichern (JSON)…")
        save_json.triggered.connect(self._save_json)

        load_json = menu.addAction("Profil laden (JSON)…")
        load_json.triggered.connect(self._load_json)

        menu.addSeparator()

        save_raw = menu.addAction("Profil speichern (.rz, kompatibel zu linapse)…")
        save_raw.triggered.connect(self._save_raw)

        load_raw = menu.addAction("Profil laden (.rz)…")
        load_raw.triggered.connect(self._load_raw)

    # -- Data <-> UI sync --
    def _refresh_all(self) -> None:
        self.profile_box.blockSignals(True)
        self.profile_box.setCurrentIndex(self.profile_num - 1)
        self.profile_box.blockSignals(False)

        r, g, b = self.device.led_state_for_profile(self.profile_num)
        color = "#%02x%02x%02x" % (255 if r else 0, 255 if g else 0, 255 if b else 0)
        self.led_preview.setStyleSheet(f"background-color: {color}; border: 1px solid #888;")

        for i, btn in enumerate(self.key_buttons):
            bind = self.profile.get_physical(i)
            btn.setText(f"{KEY_LABELS[i]}\n{bind.describe()}")

        self._update_title()

    def _update_title(self) -> None:
        star = " *" if self.dirty else ""
        self.setWindowTitle(f"Tartarus V2 Configurator {VERSION} \u2014 Profil {self.profile_num}{star}")

    def _mark_dirty(self) -> None:
        self.dirty = True
        self._update_title()

    # -- Actions --
    def _edit_key(self, index: int) -> None:
        current = self.profile.get_physical(index)
        dlg = KeyEditDialog(self, KEY_LABELS[index], current, self.device.profile_count)
        if dlg.exec() == QDialog.Accepted and dlg.result_bind is not None:
            self.profile.set_physical(index, dlg.result_bind)
            self._mark_dirty()
            self._refresh_all()

    def _confirm_discard_if_dirty(self) -> bool:
        if not self.dirty:
            return True
        choice = QMessageBox.question(
            self, "Ungespeicherte Änderungen",
            "Es gibt ungespeicherte Änderungen an diesem Profil. Trotzdem verwerfen?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        return choice == QMessageBox.Yes

    def _on_profile_selected(self, _index: int) -> None:
        new_num = self.profile_box.currentData()
        if new_num == self.profile_num:
            return
        if not self._confirm_discard_if_dirty():
            self.profile_box.blockSignals(True)
            self.profile_box.setCurrentIndex(self.profile_num - 1)
            self.profile_box.blockSignals(False)
            return

        try:
            self.profile = self.device.read_profile(new_num)
        except Exception as e:
            QMessageBox.critical(self, "Lesefehler", str(e))
            return

        self.profile_num = new_num
        self.dirty = False
        self._refresh_all()

    def _write_to_device(self) -> None:
        try:
            self.device.write_profile(self.profile, self.profile_num)
        except PermissionError as e:
            QMessageBox.critical(self, "Keine Schreibrechte", str(e))
            return
        except Exception as e:
            QMessageBox.critical(self, "Fehler beim Schreiben", str(e))
            return
        self.dirty = False
        self._update_title()
        self.statusBar().showMessage(f"Profil {self.profile_num} auf das Gerät geschrieben.", 4000)

    def _reload_from_device(self) -> None:
        if not self._confirm_discard_if_dirty():
            return
        self.profile = self.device.read_profile(self.profile_num)
        self.dirty = False
        self._refresh_all()
        self.statusBar().showMessage("Profil neu vom Gerät geladen.", 4000)

    def _save_json(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Profil speichern", f"profil_{self.profile_num}.json", "JSON (*.json)")
        if not path:
            return
        import json
        with open(path, "w") as f:
            json.dump(self.profile.to_dict(), f, indent=2)
        self.statusBar().showMessage(f"Gespeichert: {path}", 4000)

    def _load_json(self) -> None:
        if not self._confirm_discard_if_dirty():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Profil laden", "", "JSON (*.json)")
        if not path:
            return
        import json
        with open(path) as f:
            self.profile = Profile.from_dict(json.load(f))
        self.dirty = True
        self._refresh_all()
        self.statusBar().showMessage(
            f"Geladen: {path} (noch nicht aufs Gerät geschrieben)", 4000)

    def _save_raw(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Profil speichern (raw)", f"profil_{self.profile_num}.rz", "Razer Profile (*.rz)")
        if not path:
            return
        with open(path, "wb") as f:
            f.write(self.profile.to_bytes())
        self.statusBar().showMessage(f"Gespeichert: {path}", 4000)

    def _load_raw(self) -> None:
        if not self._confirm_discard_if_dirty():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Profil laden (raw)", "", "Razer Profile (*.rz)")
        if not path:
            return
        with open(path, "rb") as f:
            self.profile = Profile.from_bytes(f.read())
        self.dirty = True
        self._refresh_all()
        self.statusBar().showMessage(
            f"Geladen: {path} (noch nicht aufs Gerät geschrieben)", 4000)

    def closeEvent(self, event) -> None:
        if self.dirty:
            choice = QMessageBox.question(
                self, "Ungespeicherte Änderungen",
                "Es gibt ungespeicherte Änderungen, die nicht aufs Gerät geschrieben wurden. "
                "Trotzdem beenden?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if choice != QMessageBox.Yes:
                event.ignore()
                return
        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(900, 600)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
