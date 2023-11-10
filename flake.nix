{
  description = "virtual environments";

  inputs.devshell.url = "github:numtide/devshell";
  inputs.flake-utils.url = "github:numtide/flake-utils";

  inputs.flake-compat = {
    url = "github:edolstra/flake-compat";
    flake = false;
  };

  outputs = { self, flake-utils, devshell, nixpkgs, ... }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = import nixpkgs {
          inherit system;
          overlays = [ devshell.overlays.default ];
          config.allowUnfree = true; # nrf5-sdk
        };
      in {
        packages.default = pkgs.callPackage ./infinitime.nix { };
        devShells.default = pkgs.devshell.mkShell {
          devshell.packages = self.packages.${system}.default.nativeBuildInputs;

          commands = [{
            name = "do_cmake";
            command = ''
              cmake -DARM_NONE_EABI_TOOLCHAIN_PATH=${pkgs.gcc-arm-embedded-10} -DNRF5_SDK_PATH=${pkgs.nrf5-sdk}/share/nRF5_SDK "$@"
            '';
          }];
        };
      });
}
