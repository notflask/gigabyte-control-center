{ lib, python3Packages, qt6 }:

python3Packages.buildPythonApplication {
  pname = "gigabyte-control-center";
  version = "0.1.0";
  pyproject = true;

  src = lib.cleanSource ../.;

  build-system = [ python3Packages.setuptools ];
  dependencies = [ python3Packages.pyside6 ];

  nativeBuildInputs = [ qt6.wrapQtAppsHook ];
  buildInputs = [ qt6.qtbase qt6.qtwayland qt6.qtsvg ];

  # Let the Python wrapper carry the Qt environment instead of double wrapping
  dontWrapQtApps = true;
  preFixup = ''
    makeWrapperArgs+=("''${qtWrapperArgs[@]}")
  '';

  postInstall = ''
    install -Dm644 gigabyte-control-center.desktop -t $out/share/applications
    install -Dm644 gbcc/data/gbcc.svg $out/share/icons/hicolor/scalable/apps/gigabyte-control-center.svg
  '';

  pythonImportsCheck = [ "gbcc.daemon" "gbcc.cli" "gbcc.gui" ];

  meta = {
    description = "Keyboard backlight, fan and power mode control for Clevo-based Gigabyte laptops";
    license = lib.licenses.gpl3Plus;
    platforms = [ "x86_64-linux" ];
    mainProgram = "gbcc";
  };
}
