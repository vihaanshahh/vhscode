"""Finish a private macOS app bundle with native runtimes already staged in Contents/Resources.

Stage python/bin/python3, model/, bin/{codex,cursor-agent,git,tmux}, and their
supporting files for the target Mac first. Run this builder on macOS. The installed
app resolves all of these relative to its own location.
"""
import argparse
from pathlib import Path
import plistlib
import shutil
import subprocess


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
    shutil.copy2(package.parent / "packages/desktop/icons/prod/icon.icns", resources / "Fleet.icns")

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
exec "$resources/python/bin/python3" "$resources/fleet/fleet.py" "$@"
""")
    script(resources / "Launch Fleet.command", """#!/bin/sh
set -eu
resources=$(cd "$(/usr/bin/dirname "$0")" && pwd -P)
cd "$HOME"
exec "$resources/bin/vhscode-fleet" --dashboard
""")
    # LaunchServices supplies no controlling terminal. Terminal opens the bundled
    # command as a document without an Apple Events automation permission prompt.
    launcher = r"""
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int main(void) {
    char executable[PATH_MAX], path[PATH_MAX], command[PATH_MAX];
    uint32_t size = sizeof(executable);
    if (_NSGetExecutablePath(executable, &size) || !realpath(executable, path)) return 1;
    char *last = strrchr(path, '/');
    if (!last) return 1;
    *last = 0;
    if (snprintf(command, sizeof(command), "%s/../Resources/Launch Fleet.command", path)
        >= (int)sizeof(command)) return 1;
    execl("/usr/bin/open", "open", "-b", "com.apple.Terminal", command, (char *)NULL);
    return 1;
}
"""
    subprocess.run(["/usr/bin/clang", "-Os", "-x", "c", "-", "-o", str(contents / "MacOS/FleetWorkbench")],
                   input=launcher, text=True, check=True)
    info = dict(CFBundleExecutable="FleetWorkbench", CFBundleName="Fleet Workbench",
                CFBundleDisplayName="Fleet Workbench", CFBundleIdentifier="io.vhscode.fleet-workbench",
                CFBundleVersion="1", CFBundleShortVersionString="0.1", CFBundlePackageType="APPL",
                CFBundleIconFile="Fleet.icns", LSUIElement=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))


if __name__ == "__main__":
    main()
