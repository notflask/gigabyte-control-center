"""gbccd: root daemon that owns the hardware.

It restores the saved settings at boot, runs custom fan curves, and serves a
small JSON-lines protocol on a Unix socket for the CLI and GUI.
On exit it always hands the fans back to the firmware (automatic mode).
"""

import argparse
import grp
import json
import logging
import os
import re
import signal
import socket
import threading

from . import PROFILES, SOCKET_PATH
from .hw import FAN_COUNT, Hardware, HardwareError

log = logging.getLogger("gbccd")

STATE_PATH = "/var/lib/gigabyte-control-center/state.json"
TICK_SECONDS = 2.0
EMERGENCY_TEMP = 90        # at or above this, fans go to 100% no matter what
MAX_STEP_DOWN = 5          # % per tick, so fans spin down smoothly

DEFAULT_CURVE = [[40, 0], [55, 30], [65, 45], [75, 65], [85, 100]]
DEFAULT_STATE = {
    "profile": None,                          # None = leave the firmware's choice alone
    "keyboard": None,                         # {"color": "#rrggbb", "brightness": int}
    "fan": {"mode": "auto", "fixed": 60, "curve": DEFAULT_CURVE},
    "charge_limit": None,
}
FAN_MODES = ("auto", "max", "fixed", "curve")
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


class RequestError(Exception):
    pass


def validate_curve(curve):
    if not isinstance(curve, list) or not 2 <= len(curve) <= 10:
        raise RequestError("curve needs 2-10 points")
    points = []
    for p in curve:
        if not (isinstance(p, list) and len(p) == 2 and all(isinstance(v, int) for v in p)):
            raise RequestError("curve points must be [temp, percent] integers")
        t, pct = p
        if not (20 <= t <= 100 and 0 <= pct <= 100):
            raise RequestError("curve temp must be 20-100, percent 0-100")
        points.append([t, pct])
    points.sort()
    for (t0, p0), (t1, p1) in zip(points, points[1:]):
        if t1 == t0:
            raise RequestError("curve temperatures must be different")
        if p1 < p0:
            raise RequestError("curve percent must not decrease as temperature rises")
    if points[-1][1] < 100 or points[-1][0] > EMERGENCY_TEMP:
        raise RequestError(f"curve must reach 100% at or below {EMERGENCY_TEMP}°C")
    return points


def driver_duty(percent):
    """What the driver actually applies: below 10% the fan stops, 10-19% becomes 20%."""
    if percent < 10:
        return 0
    return max(percent, 20)


def curve_percent(curve, temp):
    if temp <= curve[0][0]:
        return curve[0][1]
    for (t0, p0), (t1, p1) in zip(curve, curve[1:]):
        if temp <= t1:
            return round(p0 + (p1 - p0) * (temp - t0) / (t1 - t0))
    return curve[-1][1]


class Daemon:
    def __init__(self, hw, state_path=STATE_PATH):
        self.hw = hw
        self.state_path = state_path
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.state = self._load_state()
        self._applied = [None] * FAN_COUNT     # last duty we set per fan, for smoothing
        self._fan_manual = False

    # --- state ---

    def _load_state(self):
        state = json.loads(json.dumps(DEFAULT_STATE))
        try:
            with open(self.state_path) as f:
                saved = json.load(f)
            for key in state:
                if key in saved:
                    state[key] = saved[key]
            state["fan"]["curve"] = validate_curve(state["fan"].get("curve", DEFAULT_CURVE))
            if state["fan"].get("mode") not in FAN_MODES:
                state["fan"]["mode"] = "auto"
        except FileNotFoundError:
            pass
        except (ValueError, RequestError, KeyError, TypeError) as e:
            log.warning("ignoring broken state file: %s", e)
            state = json.loads(json.dumps(DEFAULT_STATE))
        return state

    def _save_state(self):
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, self.state_path)

    def apply_all(self):
        s = self.state
        if s["profile"]:
            self._try(self.hw.set_profile, s["profile"])
        if s["keyboard"]:
            self._try(self.hw.kbd_set, s["keyboard"]["color"], s["keyboard"]["brightness"])
        if s["charge_limit"] and self.hw.charge_limit_supported():
            self._try(self.hw.charge_limit_set, s["charge_limit"])
        self.fan_tick()

    @staticmethod
    def _try(fn, *args):
        try:
            fn(*args)
        except (OSError, HardwareError) as e:
            log.error("%s failed: %s", fn.__name__, e)

    # --- fans ---

    def fan_tick(self):
        with self.lock:
            fan = self.state["fan"]
            mode = fan["mode"]
            try:
                readings = self.hw.fans()
            except OSError as e:
                log.error("reading fans failed, returning control to firmware: %s", e)
                self._fans_auto()
                return

            if mode == "auto":
                if self._fan_manual:
                    self._fans_auto()
                return

            targets = []
            for i, (_, temp) in enumerate(readings):
                if mode == "max":
                    want = 100
                elif mode == "fixed":
                    want = fan["fixed"]
                else:
                    want = curve_percent(fan["curve"], temp)
                    prev = self._applied[i]
                    if prev is not None and want < prev - MAX_STEP_DOWN:
                        # Spin down gradually, but below the driver's 20% floor just go straight to target
                        stepped = prev - MAX_STEP_DOWN
                        want = stepped if stepped >= 20 else want
                if temp >= EMERGENCY_TEMP:
                    want = 100
                targets.append(driver_duty(want))

            if targets != self._applied or any(abs(d - t) > 3 for (d, _), t in zip(readings, targets)):
                # Rewrite when our target changed, or when the firmware overrode us
                try:
                    self.hw.set_fan_speeds(targets)
                    self._applied = targets
                    self._fan_manual = True
                except OSError as e:
                    log.error("setting fan speed failed: %s", e)
                    self._fans_auto()

    def _fans_auto(self):
        try:
            self.hw.set_fans_auto()
        except OSError as e:
            log.error("setting fans to auto failed: %s", e)
        self._applied = [None] * FAN_COUNT
        self._fan_manual = False

    def fan_loop(self):
        while not self.stop.wait(TICK_SECONDS):
            self.fan_tick()

    # --- requests ---

    def status(self):
        with self.lock:
            fans = []
            for duty, temp in self.hw.fans():
                fans.append({"duty": duty, "temp": temp})
            try:
                kbd = self.hw.kbd_get()
            except OSError:
                kbd = None
            return {
                "settings": self.state,
                "fans": fans,
                "keyboard": kbd,
                "battery": self.hw.battery(),
                "charge_limit_supported": self.hw.charge_limit_supported(),
                "charge_limit": self.hw.charge_limit_get(),
                "profiles": list(PROFILES),
            }

    def handle(self, req):
        cmd = req.get("cmd")
        with self.lock:
            if cmd == "status":
                return self.status()
            if cmd == "info":
                return self.hw.info()
            if cmd == "profile":
                name = req.get("name")
                if name not in PROFILES:
                    raise RequestError(f"profile must be one of {', '.join(PROFILES)}")
                self.hw.set_profile(name)
                self.state["profile"] = name
            elif cmd == "keyboard":
                current = self.hw.kbd_get()
                color = req.get("color", current["color"])
                brightness = req.get("brightness", current["brightness"])
                if not (isinstance(color, str) and COLOR_RE.match(color)):
                    raise RequestError("color must look like #rrggbb")
                if not (isinstance(brightness, int) and 0 <= brightness <= current["max_brightness"]):
                    raise RequestError(f"brightness must be 0-{current['max_brightness']}")
                self.hw.kbd_set(color.lower(), brightness)
                self.state["keyboard"] = {"color": color.lower(), "brightness": brightness}
            elif cmd == "fan":
                mode = req.get("mode", self.state["fan"]["mode"])
                if mode not in FAN_MODES:
                    raise RequestError(f"fan mode must be one of {', '.join(FAN_MODES)}")
                new = dict(self.state["fan"], mode=mode)
                if "fixed" in req:
                    fixed = req["fixed"]
                    if not (isinstance(fixed, int) and 0 <= fixed <= 100):
                        raise RequestError("fixed speed must be 0-100")
                    new["fixed"] = fixed
                if "curve" in req:
                    new["curve"] = validate_curve(req["curve"])
                self.state["fan"] = new
                self._applied = [None] * FAN_COUNT
                self.fan_tick()
            elif cmd == "charge_limit":
                if not self.hw.charge_limit_supported():
                    raise RequestError("this firmware does not support a charge limit")
                limit = req.get("percent")
                if not (isinstance(limit, int) and 50 <= limit <= 100):
                    raise RequestError("charge limit must be 50-100")
                self.hw.charge_limit_set(limit)
                self.state["charge_limit"] = limit
            else:
                raise RequestError(f"unknown command {cmd!r}")
            self._save_state()
            return self.status()

    # --- socket server ---

    def serve_client(self, conn):
        with conn, conn.makefile("rwb") as f:
            for line in f:
                try:
                    reply = {"ok": True, "data": self.handle(json.loads(line))}
                except RequestError as e:
                    reply = {"ok": False, "error": str(e)}
                except (ValueError, AttributeError):
                    reply = {"ok": False, "error": "bad request"}
                except (OSError, HardwareError) as e:
                    log.exception("hardware error")
                    reply = {"ok": False, "error": f"hardware error: {e}"}
                f.write(json.dumps(reply).encode() + b"\n")
                f.flush()

    def serve(self, path, group):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.unlink(path)
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(path)
        gid = grp.getgrnam(group).gr_gid if group else -1
        os.chown(path, 0 if os.geteuid() == 0 else -1, gid)
        os.chmod(path, 0o660)
        srv.listen(8)
        srv.settimeout(0.5)
        log.info("listening on %s (group %s)", path, group)
        while not self.stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            threading.Thread(target=self.serve_client, args=(conn,), daemon=True).start()
        srv.close()
        os.unlink(path)


def main():
    ap = argparse.ArgumentParser(description="Gigabyte Control Center daemon")
    ap.add_argument("--socket", default=SOCKET_PATH)
    ap.add_argument("--group", default="users", help="group allowed to use the socket")
    ap.add_argument("--state", default=STATE_PATH)
    ap.add_argument("--fake", action="store_true", help="use simulated hardware (for testing)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if args.fake:
        from .fake import FakeHardware
        hw = FakeHardware()
    else:
        try:
            hw = Hardware()
        except HardwareError as e:
            raise SystemExit(f"gbccd: {e}")
        log.info("tuxedo_io %(module_version)s, interface %(interface)s", hw.info())

    d = Daemon(hw, args.state)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: d.stop.set())
    d.apply_all()
    threading.Thread(target=d.fan_loop, daemon=True).start()
    try:
        d.serve(args.socket, args.group)
    finally:
        d.stop.set()
        d._fans_auto()
        log.info("fans returned to automatic mode, bye")


if __name__ == "__main__":
    main()
