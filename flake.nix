{
  description = "Run configured commands in detached tmux sessions";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

  outputs =
    { self, nixpkgs }:
    let
      forAllSystems = nixpkgs.lib.genAttrs [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
    in
    {
      packages = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = self.packages.${system}.sessions;
          sessions = pkgs.python3Packages.buildPythonApplication {
            pname = "sessions";
            version = "0.1.0";
            pyproject = true;
            src = ./.;

            build-system = [ pkgs.python3Packages.hatchling ];
            dependencies = with pkgs.python3Packages; [
              libtmux
              pyyaml
            ];
          };
        }
      );

      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};
        in
        {
          default = pkgs.mkShell {
            inputsFrom = [ self.packages.${system}.default ];
            packages = [
              pkgs.nixfmt
              pkgs.ruff
            ];
          };
        }
      );
    };
}
