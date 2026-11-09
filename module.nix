{ config, lib, pkgs, ... }:

let
  cfg = config.services.knock-knock;

  configFile = pkgs.writeText "knock-knock.json" (builtins.toJSON {
    inherit (cfg)
      interface unit allowedChatIds checkHost preCheckHost
      checkInterval downTimeout pingTimeout handshakeMaxAge;
    tokenFile = "/run/credentials/knock-knock.service/token";
  });
in
{
  options.services.knock-knock = {
    enable = lib.mkEnableOption "Telegram bot that toggles a WireGuard client with a dead-server watchdog";

    interface = lib.mkOption {
      type = lib.types.str;
      example = "wg0";
      description = "WireGuard interface name.";
    };

    unit = lib.mkOption {
      type = lib.types.str;
      default = "wg-quick-${cfg.interface}.service";
      defaultText = lib.literalExpression ''"wg-quick-''${interface}.service"'';
      description = ''
        systemd unit that brings the interface up and down. For
        `networking.wireguard.interfaces` use `"wireguard-''${interface}.service"`.
      '';
    };

    tokenFile = lib.mkOption {
      type = lib.types.path;
      example = "/run/secrets/knock-knock-token";
      description = "File with the Telegram bot token. Kept out of the Nix store.";
    };

    allowedChatIds = lib.mkOption {
      type = lib.types.nonEmptyListOf lib.types.int;
      example = [ 123456789 ];
      description = ''
        Chat or user IDs allowed to control the bot. Watchdog notifications
        go to every ID in this list.
      '';
    };

    checkHost = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "10.0.0.1";
      description = ''
        Address inside the tunnel (usually the server's WireGuard address)
        pinged through the interface. If null, the latest handshake age from
        `wg show` is used instead, which reacts slower.
      '';
    };

    preCheckHost = lib.mkOption {
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "vpn.example.com";
      description = ''
        Host pinged before bringing the tunnel up (normally the server
        endpoint). If it does not answer, `/on` is refused. Leave null if the
        server drops ICMP.
      '';
    };

    checkInterval = lib.mkOption {
      type = lib.types.ints.positive;
      default = 5;
      description = "Seconds between health checks.";
    };

    downTimeout = lib.mkOption {
      type = lib.types.ints.positive;
      default = 60;
      description = ''
        Seconds without a successful check (counting from start or the last
        success) after which the tunnel is stopped.
      '';
    };

    pingTimeout = lib.mkOption {
      type = lib.types.ints.positive;
      default = 2;
      description = "Timeout of a single ping, in seconds.";
    };

    handshakeMaxAge = lib.mkOption {
      type = lib.types.ints.positive;
      default = 180;
      description = ''
        Handshake age in seconds above which the server counts as silent.
        Only used when `checkHost` is null; needs `persistentKeepalive` on
        the peer so handshakes keep happening.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    assertions = [
      {
        assertion =
          !(config.networking.wg-quick.interfaces ? ${cfg.interface})
          || !config.networking.wg-quick.interfaces.${cfg.interface}.autostart;
        message = ''
          services.knock-knock: set networking.wg-quick.interfaces.${cfg.interface}.autostart = false,
          otherwise the tunnel comes up on boot without the bot's consent.
        '';
      }
    ];

    systemd.services.knock-knock = {
      description = "Telegram bot controlling WireGuard ${cfg.interface}";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];

      path = [ config.systemd.package pkgs.iputils pkgs.wireguard-tools ];

      serviceConfig = {
        ExecStart = "${pkgs.python3}/bin/python3 -u ${./bot.py} ${configFile}";
        LoadCredential = "token:${cfg.tokenFile}";
        Restart = "always";
        RestartSec = 5;

        # Runs as root to start/stop the unit and ping through the interface.
        NoNewPrivileges = true;
        ProtectSystem = "strict";
        ProtectHome = true;
        PrivateTmp = true;
        ProtectKernelTunables = true;
        ProtectKernelModules = true;
        ProtectControlGroups = true;
        RestrictAddressFamilies = [ "AF_UNIX" "AF_INET" "AF_INET6" "AF_NETLINK" ];
        CapabilityBoundingSet = [ "CAP_NET_ADMIN" "CAP_NET_RAW" ];
        SystemCallArchitectures = "native";
      };
    };
  };
}
