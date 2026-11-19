"""Technical quality checks on a rendered draft.

These checks find technical problems: missing audio, wrong size, black frames, silence, clipping,
caption timing and file integrity. They do not and cannot tell whether a video is interesting
or will perform well.
"""

from __future__ import annotations

from pathlib import Path

from . import media

DISCLAIMER = ("Technical checks only. Passing them doesn't mean the video is engaging, accurate or "
              "ready to post. That's your call in review.")


def _check(key, label, status, detail, fix=""):
    return {"key": key, "label": label, "status": status, "detail": detail, "fix": fix}


def has_faststart(path: Path) -> bool:
    """True when the 'moov' index comes before the media data, so the video starts playing quickly online."""
    with open(path, "rb") as fh:
        pos = 0
        size = path.stat().st_size
        while pos < size:
            fh.seek(pos)
            hdr = fh.read(16)
            if len(hdr) < 8:
                return False
            box = int.from_bytes(hdr[:4], "big")
            typ = hdr[4:8]
            if box == 1:
                box = int.from_bytes(hdr[8:16], "big")
            if typ == b"moov":
                return True
            if typ == b"mdat":
                return False
            if box < 8:
                return False
            pos += box
    return False


def run_checks(path: str | Path, expected: dict) -> dict:
    """expected: {duration, width, height, captions: {timing, cues}, captions_enabled, words}."""
    path = Path(path)
    checks = []
    if not path.exists() or path.stat().st_size == 0:
        checks.append(_check("file", "Output file exists", "fail", "No output file was produced.",
                             "Re-render; check the job error message."))
        return summarize(checks)
    try:
        pr = media.probe(path)
        checks.append(_check("opens", "Video opens", "pass", f"{pr.container}, {pr.size / 1e6:.1f} MB"))
    except media.MediaError as exc:
        checks.append(_check("opens", "Video opens", "fail", str(exc), "Re-render the draft."))
        return summarize(checks)
    errors = media.decode_check(path)
    checks.append(_check("decodes", "Decodes without errors", "fail" if errors else "pass",
                         "; ".join(errors[:3]) if errors else "Every frame decoded.",
                         "Re-render; if it repeats, check the source file plays correctly." if errors else ""))
    checks.append(_check("codec", "H.264 video", "pass" if pr.video_codec == "h264" else "warn",
                         f"Video codec: {pr.video_codec}", "Use the default export settings." if pr.video_codec != "h264" else ""))
    if not pr.has_audio:
        checks.append(_check("audio", "Audio track", "fail", "The draft has no audio track.", "Re-render."))
    else:
        checks.append(_check("audio", "Audio track", "pass" if pr.audio_codec == "aac" else "warn",
                             f"Audio codec: {pr.audio_codec}"))
    ew, eh = expected.get("width", 1080), expected.get("height", 1920)
    size_ok = (pr.width, pr.height) == (ew, eh)
    checks.append(_check("size", "Dimensions", "pass" if size_ok else "fail", f"{pr.width}x{pr.height} (expected {ew}x{eh})",
                         "" if size_ok else "Re-render with the default 1080x1920 output."))
    aspect_ok = pr.width and pr.height and abs(pr.width / pr.height - 9 / 16) < 0.005
    checks.append(_check("aspect", "9:16 aspect ratio", "pass" if aspect_ok else "fail",
                         f"{pr.width}:{pr.height}"))
    exp_d = expected.get("duration")
    if exp_d:
        diff = abs(pr.duration - exp_d)
        checks.append(_check("duration", "Duration", "pass" if diff <= 0.35 else "warn",
                             f"{pr.duration:.2f}s (expected {exp_d:.2f}s)",
                             "" if diff <= 0.35 else "Re-render; if it repeats, the source may have timestamp problems."))
    black = media.detect_black(path)
    checks.append(_check("black", "Black frames", "warn" if black else "pass",
                         ", ".join(f"{a:.1f}–{b:.1f}s" for a, b in black[:5]) if black else "None found.",
                         "Move the start/end points or framing so the black section isn't included." if black else ""))
    if pr.has_audio:
        sil = media.detect_silence(path, noise_db=-40, min_dur=2.0)
        checks.append(_check("silence", "Long silences (2s+)", "warn" if sil else "pass",
                             ", ".join(f"{a:.1f}–{b:.1f}s" for a, b in sil[:5]) if sil else "None found.",
                             "Turn on 'Tighten pauses' or trim the clip." if sil else ""))
        ld = media.loudness(path)
        lufs, tp, mx = ld["integrated_lufs"], ld["true_peak_db"], ld["max_volume_db"]
        status, notes, fix = "pass", [], ""
        if (tp is not None and tp > -1.0) or (mx is not None and mx >= -0.1):
            status = "warn"
            notes.append("peaks near 0 dB: possible clipping/distortion")
            fix = "Check the source audio for distortion; lower the recording level next time."
        if lufs is not None and lufs < -20:
            status = "warn"
            notes.append("quiet compared with typical social video (~-14 LUFS)")
        if lufs is not None and lufs > -9:
            status = "warn"
            notes.append("unusually loud")
        checks.append(_check("loudness", "Audio level", status,
                             f"{lufs} LUFS, true peak {tp} dBTP, max {mx} dB" + (": " + "; ".join(notes) if notes else ""),
                             fix))
    cap = expected.get("captions") or {}
    if expected.get("captions_enabled"):
        cues = cap.get("cues") or []
        if not cues:
            checks.append(_check("captions", "Captions present", "fail", "Captions are on but none were generated.",
                                 "Make sure the clip's time range has a transcript."))
        else:
            dur = pr.duration
            problems = []
            for i, c in enumerate(cues):
                if c.get("words") and c["start"] < c["words"][0]["start"] - 1e-3:
                    problems.append(f"caption {i + 1} appears before its first word")
                if c["end"] - c["start"] < 0.25:
                    problems.append(f"caption {i + 1} is on screen for under 0.25s")
                if c["end"] - c["start"] > 7:
                    problems.append(f"caption {i + 1} stays up for over 7s")
                if c["end"] > dur + 0.3:
                    problems.append(f"caption {i + 1} runs past the end of the video")
                if i and c["start"] < cues[i - 1]["end"] - 0.02:
                    problems.append(f"captions {i} and {i + 1} overlap")
            checks.append(_check("caption_timing", "Caption timing", "warn" if problems else "pass",
                                 "; ".join(problems[:5]) if problems else f"{len(cues)} captions, timed to real words"
                                 if cap.get("timing") == "word" else f"{len(cues)} captions",
                                 "Edit the transcript or adjust clip times." if problems else ""))
            if cap.get("timing") == "line":
                checks.append(_check("caption_source", "Caption word timing", "warn",
                                     "Captions use whole-line timing (imported or edited transcript), so later words in "
                                     "a line appear before they are spoken.",
                                     "Transcribe the recording with Whisper for word-level timing, or keep lines short."))
    checks.append(_check("faststart", "Web-ready file (fast start)", "pass" if has_faststart(path) else "warn",
                         "Index at the start of the file." if has_faststart(path) else "Index is at the end.",
                         "" if has_faststart(path) else "Re-render (the default settings add fast start)."))
    return summarize(checks)


def summarize(checks: list[dict]) -> dict:
    status = "fail" if any(c["status"] == "fail" for c in checks) else \
        "warn" if any(c["status"] == "warn" for c in checks) else "pass"
    return {"status": status, "checks": checks, "disclaimer": DISCLAIMER}
