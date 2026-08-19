"""
tartarus_gui.py
PySide6 desktop GUI for the Razer Tartarus V2 (hid-tartarus driver), built on top of
tartarus_backend.py. Visual 5x5 + thumbstick layout matching the physical device,
click a key to edit its bind, switch between the 8 device profiles, save/load
profiles as JSON or raw .rz files.

Run: python3 tartarus_gui.py
"""

from __future__ import annotations

import math
import select
import sys
import xml.etree.ElementTree as ET

from PySide6.QtCore import QRectF, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QSpinBox, QCheckBox, QDialog, QDialogButtonBox,
    QMessageBox, QFileDialog, QStatusBar, QFrame, QStackedWidget,
    QGraphicsView, QGraphicsScene, QGraphicsObject, QStyleOptionGraphicsItem,
)

from tartarus_backend import (
    TartarusDevice, TartarusMouseDevice, Profile, MouseProfile, Bind, BindType, Mod, MOD_LABELS,
    KEY_LABELS, KEY_NAME_TO_CODE, KEY_CODE_TO_NAME, MACRO_NAMES,
)
from tartarus_layout import SVG_PATH, SVG_VIEWBOX, load_hitboxes
from tartarus_svg import Matrix, SvgDocument, compose

try:
    import evdev
except ImportError:
    evdev = None

# Offset resolve_event_kbd() in tartarus.c applies to a macro bind's data byte
# before calling input_report_key() - see CTRL_MACRO case (data + 0x28F lands
# on KEY_MACRO1 .. KEY_MACRO30, 0x290-0x2AD).
MACRO_KEYCODE_OFFSET = 0x28F

DEVICE_VIEW_WIDTH = 480   # display size; height follows from the SVG viewBox aspect ratio

# Overlay fill/outline per key state (brush color, pen color, pen width). Region
# hitboxes themselves are read off the SVG's named elements at build time (see
# _build_device_view()); tartarus_layout.py is only the fallback for elements
# the SVG doesn't (yet) name.
STATE_UNSET = (QColor(255, 255, 255, 25), QColor(0, 0, 0, 90), 1)
STATE_SET = (QColor(52, 152, 219, 70), QColor(41, 128, 185, 180), 1)
STATE_ACTIVE = (QColor(46, 204, 113, 160), QColor(46, 204, 113, 255), 2)


class KeyRegionItem(QGraphicsObject):
    """A clickable, rotatable overlay region on the device view (one per
    physical key, plus one for the wheel-click). QGraphicsObject is Qt's own
    QObject+QGraphicsItem hybrid base (for exactly this - a graphics item that
    needs signals), so unlike QGraphicsRectItem it draws nothing on its own -
    paint()/boundingRect() below do that."""

    clicked = Signal()

    def __init__(self, local_rect: QRectF, transform: QTransform, shape: str = "rect"):
        super().__init__()
        self._local_rect = local_rect
        self._shape_kind = shape  # "rect" or "ellipse"
        self._brush = QBrush(STATE_UNSET[0])
        self._pen = QPen(STATE_UNSET[1], STATE_UNSET[2])
        # The full local-to-scene matrix (not just a rotation angle) so shapes
        # with shear or non-uniform scale (see make_region() in
        # _build_device_view()) still render/hit-test exactly like their
        # source SVG shape.
        self.setTransform(transform)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)

    def set_state(self, state: tuple[QColor, QColor, int]) -> None:
        brush_color, pen_color, pen_width = state
        self._brush = QBrush(brush_color)
        self._pen = QPen(pen_color, pen_width)
        self.update()

    def boundingRect(self) -> QRectF:
        return self._local_rect

    def shape(self) -> QPainterPath:
        path = QPainterPath()
        if self._shape_kind == "ellipse":
            path.addEllipse(self.boundingRect())
        else:
            path.addRect(self.boundingRect())
        return path

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget=None) -> None:
        painter.setBrush(self._brush)
        painter.setPen(self._pen)
        if self._shape_kind == "ellipse":
            painter.drawEllipse(self.boundingRect())
        else:
            painter.drawRect(self.boundingRect())

    def mousePressEvent(self, event) -> None:
        self.clicked.emit()
        super().mousePressEvent(event)


def apply_state(item, state: tuple[QColor, QColor, int]) -> None:
    """Applies a STATE_* tuple to either a KeyRegionItem or (fallback grid) a
    QPushButton, so callers don't need to care which one they got."""
    if isinstance(item, KeyRegionItem):
        item.set_state(state)
        return
    brush, pen, width = state
    item.setStyleSheet(
        f"background-color: rgba({brush.red()},{brush.green()},{brush.blue()},{brush.alpha()}); "
        f"border: {width}px solid rgba({pen.red()},{pen.green()},{pen.blue()},{pen.alpha()}); "
        "border-radius: 6px;"
    )


class KeyMonitorThread(QThread):
    """Watches the KBD interface's evdev node and emits (keycode, pressed) for
    every EV_KEY event. This is the MAPPED keycode the driver reports (see
    resolve_event_kbd() in tartarus.c) - not the physical key index - so the
    caller has to reverse-map it via the currently loaded profile.
    NOTE: Keys bound to HYPERSHIFT/PROFILE/NOP never produce an EV_KEY event
    here (the driver consumes them internally), so they cannot be highlighted
    this way."""

    key_event = Signal(int, bool)

    def __init__(self, device_path, parent=None):
        super().__init__(parent)
        self.device_path = device_path
        self._stop = False

    def run(self) -> None:
        try:
            dev = evdev.InputDevice(str(self.device_path))
        except OSError:
            return
        try:
            while not self._stop:
                r, _, _ = select.select([dev.fd], [], [], 0.5)
                if not r:
                    continue
                try:
                    for event in dev.read():
                        if event.type == evdev.ecodes.EV_KEY:
                            self.key_event.emit(event.code, bool(event.value))
                except OSError:
                    break  # device unplugged
        finally:
            dev.close()

    def stop(self) -> None:
        self._stop = True

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

# resolve_event_mouse() in tartarus.c only understands NOP/KEY/PROFILE for the
# wheel-click bind (anything else - or an unconfigured NOP - falls back to the
# stock BTN_MIDDLE click), so the edit dialog only offers those here.
MOUSE_CLICK_BIND_TYPES = [BindType.NOP, BindType.KEY, BindType.PROFILE]


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

    def __init__(self, parent, key_label: str, current: Bind, profile_count: int,
                 bind_types: list[BindType] | None = None):
        super().__init__(parent)
        self.setWindowTitle(f"Taste {key_label} belegen")
        self.profile_count = profile_count
        self.bind_types = bind_types if bind_types is not None else EDITABLE_BIND_TYPES
        self.result_bind: Bind | None = None

        layout = QVBoxLayout(self)

        self.type_box = QComboBox()
        for bt in self.bind_types:
            self.type_box.addItem(BIND_TYPE_LABELS[bt], bt)
        layout.addWidget(QLabel("Aktion:"))
        layout.addWidget(self.type_box)

        # Stacked widget: one page per bind type needing extra input
        self.stack = QStackedWidget()

        # NOP - nothing to configure
        self.stack.addWidget(QLabel("Diese Taste tut nichts."))

        # KEY - editable combobox of key names + modifier checkboxes
        key_page = QWidget()
        key_layout = QVBoxLayout(key_page)
        key_layout.setContentsMargins(0, 0, 0, 0)

        self.key_combo = QComboBox()
        self.key_combo.setEditable(True)
        self.key_combo.addItems(sorted(KEY_NAME_TO_CODE.keys()))
        key_layout.addWidget(self._wrap("Taste:", self.key_combo))

        self.mod_checks: dict[Mod, QCheckBox] = {}
        mod_row = QHBoxLayout()
        mod_row.addWidget(QLabel("Modifier:"))
        for mod, label in MOD_LABELS.items():
            cb = QCheckBox(label)
            self.mod_checks[mod] = cb
            mod_row.addWidget(cb)
        mod_row.addStretch()
        key_layout.addLayout(mod_row)

        self.stack.addWidget(key_page)

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
        idx = self.bind_types.index(bind.type) if bind.type in self.bind_types else 0
        self.type_box.setCurrentIndex(idx)
        self.stack.setCurrentIndex(bind.type if bind.type in self.bind_types else 0)

        if bind.type == BindType.KEY:
            name = KEY_CODE_TO_NAME.get(bind.data, "")
            self.key_combo.setCurrentText(name)
            for mod, cb in self.mod_checks.items():
                cb.setChecked(bool(bind.mods & mod))
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
            mods = Mod.NONE
            for mod, cb in self.mod_checks.items():
                if cb.isChecked():
                    mods |= mod
            self.result_bind = Bind(BindType.KEY, code, mods)

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

        # Mouse interface (wheel-click bind) - optional: degrade gracefully if it's
        # missing rather than blocking the whole GUI over a secondary feature.
        try:
            self.mouse_device: TartarusMouseDevice | None = TartarusMouseDevice()
        except FileNotFoundError:
            self.mouse_device = None

        self.profile_num = self.device.active_profile
        self.profile = self.device.read_profile(self.profile_num)
        self.mouse_profile: MouseProfile | None = None
        if self.mouse_device is not None:
            try:
                self.mouse_profile = self.mouse_device.read_profile(self.profile_num)
            except Exception:
                self.mouse_profile = None
        self.dirty = False
        self.key_hitboxes, self.mouse_hitbox = load_hitboxes()
        self.key_buttons: list[KeyRegionItem | QPushButton] = []
        self._code_to_indices: dict[int, list[int]] = {}
        self._active_indices: set[int] = set()

        self._build_ui()
        self._refresh_all()
        self._start_key_monitor()

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

        # Device view: rendered SVG artwork with transparent click-overlay buttons
        # positioned on top of each physical key (see KEY_HITBOXES / MOUSE_CLICK_HITBOX).
        device_view = self._build_device_view()
        outer.addWidget(device_view, alignment=Qt.AlignHCenter)

        note = QLabel("Mausrad hoch/runter: im Treiber noch nicht konfigurierbar.")
        note.setStyleSheet("color: gray;")
        outer.addWidget(note, alignment=Qt.AlignHCenter)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        self._build_menu()

    def _build_device_view(self) -> QWidget:
        """Renders tartarus_v2.svg once to a background pixmap in a QGraphicsScene
        and lays a clickable KeyRegionItem over each physical key on top of it.
        Hitboxes are read directly off the SVG's own named elements (Key_1..
        Key_20, Circle, Cross, Scroll - see tartarus_v2.svg), preferring the
        exact local geometry + full transform (tartarus_svg.SvgDocument, for
        <rect>/<circle>/<ellipse> elements with a preserved Inkscape transform)
        and falling back to QSvgRenderer.boundsOnElement()/transformForElement()
        (axis-aligned) for plain <path> elements. self.key_hitboxes
        (tartarus_layout.py / tartarus_hitboxes.json) is only a last-resort
        fallback for elements the SVG doesn't (yet) name at all. Falls back to
        the plain 5x5 grid entirely if the SVG can't be loaded at all."""
        renderer = QSvgRenderer(str(SVG_PATH))
        if not renderer.isValid():
            return self._build_fallback_grid()

        try:
            svg_doc = SvgDocument(SVG_PATH)
        except ET.ParseError:
            svg_doc = None

        scale = DEVICE_VIEW_WIDTH / SVG_VIEWBOX[0]
        view_w, view_h = DEVICE_VIEW_WIDTH, round(SVG_VIEWBOX[1] * scale)

        pixmap = QPixmap(view_w, view_h)
        pixmap.fill(Qt.white)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()

        # Kept as an attribute (not just a local) - QGraphicsView.setScene() does not
        # take Python-visible ownership, so a scene with no surviving Python reference
        # gets garbage-collected out from under the view, deleting all its items too.
        self._device_scene = QGraphicsScene(0, 0, view_w, view_h)
        scene = self._device_scene
        scene.addPixmap(pixmap)

        display_scale: Matrix = (scale, 0, 0, scale, 0, 0)

        def make_region(element_id: str, fallback_box, shape: str = "rect") -> KeyRegionItem:
            geometry = svg_doc.local_geometry(element_id) if svg_doc else None
            if geometry is not None:
                shape_kind, (x, y, w, h), matrix = geometry
                local_rect = QRectF(x, y, w, h)
                final_matrix = compose(display_scale, matrix)
            elif renderer.elementExists(element_id):
                bounds = renderer.transformForElement(element_id).mapRect(
                    renderer.boundsOnElement(element_id))
                w, h = bounds.width(), bounds.height()
                local_rect = QRectF(-w / 2, -h / 2, w, h)
                local_matrix = (1, 0, 0, 1, bounds.center().x(), bounds.center().y())
                final_matrix = compose(display_scale, local_matrix)
                shape_kind = shape
            else:
                cx, cy, w, h, angle = fallback_box
                rad = math.radians(angle)
                cos_a, sin_a = math.cos(rad), math.sin(rad)
                local_rect = QRectF(-w / 2, -h / 2, w, h)
                local_matrix = (cos_a, sin_a, -sin_a, cos_a, cx, cy)
                final_matrix = compose(display_scale, local_matrix)
                shape_kind = shape
            item = KeyRegionItem(local_rect, QTransform(*final_matrix), shape=shape_kind)
            scene.addItem(item)
            return item

        self.key_buttons = []
        for i in range(20):
            item = make_region(f"Key_{i + 1}", self.key_hitboxes[i])
            item.clicked.connect(lambda checked=False, idx=i: self._edit_key(idx))
            self.key_buttons.append(item)

        circle_item = make_region("Circle", self.key_hitboxes[20], shape="ellipse")
        circle_item.clicked.connect(lambda: self._edit_key(20))
        self.key_buttons.append(circle_item)

        # The 4-way thumb rocker is one physical part / one SVG shape, but four
        # independent binds (RZKEY_THMB_U/R/D/L) - clicking it opens a small
        # chooser instead of directly editing a single physical key.
        cross_item = make_region("Cross", self.key_hitboxes[21], shape="ellipse")
        cross_item.clicked.connect(self._edit_cross)
        self.key_buttons.extend([cross_item] * 4)

        self.wheel_click_btn = make_region("Scroll", self.mouse_hitbox)
        self.wheel_click_btn.clicked.connect(self._edit_wheel_click)
        self.wheel_click_btn.setEnabled(self.mouse_profile is not None)

        view = QGraphicsView(scene)
        view.setFixedSize(view_w + 2, view_h + 2)
        view.setRenderHint(QPainter.Antialiasing)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setFrameShape(QFrame.NoFrame)
        return view

    def _build_fallback_grid(self) -> QWidget:
        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setSpacing(8)
        self.key_buttons = []
        for i, label in enumerate(KEY_LABELS):
            btn = QPushButton()
            btn.setMinimumSize(140, 70)
            btn.clicked.connect(lambda checked=False, idx=i: self._edit_key(idx))
            self.key_buttons.append(btn)
            grid.addWidget(btn, i // 5, i % 5)

        self.wheel_click_btn = QPushButton("Mausrad-Klick")
        self.wheel_click_btn.setMinimumSize(140, 70)
        self.wheel_click_btn.clicked.connect(self._edit_wheel_click)
        self.wheel_click_btn.setEnabled(self.mouse_profile is not None)
        grid.addWidget(self.wheel_click_btn, len(KEY_LABELS) // 5 + 1, 0)

        return grid_widget

    def _build_menu(self) -> None:
        menu = self.menuBar().addMenu("&Datei")

        save_json = menu.addAction("Profil speichern (JSON)…")
        save_json.triggered.connect(self._save_json)

        load_json = menu.addAction("Profil laden (JSON)…")
        load_json.triggered.connect(self._load_json)

        menu.addSeparator()

        save_raw = menu.addAction("Profil speichern (.rz, natives 3-Byte-Format)…")
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

        self._btn_base_state = []
        for i, btn in enumerate(self.key_buttons):
            bind = self.profile.get_physical(i)
            state = STATE_UNSET if bind.type == BindType.NOP else STATE_SET
            self._btn_base_state.append(state)
            btn.setToolTip(f"{KEY_LABELS[i]}: {bind.describe()}")
            apply_state(btn, state)

        if self.mouse_profile is not None:
            click = self.mouse_profile.click
            desc = click.describe() if click.type != BindType.NOP else "Mittelklick (Standard)"
            self.wheel_click_btn.setToolTip(f"Mausrad-Klick: {desc}")
            apply_state(self.wheel_click_btn, STATE_UNSET if click.type == BindType.NOP else STATE_SET)

        self._active_indices.clear()
        self._rebuild_key_event_map()
        self._update_title()

    def _rebuild_key_event_map(self) -> None:
        """Reverse-maps each physical key's current bind to the evdev keycode
        the driver will report for it, so a live EV_KEY event can be traced
        back to the physical button that caused it. Only KEY and MACRO binds
        produce an observable evdev event (see KeyMonitorThread)."""
        self._code_to_indices = {}
        for i in range(len(KEY_LABELS)):
            bind = self.profile.get_physical(i)
            if bind.type == BindType.KEY:
                code = bind.data
            elif bind.type == BindType.MACRO:
                code = bind.data + MACRO_KEYCODE_OFFSET
            else:
                continue
            self._code_to_indices.setdefault(code, []).append(i)

    def _update_title(self) -> None:
        star = " *" if self.dirty else ""
        self.setWindowTitle(f"Tartarus V2 Configurator {VERSION} \u2014 Profil {self.profile_num}{star}")

    def _mark_dirty(self) -> None:
        self.dirty = True
        self._update_title()

    # -- Live key-press highlighting --
    def _start_key_monitor(self) -> None:
        self.key_monitor: KeyMonitorThread | None = None

        if evdev is None:
            self.statusBar().showMessage(
                "Live-Tastenanzeige deaktiviert: 'python-evdev' ist nicht installiert "
                "(pip install evdev).", 6000)
            return

        event_path = self.device.find_event_device()
        if event_path is None:
            self.statusBar().showMessage(
                "Live-Tastenanzeige deaktiviert: Eventgerät für die Tartarus nicht gefunden.", 6000)
            return

        self.key_monitor = KeyMonitorThread(event_path, self)
        self.key_monitor.key_event.connect(self._on_physical_key_event)
        self.key_monitor.start()

    def _on_physical_key_event(self, code: int, pressed: bool) -> None:
        for index in self._code_to_indices.get(code, []):
            self._set_key_highlight(index, pressed)

    def _set_key_highlight(self, index: int, active: bool) -> None:
        if active:
            self._active_indices.add(index)
        else:
            self._active_indices.discard(index)
        state = STATE_ACTIVE if active else self._btn_base_state[index]
        apply_state(self.key_buttons[index], state)

    # -- Actions --
    def _edit_key(self, index: int) -> None:
        current = self.profile.get_physical(index)
        dlg = KeyEditDialog(self, KEY_LABELS[index], current, self.device.profile_count)
        if dlg.exec() == QDialog.Accepted and dlg.result_bind is not None:
            self.profile.set_physical(index, dlg.result_bind)
            self._mark_dirty()
            self._refresh_all()

    def _edit_cross(self) -> None:
        """The 4-way thumb rocker is one physical part (and one SVG shape - see
        'Cross' in tartarus_v2.svg), but four independent binds. Show a small
        chooser instead of guessing which direction a click meant."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Steuerkreuz belegen")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Welche Richtung?"))

        grid = QGridLayout()
        # index into KEY_LABELS, label, (grid col, grid row) - arranged like the physical d-pad
        directions = [
            (21, "Oben", 1, 0),
            (24, "Links", 0, 1),
            (22, "Rechts", 2, 1),
            (23, "Unten", 1, 2),
        ]
        for idx, label, col, row in directions:
            btn = QPushButton(label)
            btn.clicked.connect(lambda checked=False, i=idx: self._pick_cross_direction(dlg, i))
            grid.addWidget(btn, row, col)
        layout.addLayout(grid)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dlg.reject)
        layout.addWidget(buttons)

        dlg.exec()

    def _pick_cross_direction(self, chooser: QDialog, index: int) -> None:
        chooser.accept()
        self._edit_key(index)

    def _edit_wheel_click(self) -> None:
        if self.mouse_profile is None:
            return
        current = self.mouse_profile.click
        dlg = KeyEditDialog(self, "Mausrad-Klick", current, self.device.profile_count,
                             bind_types=MOUSE_CLICK_BIND_TYPES)
        if dlg.exec() == QDialog.Accepted and dlg.result_bind is not None:
            self.mouse_profile.click = dlg.result_bind
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
            if self.mouse_device is not None:
                self.mouse_profile = self.mouse_device.read_profile(new_num)
        except Exception as e:
            QMessageBox.critical(self, "Lesefehler", str(e))
            return

        self.profile_num = new_num
        self.dirty = False
        self._refresh_all()

    def _write_to_device(self) -> None:
        try:
            self.device.write_profile(self.profile, self.profile_num)
            if self.mouse_device is not None and self.mouse_profile is not None:
                self.mouse_device.write_profile(self.mouse_profile, self.profile_num)
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
        if self.mouse_device is not None:
            self.mouse_profile = self.mouse_device.read_profile(self.profile_num)
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

        if self.key_monitor is not None:
            self.key_monitor.stop()
            self.key_monitor.wait(1000)

        event.accept()


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.resize(900, 600)
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
