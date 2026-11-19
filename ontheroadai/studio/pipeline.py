"""What each background job does: inspect -> transcribe -> find clips; render -> QC."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..db import log, now
from . import captions, clipfinder, framing, media, render, storage, transcript, videoqc
from .jobs import enqueue

KIND_PRESET = {"podcast": "podcast_clean", "sports_story": "sports_story", "gameplay": "gta_gameplay",
               "streamer_footage": "gta_gameplay", "other": "podcast_clean"}
KIND_FRAMING = {"podcast": "both", "sports_story": "center", "gameplay": "both", "streamer_footage": "both",
                "other": "center"}
BUCKET_RANGES = {"≤15 s": (8, 15), "16–30 s": (16, 30), "31–60 s": (31, 60), "61–90 s": (61, 90),
                 "91–180 s": (91, 180)}
FORMAT_FOR_KIND = {"podcast": "podcast_clip", "sports_story": "storytelling", "gameplay": "gta_moment",
                   "streamer_footage": "streamer_story", "other": "fan_reaction"}


def recording(conn, rid: int) -> dict:
    row = conn.execute("SELECT * FROM studio_recordings WHERE id = ?", (rid,)).fetchone()
    if row is None:
        raise LookupError(f"Recording {rid} not found")
    return dict(row)


def source_path(rec: dict) -> Path:
    p = storage.resolve(rec["stored_path"])
    if p is None or not p.exists():
        raise media.MediaError("The original recording file is missing from the media folder.")
    return p


def silences_file(rid: int) -> Path:
    return storage.work_dir(rid) / "silences.json"


def load_silences(conn, rec: dict) -> list[tuple[float, float]]:
    f = silences_file(rec["id"])
    if f.exists():
        return [tuple(x) for x in json.loads(f.read_text())]
    if not rec["has_audio"]:
        return []
    sil = media.detect_silence(source_path(rec), noise_db=-35, min_dur=0.6)
    f.write_text(json.dumps(sil))
    return sil


# ---- handlers: each returns (final status, message) ----------------------------------------

def do_inspect(conn, job, progress):
    rec = recording(conn, job["recording_id"])
    src = source_path(rec)
    progress(0.2, "Reading the file's streams")
    pr = media.probe(src)
    progress(0.5, "Test-decoding the first 20 seconds")
    errors = media.decode_check(src, seconds=20)
    thumb = storage.recording_dir(rec["id"]) / "thumb.jpg"
    media.thumbnail(src, thumb, at=min(5.0, pr.duration / 3))
    status = "failed" if errors else "ready"
    conn.execute("UPDATE studio_recordings SET duration = ?, width = ?, height = ?, fps = ?, video_codec = ?, "
                 "audio_codec = ?, has_audio = ?, container = ?, status = ?, updated_at = ? WHERE id = ?",
                 (pr.duration, pr.width, pr.height, pr.fps, pr.video_codec, pr.audio_codec, int(pr.has_audio),
                  pr.container, status, now(), rec["id"]))
    conn.commit()
    if errors:
        raise media.MediaError("The video doesn't decode cleanly: " + "; ".join(errors[:2]))
    if conn.execute("SELECT 1 FROM studio_transcripts WHERE recording_id = ?", (rec["id"],)).fetchone():
        enqueue(conn, "find_clips", recording_id=rec["id"])
        return "completed", "Checked. A transcript already exists, so clip finding is queued."
    if not pr.has_audio:
        return "needs_review", "Checked. No audio track, so it can't be transcribed. Add clips by hand."
    enqueue(conn, "transcribe", recording_id=rec["id"], params=json.loads(job["params_json"] or "{}"))
    return "completed", f"Checked: {pr.width}x{pr.height}, {pr.duration / 60:.1f} min. Transcription queued."


def do_transcribe(conn, job, progress):
    rec = recording(conn, job["recording_id"])
    params = json.loads(job["params_json"] or "{}")
    model = params.get("model") or transcript.DEFAULT_MODEL
    wd = storage.work_dir(rec["id"])
    audio = wd / "audio.wav"
    progress(0.02, "Extracting audio")
    try:
        media.extract_audio(source_path(rec), audio)
        progress(0.04, "Finding pauses")
        sil = media.detect_silence(audio, noise_db=-35, min_dur=0.6)
        silences_file(rec["id"]).write_text(json.dumps(sil))
        try:
            result = transcript.transcribe(audio, model, lambda f, m: progress(0.05 + 0.93 * f, m),
                                           duration=rec["duration"])
        except transcript.TranscriptionUnavailable as exc:
            return "needs_review", (f"Transcription isn't available on this computer ({exc}). Import a transcript "
                                    "(.srt/.vtt) on the recording page instead.")
    finally:
        storage.remove_file(audio)  # temporary file: never kept
    transcript.save(conn, rec["id"], "whisper", result["segments"], model=result["model"],
                    language=result["language"])
    enqueue(conn, "find_clips", recording_id=rec["id"])
    mins = result["audio_duration"] / 60
    return "completed", (f"Transcribed {mins:.1f} min of audio in {result['elapsed'] / 60:.1f} min with {model} "
                         f"({len(result['segments'])} lines). Clip finding queued.")


def trend_context(conn, business: str, days: int = 10) -> list[dict]:
    from ..trends.text import keyword_set, named_phrases

    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    out = []
    for r in conn.execute("SELECT id, title FROM research_stories WHERE business = ? AND last_seen_at >= ? "
                          "AND dismissed = 0 ORDER BY score DESC LIMIT 200", (business, since)):
        out.append({"id": r["id"], "title": r["title"], "kw": keyword_set(r["title"]),
                    "names": set(named_phrases(r["title"]))})
    return out


def preferred_length(conn, business: str, kind: str) -> tuple[tuple[float, float] | None, str]:
    """Best-converting length band for this format from your own analytics (needs 3+ videos per band)."""
    from ..analytics import insights

    vids = insights.load_videos(conn, business, {"format": FORMAT_FOR_KIND.get(kind, "podcast_clip")})
    groups = [g for g in insights.conversion_report(vids)["by_length"] if g["n"] >= 3 and g["label"] in BUCKET_RANGES]
    if len(groups) < 2:
        return None, "Not enough of your own analytics per length band yet; using the preset's range."
    best = groups[0]
    return BUCKET_RANGES[best["label"]], (f"Your {best['label']} videos convert best ({best['pooled_per_1k']} follows "
                                          f"per 1,000 views, {best['n']} videos).")


def do_find_clips(conn, job, progress):
    rec = recording(conn, job["recording_id"])
    t = transcript.get(conn, rec["id"])
    if t is None:
        return "needs_review", "No transcript yet. Transcribe or import one first."
    params = json.loads(job["params_json"] or "{}")
    preset = params.get("preset") or KIND_PRESET.get(rec["kind"], "podcast_clean")
    progress(0.2, "Loading pauses and research")
    sil = load_silences(conn, rec) if rec["has_audio"] else []
    trend = trend_context(conn, rec["business"])
    pref, pref_note = preferred_length(conn, rec["business"], rec["kind"])
    progress(0.5, "Scoring moments")
    res = clipfinder.find_candidates(t["segments"], preset, sil, trend, pref,
                                     max_candidates=int(params.get("max", 8)))
    conn.execute("DELETE FROM studio_clips WHERE recording_id = ? AND origin = 'auto' AND status = 'candidate' AND "
                 "id NOT IN (SELECT clip_id FROM studio_drafts)", (rec["id"],))
    ts = now()
    fr = {**framing.DEFAULT, "mode": KIND_FRAMING.get(rec["kind"], "center")}
    for c in res["candidates"]:
        score_json = {"factors": c["factors"], "notes": res["notes"], "length_note": pref_note,
                      "disclaimer": "Ranks moments for your review based on the words spoken. It can't see the "
                                    "video or predict views."}
        conn.execute(
            "INSERT INTO studio_clips (recording_id, start, end, score, score_json, hook, title, category, excerpt, "
            "context_flags_json, preset, framing_json, caption_json, overlays_json, origin, trend_story_id, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'auto',?,?,?)",
            (rec["id"], c["start"], c["end"], c["score"], json.dumps(score_json), c["hook"], c["title"],
             c["category"], c["excerpt"], json.dumps(c["flags"]), preset, json.dumps(fr),
             json.dumps(captions.DEFAULT_STYLE), "[]", c["trend_story_id"], ts, ts))
    conn.commit()
    log(conn, "recording", rec["id"], "clip candidates found", str(len(res["candidates"])))
    if not res["candidates"]:
        return "needs_review", " ".join(res["notes"]) or "No clear moments found. Add clips by hand."
    return "needs_review", f"{len(res['candidates'])} clip candidates ready for review." + \
        (" " + " ".join(res["notes"]) if res["notes"] else "")


def rights_ok(conn, rec: dict) -> tuple[bool, str]:
    if rec["ownership"] != "third_party":
        return True, ""
    from ..scoring import effective_source_status

    if not rec["rights_source_id"]:
        return False, "Third-party footage has no footage-rights record."
    src = conn.execute("SELECT * FROM source_permissions WHERE id = ?", (rec["rights_source_id"],)).fetchone()
    status = effective_source_status(src) if src else "unknown"
    if status != "granted":
        return False, (f"Footage permission for {rec['creator'] or 'this creator'} is '{status}'. Record their written "
                       "permission in Footage rights before rendering.")
    return True, ""


def plan_render(conn, draft: dict) -> dict:
    clip = dict(conn.execute("SELECT * FROM studio_clips WHERE id = ?", (draft["clip_id"],)).fetchone())
    rec = recording(conn, clip["recording_id"])
    t = transcript.get(conn, rec["id"])
    segments = t["segments"] if t else []
    fr = framing.normalize(json.loads(clip["framing_json"] or "{}"))
    style = captions.normalize_style(json.loads(clip["caption_json"] or "{}"))
    sil = load_silences(conn, rec) if (clip["tighten_pauses"] and rec["has_audio"]) else []
    keep = framing.keep_intervals(clip["start"], clip["end"], sil, bool(clip["tighten_pauses"]))
    pieces = framing.build_pieces(clip["start"], clip["end"], fr, keep)
    words = transcript.words_in(segments, clip["start"], clip["end"])
    cues = captions.build_cues(words, segments, clip["start"], clip["end"], pieces, style)
    duration = framing.output_duration(pieces)
    events, problems = captions.resolve_overlays(json.loads(clip["overlays_json"] or "[]"), words, pieces, duration)
    hook = clip["hook"] if clip["preset"] == "sports_story" and clip["hook"] else None
    return {"clip": clip, "rec": rec, "framing": fr, "style": style, "pieces": pieces, "cues": cues,
            "duration": duration, "overlay_events": events, "overlay_problems": problems, "hook": hook}


def do_render(conn, job, progress):
    draft = dict(conn.execute("SELECT * FROM studio_drafts WHERE id = ?", (job["draft_id"],)).fetchone())
    plan = plan_render(conn, draft)
    rec = plan["rec"]
    ok, why = rights_ok(conn, rec)
    if not ok:
        raise PermissionError(why)
    conn.execute("UPDATE studio_drafts SET status = 'rendering', updated_at = ? WHERE id = ?", (now(), draft["id"]))
    conn.commit()
    out = storage.draft_path(draft["id"])
    ass = storage.draft_path(draft["id"], "ass")
    captions.write_ass(ass, plan["cues"], plan["style"], plan["overlay_events"], plan["hook"])
    settings = json.loads(draft["settings_json"] or "{}")
    cmd, total = render.build_command(source_path(rec), plan["pieces"], plan["framing"], rec["width"], rec["height"],
                                      rec["fps"], bool(rec["has_audio"]), out, ass, plan["clip"]["preset"],
                                      settings.get("quality", "balanced"))
    progress(0.01, "Rendering")
    render.run_render(cmd, total, progress)
    pr = media.probe(out)
    settings.update({"pieces": plan["pieces"], "captions": plan["cues"], "framing": plan["framing"],
                     "caption_style": plan["style"], "overlay_problems": plan["overlay_problems"],
                     "expected_duration": total, "clip_start": plan["clip"]["start"], "clip_end": plan["clip"]["end"],
                     "preset": plan["clip"]["preset"]})
    conn.execute("UPDATE studio_drafts SET file_path = ?, size_bytes = ?, duration = ?, width = ?, height = ?, "
                 "settings_json = ?, status = 'qc', updated_at = ? WHERE id = ?",
                 (storage.rel(out), out.stat().st_size, pr.duration, pr.width, pr.height, json.dumps(settings), now(),
                  draft["id"]))
    conn.execute("UPDATE studio_clips SET status = 'rendered', updated_at = ? WHERE id = ?", (now(), plan["clip"]["id"]))
    conn.commit()
    enqueue(conn, "qc", recording_id=rec["id"], clip_id=plan["clip"]["id"], draft_id=draft["id"])
    return "completed", f"Rendered {pr.width}x{pr.height}, {pr.duration:.1f}s ({out.stat().st_size / 1e6:.1f} MB). QC queued."


def do_qc(conn, job, progress):
    draft = dict(conn.execute("SELECT * FROM studio_drafts WHERE id = ?", (job["draft_id"],)).fetchone())
    s = json.loads(draft["settings_json"] or "{}")
    progress(0.1, "Checking the draft")
    report = videoqc.run_checks(storage.resolve(draft["file_path"]), {
        "duration": s.get("expected_duration"), "width": 1080, "height": 1920, "captions": s.get("captions"),
        "captions_enabled": (s.get("caption_style") or {}).get("enabled", True)})
    if s.get("overlay_problems"):
        report["checks"].append({"key": "overlays", "label": "Text overlays", "status": "warn",
                                 "detail": "; ".join(s["overlay_problems"]), "fix": "Set those times by hand in the clip editor."})
        report = videoqc.summarize(report["checks"])
    conn.execute("UPDATE studio_drafts SET qc_json = ?, qc_status = ?, status = 'needs_review', updated_at = ? "
                 "WHERE id = ?", (json.dumps(report), report["status"], now(), draft["id"]))
    conn.commit()
    return "needs_review", f"QC {report['status']}. Waiting for your review."


HANDLERS = {"inspect": do_inspect, "transcribe": do_transcribe, "find_clips": do_find_clips, "render": do_render,
            "qc": do_qc}


def on_failure(conn: sqlite3.Connection, job: dict, msg: str) -> None:
    if job.get("draft_id"):
        conn.execute("UPDATE studio_drafts SET status = 'failed', review_notes = ?, updated_at = ? WHERE id = ?",
                     (msg[:500], now(), job["draft_id"]))
    if job["kind"] == "inspect" and job.get("recording_id"):
        conn.execute("UPDATE studio_recordings SET status = 'failed', updated_at = ? WHERE id = ?",
                     (now(), job["recording_id"]))
    conn.commit()
