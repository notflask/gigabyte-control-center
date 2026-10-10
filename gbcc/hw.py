"""Hardware access through the tuxedo-drivers kernel modules.

The G5 KF is a Clevo board (PCI subsystem 1558:a550, ACPI CLV0001), so
tuxedo_io exposes fans and ODM performance profiles over the Clevo
interface and tuxedo_keyboard exposes the backlight as a multicolor LED.
"""

import fcntl
import os
import struct

TUXEDO_IO = "/dev/tuxedo_io"
LED_DIR = "/sys/class/leds/rgb:kbd_backlight"
BATTERY_DIR = "/sys/class/power_supply/BAT0"

# Values for the Clevo "OPT / set perf profile" command (see tuxedo-control-center)
PROFILE_ARG = {"quiet": 0x00, "power_saving": 0x01, "performance": 0x02, "entertainment": 0x03}

FAN_COUNT = 2  # CPU, GPU. The Clevo interface has a third slot this laptop doesn't use.


def _ioc(direction, magic, nr, size=8):
    # Every tuxedo_io ioctl is declared with a pointer type, so size = sizeof(void *) = 8
    return (direction << 30) | (size << 16) | (magic << 8) | nr


_IOC_WRITE, _IOC_READ = 1, 2
_MAGIC = 0xEC
_MAGIC_READ_CL = _MAGIC + 1
_MAGIC_WRITE_CL = _MAGIC + 2

R_MOD_VERSION = _ioc(_IOC_READ, _MAGIC, 0x00)
R_HWCHECK_CL = _ioc(_IOC_READ, _MAGIC, 0x05)
R_CL_HW_IF_STR = _ioc(_IOC_READ, _MAGIC_READ_CL, 0x00)
R_CL_FANINFO = [_ioc(_IOC_READ, _MAGIC_READ_CL, nr) for nr in (0x10, 0x11, 0x12)]
W_CL_FANSPEED = _ioc(_IOC_WRITE, _MAGIC_WRITE_CL, 0x10)
W_CL_FANAUTO = _ioc(_IOC_WRITE, _MAGIC_WRITE_CL, 0x11)
W_CL_PERF_PROFILE = _ioc(_IOC_WRITE, _MAGIC_WRITE_CL, 0x15)


class HardwareError(Exception):
    pass


def _read(path):
    with open(path) as f:
        return f.read().strip()


def _write(path, value):
    with open(path, "w") as f:
        f.write(str(value))


class Hardware:
    def __init__(self):
        try:
            self._fd = os.open(TUXEDO_IO, os.O_RDWR)
        except OSError as e:
            raise HardwareError(
                f"cannot open {TUXEDO_IO} ({e.strerror}); is hardware.tuxedo-drivers.enable set?"
            ) from e
        if self._read_int(R_HWCHECK_CL) != 1:
            raise HardwareError("tuxedo_io did not detect a Clevo interface")

    # --- raw ioctl helpers ---

    def _read_int(self, request):
        buf = bytearray(4)
        fcntl.ioctl(self._fd, request, buf, True)
        return struct.unpack("<i", buf)[0]

    def _read_str(self, request):
        buf = bytearray(64)
        fcntl.ioctl(self._fd, request, buf, True)
        return buf.split(b"\0", 1)[0].decode(errors="replace")

    def _write_int(self, request, value):
        fcntl.ioctl(self._fd, request, struct.pack("<i", value))

    # --- info ---

    def info(self):
        return {
            "module_version": self._read_str(R_MOD_VERSION),
            "interface": self._read_str(R_CL_HW_IF_STR),
        }

    # --- performance profile (Gigabyte's quiet / power saving / entertainment / performance) ---

    def set_profile(self, name):
        if name not in PROFILE_ARG:
            raise HardwareError(f"unknown profile {name!r}")
        self._write_int(W_CL_PERF_PROFILE, PROFILE_ARG[name])

    # --- fans ---

    def fan(self, index):
        """Return (duty_percent, temperature_c) for fan 0 (CPU) or 1 (GPU)."""
        info = self._read_int(R_CL_FANINFO[index])
        duty = round((info & 0xFF) * 100 / 0xFF)
        temp = struct.unpack("b", bytes([(info >> 16) & 0xFF]))[0]
        return duty, temp

    def fans(self):
        return [self.fan(i) for i in range(FAN_COUNT)]

    def set_fan_speeds(self, percents):
        """Set fixed duty for each fan. Duty below ~20% is turned into 0 or 20% by the driver."""
        arg = 0
        for i, pct in enumerate(percents[:3]):
            raw = round(max(0, min(100, pct)) * 0xFF / 100)
            arg |= raw << (8 * i)
        self._write_int(W_CL_FANSPEED, arg)

    def set_fans_auto(self):
        self._write_int(W_CL_FANAUTO, 0b1111)

    # --- keyboard backlight ---

    @staticmethod
    def kbd_max_brightness():
        return int(_read(f"{LED_DIR}/max_brightness"))

    @staticmethod
    def kbd_get():
        r, g, b = (int(v) for v in _read(f"{LED_DIR}/multi_intensity").split())
        return {
            "color": f"#{r:02x}{g:02x}{b:02x}",
            "brightness": int(_read(f"{LED_DIR}/brightness")),
            "max_brightness": Hardware.kbd_max_brightness(),
        }

    @staticmethod
    def kbd_set(color=None, brightness=None):
        if color is not None:
            r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
            _write(f"{LED_DIR}/multi_intensity", f"{r} {g} {b}")
        if brightness is not None:
            _write(f"{LED_DIR}/brightness", brightness)

    # --- battery charge limit (only if the firmware supports it) ---

    @staticmethod
    def charge_limit_supported():
        return os.access(f"{BATTERY_DIR}/charge_control_end_threshold", os.W_OK)

    @staticmethod
    def charge_limit_get():
        try:
            return int(_read(f"{BATTERY_DIR}/charge_control_end_threshold"))
        except OSError:
            return None

    @staticmethod
    def charge_limit_set(percent):
        _write(f"{BATTERY_DIR}/charge_control_end_threshold", percent)

    @staticmethod
    def battery():
        try:
            return {"capacity": int(_read(f"{BATTERY_DIR}/capacity")), "status": _read(f"{BATTERY_DIR}/status")}
        except OSError:
            return None
