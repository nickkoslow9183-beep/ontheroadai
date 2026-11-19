"""Video Studio integration: real FFmpeg rendering + QC on generated fixture videos, jobs,
uploads, approval rules, agent integration and migration. Transcripts are imported fixtures."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import make_ready_idea  # noqa: F401
from ontheroadai import ideas, rights
from ontheroadai.agents.orchestrator import Orchestrator
from ontheroadai.db import connect, init_db
from ontheroadai.studio import jobs, media, service, storage, videoqc
from studio_fixtures import SRT, make_video, words_json

OTRM = {"business": "on-the-road-media", "kind": "podcast", "title": "Fixture episode"}


@pytest.fixture(autouse=True)
def _no_whisper(monkeypatch):
    """Fixture videos contain a tone, not speech: don't run (or download) Whisper in these tests.
    This also exercises the 'transcription unavailable -> import a transcript' path."""
    from ontheroadai.studio import transcript

    def unavailable(*a, **k):
        raise transcript.TranscriptionUnavailable("disabled in fixture tests")
    monkeypatch.setattr(transcript, "transcribe", unavailable)


@pytest.fixture
def rec(conn, tmp_path):
    v = make_video(tmp_path / "ep.mp4", dur=8)
    rid = service.add_recording(conn, v, "ep.mp4", OTRM)
    done = jobs.run_until_idle(conn)
    assert done[0]["kind"] == "inspect"
    return rid


def _import_words(conn, rid):
    service.import_transcript(conn, rid, "t.json", words_json())
    jobs.run_until_idle(conn)


def test_add_recording_inspects_and_reports_no_transcriber(conn, rec):
    r = conn.execute("SELECT * FROM studio_recordings WHERE id = ?", (rec,)).fetchone()
    assert r["status"] == "ready" and r["width"] == 1280 and r["has_audio"] == 1
    assert storage.resolve(r["stored_path"]).exists()
    assert (storage.recording_dir(rec) / "thumb.jpg").exists()
    js = [dict(j) for j in conn.execute("SELECT kind, status, message FROM studio_jobs ORDER BY id")]
    assert [j["kind"] for j in js[:2]] == ["inspect", "transcribe"]  # transcription attempted after inspection
    assert js[1]["status"] == "needs_review" and "Import a transcript" in js[1]["message"]
    assert not (storage.work_dir(rec) / "audio.wav").exists()  # temporary audio cleaned up


def test_duplicate_upload_rejected(conn, rec, tmp_path):
    src = storage.resolve(conn.execute("SELECT stored_path FROM studio_recordings").fetchone()[0])
    with pytest.raises(ValueError, match="already in the library"):
        service.add_recording(conn, src, "again.mp4", OTRM)


def test_chunked_upload(conn, tmp_path):
    v = make_video(tmp_path / "up.mp4", dur=3)
    data = v.read_bytes()
    token = service.start_upload(conn, "My Show.mov", len(data), OTRM)
    half = len(data) // 2
    assert service.append_chunk(conn, token, 0, data[:half]) == half
    with pytest.raises(ValueError, match="offset"):
        service.append_chunk(conn, token, 0, data[half:])
    service.append_chunk(conn, token, half, data[half:])
    rid = service.complete_upload(conn, token)
    r = conn.execute("SELECT * FROM studio_recordings WHERE id = ?", (rid,)).fetchone()
    assert r["original_filename"] == "My Show.mov" and r["stored_path"].endswith("source.mov")
    assert not list((storage.media_root() / "uploads").iterdir())  # temp file cleaned up
    with pytest.raises(ValueError):
        service.start_upload(conn, "x.mkv", 100, OTRM)
    tok = service.start_upload(conn, "short.mp4", 10, OTRM)
    service.append_chunk(conn, tok, 0, b"12345")
    with pytest.raises(ValueError, match="incomplete"):
        service.complete_upload(conn, tok)


def test_import_transcript_finds_clips(conn, rec):
    service.import_transcript(conn, rec, "ep.srt", SRT)
    j = jobs.run_until_idle(conn)[-1]
    assert j["kind"] == "find_clips" and j["status"] in ("needs_review", "completed")
    with pytest.raises(ValueError, match="only"):
        service.import_transcript(conn, rec, "long.srt", "1\n00:10:00,000 --> 00:10:02,000\nhi\n")


def test_render_real_mp4_and_qc(conn, rec):
    _import_words(conn, rec)
    cid = service.add_manual_clip(conn, rec, 0.0, 7.0)
    service.update_clip(conn, cid, framing={"mode": "left", "keyframes": [{"t": 3.0, "mode": "right"}]},
                        overlays=[{"type": "board", "title": "DRAFT", "lines": [
                            {"label": "Level 1", "pick": "Chick-fil-A", "phrase": "chick-fil-a"}]}])
    did = service.render_clip(conn, cid, "fast")
    for j in jobs.run_until_idle(conn):
        assert j["status"] != "failed", j["error"]
    d = service.get_draft(conn, did)
    out = storage.resolve(d["file_path"])
    pr = media.probe(out)
    assert (pr.width, pr.height) == (1080, 1920) and pr.video_codec == "h264" and pr.audio_codec == "aac"
    assert pr.duration == pytest.approx(7.0, abs=0.3)
    assert d["status"] == "needs_review" and d["qc_status"] in ("pass", "warn")
    checks = {c["key"]: c["status"] for c in d["qc"]["checks"]}
    assert checks["decodes"] == checks["size"] == checks["aspect"] == checks["audio"] == checks["faststart"] == "pass"
    assert checks["caption_timing"] == "pass"
    assert [p["mode"] for p in d["settings"]["pieces"]] == ["left", "right"]
    assert d["settings"]["captions"]["timing"] == "word"
    assert "Technical checks only" in d["qc"]["disclaimer"]


@pytest.mark.parametrize("mode", ["both", "split", "manual"])
def test_render_layouts(conn, rec, mode):
    cid = service.add_manual_clip(conn, rec, 1.0, 3.0)
    service.update_clip(conn, cid, framing={"mode": mode, "x": 0.8}, caption={"enabled": False})
    did = service.render_clip(conn, cid, "fast")
    jobs.run_until_idle(conn)
    d = service.get_draft(conn, did)
    assert d["width"] == 1080 and d["height"] == 1920 and d["status"] == "needs_review"


def test_qc_catches_problems(tmp_path):
    wrong = make_video(tmp_path / "w.mp4", w=640, h=360, dur=3)
    r = videoqc.run_checks(wrong, {"duration": 3, "captions_enabled": True, "captions": {"cues": []}})
    st = {c["key"]: c["status"] for c in r["checks"]}
    assert r["status"] == "fail" and st["size"] == "fail" and st["aspect"] == "fail" and st["captions"] == "fail"
    silent = make_video(tmp_path / "n.mp4", w=1080, h=1920, dur=2, audio=False)
    r = videoqc.run_checks(silent, {"duration": 2})
    assert {c["key"]: c["status"] for c in r["checks"]}["audio"] == "fail"
    black = make_video(tmp_path / "b.mp4", w=1080, h=1920, dur=3, black_from=1.5)
    r = videoqc.run_checks(black, {"duration": 3})
    assert {c["key"]: c["status"] for c in r["checks"]}["black"] == "warn"
    missing = videoqc.run_checks(tmp_path / "nope.mp4", {})
    assert missing["status"] == "fail"


def test_third_party_footage_blocked_until_permission(conn, tmp_path):
    v = make_video(tmp_path / "s.mp4", dur=4)
    rid = service.add_recording(conn, v, "s.mp4", {"business": "gta6-network", "kind": "streamer_footage",
                                                   "creator": "SomeStreamer", "title": "Stream VOD"})
    jobs.run_until_idle(conn)
    r = conn.execute("SELECT * FROM studio_recordings WHERE id = ?", (rid,)).fetchone()
    assert r["ownership"] == "third_party"
    perm = conn.execute("SELECT * FROM source_permissions WHERE id = ?", (r["rights_source_id"],)).fetchone()
    assert perm["status"] == "unknown"  # never assumed
    cid = service.add_manual_clip(conn, rid, 0.5, 3.0)
    with pytest.raises(PermissionError, match="permission"):
        service.render_clip(conn, cid)
    with pytest.raises(ValueError):
        service.add_recording(conn, make_video(tmp_path / "t.mp4", dur=2), "t.mp4",
                              {"business": "gta6-network", "kind": "streamer_footage"})  # creator required
    rights.update_source_status(conn, r["rights_source_id"], "granted", evidence="Signed release on file")
    did = service.render_clip(conn, cid, "fast")
    jobs.run_until_idle(conn)
    assert service.get_draft(conn, did)["status"] == "needs_review"


def test_approval_rules_and_idea_pipeline(conn, rec):
    _import_words(conn, rec)
    cid = service.add_manual_clip(conn, rec, 0.0, 7.0)
    did = service.render_clip(conn, cid, "fast")
    with pytest.raises(ValueError, match="once rendering"):
        service.review_draft(conn, did, "approved", "Nick")  # still queued
    jobs.run_until_idle(conn)
    with pytest.raises(ValueError, match="name"):
        service.review_draft(conn, did, "approved", " ")
    d = service.get_draft(conn, did)
    if d["qc_status"] == "warn":
        with pytest.raises(ValueError, match="warnings"):
            service.review_draft(conn, did, "approved", "Nick")
    with pytest.raises(ValueError, match="Approve the draft first"):
        service.create_idea_from_draft(conn, did)
    service.review_draft(conn, did, "approved", "Nick", acknowledge_warnings=True)
    idea_id, created = service.create_idea_from_draft(conn, did)
    assert created and service.create_idea_from_draft(conn, did) == (idea_id, False)
    idea = ideas.get_idea(conn, idea_id)
    assert idea["content_type"] == "podcast_clip" and idea["status"] == "draft"
    orch = Orchestrator(conn)
    orch.plan(idea_id)
    by = {t["agent_key"]: t for t in orch.tasks(idea_id)}
    assert by["clip_finder"]["status"] == "pending" or by["clip_finder"]["status"] in ("done", "manual_required")
    while (m := [t for t in orch.tasks(idea_id) if t["status"] == "manual_required"]):
        orch.complete_manual_task(m[0]["id"], "Tester")
    by = {t["agent_key"]: t for t in orch.tasks(idea_id)}
    assert by["clip_finder"]["completed_by"] == "agent:clip_finder"
    assert by["video_editor"]["completed_by"] == "agent:video_editor"
    assert "[PASS] Video draft passed QC and was approved" in by["quality_control"]["output"]
    assert ideas.get_idea(conn, idea_id)["status"] == "ready_for_review"


def test_db_refuses_unapproved_or_failed_qc_approval(conn, rec):
    cid = service.add_manual_clip(conn, rec, 0.0, 2.0)
    did = service.render_clip(conn, cid, "fast")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE studio_drafts SET status = 'approved' WHERE id = ?", (did,))
    conn.rollback()
    conn.execute("UPDATE studio_drafts SET status = 'needs_review', qc_status = 'fail' WHERE id = ?", (did,))
    conn.commit()
    with pytest.raises(ValueError, match="QC found failures"):
        service.review_draft(conn, did, "approved", "Nick")


def test_render_failure_and_job_recovery(conn, rec):
    cid = service.add_manual_clip(conn, rec, 0.0, 2.0)
    did = service.render_clip(conn, cid, "fast")
    storage.resolve(conn.execute("SELECT stored_path FROM studio_recordings").fetchone()[0]).unlink()
    j = jobs.run_until_idle(conn)[0]
    assert j["status"] == "failed" and "missing" in j["message"]
    assert service.get_draft(conn, did)["status"] == "failed"
    # an interrupted job (app closed mid-render) is re-queued, then failed after too many attempts
    jid = jobs.enqueue(conn, "qc", draft_id=did)
    conn.execute("UPDATE studio_jobs SET status = 'processing', attempts = 1, heartbeat_at = ? WHERE id = ?",
                 ((datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(), jid))
    conn.commit()
    assert jobs.recover(conn) == 1
    assert conn.execute("SELECT status FROM studio_jobs WHERE id = ?", (jid,)).fetchone()[0] == "queued"
    conn.execute("UPDATE studio_jobs SET status = 'processing', attempts = 2 WHERE id = ?", (jid,))
    conn.commit()
    jobs.recover(conn, all_processing=True)
    assert conn.execute("SELECT status FROM studio_jobs WHERE id = ?", (jid,)).fetchone()[0] == "failed"
    assert jobs.enqueue(conn, "find_clips", recording_id=rec) == jobs.enqueue(conn, "find_clips", recording_id=rec)


def test_clip_edits_validate_and_rescore(conn, rec):
    _import_words(conn, rec)
    cid = service.add_manual_clip(conn, rec, 0.0, 7.0)
    with pytest.raises(ValueError, match="after the start"):
        service.update_clip(conn, cid, start=5, end=4)
    with pytest.raises(ValueError, match="only"):
        service.update_clip(conn, cid, start=1, end=60)
    c = service.update_clip(conn, cid, start=0.1, end=3.0)
    assert c["start"] == 0.1 and c["score_detail"].get("rescored")


def test_delete_recording_removes_files(conn, rec):
    cid = service.add_manual_clip(conn, rec, 0.0, 2.0)
    did = service.render_clip(conn, cid, "fast")
    jobs.run_until_idle(conn)
    draft_file = storage.resolve(service.get_draft(conn, did)["file_path"])
    assert draft_file.exists()
    service.delete_recording(conn, rec)
    assert not draft_file.exists() and not (storage.media_root() / "recordings" / str(rec)).exists()
    assert conn.execute("SELECT COUNT(*) FROM studio_drafts").fetchone()[0] == 0


def test_path_safety():
    with pytest.raises(ValueError):
        storage.resolve("../../etc/passwd")
    assert storage.safe_display_name("../../evil\\name<script>.mp4") == "name_script_.mp4"


def test_upgrade_from_milestone_3_database(tmp_path):
    db = tmp_path / "m3.db"
    c = connect(db)
    init_db(c)
    for t in ("studio_jobs", "studio_drafts", "studio_clips", "studio_transcripts", "studio_recordings", "studio_uploads"):
        c.execute(f"DROP TABLE {t}")
    c.execute("PRAGMA user_version = 3")
    iid = ideas.create_idea(c, "on-the-road-media", "Kept through upgrade", "fan_reaction")
    c.commit()
    c.close()
    c = connect(db)
    init_db(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] == 4
    assert ideas.get_idea(c, iid)["title"] == "Kept through upgrade"
    assert c.execute("SELECT COUNT(*) FROM studio_recordings").fetchone()[0] == 0
    c.close()
