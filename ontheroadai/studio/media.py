"""FFmpeg / ffprobe helpers: inspection, validation, audio extraction and analysis.

FFmpeg must be installed (see README). Nothing here loads a whole video into memory: FFmpeg
reads the files directly from disk.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

ALLOWED_EXTENSIONS = (".mp4", ".mov", ".m4v")
# ffprobe reports the ISO-BMFF family (MP4/MOV/M4V) as this format name.
ALLOWED_CONTAINERS = {"mov,mp4,m4a,3gp,3g2,mj2"}
MAX_BYTES = int(os.environ.get("ONTHEROADAI_MAX_UPLOAD_GB", "20")) * 1024 ** 3
MAX_DURATION = 4 * 3600
MIN_DURATION = 1.0


class MediaError(ValueError):
    """The file is not usable; the message says why in plain words."""


def _tool(name: str) -> str | None:
    custom = os.environ.get("ONTHEROADAI_FFMPEG_DIR")
    if custom:
        for cand in (Path(custom) / name, Path(custom) / f"{name}.exe"):
            if cand.exists():
                return str(cand)
    return shutil.which(name)


def ffmpeg_bin() -> str:
    b = _tool("ffmpeg")
    if not b:
        raise MediaError("FFmpeg is not installed. See README: 'Installing FFmpeg'.")
    return b


def ffprobe_bin() -> str:
    b = _tool("ffprobe")
    if not b:
        raise MediaError("ffprobe (part of FFmpeg) is not installed. See README: 'Installing FFmpeg'.")
    return b


def tools_status() -> dict:
    out = {"ffmpeg": _tool("ffmpeg"), "ffprobe": _tool("ffprobe"), "version": None, "libx264": False,
           "libass": False}
    if out["ffmpeg"]:
        try:
            v = subprocess.run([out["ffmpeg"], "-hide_banner", "-version"], capture_output=True, text=True, timeout=20)
            out["version"] = (v.stdout.splitlines() or [""])[0]
            out["libx264"] = "--enable-libx264" in v.stdout
            out["libass"] = "--enable-libass" in v.stdout
        except (OSError, subprocess.SubprocessError):
            pass
    return out


def run(cmd: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


@dataclass
class Probe:
    container: str
    duration: float
    size: int
    width: int | None
    height: int | None
    fps: float | None
    video_codec: str | None
    audio_codec: str | None
    has_audio: bool
    rotation: int
    raw: dict


def probe(path: str | Path) -> Probe:
    p = run([ffprobe_bin(), "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            timeout=120)
    if p.returncode != 0:
        raise MediaError(f"This file can't be read as a video ({(p.stderr or 'unknown error').strip()[:200]}).")
    data = json.loads(p.stdout or "{}")
    fmt = data.get("format", {})
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")),
             None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    fps = None
    if v and v.get("avg_frame_rate") and v["avg_frame_rate"] != "0/0":
        n, d = v["avg_frame_rate"].split("/")
        fps = round(float(n) / float(d), 3) if float(d) else None
    rotation = 0
    if v:
        for sd in v.get("side_data_list", []) or []:
            if "rotation" in sd:
                rotation = int(sd["rotation"])
        rotation = int(v.get("tags", {}).get("rotate", rotation) or 0)
    w, h = (v.get("width"), v.get("height")) if v else (None, None)
    if w and h and abs(rotation) % 180 == 90:  # phone recordings stored sideways
        w, h = h, w
    return Probe(container=fmt.get("format_name", ""), duration=float(fmt.get("duration") or 0),
                 size=int(fmt.get("size") or 0), width=w, height=h, fps=fps,
                 video_codec=v.get("codec_name") if v else None, audio_codec=a.get("codec_name") if a else None,
                 has_audio=a is not None, rotation=rotation, raw=data)


def validate_upload(path: str | Path, filename: str) -> Probe:
    """Check extension, real container, streams, size and duration. Extensions alone are not trusted."""
    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise MediaError("Upload an MP4, MOV or M4V video.")
    size = Path(path).stat().st_size
    if size == 0:
        raise MediaError("The file is empty.")
    if size > MAX_BYTES:
        raise MediaError(f"The file is larger than {MAX_BYTES // 1024 ** 3} GB.")
    with open(path, "rb") as fh:
        head = fh.read(64)
    if b"ftyp" not in head and b"moov" not in head and b"mdat" not in head and b"wide" not in head:
        raise MediaError("This file isn't really an MP4/MOV video (its contents don't match the extension).")
    pr = probe(path)
    if pr.container not in ALLOWED_CONTAINERS:
        raise MediaError(f"Unsupported container '{pr.container}'. Upload an MP4, MOV or M4V video.")
    if not pr.video_codec:
        raise MediaError("The file has no video stream.")
    if pr.duration < MIN_DURATION:
        raise MediaError("The video is shorter than one second.")
    if pr.duration > MAX_DURATION:
        raise MediaError("The video is longer than 4 hours. Split it first.")
    if not pr.width or not pr.height:
        raise MediaError("Could not read the video's dimensions.")
    return pr


def decode_check(path: str | Path, seconds: float | None = None, timeout: float = 600) -> list[str]:
    """Decode the file (or its first N seconds) and return any decoder errors."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-v", "error", "-xerror"]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-i", str(path), "-f", "null", "-"]
    p = run(cmd, timeout=timeout)
    errors = [ln for ln in (p.stderr or "").splitlines() if ln.strip()]
    if p.returncode != 0 and not errors:
        errors = [f"ffmpeg exited with code {p.returncode}"]
    return errors[:20]


def extract_audio(src: str | Path, dest: str | Path, timeout: float = 3600) -> Path:
    """16 kHz mono WAV, the format Whisper expects. Streams from disk, no full load into memory."""
    p = run([ffmpeg_bin(), "-hide_banner", "-v", "error", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
             "-c:a", "pcm_s16le", str(dest)], timeout=timeout)
    if p.returncode != 0 or not Path(dest).exists():
        raise MediaError(f"Could not extract audio: {(p.stderr or '').strip()[:300]}")
    return Path(dest)


_SIL_START = re.compile(r"silence_start: (-?[\d.]+)")
_SIL_END = re.compile(r"silence_end: (-?[\d.]+) \| silence_duration: ([\d.]+)")


def detect_silence(path: str | Path, noise_db: float = -35, min_dur: float = 0.6, start: float | None = None,
                   duration: float | None = None, timeout: float = 1800) -> list[tuple[float, float]]:
    """Silent intervals [(start, end)] in seconds (relative to `start` when given)."""
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-i", str(path), "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_dur}", "-f", "null", "-"]
    p = run(cmd, timeout=timeout)
    out, cur = [], None
    for line in (p.stderr or "").splitlines():
        m = _SIL_START.search(line)
        if m:
            cur = max(0.0, float(m.group(1)))
        m = _SIL_END.search(line)
        if m and cur is not None:
            out.append((round(cur, 3), round(float(m.group(1)), 3)))
            cur = None
    if cur is not None and duration:
        out.append((round(cur, 3), round(duration, 3)))
    return out


_BLACK = re.compile(r"black_start:([\d.]+) black_end:([\d.]+) black_duration:([\d.]+)")


def detect_black(path: str | Path, min_dur: float = 0.5, timeout: float = 1800) -> list[tuple[float, float]]:
    p = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(path), "-an", "-vf",
             f"blackdetect=d={min_dur}:pix_th=0.10", "-f", "null", "-"], timeout=timeout)
    return [(float(m.group(1)), float(m.group(2))) for m in _BLACK.finditer(p.stderr or "")]


def loudness(path: str | Path, timeout: float = 1800) -> dict:
    """Integrated loudness (LUFS) and true peak (dBTP) via EBU R128, plus max sample volume."""
    p = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "ebur128=peak=true",
             "-f", "null", "-"], timeout=timeout)
    err = p.stderr or ""
    summary = err[err.rfind("Summary:"):] if "Summary:" in err else err
    i = re.search(r"I:\s+(-?[\d.]+) LUFS", summary)
    tp = re.search(r"Peak:\s+(-?[\d.]+|-inf) dBFS", summary)
    v = run([ffmpeg_bin(), "-hide_banner", "-nostats", "-i", str(path), "-vn", "-af", "volumedetect", "-f", "null", "-"],
            timeout=timeout)
    mx = re.search(r"max_volume: (-?[\d.]+) dB", v.stderr or "")
    return {"integrated_lufs": float(i.group(1)) if i else None,
            "true_peak_db": float(tp.group(1)) if tp and tp.group(1) != "-inf" else None,
            "max_volume_db": float(mx.group(1)) if mx else None}


def thumbnail(src: str | Path, dest: str | Path, at: float = 1.0) -> Path | None:
    p = run([ffmpeg_bin(), "-hide_banner", "-v", "error", "-y", "-ss", f"{max(0.0, at):.2f}", "-i", str(src),
             "-frames:v", "1", "-vf", "scale=480:-2", str(dest)], timeout=120)
    return Path(dest) if p.returncode == 0 and Path(dest).exists() else None


def list_fonts() -> list[str]:
    """Font families installed on this computer (fontconfig). Falls back to common names."""
    fc = shutil.which("fc-list")
    fams = set()
    if fc:
        try:
            out = subprocess.run([fc, ":", "family"], capture_output=True, text=True, timeout=20).stdout
            for line in out.splitlines():
                name = line.split(",")[0].strip()
                if name and not name.startswith("."):
                    fams.add(name)
        except (OSError, subprocess.SubprocessError):
            pass
    if not fams:
        fams = {"Arial", "Segoe UI", "Verdana", "Tahoma", "Impact"} if os.name == "nt" else {"DejaVu Sans"}
    return sorted(fams)
