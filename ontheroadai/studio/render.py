"""FFmpeg rendering of vertical drafts: H.264 + AAC MP4, 1080x1920, captions burned in.

One FFmpeg run per draft. The input is seeked to the clip (so a clip from minute 50 of a long
podcast doesn't decode the first 50 minutes), cut into pieces (framing changes / tightened
pauses), framed, concatenated, captioned and loudness-normalised.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from .framing import OUT_H, OUT_W, output_duration, piece_filter
from .media import MediaError, ffmpeg_bin

QUALITY = {"fast": ("veryfast", 23), "balanced": ("faster", 21), "high": ("medium", 19)}
PRESET_AUDIO = {"podcast_clean": -14.0, "sports_story": -14.0, "gta_gameplay": -16.0}


def filter_path(p: Path) -> str:
    """Quote a file path for use inside an FFmpeg filter (Windows drive letters need escaping)."""
    s = str(Path(p).resolve()).replace("\\", "/").replace(":", "\\:").replace("'", "\\'")
    return f"'{s}'"


def build_command(src: Path, pieces: list[dict], framing: dict, src_w: int, src_h: int, src_fps: float | None,
                  has_audio: bool, out: Path, ass: Path | None, preset: str = "podcast_clean",
                  quality: str = "balanced") -> tuple[list[str], float]:
    if not pieces:
        raise MediaError("Nothing to render: the clip is empty.")
    t0 = pieces[0]["src_start"]
    span = pieces[-1]["src_end"] - t0
    n = len(pieces)
    fc = []
    fc.append(f"[0:v]split={n}" + "".join(f"[vs{i}]" for i in range(n)) if n > 1 else "[0:v]null[vs0]")
    if has_audio:
        fc.append(f"[0:a]asplit={n}" + "".join(f"[as{i}]" for i in range(n)) if n > 1 else "[0:a]anull[as0]")
    for i, p in enumerate(pieces):
        a, b = p["src_start"] - t0, p["src_end"] - t0
        fc.append(f"[vs{i}]trim=start={a:.3f}:end={b:.3f},setpts=PTS-STARTPTS[vt{i}]")
        fc.append(piece_filter(p["mode"], p["x"], framing, src_w, src_h, f"vt{i}", f"vf{i}", str(i)))
        if has_audio:
            fc.append(f"[as{i}]atrim=start={a:.3f}:end={b:.3f},asetpts=PTS-STARTPTS[at{i}]")
    if has_audio:
        fc.append("".join(f"[vf{i}][at{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[vc][ac]")
    else:
        fc.append("".join(f"[vf{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc]")
    fps = min(60.0, src_fps or 30.0)
    vchain = f"[vc]fps={fps:.3f}"
    if ass is not None:
        vchain += f",ass={filter_path(ass)}"
    fc.append(vchain + ",format=yuv420p[vout]")
    target = PRESET_AUDIO.get(preset, -14.0)
    if has_audio:
        fc.append(f"[ac]loudnorm=I={target}:TP=-1.5:LRA=11,aresample=48000[aout]")
    speed, crf = QUALITY.get(quality, QUALITY["balanced"])
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-y", "-ss", f"{t0:.3f}", "-t", f"{span + 0.05:.3f}",
           "-i", str(src)]
    if not has_audio:
        cmd += ["-f", "lavfi", "-t", f"{span:.3f}", "-i", "anullsrc=r=48000:cl=stereo"]
    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]" if has_audio else "1:a",
            "-c:v", "libx264", "-preset", speed, "-crf", str(crf), "-profile:v", "high", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-movflags", "+faststart",
            "-t", f"{output_duration(pieces):.3f}", "-progress", "pipe:1", "-nostats", str(out)]
    return cmd, output_duration(pieces)


def run_render(cmd: list[str], total: float, progress=None, timeout: float = 4 * 3600) -> None:
    """Run FFmpeg, reporting progress from its -progress output. Raises MediaError on failure."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    t_start = time.time()
    err_tail: list[str] = []
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    secs = int(line.split("=")[1]) / 1_000_000
                except ValueError:
                    continue
                if progress and total:
                    progress(min(0.99, secs / total), f"Rendering {secs:.0f}s of {total:.0f}s")
            if time.time() - t_start > timeout:
                proc.kill()
                raise MediaError("Rendering took too long and was stopped.")
        proc.wait()
        err_tail = (proc.stderr.read() if proc.stderr else "").strip().splitlines()[-8:]
    finally:
        if proc.poll() is None:
            proc.kill()
    if proc.returncode != 0:
        raise MediaError("FFmpeg failed: " + " | ".join(err_tail)[-600:])


def expected_size() -> tuple[int, int]:
    return OUT_W, OUT_H
