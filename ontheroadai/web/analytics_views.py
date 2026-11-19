"""Analytics pages: business dashboards, CSV import, videos, snapshots, comparison."""

from __future__ import annotations

import json
from datetime import date

from flask import abort, flash, redirect, render_template, request, url_for

from ..analytics import csv_import, insights, store
from ..analytics.fields import FIELDS, METRIC_KEYS
from ..constants import BUSINESS_PLATFORMS, CONTENT_TYPES
from .helpers import attempt, get_db


def _businesses(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM businesses ORDER BY id")]


def _business_or_404(conn, slug):
    row = conn.execute("SELECT * FROM businesses WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        abort(404)
    return dict(row)


def _accounts(conn, slug=None):
    sql = ("SELECT a.id, a.platform, a.brand_id, br.name AS brand_name, b.slug AS business_slug, b.name AS business_name "
           "FROM accounts a JOIN brands br ON br.id = a.brand_id JOIN businesses b ON b.id = br.business_id ")
    params = []
    if slug:
        sql += "WHERE b.slug = ? "
        params.append(slug)
    return [dict(r) for r in conn.execute(sql + "ORDER BY b.id, br.id, a.id", params)]


def _filters():
    a = request.args
    return {k: a.get(k, "").strip() for k in ("brand_id", "platform", "format", "length", "date_from", "date_to")
            if a.get(k, "").strip()}


def _video_or_404(conn, video_id):
    try:
        return store.get_video(conn, video_id)
    except LookupError:
        abort(404)


def register(app):
    # ---- hub + manual entry --------------------------------------------------------
    @app.get("/analytics")
    def analytics_page():
        conn = get_db()
        cards = []
        for b in _businesses(conn):
            vids = insights.load_videos(conn, b["slug"])
            cards.append({**b, "kpis": insights.kpis(vids)})
        published = [dict(r) for r in conn.execute(
            "SELECT i.id, i.title, b.slug AS business_slug FROM ideas i JOIN businesses b ON b.id = i.business_id "
            "WHERE i.status = 'published' ORDER BY i.published_at DESC")]
        return render_template("analytics_hub.html", cards=cards, accounts=_accounts(conn),
                               idea_opts=published, imports=csv_import.list_imports(conn, limit=15),
                               fields=FIELDS, metric_keys=METRIC_KEYS)

    @app.post("/analytics")
    def record_analytics():
        f = request.form
        conn = get_db()
        result = attempt(lambda: store.add_manual_entry(
            conn, int(f.get("account_id") or 0),
            {"title": f.get("title", ""), "post_url": f.get("post_url", ""), "external_id": f.get("post_id", ""),
             "published_at": f.get("published_at", ""), "duration_seconds": f.get("duration_seconds", ""),
             "topic": f.get("topic", ""), "format": f.get("format", ""), "idea_id": f.get("idea_id") or None},
            f.get("recorded_on") or None, {k: f.get(k, "") for k in METRIC_KEYS}),
            "Metrics recorded. Blank fields were stored as 'not available'.")
        if result and result is not True:
            return redirect(url_for("video_detail", video_id=result[0]))
        return redirect(request.form.get("next") or url_for("analytics_page"))

    # ---- business dashboards ---------------------------------------------------------
    @app.get("/analytics/<slug>")
    def analytics_business(slug):
        conn = get_db()
        b = _business_or_404(conn, slug)
        filters = _filters()
        videos = insights.load_videos(conn, slug, filters)
        brands = [dict(r) for r in conn.execute("SELECT id, name FROM brands WHERE business_id = ? ORDER BY id",
                                                (b["id"],))]
        formats_in_use = sorted({v["format"] for v in insights.load_videos(conn, slug) if v.get("format")})
        return render_template(
            "analytics_business.html", b=b, videos=videos, filters=filters, brands=brands,
            platforms=BUSINESS_PLATFORMS[slug], content_types=CONTENT_TYPES[slug],
            extra_formats=[f for f in formats_in_use if f not in CONTENT_TYPES[slug]],
            lengths=[label for _, label in insights.LENGTH_BUCKETS],
            kpis=insights.kpis(videos), retention=insights.retention_report(videos),
            conversion=insights.conversion_report(videos), recs=insights.recommendations(videos, slug),
            imports=csv_import.list_imports(conn, slug, limit=10))

    @app.post("/analytics/<slug>/videos/bulk")
    def analytics_bulk(slug):
        conn = get_db()
        _business_or_404(conn, slug)
        ids = [int(i) for i in request.form.getlist("ids") if i.isdigit()]
        fmt = request.form.get("bulk_format")
        topic = request.form.get("bulk_topic", "").strip()
        attempt(lambda: store.bulk_tag(conn, ids, slug, fmt if fmt else None, topic or None),
                lambda n: f"Updated {n} video(s).")
        return redirect(url_for("analytics_business", slug=slug) + "#videos")

    @app.get("/analytics/<slug>/compare")
    def analytics_compare(slug):
        conn = get_db()
        b = _business_or_404(conn, slug)
        ids = [i for i in request.args.getlist("ids") if i.isdigit()]
        try:
            result = insights.compare(conn, slug, [int(i) for i in ids])
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("analytics_business", slug=slug) + "#videos")
        return render_template("analytics_compare.html", b=b, c=result)

    # ---- CSV import --------------------------------------------------------------
    @app.route("/analytics/<slug>/import", methods=["GET", "POST"])
    def analytics_import(slug):
        conn = get_db()
        b = _business_or_404(conn, slug)
        if request.method == "POST":
            upload = request.files.get("file")
            if upload is None or not upload.filename:
                flash("Choose a CSV file to upload.", "error")
            else:
                data = upload.read()
                new_id = attempt(lambda: csv_import.start_import(
                    conn, int(request.form.get("account_id") or 0), upload.filename, data,
                    request.form.get("captured_on") or None), None)
                if new_id and new_id is not True:
                    return redirect(url_for("import_detail", import_id=new_id))
        return render_template("analytics_import.html", b=b, accounts=_accounts(conn, slug),
                               imports=csv_import.list_imports(conn, slug), today=date.today().isoformat())

    @app.route("/analytics/imports/<int:import_id>", methods=["GET", "POST"])
    def import_detail(import_id):
        conn = get_db()
        try:
            imp = csv_import.get_import(conn, import_id)
        except LookupError:
            abort(404)
        ctx = {"imp": imp, "fields": FIELDS, "plan": None}
        if imp["status"] != "pending":
            return render_template("analytics_import_detail.html", **ctx)

        f = request.form
        header_row = int(f.get("header_row") or request.args.get("header_row") or imp["header_row"] or 1)
        try:
            headers, rows = csv_import.load_rows(imp, header_row)
        except csv_import.ImportRejected as exc:
            flash(str(exc), "error")
            if header_row != 1:
                return redirect(url_for("import_detail", import_id=import_id))
            csv_import.discard_import(conn, import_id)
            return redirect(url_for("analytics_import", slug=imp["business_slug"]))
        mapping, options, source = csv_import.suggest_mapping(conn, headers)
        if request.method == "POST":
            mapping = {fl.key: (f.get(f"map_{fl.key}") or None) for fl in FIELDS}
            options = {"fraction_percent": f.get("fraction_percent") == "1", "day_first": f.get("day_first") == "1"}
            source = "your choices"
            action = f.get("action", "preview")
            if action == "discard":
                csv_import.discard_import(conn, import_id)
                flash("Upload discarded. Nothing was imported.", "ok")
                return redirect(url_for("analytics_import", slug=imp["business_slug"]))
            if action == "import":
                result = attempt(lambda: csv_import.commit_import(conn, import_id, mapping, options, header_row,
                                                                  f.get("preset_name")), None)
                if result and result is not True:
                    flash(f"Imported {result['importable']} row(s): {result['new_videos']} new video(s). "
                          f"{result['duplicates']} duplicate(s) and {result['errors']} problem row(s) skipped.", "ok")
                    return redirect(url_for("import_detail", import_id=import_id))
                try:
                    imp = csv_import.get_import(conn, import_id)
                except LookupError:
                    abort(404)
                if imp["status"] != "pending":
                    return redirect(url_for("analytics_import", slug=imp["business_slug"]))
            else:
                try:
                    ctx["plan"] = csv_import.plan_import(conn, imp, mapping, options, header_row)
                except (ValueError, csv_import.ImportRejected) as exc:
                    flash(str(exc), "error")
        ctx.update(headers=headers, sample=csv_import.sample_rows(rows, headers), mapping=mapping, options=options,
                   source=source, header_row=header_row, row_total=len(rows))
        return render_template("analytics_import_detail.html", **ctx)

    @app.post("/analytics/imports/<int:import_id>/undo")
    def import_undo(import_id):
        conn = get_db()
        attempt(lambda: csv_import.undo_import(conn, import_id),
                lambda r: f"Import undone: removed {r['snapshots']} snapshot(s) and {r['videos']} video(s).")
        return redirect(url_for("import_detail", import_id=import_id))

    # ---- videos & snapshots -----------------------------------------------------------
    @app.get("/analytics/videos/<int:video_id>")
    def video_detail(video_id):
        conn = get_db()
        v = _video_or_404(conn, video_id)
        all_videos = insights.load_videos(conn, v["business_slug"])
        derived = next((x for x in all_videos if x["id"] == video_id), None)
        snaps = store.snapshots(conn, video_id)
        ideas = [dict(r) for r in conn.execute(
            "SELECT i.id, i.title, i.status FROM ideas i JOIN businesses b ON b.id = i.business_id "
            "WHERE b.slug = ? AND i.status NOT IN ('archived') ORDER BY i.id DESC", (v["business_slug"],))]
        return render_template("analytics_video.html", v=v, d=derived, snaps=snaps, ideas=ideas,
                               content_types=CONTENT_TYPES[v["business_slug"]], fields=FIELDS,
                               metric_keys=METRIC_KEYS)

    @app.post("/analytics/videos/<int:video_id>")
    def video_update(video_id):
        conn = get_db()
        _video_or_404(conn, video_id)
        f = request.form
        attempt(lambda: store.update_video(
            conn, video_id, title=f.get("title", ""), post_url=f.get("post_url", ""), topic=f.get("topic", ""),
            format=f.get("format", ""), published_at=f.get("published_at", ""),
            duration_seconds=f.get("duration_seconds", ""), idea_id=f.get("idea_id") or None),
            "Video details saved.")
        return redirect(url_for("video_detail", video_id=video_id))

    @app.post("/analytics/videos/<int:video_id>/delete")
    def video_delete(video_id):
        conn = get_db()
        v = _video_or_404(conn, video_id)
        attempt(lambda: store.delete_video(conn, video_id), "Video and its snapshots deleted.")
        return redirect(url_for("analytics_business", slug=v["business_slug"]) + "#videos")

    @app.post("/analytics/videos/<int:video_id>/snapshots")
    def snapshot_add(video_id):
        conn = get_db()
        _video_or_404(conn, video_id)
        f = request.form

        def run():
            sid = store.add_snapshot(conn, video_id, f.get("captured_on") or None,
                                     {k: f.get(k, "") for k in METRIC_KEYS}, "manual", notes=f.get("notes", ""))
            conn.commit()
            store.after_data_change(conn, [video_id])
            return sid
        attempt(run, "Snapshot added.")
        return redirect(url_for("video_detail", video_id=video_id) + "#snapshots")

    @app.post("/analytics/snapshots/<int:snapshot_id>")
    def snapshot_update(snapshot_id):
        conn = get_db()
        row = conn.execute("SELECT video_id FROM video_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if row is None:
            abort(404)
        f = request.form
        attempt(lambda: store.update_snapshot(conn, snapshot_id, {k: f.get(k, "") for k in METRIC_KEYS},
                                              f.get("captured_on") or None, f.get("notes")), "Snapshot corrected.")
        return redirect(url_for("video_detail", video_id=row["video_id"]) + "#snapshots")

    @app.post("/analytics/snapshots/<int:snapshot_id>/delete")
    def snapshot_delete(snapshot_id):
        conn = get_db()
        row = conn.execute("SELECT video_id FROM video_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
        if row is None:
            abort(404)
        attempt(lambda: store.delete_snapshot(conn, snapshot_id), "Snapshot deleted.")
        return redirect(url_for("video_detail", video_id=row["video_id"]) + "#snapshots")

    @app.errorhandler(413)
    def too_large(_exc):
        flash("That file is too large. CSV uploads are limited to 5 MB.", "error")
        return redirect(request.referrer or url_for("analytics_page"))

    @app.template_filter("pct")
    def pct(value, digits=0):
        return "—" if value is None else f"{value * 100:.{digits}f}%"

    @app.template_filter("num")
    def num(value, digits=None):
        if value is None:
            return "—"
        if isinstance(value, int) or (digits is None and float(value).is_integer()):
            return f"{int(value):,}"
        return f"{value:,.{digits if digits is not None else 1}f}"

    @app.template_filter("tojson_attr")
    def tojson_attr(value):
        return json.dumps(value)
