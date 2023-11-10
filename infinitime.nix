{ stdenv, patch, cmake, gnumake, gcc-arm-embedded-10, nrf5-sdk, python3, adafruit-nrfutil
, nodePackages }:

stdenv.mkDerivation {
  pname = "infinitime-japanese";
  version = "1.14.0";

  src = ./.;

  nativeBuildInputs = [
    cmake
    gnumake
    gcc-arm-embedded-10
    nrf5-sdk
    (python3.withPackages
      (p: with p; [ cbor click intelhex cryptography pillow ]))
    adafruit-nrfutil
    nodePackages.lv_font_conv
  ];

  cmakeFlags = [
    "-DARM_NONE_EABI_TOOLCHAIN_PATH=${gcc-arm-embedded-10}"
    "-DNRF5_SDK_PATH=${nrf5-sdk}/share/nRF5_SDK"
    "-DBUILD_DFU=1"
    "-DBUILD_RESOURCES=1"
    "-DCMAKE_BUILD_TYPE=Release"
  ];

  makeTargets = [
    "pinetime-mcuboot-app"
    "GenerateResources"
  ];

  patchPhase = ''
    patchShebangs .
    substituteInPlace src/resources/generate-fonts.py --replace "'/usr/bin/env', 'patch'" "'${patch}/bin/patch'"
    substituteInPlace src/displayapp/fonts/generate.py --replace "'/usr/bin/env', 'patch'" "'${patch}/bin/patch'"
  '';

  installPhase = ''
    mkdir -p $out
    cp src/pinetime-mcuboot-app-dfu-*.zip $out/
    cp src/resources/infinitime-resources-*.zip $out/
  '';
}
