"""gbcc-gui: Qt window + tray icon. Talks to gbccd over its socket."""

import os
import sys

from PySide6.QtCore import QPointF, Qt, QTimer
from PySide6.QtGui import QAction, QActionGroup, QColor, QIcon, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QColorDialog, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QMainWindow, QMenu, QPushButton, QRadioButton, QSlider, QSpinBox, QSystemTrayIcon,
    QVBoxLayout, QWidget,
)

from .client import ClientError, request

ICON_PATH = os.path.join(os.path.dirname(__file__), "data", "gbcc.svg")
PROFILE_LABELS = {
    "quiet": "Quiet",
    "power_saving": "Power saving",
    "entertainment": "Entertainment",
    "performance": "Performance",
}
PRESET_COLORS = ["#ff0000", "#ff6000", "#ffff00", "#00ff00", "#00ffff", "#0000ff", "#8000ff", "#ff00ff", "#ffffff"]
FAN_MODES = [("auto", "Auto (firmware)"), ("max", "Max"), ("fixed", "Fixed"), ("curve", "Custom curve")]
CURVE_POINTS = 5


class CurvePreview(QWidget):
    """Draws the fan curve and marks the current CPU/GPU temperatures."""

    def __init__(self):
        super().__init__()
        self.curve = []
        self.temps = []
        self.setMinimumHeight(120)

    def set_data(self, curve, temps):
        self.curve, self.temps = curve, temps
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h, m = self.width(), self.height(), 24
        pal = self.palette()
        p.setPen(QPen(pal.mid().color(), 1))
        p.drawRect(m, 4, w - m - 4, h - m - 4)

        def pt(temp, pct):
            x = m + (temp - 20) / 80 * (w - m - 4)
            y = 4 + (1 - pct / 100) * (h - m - 4)
            return QPointF(x, y)

        p.setPen(pal.text().color())
        for t in (20, 40, 60, 80, 100):
            p.drawText(pt(t, 0) + QPointF(-8 if t < 100 else -26, 16), f"{t}°")
        p.drawText(QPointF(0, 14), "%")
        if self.curve:
            pts = [pt(20, self.curve[0][1])] + [pt(t, c) for t, c in self.curve] + [pt(100, 100)]
            p.setPen(QPen(QColor("#ff7a1a"), 2))
            for a, b in zip(pts, pts[1:]):
                p.drawLine(a, b)
            for q in pts[1:-1]:
                p.drawEllipse(q, 3, 3)
        for temp, color in zip(self.temps, ("#4aa3ff", "#4fd17a")):
            p.setPen(QPen(QColor(color), 1, Qt.DashLine))
            top, bottom = pt(temp, 100), pt(temp, 0)
            p.drawLine(top, bottom)


class MainWindow(QMainWindow):
    def __init__(self, tray_available):
        super().__init__()
        self.setWindowTitle("Gigabyte Control Center")
        self.setWindowIcon(QIcon(ICON_PATH))
        self.tray_available = tray_available
        self.status = None
        self.quitting = False

        root = QWidget()
        layout = QVBoxLayout(root)
        self.setCentralWidget(root)

        # --- live readings ---
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
            b.clicked.connect(lambda _=False, n=name: self.send("profile", name=n))
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
        self.kbd_slider.sliderReleased.connect(self.on_kbd_brightness)
        grid.addWidget(QLabel("Brightness"), 0, 1)
        grid.addWidget(self.kbd_slider, 0, 2, 1, 2)
        presets = QHBoxLayout()
        for c in PRESET_COLORS:
            b = QPushButton()
            b.setFixedSize(28, 28)
            b.setStyleSheet(f"background:{c}; border:1px solid #555; border-radius:4px;")
            b.setToolTip(c)
            b.clicked.connect(lambda _=False, col=c: self.send("keyboard", color=col))
            presets.addWidget(b)
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
        for mode, label in FAN_MODES:
            b = QRadioButton(label)
            self.fan_group.addButton(b)
            self.fan_buttons[mode] = b
            b.clicked.connect(lambda _=False, m=mode: self.send("fan", mode=m))
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
            lambda: self.send("fan", mode="fixed", fixed=self.fixed_slider.value()))
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
        self.curve_apply.clicked.connect(self.on_curve_apply)
        pts.addWidget(self.curve_apply, 0, CURVE_POINTS + 1, 2, 1)
        cl.addLayout(pts)
        hint = QLabel("Fans always go to 100% at 90 °C. Below 10% the fan stops; 10–19% becomes 20%.")
        hint.setStyleSheet("color: gray;")
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
        limit_apply.clicked.connect(lambda: self.send("charge_limit", percent=self.limit_spin.value()))
        bl.addWidget(QLabel("Stop charging at"))
        bl.addWidget(self.limit_spin)
        bl.addWidget(limit_apply)
        bl.addStretch()
        layout.addWidget(self.battery_box)
        self.battery_box.hide()

        self.error = QLabel()
        self.error.setStyleSheet("color: #e05050;")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        layout.addStretch()

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(2000)
        self.refresh(full=True)

    # --- talking to the daemon ---

    def send(self, cmd, **params):
        try:
            self.apply_status(request(cmd, **params), full=True)
            self.error.clear()
        except ClientError as e:
            self.refresh(full=True)
            self.error.setText(str(e))

    def refresh(self, full=False):
        try:
            self.apply_status(request("status"), full=full)
            self.error.clear()
        except ClientError as e:
            self.readings.setText("<b>Not connected</b>")
            self.error.setText(str(e))

    def apply_status(self, st, full):
        # Settings changed elsewhere (CLI, tray): redraw the controls too
        full = full or self.status is None or st["settings"] != self.status["settings"]
        self.status = st
        s = st["settings"]
        parts = [f"<b>{n}</b> {f['temp']} °C · fan {f['duty']}%" for n, f in zip(("CPU", "GPU"), st["fans"])]
        if st["battery"]:
            parts.append(f"<b>Battery</b> {st['battery']['capacity']}% {st['battery']['status'].lower()}")
        self.readings.setText("&nbsp;&nbsp;&nbsp;".join(parts))
        self.curve_preview.set_data(self.edited_curve() or s["fan"]["curve"], [f["temp"] for f in st["fans"]])
        if not full:
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
            self.kbd_custom.setStyleSheet(f"border-left: 14px solid {kbd['color']};")

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

        if self.tray_available:
            update_tray_menu(self.status)

    # --- keyboard handlers ---

    def kbd_level(self, pct):
        return round(pct * self.status["keyboard"]["max_brightness"] / 100)

    def on_kbd_toggled(self, on):
        pct = max(self.kbd_slider.value(), 10) if on else 0
        self.send("keyboard", brightness=self.kbd_level(pct))

    def on_kbd_brightness(self):
        self.send("keyboard", brightness=self.kbd_level(self.kbd_slider.value()))

    def on_kbd_custom(self):
        start = QColor(self.status["keyboard"]["color"]) if self.status and self.status["keyboard"] else Qt.white
        c = QColorDialog.getColor(start, self, "Keyboard color")
        if c.isValid():
            self.send("keyboard", color=c.name())

    # --- curve handlers ---

    def edited_curve(self):
        if not getattr(self, "curve_dirty", False):
            return None
        pts = sorted({(t.value(), pc.value()) for t, pc in self.curve_spins})
        return [list(p) for p in pts]

    def on_curve_edited(self):
        self.curve_dirty = True
        self.curve_apply.setEnabled(True)
        if self.status:
            self.curve_preview.set_data(self.edited_curve(), [f["temp"] for f in self.status["fans"]])

    def on_curve_apply(self):
        self.send("fan", mode="curve", curve=self.edited_curve())

    # --- window behaviour ---

    def closeEvent(self, event):
        if self.tray_available and not self.quitting:
            event.ignore()
            self.hide()
        else:
            event.accept()


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


_tray_actions = {}


def build_tray(app, window):
    tray = QSystemTrayIcon(QIcon(ICON_PATH), app)
    menu = QMenu()
    group = QActionGroup(menu)
    for name, label in PROFILE_LABELS.items():
        a = QAction(label, menu, checkable=True)
        a.triggered.connect(lambda _=False, n=name: window.send("profile", name=n))
        group.addAction(a)
        menu.addAction(a)
        _tray_actions[name] = a
    menu.addSeparator()
    kbd = QAction("Keyboard light", menu, checkable=True)
    kbd.triggered.connect(window.kbd_on.setChecked)
    menu.addAction(kbd)
    _tray_actions["kbd"] = kbd
    menu.addSeparator()
    show = QAction("Open", menu)
    show.triggered.connect(lambda: (window.show(), window.raise_(), window.activateWindow()))
    menu.addAction(show)
    quit_ = QAction("Quit", menu)
    quit_.triggered.connect(lambda: (setattr(window, "quitting", True), app.quit()))
    menu.addAction(quit_)
    tray.setContextMenu(menu)
    tray.setToolTip("Gigabyte Control Center")
    tray.activated.connect(
        lambda reason: reason == QSystemTrayIcon.Trigger
        and (window.hide() if window.isVisible() else (window.show(), window.raise_())))
    tray.show()
    return tray


def update_tray_menu(st):
    if not _tray_actions:
        return
    for name in PROFILE_LABELS:
        _tray_actions[name].setChecked(name == st["settings"]["profile"])
    if st["keyboard"]:
        _tray_actions["kbd"].setChecked(st["keyboard"]["brightness"] > 0)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Gigabyte Control Center")
    app.setDesktopFileName("gigabyte-control-center")
    app.setWindowIcon(QIcon(ICON_PATH))
    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    app.setQuitOnLastWindowClosed(not tray_available)

    window = MainWindow(tray_available)
    if tray_available:
        window._tray = build_tray(app, window)
        if window.status:
            update_tray_menu(window.status)
    if "--hidden" not in sys.argv or not tray_available:
        window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
