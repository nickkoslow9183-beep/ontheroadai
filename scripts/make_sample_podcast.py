"""Make a SYNTHETIC two-person 'podcast' video for testing the Video Studio end to end.

The voices are computer speech (espeak-ng), not real people, and the picture is two coloured
panels labelled LEFT HOST / RIGHT HOST. Use it to try the full workflow (upload -> transcribe ->
clips -> render -> QC) without using a private recording.

    python scripts/make_sample_podcast.py out.mp4

Needs: ffmpeg and espeak-ng on PATH.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = [
    ("L", "Welcome back to On The Road. Okay, let's get into it."),
    ("L", "Who would you trust with the last shot in a game seven?"),
    ("R", "Honestly, it has to be Steph. Nobody is more automatic when it matters."),
    ("L", "No way. Steph is great, but I'm taking Kobe every single time."),
    ("R", "Kobe? Come on. That's a hot take, and I think it's overrated."),
    ("R", "The numbers say Steph is the best shooter we have ever seen."),
    ("L", "Numbers don't hit game winners. Kobe wanted that moment, and that's why I trust him."),
    ("R", "Fair, but I'm still taking Steph. That is my final answer."),
    ("L", "Alright, next segment. The four level food draft."),
    ("L", "Level one is fast food. What's your pick?"),
    ("R", "Level one, I'm taking Chick fil A. Easy pick."),
    ("L", "Level two is pizza. Your pick?"),
    ("R", "For pizza I'm going with New York style."),
    ("L", "Level three is breakfast."),
    ("R", "Breakfast is pancakes, no question."),
    ("L", "And level four, the final level, is dessert."),
    ("R", "Dessert has to be cheesecake. That's my draft, and it's the best draft ever."),
    ("L", "That's a crazy good draft. I can't even argue with that one."),
]
VOICES = {"L": ("en-us+m3", 165), "R": ("en-us+m7", 150)}


def run(cmd):
    subprocess.run(cmd, check=True, capture_output=True)


def main(out: str) -> None:
    for tool in ("ffmpeg", "espeak-ng"):
        if not shutil.which(tool):
            sys.exit(f"{tool} is required")
    tmp = Path(tempfile.mkdtemp())
    parts = []
    for i, (who, line) in enumerate(SCRIPT):
        voice, speed = VOICES[who]
        wav = tmp / f"l{i}.wav"
        run(["espeak-ng", "-v", voice, "-s", str(speed), "-w", str(wav), line])
        norm = tmp / f"n{i}.wav"
        gap = 0.35 if i + 1 < len(SCRIPT) and SCRIPT[i + 1][0] == who else 0.6
        if i == 8:
            gap = 2.5  # a deliberate long pause between segments (dead air to detect)
        run(["ffmpeg", "-y", "-i", str(wav), "-af", f"apad=pad_dur={gap}", "-ar", "48000", "-ac", "1", str(norm)])
        parts.append(norm)
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in parts))
    audio = tmp / "audio.wav"
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(audio)])
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                                str(audio)], capture_output=True, text=True).stdout.strip())
    draw = ("drawtext=text='{t}':fontcolor=white:fontsize=64:x={x}:y=(h-text_h)/2-80,"
            "drawtext=text='SYNTHETIC TEST VIDEO':fontcolor=white@0.6:fontsize=28:x=(w-text_w)/2:y=40")
    vf = (f"[0:v][1:v]hstack=inputs=2,{draw.format(t='LEFT HOST', x='(w/2-text_w)/2')},"
          f"drawtext=text='RIGHT HOST':fontcolor=white:fontsize=64:x=w/2+(w/2-text_w)/2:y=(h-text_h)/2-80,"
          f"drawtext=text='%{{pts\\:hms}}':fontcolor=white:fontsize=36:x=(w-text_w)/2:y=h-80[v]")
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=0xC0531F:s=960x1080:d={dur}:r=30",
         "-f", "lavfi", "-i", f"color=c=0x1F4FA8:s=960x1080:d={dur}:r=30", "-i", str(audio),
         "-filter_complex", vf, "-map", "[v]", "-map", "2:a", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
         "-c:a", "aac", "-b:a", "128k", "-shortest", "-movflags", "+faststart", out])
    shutil.rmtree(tmp, ignore_errors=True)
    print(f"Wrote {out} ({dur:.1f}s)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "sample_podcast.mp4")
