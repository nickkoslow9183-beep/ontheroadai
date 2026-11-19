"""TEST FIXTURES for the Video Studio: tiny generated videos (FFmpeg test patterns + tones) and
hand-written transcripts. The videos are real files processed by real FFmpeg; the speech is not
real, so transcripts are imported rather than transcribed."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

SRT = """1
00:00:00,200 --> 00:00:02,000
Who would you trust with the last shot?

2
00:00:02,300 --> 00:00:04,600
Honestly, it has to be Steph. No question.

3
00:00:04,900 --> 00:00:07,600
No way. I'm taking Kobe, and that's my final answer.
"""


def make_video(path: Path, w: int = 1280, h: int = 720, dur: float = 8.0, audio: bool = True, fps: int = 25,
               black_from: float | None = None) -> Path:
    vf = "null"
    if black_from is not None:
        vf = f"drawbox=t=fill:c=black:enable='gte(t,{black_from})'"
    cmd = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=s={w}x{h}:r={fps}:d={dur}"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=330:sample_rate=48000:duration={dur}"]
    cmd += ["-vf", vf, "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def words_json(dur_scale: float = 1.0) -> str:
    """A JSON transcript WITH per-word timings, as Whisper would produce. Written by hand for tests."""
    def seg(i, start, words):
        t, out = start, []
        for wd in words.split():
            out.append({"start": round(t, 2), "end": round(t + 0.3, 2), "word": wd, "probability": 0.9})
            t += 0.35
        return {"id": i, "start": out[0]["start"], "end": out[-1]["end"], "text": words, "words": out}
    return json.dumps({"segments": [
        seg(0, 0.2, "Who would you trust with the last shot?"),
        seg(1, 3.2, "Level one I'm taking Chick-fil-A for sure."),
        seg(2, 6.0, "That's my final answer."),
    ]})
