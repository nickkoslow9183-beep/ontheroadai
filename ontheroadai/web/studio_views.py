"""Video Studio pages: upload, library, jobs, transcripts, clip editor, drafts, QC, approvals."""

from __future__ import annotations

import json

from flask import abort, current_app, flash, jsonify, redirect, render_template, request, send_file, url_for

from ..agents.registry import SPECS
from ..studio import captions, clipfinder, framing, jobs, media, service, storage, transcript
from ..studio.pipeline import recording as get_recording
from .helpers import USER_ERRORS, attempt, get_db

STATUS_LABELS = {"queued": "Queued", "processing": "Processing", "needs_review": "Needs review", "failed": "Failed",
                 "completed": "Completed", "cancelled": "Cancelled"}


def _counts(conn):
    j = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM studio_jobs GROUP BY status")}
    d = {r[0]: r[1] for r in conn.execute("SELECT status, COUNT(*) FROM studio_drafts GROUP BY status")}
    return {"jobs": j, "drafts": d,
            "recordings": conn.execute("SELECT COUNT(*) FROM studio_recordings").fetchone()[0],
            "candidates": conn.execute("SELECT COUNT(*) FROM studio_clips WHERE status IN ('candidate','selected')").fetchone()[0]}


def _jobs(conn, limit=50, recording_id=None):
    sql = ("SELECT j.*, r.title AS recording_title FROM studio_jobs j LEFT JOIN studio_recordings r "
           "ON r.id = j.recording_id")
    params = []
    if recording_id:
        sql += " WHERE j.recording_id = ?"
        params.append(recording_id)
    return [dict(r) for r in conn.execute(sql + " ORDER BY j.id DESC LIMIT ?", [*params, limit])]


def register(app):
    @app.context_processor
    def _studio_globals():
        return {"STUDIO_KINDS": service.KINDS, "JOB_LABELS": STATUS_LABELS, "FRAMING_MODES": framing.MODES,
                "CLIP_PRESETS": clipfinder.PRESETS, "WHISPER_MODELS": transcript.MODEL_CHOICES}

    @app.template_filter("tc")
    def timecode(sec):
        if sec is None:
            return "—"
        sec = float(sec)
        return f"{int(sec // 60)}:{sec % 60:05.2f}"

    def _rec_or_404(conn, rid):
        try:
            return get_recording(conn, rid)
        except LookupError:
            abort(404)

    def _clip_or_404(conn, cid):
        try:
            return service.get_clip(conn, cid)
        except LookupError:
            abort(404)

    def _draft_or_404(conn, did):
        try:
            return service.get_draft(conn, did)
        except LookupError:
            abort(404)

    @app.get("/studio")
    def studio_home():
        conn = get_db()
        return render_template("studio_home.html", counts=_counts(conn), jobs=_jobs(conn, 8),
                               tools=media.tools_status(), whisper=transcript.whisper_status(),
                               disk=storage.disk_usage(), worker=bool(current_app.config.get("STUDIO_WORKER")),
                               review=service.list_drafts(conn, "needs_review")[:5],
                               recordings=service.list_recordings(conn)[:5], max_gb=media.MAX_BYTES // 1024 ** 3,
                               default_model=transcript.DEFAULT_MODEL)

    # ---- chunked upload (JSON API used by static/upload.js) ----------------------------
    @app.post("/studio/upload/init")
    def studio_upload_init():
        data = request.get_json(silent=True) or {}
        try:
            token = service.start_upload(get_db(), data.get("filename", ""), int(data.get("size") or 0), data)
        except (ValueError, TypeError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"token": token, "chunk_size": service.CHUNK_MAX})

    @app.put("/studio/upload/<token>")
    def studio_upload_chunk(token):
        try:
            received = service.append_chunk(get_db(), token, int(request.args.get("offset", "-1")),
                                            request.get_data(cache=False))
        except (ValueError, LookupError) as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify({"received": received})

    @app.post("/studio/upload/<token>/complete")
    def studio_upload_complete(token):
        try:
            rid = service.complete_upload(get_db(), token)
        except USER_ERRORS as exc:
            get_db().rollback()
            return jsonify({"error": str(exc)}), 400
        return jsonify({"recording_id": rid, "url": url_for("studio_recording", rid=rid)})

    # ---- library ------------------------------------------------------------------------
    @app.get("/studio/recordings")
    def studio_recordings():
        return render_template("studio_recordings.html", recordings=service.list_recordings(get_db()))

    @app.get("/studio/recordings/<int:rid>")
    def studio_recording(rid):
        conn = get_db()
        rec = _rec_or_404(conn, rid)
        rights_row = conn.execute("SELECT * FROM source_permissions WHERE id = ?", (rec["rights_source_id"],)).fetchone() \
            if rec["rights_source_id"] else None
        return render_template("studio_recording.html", rec=rec, clips=service.list_clips(conn, rid),
                               t=transcript.get(conn, rid), jobs=_jobs(conn, 20, rid), rights_row=rights_row,
                               drafts=[d for d in service.list_drafts(conn) if d["recording_id"] == rid])

    @app.post("/studio/recordings/<int:rid>/<action>")
    def studio_recording_action(rid, action):
        conn = get_db()
        _rec_or_404(conn, rid)
        f = request.form
        if action == "transcribe":
            attempt(lambda: service.retranscribe(conn, rid, f.get("model", transcript.DEFAULT_MODEL)),
                    "Transcription queued.")
        elif action == "import":
            up = request.files.get("file")
            if not up or not up.filename:
                flash("Choose an .srt, .vtt or .json transcript file.", "error")
            else:
                text = up.read(5 * 1024 * 1024).decode("utf-8", "replace")
                attempt(lambda: service.import_transcript(conn, rid, up.filename, text),
                        "Transcript imported. Clip finding queued.")
        elif action == "find":
            attempt(lambda: jobs.enqueue(conn, "find_clips", recording_id=rid,
                                         params={"preset": f.get("preset") or None}), "Clip finding queued.")
        elif action == "manual_clip":
            cid = attempt(lambda: service.add_manual_clip(conn, rid, float(f.get("start") or 0),
                                                          float(f.get("end") or 0)), "Clip added.")
            if cid and cid is not True:
                return redirect(url_for("studio_clip", cid=cid))
        elif action == "delete":
            attempt(lambda: service.delete_recording(conn, rid), "Recording and all its files deleted.")
            return redirect(url_for("studio_recordings"))
        else:
            abort(404)
        return redirect(url_for("studio_recording", rid=rid))

    @app.get("/studio/recordings/<int:rid>/transcript")
    def studio_transcript(rid):
        conn = get_db()
        rec = _rec_or_404(conn, rid)
        return render_template("studio_transcript.html", rec=rec, t=transcript.get(conn, rid))

    @app.post("/studio/recordings/<int:rid>/transcript/<int:seg_id>")
    def studio_transcript_edit(rid, seg_id):
        conn = get_db()
        res = attempt(lambda: transcript.edit_segment(conn, rid, seg_id, request.form.get("text"),
                                                      request.form.get("speaker")), None)
        if res and res is not True:
            flash(res["note"] or "Line saved.", "ok")
        return redirect(url_for("studio_transcript", rid=rid) + f"#seg-{seg_id}")

    @app.get("/studio/recordings/<int:rid>/transcript.srt")
    def studio_transcript_srt(rid):
        t = transcript.get(get_db(), rid)
        if not t:
            abort(404)
        return current_app.response_class(transcript.to_srt(t["segments"]), mimetype="text/plain",
                                          headers={"Content-Disposition": f"attachment; filename=recording-{rid}.srt"})

    # ---- clips ----------------------------------------------------------------------------
    @app.get("/studio/clips/<int:cid>")
    def studio_clip(cid):
        conn = get_db()
        clip = _clip_or_404(conn, cid)
        rec = get_recording(conn, clip["recording_id"])
        t = transcript.get(conn, rec["id"])
        segs = [s for s in (t["segments"] if t else []) if s["end"] > clip["start"] - 15 and s["start"] < clip["end"] + 15]
        drafts = [d for d in service.list_drafts(conn) if d["clip_id"] == cid]
        return render_template("studio_clip.html", clip=clip, rec=rec, segs=segs, drafts=drafts,
                               fonts=media.list_fonts(), positions=list(captions.POSITIONS),
                               word_timings=bool(t and t["word_timings"]), overlays_json=json.dumps(clip["overlays"], indent=1))

    @app.post("/studio/clips/<int:cid>")
    def studio_clip_save(cid):
        conn = get_db()
        clip = _clip_or_404(conn, cid)
        f = request.form
        action = f.get("action", "save")
        if action in ("select", "reject", "restore"):
            attempt(lambda: service.update_clip(conn, cid, status={"select": "selected", "reject": "rejected",
                                                                   "restore": "candidate"}[action]), "Clip updated.")
            return redirect(request.form.get("next") or url_for("studio_clip", cid=cid))
        try:
            kfs = []
            for t_, m_, x_ in zip(f.getlist("kf_t"), f.getlist("kf_mode"), f.getlist("kf_x")):
                if t_.strip():
                    kfs.append({"t": float(t_), "mode": m_, "x": float(x_ or 0.5)})
            overlays = json.loads(f.get("overlays") or "[]")
            fields = {
                "start": float(f.get("start")), "end": float(f.get("end")), "preset": f.get("preset"),
                "hook": f.get("hook", ""), "title": f.get("title", ""), "tighten_pauses": f.get("tighten") == "1",
                "framing": {"mode": f.get("mode"), "x": float(f.get("x") or 0.5),
                            "left_x": float(f.get("left_x") or 0.27), "right_x": float(f.get("right_x") or 0.73),
                            "keyframes": kfs},
                "caption": {"enabled": f.get("cap_enabled") == "1", "font": f.get("font"),
                            "size": int(f.get("size") or 64), "color": f.get("color"),
                            "outline_color": f.get("outline_color"), "outline": int(f.get("outline") or 5),
                            "highlight": f.get("highlight"), "highlight_color": f.get("highlight_color"),
                            "position": f.get("position"), "max_chars": int(f.get("max_chars") or 22),
                            "uppercase": f.get("uppercase") == "1"},
                "overlays": overlays,
            }
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            flash(f"Couldn't read the form: {exc}", "error")
            return redirect(url_for("studio_clip", cid=cid))
        saved = attempt(lambda: service.update_clip(conn, cid, **fields), "Clip saved.")
        if saved and action == "render":
            did = attempt(lambda: service.render_clip(conn, cid, f.get("quality", "balanced")),
                          "Render queued. Watch progress under Processing jobs.")
            if did and did is not True:
                return redirect(url_for("studio_draft", did=did))
        return redirect(url_for("studio_clip", cid=clip["id"]))

    # ---- drafts & approvals ----------------------------------------------------------------
    @app.get("/studio/drafts")
    def studio_drafts():
        return render_template("studio_drafts.html", drafts=service.list_drafts(get_db()), title="Video drafts")

    @app.get("/studio/approvals")
    def studio_approvals():
        return render_template("studio_drafts.html", drafts=service.list_drafts(get_db(), "needs_review"),
                               title="Approval queue")

    @app.get("/studio/drafts/<int:did>")
    def studio_draft(did):
        conn = get_db()
        d = _draft_or_404(conn, did)
        job = conn.execute("SELECT * FROM studio_jobs WHERE draft_id = ? ORDER BY id DESC LIMIT 1", (did,)).fetchone()
        return render_template("studio_draft.html", d=d, clip=service.get_clip(conn, d["clip_id"]),
                               job=dict(job) if job else None)

    @app.post("/studio/drafts/<int:did>/review")
    def studio_draft_review(did):
        conn = get_db()
        _draft_or_404(conn, did)
        f = request.form
        attempt(lambda: service.review_draft(conn, did, f.get("decision", ""), f.get("reviewer", ""), f.get("notes", ""),
                                             f.get("ack") == "1"), "Review recorded.")
        return redirect(url_for("studio_draft", did=did))

    @app.post("/studio/drafts/<int:did>/idea")
    def studio_draft_idea(did):
        conn = get_db()
        _draft_or_404(conn, did)
        res = attempt(lambda: service.create_idea_from_draft(conn, did), None)
        if res and res is not True:
            flash("Idea created and linked to this draft." if res[1] else "Already linked to an idea.", "ok")
            return redirect(url_for("idea_detail", idea_id=res[0]))
        return redirect(url_for("studio_draft", did=did))

    # ---- jobs ---------------------------------------------------------------------------------
    @app.get("/studio/jobs")
    def studio_jobs():
        return render_template("studio_jobs.html", jobs=_jobs(get_db(), 100),
                               worker=bool(current_app.config.get("STUDIO_WORKER")))

    @app.get("/studio/jobs.json")
    def studio_jobs_json():
        return jsonify([{k: j[k] for k in ("id", "kind", "status", "progress", "message", "recording_id", "draft_id")}
                        for j in _jobs(get_db(), 30)])

    @app.post("/studio/jobs/<int:job_id>/retry")
    def studio_job_retry(job_id):
        conn = get_db()
        j = conn.execute("SELECT * FROM studio_jobs WHERE id = ?", (job_id,)).fetchone()
        if j is None:
            abort(404)
        if j["status"] != "failed":
            flash("Only failed jobs can be retried.", "error")
        else:
            conn.execute("UPDATE studio_jobs SET status = 'queued', attempts = 0, error = '', message = 'Retry queued' "
                         "WHERE id = ?", (job_id,))
            if j["draft_id"]:
                conn.execute("UPDATE studio_drafts SET status = 'queued' WHERE id = ?", (j["draft_id"],))
            conn.commit()
            flash("Job queued again.", "ok")
        return redirect(url_for("studio_jobs"))

    # ---- media files (streamed from disk, with seeking support) ---------------------------------
    @app.get("/studio/media/recording/<int:rid>")
    def studio_media_recording(rid):
        rec = _rec_or_404(get_db(), rid)
        p = storage.resolve(rec["stored_path"])
        if not p or not p.exists():
            abort(404)
        return send_file(p, conditional=True, mimetype="video/mp4")

    @app.get("/studio/media/recording/<int:rid>/thumb.jpg")
    def studio_media_thumb(rid):
        _rec_or_404(get_db(), rid)
        p = storage.recording_dir(rid) / "thumb.jpg"
        if not p.exists():
            abort(404)
        return send_file(p, mimetype="image/jpeg")

    @app.get("/studio/media/draft/<int:did>")
    def studio_media_draft(did):
        d = _draft_or_404(get_db(), did)
        p = storage.resolve(d["file_path"]) if d["file_path"] else None
        if not p or not p.exists():
            abort(404)
        dl = request.args.get("download") == "1"
        return send_file(p, conditional=True, mimetype="video/mp4", as_attachment=dl,
                         download_name=service.safe_download_name(d) if dl else None)

    @app.get("/agents/studio-status")
    def studio_agent_status():
        return jsonify({k: SPECS[k].status for k in ("clip_finder", "video_editor", "quality_control")})
