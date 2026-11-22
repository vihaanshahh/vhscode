"""Finish a private macOS app bundle with native runtimes already staged in Contents/Resources.

Stage python/bin/python3, model/, bin/{codex,cursor-agent,git,tmux}, and their
supporting files for the target Mac first. Run this builder on macOS. The installed
app resolves all of these relative to its own location.
"""
import argparse
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import tempfile


def script(path, content):
    path.write_text(content)
    path.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("app", type=Path)
    args = parser.parse_args()
    contents = args.app.resolve() / "Contents"
    resources = contents / "Resources"
    required = ("python/bin/python3", "bin/codex", "bin/cursor-agent", "bin/git",
                "bin/tmux", "model/onnx/model_quantized.onnx")
    for name in required:
        if not (resources / name).is_file():
            parser.error(f"Stage the runtime component {name} in {resources} first")
    package = Path(__file__).resolve().parents[1]
    (resources / "bin").mkdir(parents=True, exist_ok=True)
    (contents / "MacOS").mkdir(exist_ok=True)
    (resources / "fleet").mkdir(exist_ok=True)
    for file in (package / "fleet").glob("*.py"):
        shutil.copy2(file, resources / "fleet" / file.name)
    shutil.copy2(package / "fleet/README.md", resources / "Fleet Guide.md")
    shutil.copy2(package / "LICENSE", resources / "LICENSE")
    sources = package / "desktop-macos"
    with tempfile.TemporaryDirectory() as temp:
        iconset = Path(temp) / "Fleet.iconset"
        iconset.mkdir()
        raster = Path(temp) / "Fleet.png"
        subprocess.run(["sips", "-s", "format", "png", str(sources / "Fleet.svg"), "--out", str(raster)],
                       check=True, stdout=subprocess.DEVNULL)
        for size in (16, 32, 128, 256, 512):
            for scale in (1, 2):
                pixels = str(size * scale)
                suffix = "@2x" if scale == 2 else ""
                output = iconset / f"icon_{size}x{size}{suffix}.png"
                subprocess.run(["sips", "-z", pixels, pixels, str(raster), "--out", str(output)],
                               check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(resources / "Fleet.icns")], check=True)

    script(resources / "bin/vhscode-fleet", """#!/bin/sh
set -eu
self=$0
while [ -L "$self" ]; do
  link=$(/usr/bin/readlink "$self")
  case "$link" in
    /*) self=$link ;;
    *) self=$(/usr/bin/dirname "$self")/$link ;;
  esac
done
resources=$(cd "$(/usr/bin/dirname "$self")/.." && pwd -P)
export PATH="$resources/bin:${PATH:-/usr/bin:/bin}"
unset PYTHONHOME PYTHONPATH PYTHONUSERBASE
export PYTHONNOUSERSITE=1 PYTHONDONTWRITEBYTECODE=1
if [ -d "$resources/terminfo" ]; then
  export TERMINFO_DIRS="$resources/terminfo:${TERMINFO_DIRS:-}:/usr/share/terminfo"
fi
if [ "$#" -eq 0 ]; then
  exec /usr/bin/open "$resources/../.."
fi
exec "$resources/python/bin/python3" "$resources/fleet/fleet.py" "$@"
""")
    script(resources / "bin/fleet-askpass", """#!/bin/sh
set -eu
bin=$(cd "$(/usr/bin/dirname "$0")" && pwd -P)
exec "$bin/../../MacOS/FleetWorkbench" --ssh-askpass "$@"
""")
    previous_command = resources / "Launch Fleet.command"
    previous_command.unlink(missing_ok=True)
    sdk = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    platform_path = subprocess.check_output(["xcrun", "--sdk", "macosx", "--show-sdk-platform-path"], text=True).strip()
    plugins = Path(platform_path) / "Developer/usr/lib/swift/host/plugins"
    plugin_args = []
    if (plugins / "libSwiftUIMacros.dylib").is_file():
        server = subprocess.check_output(["xcrun", "--find", "swift-plugin-server"], text=True).strip()
        plugin_args = ["-external-plugin-path", f"{plugins}#{server}"]
    subprocess.run(["/usr/bin/xcrun", "--sdk", "macosx", "swiftc", "-sdk", sdk,
                    "-target", f"{platform.machine()}-apple-macos14.0", *plugin_args,
                    "-O", "-whole-module-optimization", "-parse-as-library",
                    *map(str, sorted(sources.glob("*.swift"))),
                    "-o", str(contents / "MacOS/FleetWorkbench")], check=True)
    info = dict(CFBundleExecutable="FleetWorkbench", CFBundleName="Fleet Workbench",
                CFBundleDisplayName="Fleet Workbench", CFBundleIdentifier="io.vhscode.fleet-workbench",
                CFBundleVersion="6", CFBundleShortVersionString="0.6", CFBundlePackageType="APPL",
                CFBundleIconFile="Fleet.icns", NSHighResolutionCapable=True, LSMinimumSystemVersion="14.0")
    info["CFBundleDocumentTypes"] = [dict(CFBundleTypeName="VS Code Workspace",
                                          CFBundleTypeRole="Editor", LSHandlerRank="Alternate",
                                          CFBundleTypeExtensions=["code-workspace"])]
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))


if __name__ == "__main__":
    main()
