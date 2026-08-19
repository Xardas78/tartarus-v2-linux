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
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt, QThread, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPainterPath, QPen, QPixmap, QTransform
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QGridLayout, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QSpinBox, QCheckBox, QDialog, QDialogButtonBox,
    QMessageBox, QFileDialog, QStatusBar, QFrame, QStackedWidget, QSizePolicy,
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

DEVICE_VIEW_WIDTH = 420   # device image display size; sidebars sit either side of it

# The stock product graphic, used as the device-view background instead of the
# traced line art (tartarus_v2.svg) - same aspect ratio as SVG_VIEWBOX (both
# come from the same source image), so overlay geometry computed from the SVG
# still lines up. device_bg.png is T2.png with its white background keyed out
# to transparent (see the alpha channel - regenerate by re-running the PIL
# snippet from that change if T2.png is ever replaced), so the scene's own
# background - which follows the app's palette/theme - shows through instead
# of a flat white box. Falls back to rendering the SVG itself if missing.
DEVICE_PHOTO_PATH = SVG_PATH.parent / "device_bg.png"

# Compass-style D-pad graphic (own artwork, hand-built to a similar layout as
# Razer Synapse's own D-PAD dialog - not a copy of it) shown in _edit_cross()'s
# direction chooser - see _build_dpad_widget(). Positions are read off
# dpad_icon.svg's own text/path coordinates directly (it has no element ids to
# look up via SvgDocument), in the SVG's 0-600 viewBox units:
#  - DPAD_REGIONS: clickable rect (cx, cy, w, h) per direction, covering both
#    the "Up"/"Down"/... label and its arrow petal.
#  - DPAD_INFO_POS: where the small "currently bound to" readout goes -
#    beneath the label for Oben/Links/Rechts, but above it for Unten (that
#    label sits at y=555, 45px from the SVG's bottom edge - no room below).
DPAD_SVG_PATH = SVG_PATH.parent / "dpad_icon.svg"
DPAD_VIEWBOX = 600
DPAD_DISPLAY_SIZE = 200
DPAD_REGIONS = {
    "Oben": (300, 165, 140, 230),
    "Unten": (300, 440, 140, 230),
    "Links": (175, 309, 220, 70),
    "Rechts": (425, 309, 220, 70),
}
DPAD_INFO_POS = {
    "Oben": (300, 100),
    "Unten": (300, 510),
    "Links": (90, 335),
    "Rechts": (510, 335),
}

# Overlay fill/outline per key state (brush color, pen color, pen width),
# drawn over the key's own exact shape (see MarkerItem).
MARKER_UNSET = (QColor(255, 255, 255, 25), QColor(0, 0, 0, 90), 1)
MARKER_SET = (QColor(52, 152, 219, 70), QColor(41, 128, 185, 180), 1)
MARKER_ACTIVE = (QColor(46, 204, 113, 160), QColor(46, 204, 113, 255), 2)
MARKER_HOVER = (QColor(243, 156, 18, 110), QColor(230, 126, 0, 220), 2)

# Sidebar row (QPushButton) style per key state.
ROW_STYLE_UNSET = "text-align: left; padding: 4px 8px; color: palette(mid);"
ROW_STYLE_SET = "text-align: left; padding: 4px 8px; border: 1px solid #3498db;"
ROW_STYLE_ACTIVE = "text-align: left; padding: 4px 8px; border: 2px solid #2ecc71; background: rgba(46,204,113,60);"
ROW_HOVER_SUFFIX = "border: 2px solid #f39c12;"


class MarkerItem(QGraphicsObject):
    """A clickable overlay on the device view, shaped and positioned to match
    one physical key/Circle/Cross/wheel-click exactly (local_rect + the SVG
    element's own full local-to-scene transform - see _build_device_view()).
    Also emits `hovered` so the matching sidebar row can highlight itself
    (there's no leader line - the printed key numbers in the artwork plus this
    hover sync is how a row's row is identified). QGraphicsObject is Qt's own
    QObject+QGraphicsItem hybrid base (for exactly this - a graphics item that
    needs signals), so unlike QGraphicsRectItem it draws nothing on its own -
    paint()/boundingRect() below do that."""

    clicked = Signal()
    hovered = Signal(bool)

    def __init__(self, local_rect: QRectF, transform: QTransform, shape: str = "rect"):
        super().__init__()
        self._local_rect = local_rect
        self._shape_kind = shape  # "rect" or "ellipse"
        self._state = MARKER_UNSET  # the "real" state - restored after a hover ends
        self._hovering = False
        self._apply_visual(MARKER_UNSET)
        self.setTransform(transform)
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)

    def _apply_visual(self, state: tuple[QColor, QColor, int]) -> None:
        brush_color, pen_color, pen_width = state
        self._brush = QBrush(brush_color)
        self._pen = QPen(pen_color, pen_width)
        self.update()

    def set_state(self, state: tuple[QColor, QColor, int]) -> None:
        self._state = state
        if not self._hovering:
            self._apply_visual(state)

    def set_hover(self, hovering: bool) -> None:
        """Temporarily shows the hover color without disturbing the tracked
        "real" state (bind-configured / live-key-active) - restored once the
        hover ends. Called both for this marker's own hover and (via the
        sidebar row's hover signal - see _build_device_view()) when the
        matching row is hovered instead, so highlighting works both ways."""
        self._hovering = hovering
        self._apply_visual(MARKER_HOVER if hovering else self._state)

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

    def hoverEnterEvent(self, event) -> None:
        self.set_hover(True)
        self.hovered.emit(True)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:
        self.set_hover(False)
        self.hovered.emit(False)
        super().hoverLeaveEvent(event)


def apply_marker_state(item, state: tuple[QColor, QColor, int]) -> None:
    """Applies a MARKER_* tuple to either a MarkerItem or (fallback grid) a
    QPushButton, so callers don't need to care which one they got."""
    if isinstance(item, MarkerItem):
        item.set_state(state)
        return
    brush, pen, width = state
    item.setStyleSheet(
        f"background-color: rgba({brush.red()},{brush.green()},{brush.blue()},{brush.alpha()}); "
        f"border: {width}px solid rgba({pen.red()},{pen.green()},{pen.blue()},{pen.alpha()}); "
        "border-radius: 6px;"
    )


def apply_row_style(row: QPushButton, style: str) -> None:
    row.setStyleSheet(style)


class HoverButton(QPushButton):
    """A QPushButton that also announces mouse hover via a signal, so the
    matching MarkerItem on the device image can highlight itself too (see
    _build_device_view()) - plain QPushButton only offers hover as a :hover
    stylesheet pseudo-state, not something another widget can react to."""

    hovered = Signal(bool)

    def enterEvent(self, event) -> None:
        self.hovered.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.hovered.emit(False)
        super().leaveEvent(event)


class ScalingGraphicsView(QGraphicsView):
    """A QGraphicsView that keeps its whole scene (device image, markers,
    sidebars, leader lines - all one composite, see _build_device_view())
    fitted to the available space as the window is resized."""

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.scene() is not None:
            self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)


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

# Version of this GUI/backend tooling, kept in step with dkms.conf's
# PACKAGE_VERSION since both the driver (3-byte binds, mouse profile support)
# and the GUI (SVG device view) changed together in this release.
VERSION = "v0.2"

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
        self.key_buttons: list[MarkerItem | QPushButton] = []
        self.key_rows: list[QPushButton] = []
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

        # Device view: rendered SVG artwork, a small clickable marker per physical
        # key/Circle/Cross/wheel-click, and a sidebar either side listing each
        # bind's text (Piper-style) - see _build_device_view(). The whole thing
        # is one QGraphicsScene so it scales together as the window is resized.
        device_view = self._build_device_view()
        device_view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        outer.addWidget(device_view)

        note = QLabel("Mausrad hoch/runter: im Treiber noch nicht konfigurierbar.")
        note.setStyleSheet("color: gray;")
        outer.addWidget(note, alignment=Qt.AlignHCenter)

        self.setCentralWidget(central)
        self.setStatusBar(QStatusBar())

        self._build_menu()

    def _build_device_view(self) -> QWidget:
        """Renders tartarus_v2.svg once to a background pixmap, then lays out
        one composite QGraphicsScene: a clickable MarkerItem shaped and
        positioned to match each physical key/Circle/Cross/wheel-click exactly
        on the device image, plus a sidebar list either side showing that
        region's bind as text (hovering a key highlights its row - see
        _set_row_hover() - since there's no drawn line between them; the
        printed key numbers in the artwork are the association). Marker
        geometry is read directly off the SVG's own named elements (Key_1..
        Key_20, Circle, Cross, Scroll - see tartarus_v2.svg) via
        tartarus_svg.SvgDocument, falling back to
        QSvgRenderer.boundsOnElement()/transformForElement() (axis-aligned)
        and then to self.key_hitboxes (tartarus_layout.py /
        tartarus_hitboxes.json) for anything the SVG doesn't (yet) name. Being
        one scene (not a fixed-size image plus separate widgets) is what lets
        ScalingGraphicsView scale the whole composite together as the window
        is resized. Falls back to the plain 5x5 grid entirely if the SVG
        can't be loaded at all."""
        renderer = QSvgRenderer(str(SVG_PATH))
        if not renderer.isValid():
            return self._build_fallback_grid()

        try:
            svg_doc = SvgDocument(SVG_PATH)
        except ET.ParseError:
            svg_doc = None
        self._svg_doc = svg_doc  # reused by _edit_cross() to crop a preview image

        img_scale = DEVICE_VIEW_WIDTH / SVG_VIEWBOX[0]
        image_w, image_h = DEVICE_VIEW_WIDTH, SVG_VIEWBOX[1] * img_scale

        photo = QPixmap(str(DEVICE_PHOTO_PATH))
        if not photo.isNull():
            # Force the exact target size rather than scaledToWidth() preserving
            # the photo's own aspect - they're only equal to ~1e-4, and overlay
            # geometry below is laid out against (image_w, image_h) exactly.
            pixmap = photo.scaled(round(image_w), round(image_h),
                                   Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        else:
            pixmap = QPixmap(round(image_w), round(image_h))
            pixmap.fill(Qt.white)
            painter = QPainter(pixmap)
            renderer.render(painter)
            painter.end()

        def marker_geometry(element_id: str, fallback_box,
                             default_shape: str = "rect") -> tuple[QRectF, QTransform, str]:
            geometry = svg_doc.local_geometry(element_id) if svg_doc else None
            if geometry is not None:
                shape_kind, (x, y, w, h), matrix = geometry
                local_rect = QRectF(x, y, w, h)
                final_matrix = compose(img_transform, matrix)
            elif renderer.elementExists(element_id):
                bounds = renderer.transformForElement(element_id).mapRect(
                    renderer.boundsOnElement(element_id))
                w, h = bounds.width(), bounds.height()
                local_rect = QRectF(-w / 2, -h / 2, w, h)
                local_matrix = (1, 0, 0, 1, bounds.center().x(), bounds.center().y())
                final_matrix = compose(img_transform, local_matrix)
                shape_kind = default_shape
            else:
                cx, cy, w, h, angle = fallback_box
                rad = math.radians(angle)
                cos_a, sin_a = math.cos(rad), math.sin(rad)
                local_rect = QRectF(-w / 2, -h / 2, w, h)
                local_matrix = (cos_a, sin_a, -sin_a, cos_a, cx, cy)
                final_matrix = compose(img_transform, local_matrix)
                shape_kind = default_shape
            return local_rect, QTransform(*final_matrix), shape_kind

        # (element_id, KEY_LABELS index this region covers, sidebar row label).
        # The 4-way thumb rocker is one physical part / one SVG shape ("Cross")
        # but four independent binds (RZKEY_THMB_U/R/D/L) - each direction gets
        # its own sidebar row (like Razer's own Synapse D-PAD list) sharing that
        # one marker on the image; clicking the marker itself (ambiguous - one
        # shape, four possible targets) opens a small chooser instead - see
        # add_region()/_edit_cross() below.
        key_regions = [(f"Key_{i + 1}", (i,), f"{i + 1:02d}") for i in range(20)]
        key_regions.append(("Circle", (20,), "Circle"))
        key_regions.append(("Cross", (21,), "Oben"))
        key_regions.append(("Cross", (22,), "Rechts"))
        key_regions.append(("Cross", (23,), "Unten"))
        key_regions.append(("Cross", (24,), "Links"))
        left_regions, right_regions = key_regions[:13], key_regions[13:]

        # -- Scene layout geometry --
        sidebar_w, row_h, row_gap, side_gap, margin = 210, 30, 6, 30, 20
        image_x = sidebar_w + side_gap
        image_y = margin
        right_x = image_x + image_w + side_gap
        # Maps an element's own local/document coordinates straight to scene
        # coordinates - scale to display size, then shift by the image's
        # position in the scene (it doesn't sit at the scene origin, the left
        # sidebar does - see marker_geometry() above).
        img_transform: Matrix = (img_scale, 0, 0, img_scale, image_x, image_y)

        def column_height(n: int) -> float:
            return n * row_h + max(0, n - 1) * row_gap

        left_h = column_height(len(left_regions))
        right_h = column_height(len(right_regions) + 1)  # +1 for the wheel-click row
        left_start_y = image_y + (image_h - left_h) / 2
        right_start_y = image_y + (image_h - right_h) / 2

        scene_h = max(image_y + image_h, left_start_y + left_h, right_start_y + right_h) + margin
        scene_w = right_x + sidebar_w

        # Kept as an attribute (not just a local) - QGraphicsView.setScene() does not
        # take Python-visible ownership, so a scene with no surviving Python reference
        # gets garbage-collected out from under the view, deleting all its items too.
        self._device_scene = QGraphicsScene(0, 0, scene_w, scene_h)
        scene = self._device_scene
        scene.addPixmap(pixmap).setPos(image_x, image_y)

        def add_row(label: str, x: float, y: float) -> HoverButton:
            btn = HoverButton(label)
            btn.setFixedSize(sidebar_w, row_h)
            btn._region_label = label  # read back in _refresh_all() to rebuild "label: bind" text
            btn._base_style = ""
            scene.addWidget(btn).setPos(x, y)
            return btn

        # element_id -> its MarkerItem. Several rows can share one marker (the
        # Cross directions all sit on the same physical/SVG shape), in which
        # case add_region() below reuses the existing item instead of stacking
        # duplicate overlays on top of each other.
        created_markers: dict[str, MarkerItem] = {}

        def add_region(element_id: str, fallback_box, row_x: float, row_y: float,
                        label: str, shape: str = "rect") -> tuple[MarkerItem, HoverButton]:
            marker = created_markers.get(element_id)
            if marker is None:
                local_rect, transform, shape_kind = marker_geometry(element_id, fallback_box, shape)
                marker = MarkerItem(local_rect, transform, shape=shape_kind)
                scene.addItem(marker)
                created_markers[element_id] = marker
            row = add_row(label, row_x, row_y)
            # Hover syncs both ways: hovering the key on the image highlights its
            # row (all of them, if several share this marker), and hovering a row
            # highlights the key (no leader line draws the association, so this
            # is how the two sides visually connect).
            marker.hovered.connect(lambda on, r=row: self._set_row_hover(r, on))
            row.hovered.connect(lambda on, m=marker: m.set_hover(on))
            return marker, row

        self.key_buttons = [None] * len(KEY_LABELS)
        self.key_rows = [None] * len(KEY_LABELS)
        connected_markers: set[str] = set()

        for col_regions, start_y, col_right in (
                (left_regions, left_start_y, False), (right_regions, right_start_y, True)):
            for i, (element_id, indices, label) in enumerate(col_regions):
                y = start_y + i * (row_h + row_gap)
                x = right_x if col_right else 0
                idx = indices[0]
                fallback = self.key_hitboxes[idx]
                shape = "ellipse" if element_id in ("Circle", "Cross") else "rect"
                marker, row = add_region(element_id, fallback, x, y, label, shape)
                row._region_indices = indices

                # A row always edits its own single key directly. The marker on
                # the image is only wired once per element - for Cross that's a
                # click on the shared shape, ambiguous between 4 directions, so
                # it opens the chooser instead of guessing.
                row.clicked.connect(lambda checked=False, i=idx: self._edit_key(i))
                if element_id not in connected_markers:
                    connected_markers.add(element_id)
                    if element_id == "Cross":
                        marker.clicked.connect(self._edit_cross)
                    else:
                        marker.clicked.connect(lambda checked=False, i=idx: self._edit_key(i))

                self.key_buttons[idx] = marker
                self.key_rows[idx] = row

        wheel_y = right_start_y + column_height(len(right_regions))
        self.wheel_click_btn, self.wheel_click_row = add_region(
            "Scroll", self.mouse_hitbox, right_x, wheel_y, "Mausrad-Klick")
        self.wheel_click_btn.clicked.connect(self._edit_wheel_click)
        self.wheel_click_row.clicked.connect(self._edit_wheel_click)
        enabled = self.mouse_profile is not None
        self.wheel_click_btn.setEnabled(enabled)
        self.wheel_click_row.setEnabled(enabled)

        view = ScalingGraphicsView(scene)
        view.setRenderHint(QPainter.Antialiasing)
        view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        view.setFrameShape(QFrame.NoFrame)
        view.setMinimumSize(round(scene_w * 0.4), round(scene_h * 0.4))
        return view

    def _build_fallback_grid(self) -> QWidget:
        """Used only if tartarus_v2.svg itself fails to load. No sidebar here -
        each button is both the "marker" and the "row" (see _refresh_all()),
        showing its own label and bind text directly, like this GUI's original
        5x5 grid."""
        grid_widget = QWidget()
        grid = QGridLayout(grid_widget)
        grid.setSpacing(8)
        self.key_buttons = []
        self.key_rows = []
        for i, label in enumerate(KEY_LABELS):
            btn = QPushButton()
            btn.setMinimumSize(140, 70)
            btn.clicked.connect(lambda checked=False, idx=i: self._edit_key(idx))
            self.key_buttons.append(btn)
            self.key_rows.append(btn)
            grid.addWidget(btn, i // 5, i % 5)

        self.wheel_click_btn = QPushButton("Mausrad-Klick")
        self.wheel_click_btn.setMinimumSize(140, 70)
        self.wheel_click_btn.clicked.connect(self._edit_wheel_click)
        self.wheel_click_btn.setEnabled(self.mouse_profile is not None)
        self.wheel_click_row = self.wheel_click_btn
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
        # Several indices can share one marker (Cross: 4 directions, one shape -
        # see add_region() in _build_device_view()), so track that here for a
        # combined tooltip below rather than each index's setToolTip() clobbering
        # the last one written.
        marker_indices: dict[int, list[int]] = {}
        seen_rows: set[int] = set()
        for i, marker in enumerate(self.key_buttons):
            bind = self.profile.get_physical(i)
            state = MARKER_UNSET if bind.type == BindType.NOP else MARKER_SET
            self._btn_base_state.append(state)
            desc = bind.describe()
            apply_marker_state(marker, state)

            row = self.key_rows[i]
            if row is marker:
                # Fallback grid (SVG failed to load): one widget is both marker and row.
                row.setText(f"{KEY_LABELS[i]}\n{desc}")
                marker.setToolTip(row.text())
                continue
            marker_indices.setdefault(id(marker), []).append(i)
            if id(row) in seen_rows:
                continue
            seen_rows.add(id(row))
            row.setText(f"{row._region_label}: {desc}")
            row_style = ROW_STYLE_UNSET if state == MARKER_UNSET else ROW_STYLE_SET
            row._base_style = row_style
            apply_row_style(row, row_style)

        for idxs in marker_indices.values():
            marker = self.key_buttons[idxs[0]]
            if len(idxs) == 1:
                marker.setToolTip(f"{KEY_LABELS[idxs[0]]}: {self.profile.get_physical(idxs[0]).describe()}")
            else:
                n_set = sum(1 for j in idxs if self.profile.get_physical(j).type != BindType.NOP)
                marker.setToolTip(f"Steuerkreuz ({n_set}/{len(idxs)} belegt)")

        if self.mouse_profile is not None:
            click = self.mouse_profile.click
            desc = click.describe() if click.type != BindType.NOP else "Mittelklick (Standard)"
            tooltip = f"Mausrad-Klick: {desc}"
            state = MARKER_UNSET if click.type == BindType.NOP else MARKER_SET
            self.wheel_click_btn.setToolTip(tooltip)
            apply_marker_state(self.wheel_click_btn, state)
            if self.wheel_click_row is not self.wheel_click_btn:
                self.wheel_click_row.setText(f"Mausrad-Klick: {desc}")
                row_style = ROW_STYLE_UNSET if state == MARKER_UNSET else ROW_STYLE_SET
                self.wheel_click_row._base_style = row_style
                apply_row_style(self.wheel_click_row, row_style)

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
        state = MARKER_ACTIVE if active else self._btn_base_state[index]
        apply_marker_state(self.key_buttons[index], state)

        row = self.key_rows[index]
        if row is self.key_buttons[index]:
            return  # fallback grid: marker IS the row, already updated above
        style = ROW_STYLE_ACTIVE if active else getattr(row, "_base_style", "")
        apply_row_style(row, style)

    def _set_row_hover(self, row: QPushButton, hovering: bool) -> None:
        """Highlights a sidebar row while the mouse hovers its marker on the
        device image (or the row itself - HoverButton fires the same signal -
        which then also re-highlights the marker via MarkerItem.set_hover(),
        making the association visible from either side without a drawn
        line)."""
        base = getattr(row, "_base_style", "")
        apply_row_style(row, base + ROW_HOVER_SUFFIX if hovering else base)

    # -- Actions --
    def _edit_key(self, index: int) -> None:
        current = self.profile.get_physical(index)
        dlg = KeyEditDialog(self, KEY_LABELS[index], current, self.device.profile_count)
        if dlg.exec() == QDialog.Accepted and dlg.result_bind is not None:
            self.profile.set_physical(index, dlg.result_bind)
            self._mark_dirty()
            self._refresh_all()

    def _build_dpad_widget(self, dlg: QDialog) -> QWidget | None:
        """Renders dpad_icon.svg (a compass-style D-pad graphic - own artwork,
        not Razer's) and lays a transparent clickable button over each of its
        four petals plus its label (DPAD_REGIONS), with a small "currently
        bound to" readout next to each label (DPAD_INFO_POS). Returns None if
        the SVG can't be loaded, so _edit_cross() can fall back to plain
        labeled buttons."""
        renderer = QSvgRenderer(str(DPAD_SVG_PATH))
        if not renderer.isValid():
            return None

        pixmap = QPixmap(DPAD_DISPLAY_SIZE, DPAD_DISPLAY_SIZE)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        renderer.render(painter)
        painter.end()

        container = QWidget()
        container.setFixedSize(DPAD_DISPLAY_SIZE, DPAD_DISPLAY_SIZE)
        bg_label = QLabel(container)
        bg_label.setPixmap(pixmap)
        bg_label.setGeometry(0, 0, DPAD_DISPLAY_SIZE, DPAD_DISPLAY_SIZE)

        scale = DPAD_DISPLAY_SIZE / DPAD_VIEWBOX
        directions = [(21, "Oben"), (24, "Links"), (22, "Rechts"), (23, "Unten")]
        for idx, label in directions:
            cx, cy, w, h = DPAD_REGIONS[label]
            btn = QPushButton(container)
            btn.setGeometry(round((cx - w / 2) * scale), round((cy - h / 2) * scale),
                             round(w * scale), round(h * scale))
            btn.setToolTip(label)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet(
                "QPushButton { background: transparent; border: none; }"
                "QPushButton:hover { background: rgba(255,255,255,30); border-radius: 8px; }")
            btn.clicked.connect(lambda checked=False, i=idx: self._pick_cross_direction(dlg, i))

            info = QLabel(self.profile.get_physical(idx).describe(), container)
            info.setStyleSheet("color: #3498db; font-size: 11px;")
            info.setAlignment(Qt.AlignCenter)
            info.setAttribute(Qt.WA_TransparentForMouseEvents)  # clicks pass through to btn
            info_w = 90
            ix, iy = DPAD_INFO_POS[label]
            info.setGeometry(round(ix * scale - info_w / 2), round(iy * scale - 8), info_w, 16)
        return container

    def _edit_cross(self) -> None:
        """The 4-way thumb rocker is one physical part (and one SVG shape - see
        'Cross' in tartarus_v2.svg), but four independent binds. Show a small
        chooser instead of guessing which direction a click meant."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Steuerkreuz belegen")
        layout = QVBoxLayout(dlg)
        layout.addWidget(QLabel("Welche Richtung?"))

        dpad = self._build_dpad_widget(dlg)
        if dpad is not None:
            layout.addWidget(dpad, alignment=Qt.AlignCenter)
        else:
            # Fallback: plain labeled buttons in a cross layout, no graphic.
            grid = QGridLayout()
            directions = [
                (21, "Oben", 1, 0), (24, "Links", 0, 1),
                (22, "Rechts", 2, 1), (23, "Unten", 1, 2),
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
