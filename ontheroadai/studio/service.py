"""Video Studio actions: uploads, library, transcripts, clips, drafts and approval."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import ideas as ideas_mod
from .. import rights
from ..db import log, now
from . import captions, clipfinder, framing, media, storage, transcript
from .jobs import enqueue
from .pipeline import FORMAT_FOR_KIND, KIND_FRAMING, KIND_PRESET, load_silences, recording, rights_ok

KINDS = {"podcast": "Podcast episode", "sports_story": "Sports story footage", "gameplay": "GTA gameplay (ours)",
         "streamer_footage": "Streamer / third-party footage", "other": "Other"}
CHUNK_MAX = 8 * 1024 * 1024


# ---- uploads (chunked, never fully in memory) ---------------------------------------------

def _meta(meta: dict) -> dict:
    m = {"business": meta.get("business", "on-the-road-media"), "kind": meta.get("kind", "podcast"),
         "title": (meta.get("title") or "").strip()[:200], "ownership": meta.get("ownership", "own"),
         "creator": (meta.get("creator") or "").strip()[:120], "source_url": (meta.get("source_url") or "").strip()[:500],
         "notes": (meta.get("notes") or "").strip()[:1000], "model": meta.get("model") or transcript.DEFAULT_MODEL}
    if m["business"] not in ("on-the-road-media", "gta6-network"):
        raise ValueError("Choose a business.")
    if m["kind"] not in KINDS:
        raise ValueError("Choose what kind of recording this is.")
    if m["kind"] == "streamer_footage":
        m["ownership"] = "third_party"
    if m["ownership"] not in ("own", "third_party"):
        raise ValueError("Say whether you own this footage.")
    if m["ownership"] == "third_party" and not m["creator"]:
        raise ValueError("Name the creator/streamer this footage belongs to.")
    if m["source_url"] and not m["source_url"].startswith(("http://", "https://")):
        raise ValueError("The source link must start with https://")
    if m["model"] not in transcript.MODEL_CHOICES:
        raise ValueError("Unknown transcription model.")
    return m


def start_upload(conn: sqlite3.Connection, filename: str, size: int, meta: dict) -> str:
    if not str(filename).lower().endswith(media.ALLOWED_EXTENSIONS):
        raise ValueError("Upload an MP4, MOV or M4V video.")
    size = int(size)
    if size <= 0:
        raise ValueError("The file is empty.")
    if size > media.MAX_BYTES:
        raise ValueError(f"The file is larger than {media.MAX_BYTES // 1024 ** 3} GB.")
    free = shutil.disk_usage(storage.media_root()).free
    if size * 1.2 > free:
        raise ValueError(f"Not enough free disk space ({free / 1e9:.1f} GB free).")
    m = _meta(meta)
    cleanup_uploads(conn)
    token = secrets.token_urlsafe(16)
    tmp = storage.media_root() / "uploads" / f"{token}.part"
    tmp.write_bytes(b"")
    conn.execute("INSERT INTO studio_uploads (token, filename, size_expected, temp_path, meta_json, created_at) "
                 "VALUES (?,?,?,?,?,?)", (token, storage.safe_display_name(filename), size, storage.rel(tmp),
                                         json.dumps(m), now()))
    conn.commit()
    return token


def _upload(conn, token: str) -> dict:
    row = conn.execute("SELECT * FROM studio_uploads WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise LookupError("Upload not found or expired. Start again.")
    return dict(row)


def append_chunk(conn: sqlite3.Connection, token: str, offset: int, data: bytes) -> int:
    up = _upload(conn, token)
    if len(data) > CHUNK_MAX:
        raise ValueError("Chunk too large.")
    if int(offset) != up["received"]:
        raise ValueError(f"Unexpected offset {offset}; resume from {up['received']}.")
    if up["received"] + len(data) > up["size_expected"]:
        raise ValueError("More data than the announced file size.")
    with open(storage.resolve(up["temp_path"]), "ab") as fh:
        fh.write(data)
    received = up["received"] + len(data)
    conn.execute("UPDATE studio_uploads SET received = ? WHERE token = ?", (received, token))
    conn.commit()
    return received


def complete_upload(conn: sqlite3.Connection, token: str) -> int:
    up = _upload(conn, token)
    tmp = storage.resolve(up["temp_path"])
    try:
        if up["received"] != up["size_expected"]:
            raise ValueError(f"Upload incomplete ({up['received']} of {up['size_expected']} bytes).")
        return add_recording(conn, tmp, up["filename"], json.loads(up["meta_json"]), move=True)
    finally:
        storage.remove_file(tmp)
        conn.execute("DELETE FROM studio_uploads WHERE token = ?", (token,))
        conn.commit()


def cleanup_uploads(conn: sqlite3.Connection, older_than_hours: int = 24) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=older_than_hours)).isoformat(timespec="seconds")
    n = 0
    for r in conn.execute("SELECT * FROM studio_uploads WHERE created_at < ?", (cutoff,)).fetchall():
        storage.remove_file(storage.resolve(r["temp_path"]))
        conn.execute("DELETE FROM studio_uploads WHERE token = ?", (r["token"],))
        n += 1
    conn.commit()
    return n


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def add_recording(conn: sqlite3.Connection, path: Path, filename: str, meta: dict, move: bool = False) -> int:
    """Validate a video file and add it to the library (also used by the CLI and tests)."""
    m = _meta(meta)
    pr = media.validate_upload(path, filename)
    digest = sha256_file(path)
    dup = conn.execute("SELECT id, title FROM studio_recordings WHERE sha256 = ? AND status <> 'deleted'",
                       (digest,)).fetchone()
    if dup:
        raise ValueError(f"This exact video is already in the library as #{dup['id']} ({dup['title']}).")
    title = m["title"] or os.path.splitext(storage.safe_display_name(filename))[0]
    ts = now()
    rights_id = None
    if m["ownership"] == "third_party":
        existing = conn.execute("SELECT rights_source_id FROM creators WHERE lower(name) = lower(?) AND "
                                "rights_source_id IS NOT NULL", (m["creator"],)).fetchone()
        rights_id = existing[0] if existing else rights.create_source(
            conn, m["business"], f"{m['creator']}: footage '{title[:60]}'", "creator_permission", "unknown",
            source_url=m["source_url"], rights_holder=m["creator"],
            notes="Created by Video Studio. Permission is NOT assumed: record written permission before rendering.")
    cur = conn.execute(
        "INSERT INTO studio_recordings (business, kind, title, original_filename, stored_path, sha256, size_bytes, "
        "duration, width, height, fps, video_codec, audio_codec, has_audio, container, ownership, creator, source_url, "
        "rights_source_id, status, notes, created_at, updated_at) VALUES (?,?,?,?,'',?,?,?,?,?,?,?,?,?,?,?,?,?,?,'processing',?,?,?)",
        (m["business"], m["kind"], title, storage.safe_display_name(filename), digest, pr.size or Path(path).stat().st_size,
         pr.duration, pr.width, pr.height, pr.fps, pr.video_codec, pr.audio_codec, int(pr.has_audio), pr.container,
         m["ownership"], m["creator"], m["source_url"], rights_id, m["notes"], ts, ts))
    rid = cur.lastrowid
    ext = os.path.splitext(filename)[1].lower()
    dest = storage.recording_dir(rid) / f"source{ext}"
    if move:
        shutil.move(str(path), dest)
    else:
        shutil.copyfile(path, dest)
    conn.execute("UPDATE studio_recordings SET stored_path = ? WHERE id = ?", (storage.rel(dest), rid))
    log(conn, "recording", rid, "uploaded", title)
    conn.commit()
    from ..agents.orchestrator import Orchestrator

    Orchestrator(conn).studio_process_recording(rid, model=m["model"])
    return rid


def list_recordings(conn) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM studio_clips c WHERE c.recording_id = r.id AND c.status <> 'rejected') AS clips, "
        "(SELECT COUNT(*) FROM studio_drafts d WHERE d.recording_id = r.id) AS drafts, "
        "(SELECT source FROM studio_transcripts t WHERE t.recording_id = r.id) AS transcript_source, "
        "p.status AS rights_status FROM studio_recordings r LEFT JOIN source_permissions p ON p.id = r.rights_source_id "
        "WHERE r.status <> 'deleted' ORDER BY r.id DESC")]
    return rows


def delete_recording(conn, rid: int) -> None:
    recording(conn, rid)
    for d in conn.execute("SELECT id FROM studio_drafts WHERE recording_id = ?", (rid,)).fetchall():
        storage.remove_file(storage.draft_path(d["id"]))
        storage.remove_file(storage.draft_path(d["id"], "ass"))
    storage.remove_tree(storage.media_root() / "recordings" / str(rid))
    storage.remove_tree(storage.media_root() / "work" / str(rid))
    conn.execute("UPDATE studio_jobs SET status = 'cancelled' WHERE recording_id = ? AND status IN ('queued','processing')",
                 (rid,))
    conn.execute("DELETE FROM studio_recordings WHERE id = ?", (rid,))
    log(conn, "recording", rid, "deleted with all files")
    conn.commit()


def import_transcript(conn, rid: int, filename: str, text: str) -> int:
    rec = recording(conn, rid)
    source, segs = transcript.parse_subtitles(text, filename)
    if rec["duration"] and segs[-1]["end"] > rec["duration"] + 2:
        raise ValueError(f"The transcript runs to {segs[-1]['end']:.0f}s but the video is only {rec['duration']:.0f}s. "
                         "Is it the right file?")
    transcript.save(conn, rid, source, segs)
    return enqueue(conn, "find_clips", recording_id=rid)


def retranscribe(conn, rid: int, model: str) -> int:
    rec = recording(conn, rid)
    if not rec["has_audio"]:
        raise ValueError("This video has no audio track.")
    if model not in transcript.MODEL_CHOICES:
        raise ValueError("Unknown model.")
    return enqueue(conn, "transcribe", recording_id=rid, params={"model": model})


# ---- clips -------------------------------------------------------------------------------

def get_clip(conn, cid: int) -> dict:
    row = conn.execute("SELECT * FROM studio_clips WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise LookupError(f"Clip {cid} not found")
    d = dict(row)
    d["score_detail"] = json.loads(d["score_json"] or "{}")
    for k, default in (("context_flags_json", []), ("framing_json", {}), ("caption_json", {}), ("overlays_json", [])):
        d[k.replace("_json", "")] = json.loads(d[k] or json.dumps(default))
    d["framing"] = framing.normalize(d["framing"])
    d["caption"] = captions.normalize_style(d["caption"])
    d["duration"] = round(d["end"] - d["start"], 2)
    return d


def list_clips(conn, rid: int) -> list[dict]:
    return [get_clip(conn, r[0]) for r in conn.execute(
        "SELECT id FROM studio_clips WHERE recording_id = ? ORDER BY status = 'rejected', score DESC, start", (rid,))]


def _rescore(conn, rec: dict, start: float, end: float, preset: str) -> dict | None:
    t = transcript.get(conn, rec["id"])
    if not t:
        return None
    segs = [s for s in t["segments"] if s["end"] > start and s["start"] < end]
    if not segs:
        return None
    nxt = next((s for s in t["segments"] if s["start"] >= end), None)
    sil = load_silences(conn, rec) if rec["has_audio"] else []
    from .pipeline import trend_context

    return clipfinder.score_window(segs, clipfinder.PRESETS[preset], sil, trend_context(conn, rec["business"]), None, nxt)


def update_clip(conn, cid: int, **fields) -> dict:
    clip = get_clip(conn, cid)
    rec = recording(conn, clip["recording_id"])
    sets: dict = {}
    start = float(fields.get("start", clip["start"]))
    end = float(fields.get("end", clip["end"]))
    if start < 0 or end <= start:
        raise ValueError("The end must be after the start.")
    if rec["duration"] and end > rec["duration"] + 0.01:
        raise ValueError(f"The recording is only {rec['duration']:.1f}s long.")
    if end - start < 1:
        raise ValueError("Clips must be at least 1 second.")
    if end - start > 180:
        raise ValueError("Clips can be at most 3 minutes.")
    preset = fields.get("preset", clip["preset"])
    if preset not in clipfinder.PRESETS:
        raise ValueError("Unknown preset.")
    if (start, end, preset) != (clip["start"], clip["end"], clip["preset"]):
        sets.update(start=round(start, 3), end=round(end, 3), preset=preset)
        sc = _rescore(conn, rec, start, end, preset)
        if sc:
            sets.update(score=sc["score"], excerpt=sc["excerpt"], context_flags_json=json.dumps(sc["flags"]),
                        score_json=json.dumps({**clip["score_detail"], "factors": sc["factors"],
                                               "rescored": "Score recalculated for the edited times."}))
    if "framing" in fields:
        sets["framing_json"] = json.dumps(framing.normalize(fields["framing"]))
    if "caption" in fields:
        sets["caption_json"] = json.dumps(captions.normalize_style(fields["caption"]))
    if "overlays" in fields:
        ov = fields["overlays"]
        if not isinstance(ov, list) or len(ov) > 6:
            raise ValueError("Overlays must be a list of at most 6 items.")
        for o in ov:
            if o.get("type") == "board" and len(o.get("lines") or []) > 8:
                raise ValueError("A board can have at most 8 lines.")
        sets["overlays_json"] = json.dumps(ov)
    for k in ("hook", "title"):
        if k in fields:
            sets[k] = (fields[k] or "").strip()[:150]
    if "tighten_pauses" in fields:
        sets["tighten_pauses"] = 1 if fields["tighten_pauses"] else 0
    if "status" in fields:
        if fields["status"] not in ("candidate", "selected", "rejected"):
            raise ValueError("Bad clip status.")
        sets["status"] = fields["status"]
    if sets:
        cols = ", ".join(f"{k} = ?" for k in sets)
        conn.execute(f"UPDATE studio_clips SET {cols}, updated_at = ? WHERE id = ?", [*sets.values(), now(), cid])
        conn.commit()
    return get_clip(conn, cid)


def add_manual_clip(conn, rid: int, start: float, end: float) -> int:
    rec = recording(conn, rid)
    if end <= start or start < 0 or (rec["duration"] and end > rec["duration"] + 0.01):
        raise ValueError("Enter a start and end inside the recording.")
    preset = KIND_PRESET.get(rec["kind"], "podcast_clean")
    sc = _rescore(conn, rec, start, end, preset)
    ts = now()
    fr = {**framing.DEFAULT, "mode": KIND_FRAMING.get(rec["kind"], "center")}
    cur = conn.execute(
        "INSERT INTO studio_clips (recording_id, start, end, score, score_json, hook, title, category, excerpt, "
        "context_flags_json, status, preset, framing_json, caption_json, origin, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'selected',?,?,?,'manual',?,?)",
        (rid, round(start, 3), round(end, 3), sc["score"] if sc else None,
         json.dumps({"factors": sc["factors"]} if sc else {"note": "No transcript in this range, so it isn't scored."}),
         sc["hook"] if sc else "", sc["title"] if sc else f"Clip {start:.0f}s–{end:.0f}s", sc["category"] if sc else "general",
         sc["excerpt"] if sc else "", json.dumps(sc["flags"] if sc else []), preset, json.dumps(fr),
         json.dumps(captions.DEFAULT_STYLE), ts, ts))
    conn.commit()
    return cur.lastrowid


# ---- drafts -------------------------------------------------------------------------------

def render_clip(conn, cid: int, quality: str = "balanced") -> int:
    clip = get_clip(conn, cid)
    rec = recording(conn, clip["recording_id"])
    ok, why = rights_ok(conn, rec)
    if not ok:
        raise PermissionError(why)
    if quality not in ("fast", "balanced", "high"):
        raise ValueError("Unknown quality setting.")
    version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM studio_drafts WHERE clip_id = ?", (cid,)).fetchone()[0]
    ts = now()
    did = conn.execute("INSERT INTO studio_drafts (clip_id, recording_id, version, settings_json, created_at, updated_at) "
                       "VALUES (?,?,?,?,?,?)", (cid, rec["id"], version, json.dumps({"quality": quality}), ts, ts)).lastrowid
    conn.execute("UPDATE studio_clips SET status = CASE WHEN status = 'candidate' THEN 'selected' ELSE status END "
                 "WHERE id = ?", (cid,))
    conn.commit()
    from ..agents.orchestrator import Orchestrator

    Orchestrator(conn).studio_render(did)
    return did


def get_draft(conn, did: int) -> dict:
    row = conn.execute("SELECT d.*, c.title AS clip_title, c.hook, c.start AS clip_start, c.end AS clip_end, "
                       "r.title AS recording_title, r.business, r.kind, r.ownership, r.creator, r.rights_source_id "
                       "FROM studio_drafts d JOIN studio_clips c ON c.id = d.clip_id "
                       "JOIN studio_recordings r ON r.id = d.recording_id WHERE d.id = ?", (did,)).fetchone()
    if row is None:
        raise LookupError(f"Draft {did} not found")
    d = dict(row)
    d["qc"] = json.loads(d["qc_json"] or "{}")
    d["settings"] = json.loads(d["settings_json"] or "{}")
    return d


def list_drafts(conn, status: str | None = None) -> list[dict]:
    sql = "SELECT id FROM studio_drafts"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    return [get_draft(conn, r[0]) for r in conn.execute(sql + " ORDER BY id DESC", params)]


def review_draft(conn, did: int, decision: str, reviewer: str, notes: str = "", acknowledge_warnings: bool = False) -> None:
    d = get_draft(conn, did)
    reviewer = (reviewer or "").strip()
    if not reviewer:
        raise ValueError("Enter your name.")
    if d["status"] not in ("needs_review", "approved"):
        raise ValueError(f"This draft is '{d['status']}'; it can be reviewed once rendering and QC are finished.")
    if decision == "approved":
        if d["qc_status"] == "fail":
            raise ValueError("QC found failures. Fix them and re-render before approving.")
        if d["qc_status"] == "warn" and not acknowledge_warnings:
            raise ValueError("QC has warnings. Tick 'I've reviewed the warnings' to approve anyway.")
        ok, why = rights_ok(conn, recording(conn, d["recording_id"]))
        if not ok:
            raise PermissionError(why)
        conn.execute("UPDATE studio_drafts SET status = 'approved', approved_by = ?, approved_at = ?, review_notes = ?, "
                     "updated_at = ? WHERE id = ?", (reviewer, now(), notes.strip(), now(), did))
    elif decision in ("rejected", "changes_requested"):
        note = ("Changes requested: " if decision == "changes_requested" else "") + notes.strip()
        conn.execute("UPDATE studio_drafts SET status = 'rejected', approved_by = NULL, approved_at = NULL, "
                     "review_notes = ?, updated_at = ? WHERE id = ?", (note, now(), did))
        if decision == "changes_requested":
            conn.execute("UPDATE studio_clips SET status = 'selected' WHERE id = ?", (d["clip_id"],))
    else:
        raise ValueError("Unknown decision.")
    log(conn, "draft", did, decision, reviewer)
    conn.commit()


def create_idea_from_draft(conn, did: int) -> tuple[int, bool]:
    """Send an approved draft into the idea pipeline (scoring, QC, human approval, manual posting)."""
    d = get_draft(conn, did)
    if d["idea_id"]:
        return d["idea_id"], False
    if d["status"] != "approved":
        raise ValueError("Approve the draft first.")
    clip = get_clip(conn, d["clip_id"])
    fmt = FORMAT_FOR_KIND.get(d["kind"], "podcast_clip")
    brand_slug = {"gta_moment": "gta6-moments", "streamer_story": "gta-streamer-stories"}.get(fmt, "on-the-road-media")
    if d["business"] == "gta6-network" and fmt not in ("gta_moment", "streamer_story", "gta_discovery"):
        fmt, brand_slug = "gta_moment", "gta6-moments"
    if d["business"] == "on-the-road-media" and fmt.startswith(("gta", "streamer")):
        fmt, brand_slug = "podcast_clip", "on-the-road-media"
    brand = conn.execute("SELECT id FROM brands WHERE slug = ?", (brand_slug,)).fetchone()
    third = d["ownership"] == "third_party"
    idea_id = ideas_mod.create_idea(
        conn, d["business"], (clip["title"] or d["recording_title"])[:200], fmt,
        description=f"From Video Studio draft #{did} (recording '{d['recording_title']}', "
                    f"{clip['start']:.1f}s–{clip['end']:.1f}s).\n\n{clip['excerpt'][:800]}",
        hook=clip["hook"], brand_id=brand[0] if brand else None,
        target_platforms=["tiktok", "instagram", "youtube_shorts"], needs_external_footage=third,
        hook_strength=min(10, max(0, round((clip["score"] or 50) / 10))))
    if third and d["rights_source_id"]:
        rights.link_source(conn, idea_id, d["rights_source_id"])
    conn.execute("UPDATE studio_drafts SET idea_id = ? WHERE id = ?", (idea_id, did))
    log(conn, "draft", did, "sent to idea pipeline", str(idea_id))
    conn.commit()
    return idea_id, True


def draft_for_idea(conn, idea_id: int) -> dict | None:
    row = conn.execute("SELECT id FROM studio_drafts WHERE idea_id = ? ORDER BY id DESC LIMIT 1", (idea_id,)).fetchone()
    return get_draft(conn, row[0]) if row else None


def safe_download_name(d: dict) -> str:
    base = re.sub(r"[^\w\- ]+", "", (d.get("clip_title") or "draft"))[:60].strip().replace(" ", "-") or "draft"
    return f"{base}-v{d['version']}.mp4"
