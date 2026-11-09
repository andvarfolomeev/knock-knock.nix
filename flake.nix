{
  description = "Telegram bot that toggles a WireGuard client and drops it when the server dies";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }: {
    nixosModules.default = import ./module.nix;
    nixosModules.knock-knock = self.nixosModules.default;
  };
}
