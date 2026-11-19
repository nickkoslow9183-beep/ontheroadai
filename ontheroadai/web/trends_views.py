"""Trend Scout pages."""

from __future__ import annotations

import json
from datetime import datetime, timezone

from flask import abort, current_app, flash, redirect, render_template, request, url_for

from ..agents.orchestrator import Orchestrator
from ..constants import CONTENT_TYPES
from ..trends import opportunities as opp
from ..trends import scheduler, sources, watchlist
from ..trends.classify import SPORT_LABELS, TOPIC_LABELS, VERIFICATION
from ..trends.job import RunInProgress, add_manual_item, rescore_recent
from .helpers import attempt, get_db

GTA_ACCOUNTS = {"gta6-moments": "GTA 6 Moments", "gta6-discoveries": "GTA 6 Discoveries",
                "gta-streamer-stories": "GTA Streamer Stories"}


def _status(conn):
    last = conn.execute("SELECT * FROM research_runs ORDER BY id DESC LIMIT 1").fetchone()
    last_ok = conn.execute("SELECT * FROM research_runs WHERE status IN ('success', 'partial') "
                           "ORDER BY id DESC LIMIT 1").fetchone()
    health = {}
    for s in sources.list_sources(conn):
        health[s["health"]] = health.get(s["health"], 0) + 1
    nd = scheduler.next_due(conn)
    return {"last": dict(last) if last else None, "last_ok": dict(last_ok) if last_ok else None,
            "interval": scheduler.interval_hours(conn), "next_due": nd.isoformat(timespec="minutes") if nd else None,
            "health": health, "choices": scheduler.CHOICES,
            "scheduler_running": bool(current_app.config.get("SCHEDULER"))}


def register(app):
    @app.context_processor
    def _trend_globals():
        return {"TOPIC_LABELS": TOPIC_LABELS, "SPORT_LABELS": SPORT_LABELS, "VERIFICATION": VERIFICATION,
                "GTA_ACCOUNTS": GTA_ACCOUNTS, "RELIABILITY": sources.RELIABILITY}

    @app.get("/trends")
    def trends_home():
        conn = get_db()
        view = request.args.get("view", "sports")
        filters = {k: request.args.get(k, "").strip() for k in
                   ("category", "topic", "verification", "freshness", "min_score", "production", "account",
                    "min_reliability") if request.args.get(k, "").strip()}
        base = dict(filters)
        if view == "sports":
            base["business"] = "on-the-road-media"
        elif view == "gta":
            base["business"] = "gta6-network"
        elif view == "saved":
            base["saved"] = True
        elif view == "new":
            base["status"] = "new"
        rows = opp.list_opportunities(conn, base)
        return render_template("trends_home.html", view=view, filters=filters, rows=rows, st=_status(conn),
                               sports=["nba", "nfl", "ufc", "mlb", "nhl", "general"],
                               gta_topics=list(CONTENT_TYPES["gta6-network"]))

    @app.post("/trends/run")
    def trends_run():
        conn = get_db()
        force = request.form.get("force") == "1"
        try:
            run_id = Orchestrator(conn).run_research(trigger="manual", force=force,
                                                     transport=current_app.config.get("TREND_TRANSPORT"))
        except RunInProgress as exc:
            flash(str(exc), "error")
            return redirect(url_for("trends_home"))
        run = conn.execute("SELECT * FROM research_runs WHERE id = ?", (run_id,)).fetchone()
        msg = (f"Research run #{run_id}: {run['status']}. {run['sources_ok']} source(s) read, {run['sources_failed']} "
               f"failed, {run['sources_skipped']} not due. {run['items_new']} new item(s), {run['stories_new']} new "
               f"and {run['stories_updated']} updated stories.")
        flash(msg, "ok" if run["status"] in ("success", "partial") else "error")
        return redirect(url_for("trends_run_detail", run_id=run_id))

    @app.post("/trends/schedule")
    def trends_schedule():
        conn = get_db()
        attempt(lambda: scheduler.set_interval(conn, int(request.form.get("hours", "0"))),
                "Schedule saved. Automatic runs only happen while this app is open on your computer.")
        return redirect(url_for("trends_home"))

    @app.post("/trends/rescore")
    def trends_rescore():
        conn = get_db()
        attempt(lambda: rescore_recent(conn), lambda n: f"Re-scored {n} recent opportunities with your latest analytics.")
        return redirect(url_for("trends_home"))

    # ---- opportunity ---------------------------------------------------------------
    def _story_or_404(conn, story_id):
        try:
            return opp.get(conn, story_id)
        except LookupError:
            abort(404)

    @app.get("/trends/o/<int:story_id>")
    def trends_opportunity(story_id):
        conn = get_db()
        s = _story_or_404(conn, story_id)
        return render_template("trends_opportunity.html", s=s, rec=s["rec"], sc=s["score_detail"],
                               handoff_json=json.dumps(s["rec"].get("handoff", {}), indent=2))

    @app.post("/trends/o/<int:story_id>/<action>")
    def trends_opportunity_action(story_id, action):
        conn = get_db()
        _story_or_404(conn, story_id)
        if action == "save":
            attempt(lambda: opp.set_flag(conn, story_id, saved=request.form.get("value") != "0"), "Saved list updated.")
        elif action == "dismiss":
            attempt(lambda: opp.set_flag(conn, story_id, dismissed=request.form.get("value") != "0"),
                    "Opportunity hidden." if request.form.get("value") != "0" else "Opportunity restored.")
        elif action == "ack":
            attempt(lambda: opp.acknowledge_claims(conn, story_id, request.form.get("who", "")),
                    "Acknowledged: the video will present this as unconfirmed.")
        elif action == "idea":
            res = attempt(lambda: opp.convert_to_idea(conn, story_id), None)
            if res and res is not True:
                idea_id, created = res
                flash("Idea created from this opportunity." if created else
                      "Already linked to an existing idea, so no duplicate was created.", "ok")
                return redirect(url_for("idea_detail", idea_id=idea_id))
        else:
            abort(404)
        return redirect(request.form.get("next") or url_for("trends_opportunity", story_id=story_id))

    @app.post("/trends/submit")
    def trends_submit():
        conn = get_db()
        biz = request.form.get("business", "")
        lines = []
        if request.form.get("title"):
            lines.append((request.form.get("title"), request.form.get("url", ""), request.form.get("notes", "")))
        bulk = request.form.get("bulk", "")
        upload = request.files.get("file")
        if upload and upload.filename:
            if not upload.filename.lower().endswith((".txt", ".md", ".csv")):
                flash("Research files must be .txt, .md or .csv (one item per line: headline | link).", "error")
                return redirect(url_for("trends_home") + "#submit")
            bulk += "\n" + upload.read(512 * 1024).decode("utf-8", "replace")
        for line in bulk.splitlines():
            if line.strip():
                parts = [p.strip() for p in line.replace("\t", "|").split("|")]
                url = next((p for p in parts if p.startswith("http")), "")
                title = next((p for p in parts if p and not p.startswith("http")), "")
                lines.append((title or url, url, ""))
        added, errors = 0, []
        for title, url, notes in lines[:200]:
            try:
                add_manual_item(conn, biz, title, url, notes)
                added += 1
            except ValueError as exc:
                errors.append(f"{title[:40]}: {exc}")
        if added:
            flash(f"Added {added} item(s) to research. They are marked 'added by you', so verify them before filming.", "ok")
        for e in errors[:5]:
            flash(e, "error")
        if not lines:
            flash("Nothing to add. Enter a headline or paste lines.", "error")
        return redirect(url_for("trends_home", view="gta" if biz == "gta6-network" else "sports"))

    # ---- topics, sources, watchlist, history ---------------------------------------------
    @app.get("/trends/topics")
    def trends_topics():
        conn = get_db()
        hours = int(request.args.get("hours", "48") or 48)
        return render_template("trends_topics.html", hours=hours,
                               otrm=opp.trending_topics(conn, "on-the-road-media", hours),
                               gta=opp.trending_topics(conn, "gta6-network", hours))

    @app.get("/trends/sources")
    def trends_sources():
        conn = get_db()
        return render_template("trends_sources.html", sources=sources.list_sources(conn),
                               unavailable=sources.UNAVAILABLE, st=_status(conn))

    @app.post("/trends/sources")
    def trends_source_add():
        conn = get_db()
        f = request.form
        attempt(lambda: sources.add_source(conn, f.get("name", ""), f.get("url", ""), f.get("business", ""),
                                           f.get("reliability", "publisher"), f.get("category", ""),
                                           interval=int(f.get("interval") or 120)),
                "Source added. Use 'Test' to check it can be read.")
        return redirect(url_for("trends_sources"))

    @app.post("/trends/sources/<int:source_id>")
    def trends_source_update(source_id):
        conn = get_db()
        f = request.form
        action = f.get("action", "save")
        if action == "test":
            try:
                run_id = Orchestrator(conn).run_research(trigger="manual", force=True, source_ids=[source_id],
                                                         transport=current_app.config.get("TREND_TRANSPORT"))
            except RunInProgress as exc:
                flash(str(exc), "error")
                return redirect(url_for("trends_sources"))
            row = conn.execute("SELECT status, item_count, error FROM research_run_sources WHERE run_id = ? "
                               "AND source_id = ?", (run_id, source_id)).fetchone()
            if row is None:
                flash("That source is disabled or needs credentials, so it wasn't tested.", "error")
            elif row["status"] in ("ok", "not_modified"):
                flash(f"Works: {row['status'].replace('_', ' ')}, {row['item_count']} item(s) in the feed.", "ok")
            else:
                flash(f"Not working: {row['status'].replace('_', ' ')}. {row['error']}", "error")
        elif action == "delete":
            attempt(lambda: sources.delete_source(conn, source_id), "Source deleted.")
        else:
            attempt(lambda: sources.update_source(conn, source_id, enabled=f.get("enabled") == "1",
                                                  url=f.get("url"), interval=int(f.get("interval") or 60)),
                    "Source updated.")
        return redirect(url_for("trends_sources") + f"#src-{source_id}")

    @app.get("/trends/watchlist")
    def trends_watchlist():
        conn = get_db()
        return render_template("trends_watchlist.html", creators=watchlist.list_creators(conn),
                               categories=watchlist.CATEGORIES)

    @app.post("/trends/watchlist")
    def trends_watchlist_add():
        conn = get_db()
        f = request.form
        attempt(lambda: watchlist.add_creator(conn, f.get("name", ""), f.get("category", ""), f.get("profile_url", ""),
                                              f.get("feed", ""), f.get("notes", "")),
                "Added to the watchlist.")
        return redirect(url_for("trends_watchlist"))

    @app.post("/trends/watchlist/<int:creator_id>/<action>")
    def trends_watchlist_action(creator_id, action):
        conn = get_db()
        if action == "delete":
            attempt(lambda: watchlist.delete_creator(conn, creator_id), "Removed from the watchlist.")
        elif action == "rights":
            sid = attempt(lambda: watchlist.link_rights_record(conn, creator_id),
                          "Footage-rights record created (status: unknown). Record their permission there.")
            if sid and sid is not True:
                return redirect(url_for("rights_page"))
        else:
            abort(404)
        return redirect(url_for("trends_watchlist"))

    @app.get("/trends/runs")
    def trends_runs():
        conn = get_db()
        runs = [dict(r) for r in conn.execute("SELECT * FROM research_runs ORDER BY id DESC LIMIT 100")]
        return render_template("trends_runs.html", runs=runs, st=_status(conn))

    @app.get("/trends/runs/<int:run_id>")
    def trends_run_detail(run_id):
        conn = get_db()
        run = conn.execute("SELECT * FROM research_runs WHERE id = ?", (run_id,)).fetchone()
        if run is None:
            abort(404)
        per_source = [dict(r) for r in conn.execute(
            "SELECT rs.*, s.name, s.url FROM research_run_sources rs JOIN research_sources s ON s.id = rs.source_id "
            "WHERE rs.run_id = ? ORDER BY rs.status <> 'ok', s.name", (run_id,))]
        new_stories = [opp.get(conn, r[0]) for r in conn.execute(
            "SELECT id FROM research_stories WHERE last_run_id = ? AND status IN ('new', 'updated') "
            "ORDER BY score DESC LIMIT 30", (run_id,))]
        return render_template("trends_run.html", run=dict(run), report=json.loads(run["report_json"] or "{}"),
                               per_source=per_source, new_stories=new_stories)

    @app.template_filter("ago")
    def ago(iso):
        if not iso:
            return "never"
        try:
            dt = datetime.fromisoformat(iso)
        except ValueError:
            return iso
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        mins = (datetime.now(timezone.utc) - dt).total_seconds() / 60
        if mins < 0:
            return "just now"
        if mins < 60:
            return f"{mins:.0f} min ago"
        if mins < 48 * 60:
            return f"{mins / 60:.0f} h ago"
        return f"{mins / 1440:.0f} days ago"
