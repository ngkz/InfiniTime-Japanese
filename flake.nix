{
  description = "virtual environments";

  inputs.devshell.url = "github:numtide/devshell";
  inputs.flake-utils.url = "github:numtide/flake-utils";

  inputs.flake-compat = {
    url = "github:edolstra/flake-compat";
    flake = false;
  };

  outputs = { self, flake-utils, devshell, nixpkgs, ... }:
    flake-utils.lib.eachDefaultSystem (system: {
      devShells.default = let
        pkgs = import nixpkgs {
          inherit system;

          overlays = [ devshell.overlays.default ];
          config.allowUnfree = true;
        };
      in pkgs.devshell.mkShell {
        devshell.packages = with pkgs; [
          cmake
          gnumake
          gcc-arm-embedded-10
          nrf5-sdk
          (python3.withPackages
            (p: with p; [ cbor click intelhex cryptography pillow ]))
          python3Packages.adafruit-nrfutil
          nodePackages.lv_font_conv
          lv_img_conv
        ];

        commands = [{
          name = "do_cmake";
          command = "cmake -DARM_NONE_EABI_TOOLCHAIN_PATH=$ARM_NONE_EABI_TOOLCHAIN_PATH -DNRF5_SDK_PATH=$NRF5_SDK_PATH -DBUILD_DFU=1 -DBUILD_RESOURCES=1 \"$@\"";
        }];

        env = [
          {
            name = "ARM_NONE_EABI_TOOLCHAIN_PATH";
            value = "${pkgs.gcc-arm-embedded-10}";
          }
          {
            name = "NRF5_SDK_PATH";
            value = "${pkgs.nrf5-sdk}/share/nRF5_SDK";
          }
        ];
      };
    });
}
