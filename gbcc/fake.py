"""Simulated hardware so the daemon, CLI and GUI can be tested without the laptop."""

import random

from .hw import FAN_COUNT


class FakeHardware:
    def __init__(self):
        self.duty = [30] * FAN_COUNT
        self.auto = True
        self.profile = None
        self.kbd = {"color": "#ffffff", "brightness": 128, "max_brightness": 255}
        self.limit = 100

    def info(self):
        return {"module_version": "fake", "interface": "fake"}

    def set_profile(self, name):
        self.profile = name

    def fans(self):
        return [(d, 45 + i * 3 + random.randint(-2, 2)) for i, d in enumerate(self.duty)]

    def set_fan_speeds(self, percents):
        self.duty = list(percents[:FAN_COUNT])
        self.auto = False

    def set_fans_auto(self):
        self.auto = True
        self.duty = [30] * FAN_COUNT

    def kbd_get(self):
        return dict(self.kbd)

    def kbd_set(self, color=None, brightness=None):
        if color is not None:
            self.kbd["color"] = color
        if brightness is not None:
            self.kbd["brightness"] = brightness

    def charge_limit_supported(self):
        return True

    def charge_limit_get(self):
        return self.limit

    def charge_limit_set(self, percent):
        self.limit = percent

    def battery(self):
        return {"capacity": 87, "status": "Charging"}
