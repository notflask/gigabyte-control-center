"""gbcc-gui: tray icon, quick popup and full window. Talks to gbccd over its socket.

Colors come from the system Qt theme (palette); nothing here hard-codes them
except the keyboard color presets and the app icon.
"""

import html
import os
import sys
import time

from PySide6.QtCore import QByteArray, QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QCursor, QGuiApplication, QIcon, QPainter, QPalette, QPen, QPixmap
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QColorDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QMainWindow, QMenu, QPushButton, QRadioButton, QSlider, QSpinBox, QSystemTrayIcon,
    QToolButton, QVBoxLayout, QWidget,
)

from .client import ClientError, request
from .daemon import curve_percent

ICON_PATH = os.path.join(os.path.dirname(__file__), "data", "gbcc.svg")
POPUP_TITLE = "Gigabyte Control Center Quick"  # window rules match on this title
PROFILE_LABELS = {
    "quiet": "Quiet",
    "power_saving": "Power saving",
    "entertainment": "Entertainment",
    "performance": "Performance",
}
PRESET_COLORS = ["#ff0000", "#ff6000", "#ffff00", "#00ff00", "#00ffff", "#0000ff", "#8000ff", "#ff00ff", "#ffffff"]
FAN_MODES = [("auto", "Auto"), ("max", "Max"), ("fixed", "Fixed"), ("curve", "Curve")]
FAN_MODE_LABELS = {"auto": "Auto (firmware)", "max": "Max", "fixed": "Fixed", "curve": "Custom curve"}
CURVE_POINTS = 5
SPARK = " ▁▂▃▄▅▆▇█"


def app_icon(color=None):
    """The fan icon in one color: by default the theme's text color, so it fits
    light and dark panels the way symbolic tray icons do."""
    if color is None:
        color = QApplication.palette().windowText().color().name()
    with open(ICON_PATH) as f:
        svg = QByteArray(f.read().replace("#000000", color).encode())
    renderer = QSvgRenderer(svg)
    icon = QIcon()
    for size in (16, 22, 24, 32, 48, 64, 128, 256):
        pix = QPixmap(size, size)
        pix.fill(Qt.transparent)
        p = QPainter(pix)
        renderer.render(p)
        p.end()
        icon.addPixmap(pix)
    return icon


def color_dot(color, size=16):
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(color))
    p.setPen(Qt.NoPen)
    p.drawEllipse(1, 1, size - 2, size - 2)
    p.end()
    return QIcon(pix)


def readings_text(st):
    return "   ".join(f"{n} {f['temp']} °C · {f['duty']}%" for n, f in zip(("CPU", "GPU"), st["fans"]))


def tooltip_markup(st):
    """Tray tooltip as Pango markup (Waybar renders the SNI tooltip title as markup)."""
    s = st["settings"]
    e = html.escape
    lines = ["<b>Gigabyte Control Center</b>"]
    lines.append(f"Mode: <b>{e(PROFILE_LABELS.get(s['profile'], 'firmware default'))}</b>")

    kbd = st["keyboard"]
    if kbd:
        if kbd["brightness"] > 0:
            pct = round(kbd["brightness"] * 100 / kbd["max_brightness"])
            lines.append(f"Keyboard: <span foreground='{e(kbd['color'])}'>⬤</span> {e(kbd['color'])} · {pct}%")
        else:
            lines.append("Keyboard: off")

    fan = s["fan"]
    mode_label = FAN_MODE_LABELS[fan["mode"]]
    if fan["mode"] == "fixed":
        mode_label += f" {fan['fixed']}%"
    lines.append(f"Fans: <b>{e(mode_label)}</b>")
    if fan["mode"] == "curve":
        curve = fan["curve"]
        spark = "".join(SPARK[round(curve_percent(curve, t) / 100 * 8)] for t in range(20, 101, 5))
        lines.append(f"<tt>20° {spark} 100°</tt>")
        lines.append("<small>" + e("  ".join(f"{t}°→{p}%" for t, p in curve)) + "</small>")

    lines.append(e(readings_text(st)))
    if st["battery"]:
        b = st["battery"]
        lines.append(e(f"Battery {b['capacity']}% {b['status'].lower()}"))
    return "\n".join(lines)


class Blocked:
    """Temporarily block widget signals while we update them from daemon state."""

    def __init__(self, *widgets):
        self.widgets = widgets

    def __enter__(self):
        for w in self.widgets:
            w.blockSignals(True)

    def __exit__(self, *_):
        for w in self.widgets:
            w.blockSignals(False)


class Backend(QObject):
    """Polls the daemon and sends commands; every view listens to its signals."""

    # status dict, and whether the settings changed (controls need redrawing)
    updated = Signal(object, bool)
    failed = Signal(str)
    disconnected = Signal(str)

    def __init__(self):
        super().__init__()
        self.status = None
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)

    def _publish(self, st, force):
        changed = force or self.status is None or st["settings"] != self.status["settings"]
        self.status = st
        self.updated.emit(st, changed)

    def refresh(self, force=False):
        try:
            self._publish(request("status"), force)
        except ClientError as e:
            self.disconnected.emit(str(e))

    def send(self, cmd, **params):
        try:
            self._publish(request(cmd, **params), True)
        except ClientError as e:
            self.refresh(force=True)
            self.failed.emit(str(e))

    def kbd_level(self, pct):
        return round(pct * self.status["keyboard"]["max_brightness"] / 100)


class CurvePreview(QWidget):
    """Draws the fan curve and marks the current CPU/GPU temperatures."""

    def __init__(self, compact=False):
        super().__init__()
        self.curve = []
        self.temps = []
        self.compact = compact
        self.setMinimumHeight(64 if compact else 120)

    def set_data(self, curve, temps):
        self.curve, self.temps = curve, temps
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        left = 4 if self.compact else 24
        bottom = 4 if self.compact else 24
        w, h = self.width(), self.height()

        def pt(temp, pct):
            x = left + (temp - 20) / 80 * (w - left - 4)
            y = 4 + (1 - pct / 100) * (h - bottom - 8)
            return QPointF(x, y)

        p.setPen(QPen(pal.mid().color(), 1))
        p.drawRoundedRect(QRectF(left, 4, w - left - 4, h - bottom - 8), 4, 4)
        if not self.compact:
            p.setPen(pal.text().color())
            for t in (20, 40, 60, 80, 100):
                p.drawText(pt(t, 0) + QPointF(-8 if t < 100 else -26, 16), f"{t}°")
            p.drawText(QPointF(0, 14), "%")
        if self.curve:
            pts = [pt(20, self.curve[0][1])] + [pt(t, c) for t, c in self.curve] + [pt(100, 100)]
            p.setPen(QPen(pal.highlight().color(), 2))
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
            if not self.compact:
                for q in pts[1:-1]:
                    p.drawEllipse(q, 3, 3)
        # CPU in the theme's "visited link" color, GPU in placeholder gray
        for temp, color in zip(self.temps, (pal.linkVisited().color(), pal.placeholderText().color())):
            p.setPen(QPen(color, 1, Qt.DashLine))
            p.drawLine(pt(temp, 100), pt(temp, 0))


def preset_row(backend, size):
    row = QHBoxLayout()
    row.setSpacing(6)
    for c in PRESET_COLORS:
        b = QPushButton()
        b.setFixedSize(size, size)
        b.setStyleSheet(f"background:{c}; border:1px solid palette(mid); border-radius:{size // 2}px;")
        b.setToolTip(c)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda _=False, col=c: backend.send("keyboard", color=col))
        row.addWidget(b)
    return row


class MainWindow(QMainWindow):
    def __init__(self, backend):
        super().__init__()
        self.setWindowTitle("Gigabyte Control Center")
        self.setWindowIcon(app_icon())
        self.backend = backend
        self.quitting = False
        self.keep_running = False  # set when a tray icon keeps the app alive

        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        self.readings = QLabel("Connecting…")
        self.readings.setTextFormat(Qt.RichText)
        layout.addWidget(self.readings)

        # --- power mode ---
        box = QGroupBox("Power mode")
        row = QHBoxLayout(box)
        self.profile_group = QButtonGroup(self)
        self.profile_buttons = {}
        for name, label in PROFILE_LABELS.items():
            b = QPushButton(label)
            b.setCheckable(True)
            b.setMinimumHeight(40)
            self.profile_group.addButton(b)
            self.profile_buttons[name] = b
            b.clicked.connect(lambda _=False, n=name: backend.send("profile", name=n))
            row.addWidget(b)
        layout.addWidget(box)

        # --- keyboard ---
        box = QGroupBox("Keyboard backlight")
        grid = QGridLayout(box)
        self.kbd_on = QCheckBox("On")
        self.kbd_on.toggled.connect(self.on_kbd_toggled)
        grid.addWidget(self.kbd_on, 0, 0)
        self.kbd_slider = QSlider(Qt.Horizontal)
        self.kbd_slider.setRange(0, 100)
        self.kbd_slider.sliderReleased.connect(
            lambda: backend.send("keyboard", brightness=backend.kbd_level(self.kbd_slider.value())))
        grid.addWidget(QLabel("Brightness"), 0, 1)
        grid.addWidget(self.kbd_slider, 0, 2, 1, 2)
        presets = preset_row(backend, 28)
        self.kbd_custom = QPushButton("Custom…")
        self.kbd_custom.clicked.connect(self.on_kbd_custom)
        presets.addWidget(self.kbd_custom)
        presets.addStretch()
        grid.addLayout(presets, 1, 0, 1, 4)
        layout.addWidget(box)

        # --- fans ---
        box = QGroupBox("Fans")
        fl = QVBoxLayout(box)
        row = QHBoxLayout()
        self.fan_group = QButtonGroup(self)
        self.fan_buttons = {}
        for mode, _ in FAN_MODES:
            b = QRadioButton(FAN_MODE_LABELS[mode])
            self.fan_group.addButton(b)
            self.fan_buttons[mode] = b
            b.clicked.connect(lambda _=False, m=mode: backend.send("fan", mode=m))
            row.addWidget(b)
        fl.addLayout(row)

        self.fixed_row = QWidget()
        r = QHBoxLayout(self.fixed_row)
        r.setContentsMargins(0, 0, 0, 0)
        self.fixed_slider = QSlider(Qt.Horizontal)
        self.fixed_slider.setRange(0, 100)
        self.fixed_label = QLabel()
        self.fixed_slider.valueChanged.connect(lambda v: self.fixed_label.setText(f"{v}%"))
        self.fixed_slider.sliderReleased.connect(
            lambda: backend.send("fan", mode="fixed", fixed=self.fixed_slider.value()))
        r.addWidget(QLabel("Speed"))
        r.addWidget(self.fixed_slider)
        r.addWidget(self.fixed_label)
        fl.addWidget(self.fixed_row)

        self.curve_row = QWidget()
        cl = QVBoxLayout(self.curve_row)
        cl.setContentsMargins(0, 0, 0, 0)
        self.curve_preview = CurvePreview()
        cl.addWidget(self.curve_preview)
        pts = QGridLayout()
        pts.addWidget(QLabel("Temp °C"), 0, 0)
        pts.addWidget(QLabel("Fan %"), 1, 0)
        self.curve_spins = []
        for i in range(CURVE_POINTS):
            t, pc = QSpinBox(), QSpinBox()
            t.setRange(20, 90)
            pc.setRange(0, 100)
            for s in (t, pc):
                s.valueChanged.connect(self.on_curve_edited)
            pts.addWidget(t, 0, i + 1)
            pts.addWidget(pc, 1, i + 1)
            self.curve_spins.append((t, pc))
        self.curve_apply = QPushButton("Apply curve")
        self.curve_apply.clicked.connect(lambda: backend.send("fan", mode="curve", curve=self.edited_curve()))
        pts.addWidget(self.curve_apply, 0, CURVE_POINTS + 1, 2, 1)
        cl.addLayout(pts)
        hint = QLabel("Fans always go to 100% at 90 °C. Below 10% the fan stops; 10–19% becomes 20%.")
        hint.setForegroundRole(QPalette.PlaceholderText)
        cl.addWidget(hint)
        fl.addWidget(self.curve_row)
        layout.addWidget(box)

        # --- battery ---
        self.battery_box = QGroupBox("Battery")
        bl = QHBoxLayout(self.battery_box)
        self.limit_spin = QSpinBox()
        self.limit_spin.setRange(50, 100)
        self.limit_spin.setSuffix(" %")
        limit_apply = QPushButton("Set")
        limit_apply.clicked.connect(lambda: backend.send("charge_limit", percent=self.limit_spin.value()))
        bl.addWidget(QLabel("Stop charging at"))
        bl.addWidget(self.limit_spin)
        bl.addWidget(limit_apply)
        bl.addStretch()
        layout.addWidget(self.battery_box)
        self.battery_box.hide()

        self.error = QLabel()
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        layout.addStretch()

        self.curve_dirty = False
        backend.updated.connect(self.apply_status)
        backend.failed.connect(lambda msg: self.error.setText(f"⚠ {msg}"))
        backend.disconnected.connect(self.on_disconnected)

    def on_disconnected(self, msg):
        self.readings.setText("<b>Not connected</b>")
        self.error.setText(f"⚠ {msg}")

    def apply_status(self, st, changed):
        s = st["settings"]
        self.error.clear()
        parts = [f"<b>{n}</b> {f['temp']} °C · fan {f['duty']}%" for n, f in zip(("CPU", "GPU"), st["fans"])]
        if st["battery"]:
            parts.append(f"<b>Battery</b> {st['battery']['capacity']}% {st['battery']['status'].lower()}")
        self.readings.setText("&nbsp;&nbsp;&nbsp;".join(parts))
        self.curve_preview.set_data(self.edited_curve() or s["fan"]["curve"], [f["temp"] for f in st["fans"]])
        if not changed:
            return

        for name, b in self.profile_buttons.items():
            b.setChecked(name == s["profile"])

        kbd = st["keyboard"]
        if kbd:
            pct = round(kbd["brightness"] * 100 / kbd["max_brightness"])
            with Blocked(self.kbd_on, self.kbd_slider):
                self.kbd_on.setChecked(kbd["brightness"] > 0)
                if pct > 0:
                    self.kbd_slider.setValue(pct)
            self.kbd_custom.setIcon(color_dot(kbd["color"]))

        fan = s["fan"]
        self.fan_buttons[fan["mode"]].setChecked(True)
        self.fixed_row.setVisible(fan["mode"] == "fixed")
        self.curve_row.setVisible(fan["mode"] == "curve")
        with Blocked(self.fixed_slider):
            self.fixed_slider.setValue(fan["fixed"])
        self.fixed_label.setText(f"{fan['fixed']}%")
        curve = fan["curve"][:CURVE_POINTS]
        while len(curve) < CURVE_POINTS:
            curve.append(curve[-1])
        with Blocked(*[w for pair in self.curve_spins for w in pair]):
            for (t, pc), (tv, pv) in zip(self.curve_spins, curve):
                t.setValue(tv)
                pc.setValue(pv)
        self.curve_dirty = False
        self.curve_apply.setEnabled(False)

        self.battery_box.setVisible(st["charge_limit_supported"])
        if st["charge_limit"]:
            self.limit_spin.setValue(st["charge_limit"])

    def on_kbd_toggled(self, on):
        pct = max(self.kbd_slider.value(), 10) if on else 0
        self.backend.send("keyboard", brightness=self.backend.kbd_level(pct))

    def on_kbd_custom(self):
        st = self.backend.status
        start = QColor(st["keyboard"]["color"]) if st and st["keyboard"] else QColor("white")
        c = QColorDialog.getColor(start, self, "Keyboard color")
        if c.isValid():
            self.backend.send("keyboard", color=c.name())

    def edited_curve(self):
        if not self.curve_dirty:
            return None
        return [list(p) for p in sorted({(t.value(), pc.value()) for t, pc in self.curve_spins})]

    def on_curve_edited(self):
        self.curve_dirty = True
        self.curve_apply.setEnabled(True)
        if self.backend.status:
            self.curve_preview.set_data(self.edited_curve(), [f["temp"] for f in self.backend.status["fans"]])

    def open(self):
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event):
        if self.keep_running and not self.quitting:
            event.ignore()
            self.hide()
        else:
            event.accept()


class QuickPopup(QWidget):
    """Compact panel opened from the tray icon. Works on any desktop: on X11 it
    opens next to the pointer; on Wayland the compositor places it (window rules
    can match the title POPUP_TITLE)."""

    def __init__(self, backend, open_full):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setWindowTitle(POPUP_TITLE)
        self.setWindowIcon(app_icon())
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFixedWidth(340)
        self.backend = backend
        self.hidden_at = 0.0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)

        # header
        head = QHBoxLayout()
        self.logo = logo = QLabel()
        logo.setPixmap(app_icon().pixmap(22, 22))
        title = QLabel("<b>Control Center</b>")
        more = QToolButton()
        more.setIcon(QIcon.fromTheme("configure", QIcon.fromTheme("preferences-system")))
        if more.icon().isNull():
            more.setText("⚙")
        more.setToolTip("Open full window")
        more.setAutoRaise(True)
        more.clicked.connect(lambda: (self.hide(), open_full()))
        head.addWidget(logo)
        head.addWidget(title)
        head.addStretch()
        head.addWidget(more)
        layout.addLayout(head)

        self.readings = QLabel("Connecting…")
        self.readings.setForegroundRole(QPalette.PlaceholderText)
        layout.addWidget(self.readings)

        # power mode, 2 × 2
        layout.addWidget(self.section("Power mode"))
        grid = QGridLayout()
        grid.setSpacing(6)
        self.profile_buttons = {}
        group = QButtonGroup(self)
        for i, (name, label) in enumerate(PROFILE_LABELS.items()):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setMinimumHeight(34)
            group.addButton(b)
            b.clicked.connect(lambda _=False, n=name: backend.send("profile", name=n))
            grid.addWidget(b, i // 2, i % 2)
            self.profile_buttons[name] = b
        layout.addLayout(grid)

        # keyboard
        layout.addWidget(self.section("Keyboard"))
        row = QHBoxLayout()
        self.kbd_on = QCheckBox("On")
        self.kbd_on.toggled.connect(self.on_kbd_toggled)
        self.kbd_slider = QSlider(Qt.Horizontal)
        self.kbd_slider.setRange(0, 100)
        self.kbd_slider.sliderReleased.connect(
            lambda: backend.send("keyboard", brightness=backend.kbd_level(self.kbd_slider.value())))
        row.addWidget(self.kbd_on)
        row.addWidget(self.kbd_slider)
        layout.addLayout(row)
        presets = preset_row(backend, 22)
        presets.addStretch()
        layout.addLayout(presets)

        # fans
        layout.addWidget(self.section("Fans"))
        row = QHBoxLayout()
        row.setSpacing(6)
        self.fan_buttons = {}
        group = QButtonGroup(self)
        for mode, label in FAN_MODES:
            b = QPushButton(label)
            b.setCheckable(True)
            group.addButton(b)
            b.clicked.connect(lambda _=False, m=mode: backend.send("fan", mode=m))
            row.addWidget(b)
            self.fan_buttons[mode] = b
        layout.addLayout(row)
        self.fixed_slider = QSlider(Qt.Horizontal)
        self.fixed_slider.setRange(0, 100)
        self.fixed_slider.sliderReleased.connect(
            lambda: backend.send("fan", mode="fixed", fixed=self.fixed_slider.value()))
        layout.addWidget(self.fixed_slider)
        self.curve = CurvePreview(compact=True)
        layout.addWidget(self.curve)

        self.footer = QLabel()
        self.footer.setForegroundRole(QPalette.PlaceholderText)
        self.footer.setWordWrap(True)
        layout.addWidget(self.footer)

        backend.updated.connect(self.apply_status)
        backend.failed.connect(lambda msg: self.footer.setText(f"⚠ {msg}"))
        backend.disconnected.connect(lambda msg: (self.readings.setText("Not connected"), self.footer.setText(f"⚠ {msg}")))

    @staticmethod
    def section(text):
        label = QLabel(text.upper())
        font = label.font()
        font.setPointSizeF(font.pointSizeF() * 0.8)
        font.setBold(True)
        label.setFont(font)
        label.setForegroundRole(QPalette.PlaceholderText)
        return label

    def apply_status(self, st, changed):
        s = st["settings"]
        self.readings.setText(readings_text(st))
        self.curve.set_data(s["fan"]["curve"], [f["temp"] for f in st["fans"]])
        b = st["battery"]
        self.footer.setText(f"Battery {b['capacity']}% · {b['status'].lower()}" if b else "")
        if not changed:
            return
        for name, btn in self.profile_buttons.items():
            btn.setChecked(name == s["profile"])
        kbd = st["keyboard"]
        if kbd:
            pct = round(kbd["brightness"] * 100 / kbd["max_brightness"])
            with Blocked(self.kbd_on, self.kbd_slider):
                self.kbd_on.setChecked(kbd["brightness"] > 0)
                if pct > 0:
                    self.kbd_slider.setValue(pct)
        fan = s["fan"]
        self.fan_buttons[fan["mode"]].setChecked(True)
        self.fixed_slider.setVisible(fan["mode"] == "fixed")
        self.curve.setVisible(fan["mode"] == "curve")
        with Blocked(self.fixed_slider):
            self.fixed_slider.setValue(fan["fixed"])
        self.adjustSize()

    def on_kbd_toggled(self, on):
        pct = max(self.kbd_slider.value(), 10) if on else 0
        self.backend.send("keyboard", brightness=self.backend.kbd_level(pct))

    # --- show / hide ---

    def toggle(self):
        # A click on the tray icon first takes focus away, which already hid us
        if self.isVisible() or time.monotonic() - self.hidden_at < 0.3:
            self.hide()
            return
        self.backend.refresh(force=True)
        self.adjustSize()
        if QGuiApplication.platformName() == "xcb":
            self.move(self.position_near(QCursor.pos()))
        self.show()
        self.raise_()
        self.activateWindow()

    def position_near(self, pos):
        screen = QGuiApplication.screenAt(pos) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = min(max(pos.x() - self.width() // 2, area.left() + 8), area.right() - self.width() - 8)
        y = pos.y() + 16 if pos.y() < area.center().y() else pos.y() - self.height() - 16
        y = min(max(y, area.top() + 8), area.bottom() - self.height() - 8)
        return QPoint(x, y)

    def changeEvent(self, event):
        if event.type() == event.Type.ActivationChange and not self.isActiveWindow() and self.isVisible():
            self.hide()
        super().changeEvent(event)

    def hideEvent(self, event):
        self.hidden_at = time.monotonic()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _):
        # Rounded panel in the theme's window color
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        p.setBrush(pal.window())
        p.setPen(QPen(pal.mid().color(), 1))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), 14, 14)


class Tray(QSystemTrayIcon):
    def __init__(self, app, backend, window, popup):
        super().__init__(app_icon(), app)
        self.backend = backend
        self.app = app
        self.popup = popup
        self.icon_color = QApplication.palette().windowText().color().name()
        menu = QMenu()
        group = QActionGroup(menu)
        self.profile_actions = {}
        for name, label in PROFILE_LABELS.items():
            a = QAction(label, menu, checkable=True)
            a.triggered.connect(lambda _=False, n=name: backend.send("profile", name=n))
            group.addAction(a)
            menu.addAction(a)
            self.profile_actions[name] = a
        menu.addSeparator()
        self.kbd_action = QAction("Keyboard light", menu, checkable=True)
        self.kbd_action.triggered.connect(self.on_kbd)
        menu.addAction(self.kbd_action)
        menu.addSeparator()
        menu.addAction("Open", window.open)
        menu.addAction("Quit", lambda: (setattr(window, "quitting", True), app.quit()))
        self.setContextMenu(menu)
        self.activated.connect(
            lambda reason: reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) and popup.toggle())
        backend.updated.connect(self.apply_status)
        backend.disconnected.connect(
            lambda _: self.setToolTip("<b>Gigabyte Control Center</b>\nNot connected to the service"))

    def on_kbd(self, on):
        st = self.backend.status
        if st and st["keyboard"]:
            self.backend.send("keyboard", brightness=st["keyboard"]["max_brightness"] if on else 0)

    def apply_status(self, st, _changed):
        # Light/dark switch (matugen + KGlobalSettings.notifyChange): recolor the fan
        color = QApplication.palette().windowText().color().name()
        if color != self.icon_color:
            self.icon_color = color
            icon = app_icon(color)
            self.setIcon(icon)
            self.app.setWindowIcon(icon)
            self.popup.logo.setPixmap(icon.pixmap(22, 22))
        markup = tooltip_markup(st)
        if markup != self.toolTip():
            self.setToolTip(markup)
        for name, a in self.profile_actions.items():
            a.setChecked(name == st["settings"]["profile"])
        if st["keyboard"]:
            self.kbd_action.setChecked(st["keyboard"]["brightness"] > 0)


def instance_name():
    return f"gigabyte-control-center-{os.getuid()}"


def forward_to_running_instance(command):
    """If the app already runs, hand it the command (show / popup) and return True."""
    sock = QLocalSocket()
    sock.connectToServer(instance_name())
    if not sock.waitForConnected(300):
        return False
    sock.write(command.encode())
    sock.waitForBytesWritten(300)
    sock.disconnectFromServer()
    return True


def main():
    args = sys.argv[1:]
    app = QApplication(sys.argv)
    app.setApplicationName("Gigabyte Control Center")
    app.setDesktopFileName("gigabyte-control-center")
    app.setWindowIcon(app_icon())

    command = "popup" if "--popup" in args else ("tray" if "--hidden" in args else "show")
    if forward_to_running_instance(command):
        return

    backend = Backend()
    window = MainWindow(backend)
    popup = QuickPopup(backend, window.open)
    tray = None
    if QSystemTrayIcon.isSystemTrayAvailable():
        tray = Tray(app, backend, window, popup)
        tray.show()
        window.keep_running = True
        app.setQuitOnLastWindowClosed(False)

    def handle(cmd):
        if cmd == "popup":
            popup.toggle()
        elif cmd == "show" or not tray:
            window.open()

    server = QLocalServer()
    QLocalServer.removeServer(instance_name())
    server.listen(instance_name())

    def on_connection():
        conn = server.nextPendingConnection()
        conn.waitForReadyRead(300)
        handle(bytes(conn.readAll()).decode(errors="ignore"))
        conn.deleteLater()

    server.newConnection.connect(on_connection)
    backend.refresh(force=True)
    handle(command)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
