"""Browser dashboard (Flask). Runs locally; binds to 127.0.0.1 by default."""

from __future__ import annotations

import os
import sqlite3

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for
from markupsafe import Markup, escape

from .. import analytics as analytics_mod
from ..analytics.insights import format_benchmark
from .. import approvals, ideas, publishing, rights
from ..agents import SPECS
from ..agents.orchestrator import Orchestrator
from ..constants import (BUSINESS_PLATFORMS, CONTENT_TYPES, PLATFORMS, SCORE_INPUTS, SOURCE_STATUSES,
                         SOURCE_TYPES)
from ..db import connect, default_db_path, init_db
from ..scoring import FACTORS, WEIGHTS
from .helpers import attempt, get_db


def create_app(db_path: str | None = None, start_scheduler: bool = False) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = str(db_path or default_db_path())
    # Random per-process key for flash messages only. Never committed.
    app.config["SECRET_KEY"] = os.environ.get("ONTHEROADAI_SECRET_KEY") or os.urandom(32).hex()
    app.config["MAX_CONTENT_LENGTH"] = 6 * 1024 * 1024  # uploads; CSV import itself caps files at 5 MB

    with app.app_context():
        conn = connect(app.config["DB_PATH"])
        init_db(conn)
        conn.close()

    db = get_db

    @app.teardown_appcontext
    def _close(_exc):
        conn = g.pop("db", None)
        if conn is not None:
            conn.close()

    def businesses():
        return [dict(r) for r in db().execute("SELECT * FROM businesses ORDER BY id").fetchall()]

    def business_or_404(slug):
        row = db().execute("SELECT * FROM businesses WHERE slug = ?", (slug,)).fetchone()
        if row is None:
            abort(404)
        return dict(row)

    def idea_or_404(idea_id):
        try:
            return ideas.get_idea(db(), idea_id)
        except LookupError:
            abort(404)

    @app.template_global()
    def badge(status):
        status = str(status or "")
        return Markup('<span class="badge s-{0}">{1}</span>').format(
            escape(status), escape(status.replace("_", " ")))

    @app.context_processor
    def _globals():
        return {"nav_businesses": businesses(), "PLATFORMS": PLATFORMS, "SPECS": SPECS,
                "SOURCE_TYPES": SOURCE_TYPES, "SOURCE_STATUSES": SOURCE_STATUSES}

    # ---- overview -------------------------------------------------------
    @app.get("/")
    def overview():
        conn = db()
        cards = []
        for b in businesses():
            counts = {r["status"]: r["n"] for r in conn.execute(
                "SELECT status, COUNT(*) AS n FROM ideas WHERE business_id = ? GROUP BY status", (b["id"],))}
            cards.append({**b, "counts": counts, "total": sum(counts.values())})
        orch = Orchestrator(conn)
        return render_template(
            "overview.html", cards=cards, open_tasks=orch.open_tasks(),
            review=ideas.list_ideas(conn, status="ready_for_review"),
            counts=orch.status_counts(),
            activity=[dict(r) for r in conn.execute("SELECT * FROM activity_log ORDER BY id DESC LIMIT 15")],
        )

    @app.get("/health")
    def health():
        db().execute("SELECT 1")
        return {"status": "ok"}

    # ---- business sections ---------------------------------------------
    @app.get("/business/<slug>")
    def business(slug):
        b = business_or_404(slug)
        conn = db()
        status = request.args.get("status") or None
        brands = [dict(r) for r in conn.execute("SELECT * FROM brands WHERE business_id = ? ORDER BY id", (b["id"],))]
        for br in brands:
            br["accounts"] = [dict(r) for r in conn.execute(
                "SELECT * FROM accounts WHERE brand_id = ? ORDER BY id", (br["id"],))]
        return render_template(
            "business.html", b=b, brands=brands, status=status,
            ideas=ideas.list_ideas(conn, slug, status=status),
            content_types=CONTENT_TYPES[slug], platforms=BUSINESS_PLATFORMS[slug],
            score_inputs=SCORE_INPUTS, factors=FACTORS, weights=WEIGHTS,
            open_tasks=Orchestrator(conn).open_tasks(slug),
            summary=analytics_mod.summary(conn, slug),
        )

    @app.post("/business/<slug>/ideas")
    def create_idea(slug):
        business_or_404(slug)
        f = request.form
        new_id = attempt(lambda: ideas.create_idea(
            db(), slug, f.get("title", ""), f.get("content_type", ""),
            description=f.get("description", ""), hook=f.get("hook", ""), brand_id=f.get("brand_id"),
            target_platforms=f.getlist("target_platforms"),
            needs_external_footage=f.get("needs_external_footage") == "1",
            **{k: f.get(k, 5) for k in SCORE_INPUTS},
        ), "Idea saved and scored.")
        if new_id:
            return redirect(url_for("idea_detail", idea_id=new_id))
        return redirect(url_for("business", slug=slug) + "#new-idea")

    # ---- idea detail ----------------------------------------------------
    @app.get("/ideas/<int:idea_id>")
    def idea_detail(idea_id):
        idea = idea_or_404(idea_id)
        conn = db()
        ready, reasons = publishing.readiness(conn, idea_id)
        linked = rights.sources_for_idea(conn, idea_id)
        linked_ids = {s["id"] for s in linked}
        brands = [dict(r) for r in conn.execute(
            "SELECT * FROM brands WHERE business_id = ? ORDER BY id", (idea["business_id"],))]
        return render_template(
            "idea.html", idea=idea, tasks=Orchestrator(conn).tasks(idea_id),
            linked=linked,
            available=[s for s in rights.list_sources(conn, idea["business_slug"]) if s["id"] not in linked_ids],
            approvals=approvals.history(conn, idea_id), ready=ready, reasons=reasons,
            content_types=CONTENT_TYPES[idea["business_slug"]],
            platforms=BUSINESS_PLATFORMS[idea["business_slug"]], brands=brands,
            score_inputs=SCORE_INPUTS, factors=FACTORS,
            benchmark=format_benchmark(conn, idea["business_slug"], idea["content_type"]),
            linked_videos=[dict(r) for r in conn.execute(
                "SELECT v.id, v.title, a.platform FROM videos v JOIN accounts a ON a.id = v.account_id "
                "WHERE v.idea_id = ?", (idea_id,))],
        )

    @app.post("/ideas/<int:idea_id>/update")
    def update_idea(idea_id):
        idea_or_404(idea_id)
        f = request.form
        attempt(lambda: ideas.update_idea(
            db(), idea_id, title=f.get("title", ""), content_type=f.get("content_type", ""),
            description=f.get("description", ""), hook=f.get("hook", ""), brand_id=f.get("brand_id"),
            target_platforms=f.getlist("target_platforms"),
            needs_external_footage=f.get("needs_external_footage") == "1",
            **{k: f.get(k, 5) for k in SCORE_INPUTS},
        ), "Idea updated and re-scored.")
        return redirect(url_for("idea_detail", idea_id=idea_id))

    @app.post("/ideas/<int:idea_id>/orchestrate")
    def orchestrate(idea_id):
        idea_or_404(idea_id)
        attempt(lambda: Orchestrator(db()).plan(idea_id), "Orchestrator planned and advanced the pipeline.")
        return redirect(url_for("idea_detail", idea_id=idea_id))

    @app.post("/ideas/<int:idea_id>/advance")
    def advance(idea_id):
        idea_or_404(idea_id)
        attempt(lambda: Orchestrator(db()).advance(idea_id), "Orchestrator re-checked the pipeline.")
        return redirect(url_for("idea_detail", idea_id=idea_id))

    @app.post("/ideas/<int:idea_id>/archive")
    def archive(idea_id):
        idea_or_404(idea_id)
        attempt(lambda: ideas.archive_idea(db(), idea_id), "Idea archived.")
        return redirect(url_for("idea_detail", idea_id=idea_id))

    @app.post("/ideas/<int:idea_id>/sources")
    def link_source(idea_id):
        idea_or_404(idea_id)
        attempt(lambda: rights.link_source(db(), idea_id, int(request.form["source_id"])), "Source linked.")
        return redirect(url_for("idea_detail", idea_id=idea_id) + "#rights")

    @app.post("/ideas/<int:idea_id>/sources/<int:source_id>/unlink")
    def unlink_source(idea_id, source_id):
        idea_or_404(idea_id)
        attempt(lambda: rights.unlink_source(db(), idea_id, source_id), "Source unlinked.")
        return redirect(url_for("idea_detail", idea_id=idea_id) + "#rights")

    @app.post("/ideas/<int:idea_id>/review")
    def review(idea_id):
        idea_or_404(idea_id)
        f = request.form
        attempt(lambda: approvals.record_decision(db(), idea_id, f.get("decision", ""), f.get("reviewer", ""),
                                                  f.get("notes", "")), "Review decision recorded.")
        return redirect(url_for("idea_detail", idea_id=idea_id) + "#review")

    @app.post("/ideas/<int:idea_id>/publish")
    def publish(idea_id):
        idea_or_404(idea_id)
        f = request.form
        if f.get("confirm_manual_post") != "yes":
            flash("Tick the box confirming a person has already posted this manually.", "error")
        else:
            attempt(lambda: publishing.mark_published(db(), idea_id, f.get("confirmed_by", ""),
                                                      f.get("platform", ""), f.get("published_url", "")),
                    "Recorded as published. Post-publish tasks created.")
        return redirect(url_for("idea_detail", idea_id=idea_id) + "#publish")

    # ---- tasks ----------------------------------------------------------
    @app.post("/tasks/<int:task_id>/complete")
    def complete_task(task_id):
        orch = Orchestrator(db())
        try:
            task = orch.get_task(task_id)
        except LookupError:
            abort(404)
        attempt(lambda: orch.complete_manual_task(task_id, request.form.get("completed_by", ""),
                                                  request.form.get("notes", "")), "Task marked done.")
        return redirect(request.form.get("next") or url_for("idea_detail", idea_id=task["idea_id"]))

    # ---- rights ---------------------------------------------------------
    @app.get("/rights")
    def rights_page():
        return render_template("rights.html", sources=rights.list_sources(db()))

    @app.post("/rights")
    def create_source():
        f = request.form
        attempt(lambda: rights.create_source(
            db(), f.get("business_slug", ""), f.get("source_name", ""), f.get("source_type", "unknown"),
            f.get("status", "unknown"), f.get("source_url", ""), f.get("rights_holder", ""),
            f.get("evidence", ""), f.get("expires_on") or None, f.get("notes", "")), "Source recorded.")
        return redirect(url_for("rights_page"))

    @app.post("/rights/<int:source_id>")
    def update_source(source_id):
        f = request.form
        attempt(lambda: rights.update_source_status(
            db(), source_id, f.get("status", ""), f.get("evidence"), f.get("expires_on"), f.get("notes")),
            "Permission status updated; linked ideas re-scored.")
        return redirect(url_for("rights_page"))

    # ---- agents ---------------------------------------------------------
    @app.get("/agents")
    def agents_page():
        return render_template("agents.html", counts=Orchestrator(db()).status_counts())

    from .analytics_views import register
    from .trends_views import register as register_trends

    register(app)
    register_trends(app)
    app.config.setdefault("SCHEDULER", None)
    if start_scheduler:
        from ..trends.scheduler import Scheduler

        app.config["SCHEDULER"] = Scheduler(app.config["DB_PATH"])
        app.config["SCHEDULER"].start()
    return app
