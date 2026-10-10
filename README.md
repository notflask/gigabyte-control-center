# Gigabyte Control Center for Linux

A replacement for Gigabyte's Windows *Fn Control Center* on Clevo-based Gigabyte
laptops. Tested target: **Gigabyte G5 KF** (BIOS FB12, PCI subsystem `1558:a550`).

- **Power modes**: Quiet, Power saving, Entertainment, Performance (the same firmware modes as on Windows)
- **Keyboard backlight**: color, brightness, on/off
- **Fans**: firmware auto, max, fixed speed, or a custom temperature curve
- **Battery charge limit**, if the firmware supports it (the G5 KF probably doesn't)
- GUI with tray icon (`gbcc-gui`) and a CLI (`gbcc`)

It uses the [tuxedo-drivers](https://gitlab.com/tuxedocomputers/development/packages/tuxedo-drivers)
kernel modules, which speak the Clevo firmware interface these laptops share with TUXEDO notebooks.

## Install (NixOS flake)

```nix
# flake.nix
inputs.gigabyte-control-center.url = "github:notflask/gigabyte-control-center";

# in your nixosSystem modules:
modules = [
  inputs.gigabyte-control-center.nixosModules.default
  { services.gigabyte-control-center.enable = true; }
];
```

This turns on `hardware.tuxedo-drivers`, starts the `gigabyte-control-center` system service,
and adds the tray icon to autostart. Options:

| Option | Default | |
|---|---|---|
| `services.gigabyte-control-center.group` | `"users"` | Who may change settings |
| `services.gigabyte-control-center.autostart` | `true` | Start the tray icon on login |

Don't run TUXEDO Control Center or `tuxedo-rs` (tailord) at the same time; they would fight over the fans.

## Tray and theming

- Hover the tray icon to see the current mode, keyboard color, fan mode, the fan
  curve as a small graph, temperatures and battery. Waybar renders it with formatting.
- The app has no colors of its own: it uses your Qt platform theme. With
  `QT_QPA_PLATFORMTHEME=kde` + Breeze + a matugen-generated `~/.config/kdeglobals`, the
  window, the fan graph and the tray icon follow the wallpaper. They also update live
  when `KGlobalSettings.notifyChange` is sent.
- The NixOS module builds the app with your system's nixpkgs, so its Qt matches the
  installed `plasma-integration` plugin.

## CLI

```
gbcc                              # status
gbcc mode quiet|power_saving|entertainment|performance
gbcc kbd -c red -b 60             # color name or #rrggbb, brightness in %
gbcc kbd off | on
gbcc fan auto | max
gbcc fan fixed 60
gbcc fan curve 40:0,55:30,65:45,75:65,85:100
gbcc charge-limit 80
```

## How it works

- `gbccd` runs as root, owns the hardware and saves your settings in
  `/var/lib/gigabyte-control-center/state.json`. It restores them at boot.
- The GUI and CLI talk to it over `/run/gigabyte-control-center/gbcc.sock` (group-restricted).
- Custom fan curves are applied every 2 seconds from the CPU/GPU fan temperatures.
- Safety: at 90 °C the fans go to 100% whatever the setting; curves must reach 100% by 90 °C;
  if anything fails, and whenever the daemon stops, the fans go back to firmware auto mode.

## Development

```
nix develop
python -m gbcc.daemon --fake --socket /tmp/gbcc.sock --state /tmp/gbcc.json --group $(id -gn) &
GBCC_SOCKET=/tmp/gbcc.sock python -m gbcc.cli
GBCC_SOCKET=/tmp/gbcc.sock python -m gbcc.gui
```

`--fake` simulates the hardware, so you can work on it without the laptop.
