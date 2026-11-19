"""Transcripts: local Whisper transcription, subtitle import and safe editing.

Segment format (stored as JSON):
    {"id": 0, "start": 1.20, "end": 4.85, "text": "...", "avg_logprob": -0.21, "no_speech_prob": 0.01,
     "words": [{"start": 1.20, "end": 1.41, "word": "Who", "probability": 0.98}, ...] or null,
     "speaker": null | "Left host" | ..., "speaker_source": null | "manual"}

Rules:
- Word timings come only from the transcription engine. Imported subtitles have segment timings only,
  and words are left null. Nothing is estimated.
- Editing a segment's text keeps its word timings only if the words are unchanged apart from
  punctuation and capitals. Otherwise the timings for that segment are removed, never re-guessed.
- Speakers are never detected automatically. Both hosts share one audio track, and diarization is
  not implemented. A person can label segments by hand.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from pathlib import Path

from ..db import log, now

LOW_CONFIDENCE = 0.5      # word probability below this is highlighted for review
MODEL_CHOICES = {
    "tiny.en": "Fastest, least accurate (~75 MB)",
    "base.en": "Recommended for a 6-core CPU with 12 GB RAM (~140 MB)",
    "small.en": "More accurate, roughly 3x slower (~480 MB)",
}
DEFAULT_MODEL = os.environ.get("ONTHEROADAI_WHISPER_MODEL", "base.en")


class TranscriptionUnavailable(RuntimeError):
    """faster-whisper (or its model) can't be used here. Import a subtitle file instead."""


def whisper_status() -> dict:
    try:
        import faster_whisper  # noqa: F401

        return {"installed": True, "version": getattr(__import__("faster_whisper"), "__version__", "?")}
    except Exception as exc:  # ImportError or a broken native dependency
        return {"installed": False, "error": str(exc)[:200]}


def transcribe(audio_path: str | Path, model_name: str = DEFAULT_MODEL, progress=None, duration: float | None = None,
               language: str = "en") -> dict:
    """Run faster-whisper on CPU (int8). `progress(fraction, message)` is called as segments arrive."""
    try:
        from faster_whisper import WhisperModel
    except Exception as exc:
        raise TranscriptionUnavailable(f"faster-whisper is not installed or failed to load: {exc}") from None
    if model_name not in MODEL_CHOICES:
        raise ValueError("Unknown transcription model.")
    t0 = time.time()
    if progress:
        progress(0.01, f"Loading the {model_name} model (the first time downloads it, ~1 minute)")
    try:
        model = WhisperModel(model_name, device="cpu", compute_type="int8",
                             cpu_threads=max(1, (os.cpu_count() or 4) - 1))
    except Exception as exc:
        raise TranscriptionUnavailable(f"Could not load the Whisper model '{model_name}': {exc}") from None
    samples = load_wav_16k(audio_path)
    seg_iter, info = model.transcribe(samples, language=language, word_timestamps=True, vad_filter=True,
                                      vad_parameters={"min_silence_duration_ms": 500}, beam_size=1,
                                      condition_on_previous_text=False)
    total = duration or info.duration or 1
    segments = []
    for i, s in enumerate(seg_iter):
        words = [{"start": round(w.start, 3), "end": round(w.end, 3), "word": w.word.strip(),
                  "probability": round(w.probability, 3)} for w in (s.words or []) if w.word.strip()]
        segments.append({"id": i, "start": round(s.start, 3), "end": round(s.end, 3), "text": s.text.strip(),
                         "avg_logprob": round(s.avg_logprob, 3), "no_speech_prob": round(s.no_speech_prob, 3),
                         "words": words or None, "speaker": None, "speaker_source": None})
        if progress:
            progress(min(0.99, s.end / total), f"Transcribed {s.end / 60:.1f} of {total / 60:.1f} minutes")
    return {"segments": segments, "language": info.language, "model": model_name,
            "elapsed": round(time.time() - t0, 1), "audio_duration": round(info.duration, 2)}


def load_wav_16k(path: str | Path):
    """Read the 16 kHz mono 16-bit WAV that FFmpeg extracted, as float32 samples.

    Passing samples (instead of a file path) means faster-whisper doesn't need PyAV to decode audio,
    which avoids PyAV version problems. FFmpeg already did the decoding."""
    import wave

    import numpy as np

    with wave.open(str(path), "rb") as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("Expected 16 kHz mono 16-bit audio")
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


# ---- import ------------------------------------------------------------------------

_TS = re.compile(r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})")


def _ts(value: str) -> float:
    m = _TS.search(value)
    if not m:
        raise ValueError(f"Bad timestamp '{value}'")
    h, mi, s, ms = m.groups()
    return int(h or 0) * 3600 + int(mi) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_subtitles(text: str, filename: str) -> tuple[str, list[dict]]:
    """SRT, WebVTT, or our own JSON. Returns (source, segments). Segment timing only, no word timings."""
    name = filename.lower()
    if name.endswith(".json"):
        data = json.loads(text)
        segs = data["segments"] if isinstance(data, dict) else data
        out = []
        for i, s in enumerate(segs):
            words = s.get("words") or None
            if words:  # accept only real per-word timings that sit inside the segment
                words = [{"start": float(w["start"]), "end": float(w["end"]), "word": str(w["word"]).strip(),
                          "probability": w.get("probability")} for w in words]
            out.append({"id": i, "start": float(s["start"]), "end": float(s["end"]), "text": str(s["text"]).strip(),
                        "words": words, "speaker": s.get("speaker"), "speaker_source": "manual" if s.get("speaker") else None})
        return "import_json", _validate(out)
    if not name.endswith((".srt", ".vtt")):
        raise ValueError("Import an .srt, .vtt or .json transcript.")
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")
    blocks = re.split(r"\n\s*\n", text.strip())
    out = []
    for b in blocks:
        lines = [ln for ln in b.split("\n") if ln.strip()]
        idx = next((k for k, ln in enumerate(lines) if "-->" in ln), None)
        if idx is None:
            continue
        a, _, z = lines[idx].partition("-->")
        body = " ".join(re.sub(r"<[^>]+>", "", ln).strip() for ln in lines[idx + 1:])
        speaker = None
        m = re.match(r"^\[?([A-Z][\w .'-]{0,30})\]?:\s+(.*)$", body)
        if m and len(m.group(1).split()) <= 3:
            speaker, body = m.group(1), m.group(2)
        if body.strip():
            out.append({"id": len(out), "start": _ts(a), "end": _ts(z.split()[0]), "text": body.strip(),
                        "words": None, "speaker": speaker, "speaker_source": "manual" if speaker else None})
    return ("import_vtt" if name.endswith(".vtt") else "import_srt"), _validate(out)


def _validate(segs: list[dict]) -> list[dict]:
    if not segs:
        raise ValueError("No timed lines found in the file.")
    segs = sorted(segs, key=lambda s: s["start"])
    for s in segs:
        if s["end"] <= s["start"]:
            raise ValueError(f"A line ends before it starts at {s['start']:.2f}s.")
        if s.get("words"):
            if any(w["end"] < w["start"] or w["start"] < s["start"] - 0.05 or w["end"] > s["end"] + 0.05
                   for w in s["words"]):
                s["words"] = None  # inconsistent timings are dropped, not repaired by guessing
    for i, s in enumerate(segs):
        s["id"] = i
    return segs


# ---- storage & editing ---------------------------------------------------------------

def save(conn: sqlite3.Connection, recording_id: int, source: str, segments: list[dict], model: str = "",
         language: str = "en") -> None:
    ts = now()
    word_timings = int(any(s.get("words") for s in segments))
    conn.execute(
        "INSERT INTO studio_transcripts (recording_id, source, model, language, segments_json, word_timings, edited, "
        "created_at, updated_at) VALUES (?,?,?,?,?,?,0,?,?) ON CONFLICT(recording_id) DO UPDATE SET "
        "source = excluded.source, model = excluded.model, language = excluded.language, "
        "segments_json = excluded.segments_json, word_timings = excluded.word_timings, edited = 0, "
        "updated_at = excluded.updated_at",
        (recording_id, source, model, language, json.dumps(segments), word_timings, ts, ts))
    log(conn, "recording", recording_id, "transcript saved", f"{source}, {len(segments)} segments")
    conn.commit()


def get(conn: sqlite3.Connection, recording_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM studio_transcripts WHERE recording_id = ?", (recording_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["segments"] = json.loads(d.pop("segments_json"))
    for s in d["segments"]:
        s["low_conf"] = [w["word"] for w in (s.get("words") or []) if (w.get("probability") or 1) < LOW_CONFIDENCE]
    return d


def _norm_words(text: str) -> list[str]:
    return [re.sub(r"[^\w']", "", w).lower() for w in text.split() if re.sub(r"[^\w']", "", w)]


def edit_segment(conn: sqlite3.Connection, recording_id: int, seg_id: int, text: str | None = None,
                 speaker: str | None = None) -> dict:
    t = get(conn, recording_id)
    if t is None:
        raise LookupError("No transcript yet")
    seg = next((s for s in t["segments"] if s["id"] == seg_id), None)
    if seg is None:
        raise LookupError("Segment not found")
    note = ""
    if text is not None:
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            raise ValueError("A line can't be empty. Remove it in the source file instead.")
        if seg.get("words"):
            if _norm_words(text) == _norm_words(" ".join(w["word"] for w in seg["words"])):
                # same words: keep timings, apply new capitalisation/punctuation word by word
                for w, new in zip(seg["words"], text.split()):
                    w["word"] = new
            else:
                seg["words"] = None
                note = "Words changed, so this line's word timings were removed (captions use the line's timing)."
        seg["text"] = text
    if speaker is not None:
        speaker = speaker.strip()[:40]
        seg["speaker"] = speaker or None
        seg["speaker_source"] = "manual" if speaker else None
    for s in t["segments"]:
        s.pop("low_conf", None)
    conn.execute("UPDATE studio_transcripts SET segments_json = ?, word_timings = ?, edited = 1, updated_at = ? "
                 "WHERE recording_id = ?",
                 (json.dumps(t["segments"]), int(any(s.get("words") for s in t["segments"])), now(), recording_id))
    conn.commit()
    return {"note": note}


def words_in(segments: list[dict], start: float, end: float) -> list[dict]:
    """Real word timings between start and end (only words fully inside)."""
    out = []
    for s in segments:
        if s["end"] < start or s["start"] > end:
            continue
        for w in s.get("words") or []:
            if w["start"] >= start - 0.01 and w["end"] <= end + 0.01:
                out.append(w)
    return out


def text_between(segments: list[dict], start: float, end: float) -> str:
    return " ".join(s["text"] for s in segments if s["start"] >= start - 0.05 and s["end"] <= end + 0.05)


def to_srt(segments: list[dict]) -> str:
    def f(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    return "\n".join(f"{i + 1}\n{f(s['start'])} --> {f(s['end'])}\n"
                     f"{(s['speaker'] + ': ') if s.get('speaker') else ''}{s['text']}\n" for i, s in enumerate(segments))
