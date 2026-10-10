"""gbcc: command line interface."""

import argparse
import json
import sys

from . import PROFILES
from .client import ClientError, request

COLORS = {
    "red": "#ff0000", "green": "#00ff00", "blue": "#0000ff", "white": "#ffffff",
    "cyan": "#00ffff", "magenta": "#ff00ff", "yellow": "#ffff00", "orange": "#ff6000",
    "purple": "#8000ff",
}


def parse_color(value):
    value = COLORS.get(value.lower(), value)
    if not value.startswith("#"):
        value = "#" + value
    return value


def parse_curve(value):
    # "40:0,55:30,65:45,75:65,85:100"
    try:
        return [[int(t), int(p)] for t, p in (pt.split(":") for pt in value.split(","))]
    except ValueError:
        raise argparse.ArgumentTypeError("curve looks like 40:0,60:40,80:100")


def print_status(st):
    s = st["settings"]
    print(f"Mode:      {s['profile'] or '(not set, firmware default)'}")
    kbd = st["keyboard"]
    if kbd:
        pct = round(kbd["brightness"] * 100 / kbd["max_brightness"])
        print(f"Keyboard:  {kbd['color']}  brightness {pct}%")
    fan = s["fan"]
    extra = {"fixed": f" {fan['fixed']}%", "curve": " " + ",".join(f"{t}:{p}" for t, p in fan["curve"])}
    print(f"Fan mode:  {fan['mode']}{extra.get(fan['mode'], '')}")
    for name, f in zip(("CPU", "GPU"), st["fans"]):
        print(f"{name} fan:   {f['duty']:3d}%   {f['temp']}°C")
    if st["battery"]:
        b = st["battery"]
        limit = f", charge limit {st['charge_limit']}%" if st["charge_limit_supported"] else ""
        print(f"Battery:   {b['capacity']}% {b['status']}{limit}")


def main():
    ap = argparse.ArgumentParser(prog="gbcc", description="Gigabyte Control Center")
    ap.add_argument("--json", action="store_true", help="print raw JSON")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("status", help="show current state (default)")

    p = sub.add_parser("mode", help="set power mode")
    p.add_argument("name", choices=PROFILES + ("battery", "silent", "gaming"))

    p = sub.add_parser("kbd", help="keyboard backlight")
    p.add_argument("--color", "-c", type=parse_color, help=f"#rrggbb or {', '.join(COLORS)}")
    p.add_argument("--brightness", "-b", type=int, metavar="0-100", help="brightness in percent")
    p.add_argument("state", nargs="?", choices=("on", "off"))

    p = sub.add_parser("fan", help="fan control")
    p.add_argument("mode", choices=("auto", "max", "fixed", "curve"))
    p.add_argument("value", nargs="?", help="percent for 'fixed', points for 'curve' (40:0,60:40,80:100)")

    p = sub.add_parser("charge-limit", help="battery charge limit (if supported)")
    p.add_argument("percent", type=int)

    args = ap.parse_args()
    aliases = {"battery": "power_saving", "silent": "quiet", "gaming": "performance"}

    try:
        if args.cmd in (None, "status"):
            st = request("status")
        elif args.cmd == "mode":
            st = request("profile", name=aliases.get(args.name, args.name))
        elif args.cmd == "kbd":
            st = request("status")
            kbd = st["keyboard"]
            params = {}
            if args.color:
                params["color"] = args.color
            if args.brightness is not None:
                params["brightness"] = round(max(0, min(100, args.brightness)) * kbd["max_brightness"] / 100)
            if args.state == "off":
                params["brightness"] = 0
            elif args.state == "on" and "brightness" not in params:
                params["brightness"] = kbd["max_brightness"]
            if not params:
                ap.error("kbd: give a color, brightness, on or off")
            st = request("keyboard", **params)
        elif args.cmd == "fan":
            params = {"mode": args.mode}
            if args.mode == "fixed":
                if args.value is None:
                    ap.error("fan fixed needs a percent")
                params["fixed"] = int(args.value)
            elif args.mode == "curve" and args.value:
                params["curve"] = parse_curve(args.value)
            st = request("fan", **params)
        elif args.cmd == "charge-limit":
            st = request("charge_limit", percent=args.percent)
    except ClientError as e:
        sys.exit(f"gbcc: {e}")

    if args.json:
        print(json.dumps(st, indent=2))
    else:
        print_status(st)


if __name__ == "__main__":
    main()
