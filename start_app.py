"""Validate the Python environment and launch the ground station."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

REQUIRED = {"customtkinter": "customtkinter", "serial": "pyserial", "matplotlib": "matplotlib", "tkintermapview": "tkintermapview", "PIL": "Pillow"}


def main() -> int:
    if sys.version_info < (3, 10):
        print("AstrasenseNexus Ground Station requires Python 3.10 or newer.", file=sys.stderr)
        return 2
    missing = [package for module, package in REQUIRED.items() if importlib.util.find_spec(module) is None]
    if missing:
        print("Missing dependencies: " + ", ".join(missing), file=sys.stderr)
        print(f'Install them with: "{sys.executable}" -m pip install -r requirements.txt', file=sys.stderr)
        return 2
    main_file = Path(__file__).resolve().with_name("main.py")
    try:
        return subprocess.call([sys.executable, str(main_file)])
    except OSError as exc:
        print(f"Could not launch main.py: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
