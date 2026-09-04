"""Package the tray app as a single self-contained TeamsMemeDisplay.exe.

    python tools/build_exe.py

The result in dist/ needs no Python installed and can be copied to any Windows PC. It does NOT
contain the firmware or the memes -- those live on the board, so a machine running the .exe only
needs the board plugged in.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ICON = REPO / "assets" / "icon.ico"
DIST = REPO / "dist"
NAME = "TeamsMemeDisplay"


def main() -> int:
    if not ICON.exists():
        print("icon missing, generating it first")
        subprocess.check_call([sys.executable, str(REPO / "tools" / "make_icon.py")])

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        # Windowed: this is a tray app, and a console window flashing up on login is not wanted.
        # main.py logs to %APPDATA%\TeamsMemeDisplay\app.log so there is still a way to debug it.
        "--windowed",
        "--name", NAME,
        "--icon", str(ICON),
        "--distpath", str(DIST),
        "--workpath", str(REPO / "build" / "pyinstaller"),
        "--specpath", str(REPO / "build"),
        # PyInstaller cannot see pystray's backend, which is chosen at runtime by platform.
        "--hidden-import", "pystray._win32",
        # Trim the biggest things PyInstaller pulls in that this app never touches.
        "--exclude-module", "tkinter",
        "--exclude-module", "unittest",
        "--exclude-module", "pytest",
        "--exclude-module", "numpy",
        str(REPO / "pc_app" / "main.py"),
    ]
    print(" ".join(args), "\n")
    result = subprocess.call(args)
    if result != 0:
        return result

    exe = DIST / f"{NAME}.exe"
    if not exe.exists():
        print(f"expected {exe} but it is not there", file=sys.stderr)
        return 1
    print(f"\nbuilt {exe.relative_to(REPO)} ({exe.stat().st_size / 1_048_576:.1f} MB)")
    print("Copy it anywhere; it needs no Python. Plug in the board and run it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
