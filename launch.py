"""Start OnTheRoadAI and open it in your browser.

Double-click "Start OnTheRoadAI.bat" (Windows) or "Start OnTheRoadAI.command" (Mac),
which run this file. The first run installs what it needs (about a minute); later
runs start in a few seconds. Keep the window open while you use the dashboard and
close it to stop. Uses only Python's standard library to get going.
"""

from __future__ import annotations

import argparse
import os
import shutil
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


def find_ffmpeg_dir() -> str | None:
    """FFmpeg on PATH, in ONTHEROADAI_FFMPEG_DIR, in tools/ffmpeg/bin, or where winget installs it."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return ""
    candidates = [os.environ.get("ONTHEROADAI_FFMPEG_DIR", ""), str(ROOT / "tools" / "ffmpeg" / "bin")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        candidates += [str(p) for p in (Path(local) / "Microsoft" / "WinGet" / "Packages").glob("Gyan.FFmpeg*/*/bin")]
    for c in candidates:
        if c and any((Path(c) / n).exists() for n in ("ffprobe", "ffprobe.exe")):
            return c
    return None


def ensure_ffmpeg() -> None:
    """The Video Studio needs FFmpeg. Everything else works without it."""
    found = find_ffmpeg_dir()
    if found is not None:
        if found:
            os.environ["ONTHEROADAI_FFMPEG_DIR"] = found
        return
    print("\n  FFmpeg was not found. The Video Studio needs it (everything else works without it).")
    if os.name == "nt" and shutil.which("winget"):
        ans = input("  Install FFmpeg now with Windows Package Manager (winget)? [y/N] ").strip().lower()
        if ans == "y":
            subprocess.call(["winget", "install", "--id", "Gyan.FFmpeg", "-e", "--accept-source-agreements",
                             "--accept-package-agreements"])
            found = find_ffmpeg_dir()
            if found:
                os.environ["ONTHEROADAI_FFMPEG_DIR"] = found
                print("  FFmpeg installed.")
                return
    elif sys.platform == "darwin":
        print("  On a Mac, install it with Homebrew: brew install ffmpeg")
    print("  See README > Installing FFmpeg. Continuing without the Video Studio.\n")


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
    if subprocess.run([str(PY), "-c", "import flask, faster_whisper"], capture_output=True).returncode != 0:
        print("Installing dependencies (Flask, and faster-whisper for transcription). The first time takes a few minutes...")
        rc = subprocess.call([str(PY), "-m", "pip", "install", "--disable-pip-version-check", "-q",
                              "-r", str(ROOT / "requirements.txt")])
        if rc != 0:
            if subprocess.run([str(PY), "-c", "import flask"], capture_output=True).returncode != 0:
                print("Could not install the dashboard's dependencies. Check your internet connection and try again.")
                return 1
            print("Transcription (faster-whisper) could not be installed. Everything else works; in the Video "
                  "Studio you can import .srt/.vtt transcripts instead.")
    ensure_ffmpeg()
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
