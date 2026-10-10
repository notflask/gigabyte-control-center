{ config, lib, pkgs, ... }:

let
  cfg = config.services.gigabyte-control-center;
in
{
  options.services.gigabyte-control-center = {
    enable = lib.mkEnableOption "Gigabyte Control Center (keyboard light, fans, power modes)";

    package = lib.mkOption {
      type = lib.types.package;
      # Built with the system's nixpkgs, so its Qt matches the system's Qt plugins
      # (plasma-integration / Breeze). With a different Qt version the KDE platform
      # theme can't load and the app falls back to the plain Fusion look.
      default = pkgs.callPackage ./package.nix { };
      defaultText = lib.literalExpression "pkgs.callPackage ./nix/package.nix { }";
      description = "The gigabyte-control-center package.";
    };

    group = lib.mkOption {
      type = lib.types.str;
      default = "users";
      description = "Members of this group can change settings through the GUI and `gbcc`.";
    };

    profile = lib.mkOption {
      type = lib.types.nullOr (lib.types.enum [ "quiet" "power_saving" "entertainment" "performance" ]);
      default = null;
      example = "performance";
      description = ''
        Firmware power mode to start with. Used until you pick a mode in the app;
        after that your choice is remembered across reboots. `null` leaves the
        firmware default.
      '';
    };

    autostart = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Start the tray icon when you log in.";
    };
  };

  config = lib.mkIf cfg.enable {
    hardware.tuxedo-drivers.enable = true;

    environment.systemPackages = [ cfg.package ];

    environment.etc."xdg/autostart/gigabyte-control-center.desktop" = lib.mkIf cfg.autostart {
      text = ''
        [Desktop Entry]
        Type=Application
        Name=Gigabyte Control Center
        Exec=${cfg.package}/bin/gbcc-gui --hidden
        Icon=gigabyte-control-center
        X-GNOME-Autostart-enabled=true
      '';
    };

    systemd.services.gigabyte-control-center = {
      description = "Gigabyte Control Center daemon";
      wantedBy = [ "multi-user.target" ];
      after = [ "systemd-modules-load.service" ];
      serviceConfig = {
        ExecStart = "${cfg.package}/bin/gbccd --group ${cfg.group}"
          + lib.optionalString (cfg.profile != null) " --default-profile ${cfg.profile}";
        Restart = "on-failure";
        RestartSec = 3;
        StateDirectory = "gigabyte-control-center";
        RuntimeDirectory = "gigabyte-control-center";
        # Fans go back to firmware control when the daemon stops
        KillSignal = "SIGTERM";
        ProtectHome = true;
        PrivateTmp = true;
        NoNewPrivileges = true;
      };
    };
  };
}
