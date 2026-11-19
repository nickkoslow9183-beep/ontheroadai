"""Start OnTheRoadAI and open it in your browser.

Double-click "Start OnTheRoadAI.bat" (Windows) or "Start OnTheRoadAI.command" (Mac),
which run this file. The first run installs what it needs (about a minute); later
runs start in a few seconds. Keep the window open while you use the dashboard and
close it to stop. Uses only Python's standard library to get going.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import threading
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
PY = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def free_port(start: int) -> int:
    for port in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("No free port found between %d and %d." % (start, start + 19))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if sys.version_info < (3, 10):
        print("OnTheRoadAI needs Python 3.10 or newer. Download it free from https://www.python.org/downloads/")
        return 1
    if not PY.exists():
        print("First run: setting things up. This takes about a minute...")
        venv.create(VENV, with_pip=True)
    if subprocess.run([str(PY), "-c", "import flask"], capture_output=True).returncode != 0:
        print("Installing the dashboard's one dependency (Flask)...")
        subprocess.check_call([str(PY), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                               "-r", str(ROOT / "requirements.txt")])
    port = free_port(args.port)
    url = f"http://127.0.0.1:{port}"
    print("\n  OnTheRoadAI is running at " + url)
    print("  Keep this window open while you use it. Close it (or press Ctrl+C) to stop.\n")
    if not args.no_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    try:
        return subprocess.call([str(PY), "-m", "ontheroadai", "run", "--port", str(port)], cwd=str(ROOT))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
