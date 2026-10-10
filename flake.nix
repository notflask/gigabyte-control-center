{
  description = "Gigabyte Control Center for Linux (Clevo-based Gigabyte laptops)";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs = { self, nixpkgs }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
    in
    {
      packages.${system}.default = pkgs.callPackage ./nix/package.nix { };

      nixosModules.default = import ./nix/module.nix self;

      devShells.${system}.default = pkgs.mkShell {
        packages = [ (pkgs.python3.withPackages (ps: [ ps.pyside6 ])) ];
      };
    };
}
