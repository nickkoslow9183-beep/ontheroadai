"""The Trend Scout research run.

1. Pick enabled sources that are due (respecting each source's minimum interval).
2. Fetch politely (robots.txt, conditional GET, per-host spacing). See fetcher.py.
3. Parse and normalise items (title, summary, time, canonical URL).
4. Drop duplicates (same canonical URL seen before, by any source).
5. Group related items into stories, attaching to recent stories when they match.
6. Classify (sport / GTA concept, topic type), verify (confirmed / reported / rumor),
   draft recommendations and score every touched story.
7. Store everything in SQLite and write a run report.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime, timedelta, timezone

from ..db import log, now as now_iso
from . import classify, recommend, scoring
from .fetcher import Fetcher
from .parser import FeedError, canonical_url, parse_feed
from .text import fingerprint, jaccard, keyword_set, named_phrases, related

STORY_WINDOW_DAYS = 7
RESCORE_WINDOW_DAYS = 14
LOCK_MINUTES = 30


class RunInProgress(RuntimeError):
    pass


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def _due(src, now: datetime) -> bool:
    if not src["last_attempt_at"]:
        return True
    last = datetime.fromisoformat(src["last_attempt_at"])
    wait = timedelta(minutes=src["min_interval_minutes"])
    if src["last_status"] == "rate_limited":
        wait = max(wait, timedelta(hours=2))
    if src["consecutive_failures"] >= 3:
        wait = max(wait, timedelta(hours=6))  # back off from sources that keep failing
    return now - last >= wait


def run_trend_scout(conn: sqlite3.Connection, trigger: str = "manual", transport=None, now: datetime | None = None,
                    force: bool = False, source_ids: list[int] | None = None, fetcher: Fetcher | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    running = conn.execute("SELECT id, started_at FROM research_runs WHERE status = 'running'").fetchall()
    for r in running:
        if now - datetime.fromisoformat(r["started_at"]) < timedelta(minutes=LOCK_MINUTES):
            raise RunInProgress(f"Research run #{r['id']} is still running.")
        conn.execute("UPDATE research_runs SET status = 'failed', error = 'Interrupted (app closed during run)', "
                     "finished_at = ? WHERE id = ?", (_iso(now), r["id"]))
    run_id = conn.execute("INSERT INTO research_runs (trigger, started_at) VALUES (?, ?)",
                          (trigger, _iso(now))).lastrowid
    conn.commit()
    try:
        return _run(conn, run_id, trigger, transport, now, force, source_ids, fetcher)
    except Exception as exc:  # record, then re-raise for logs
        conn.rollback()
        conn.execute("UPDATE research_runs SET status = 'failed', finished_at = ?, error = ? WHERE id = ?",
                     (_iso(datetime.now(timezone.utc)), f"{type(exc).__name__}: {exc}", run_id))
        conn.commit()
        raise


def _run(conn, run_id, trigger, transport, now, force, source_ids, fetcher):
    fetcher = fetcher or Fetcher(transport)
    srcs = conn.execute("SELECT * FROM research_sources WHERE kind <> 'manual' AND enabled = 1 "
                        "AND requires_credentials = 0 ORDER BY id").fetchall()
    if source_ids:
        srcs = [s for s in srcs if s["id"] in set(source_ids)]
    attempted, ok, failed, skipped = 0, 0, 0, 0
    new_item_ids: list[int] = []
    dup_count = 0
    source_report = []
    for src in srcs:
        if not force and not _due(src, now):
            skipped += 1
            continue
        attempted += 1
        t0 = time.monotonic()
        res = fetcher.fetch(src["url"], src["etag"], src["last_modified"])
        status, err, count, new_here = res.status, res.error, 0, 0
        if res.status == "ok":
            try:
                feed = parse_feed(res.body, src["url"], now)
                count = len(feed["items"])
                for it in feed["items"]:
                    iid = _insert_item(conn, src, it, run_id, now)
                    if iid:
                        new_item_ids.append(iid)
                        new_here += 1
                    else:
                        dup_count += 1
            except FeedError as exc:
                status, err = "parse_error", str(exc)
        success = status in ("ok", "not_modified")
        ok += success
        failed += not success
        conn.execute(
            "UPDATE research_sources SET last_attempt_at = ?, last_status = ?, last_error = ?, "
            "last_success_at = CASE WHEN ? THEN ? ELSE last_success_at END, "
            "etag = CASE WHEN ? THEN ? ELSE etag END, last_modified = CASE WHEN ? THEN ? ELSE last_modified END, "
            "last_item_count = CASE WHEN ? = 'ok' THEN ? ELSE last_item_count END, "
            "consecutive_failures = CASE WHEN ? THEN 0 ELSE consecutive_failures + 1 END, updated_at = ? WHERE id = ?",
            (_iso(now), status, err, success, _iso(now), status == "ok", res.etag, status == "ok", res.last_modified,
             status, count, success, now_iso(), src["id"]))
        conn.execute(
            "INSERT INTO research_run_sources (run_id, source_id, status, http_status, item_count, new_items, error, "
            "duration_ms) VALUES (?,?,?,?,?,?,?,?)",
            (run_id, src["id"], status, res.http_status, count, new_here, err, int((time.monotonic() - t0) * 1000)))
        source_report.append({"source": src["name"], "status": status, "items": count, "new": new_here, "error": err})
        conn.commit()

    created, updated = process_items(conn, new_item_ids, run_id, now)
    rescore_recent(conn, now)
    if new_item_ids:
        conn.execute("UPDATE research_stories SET status = 'seen' WHERE status IN ('new', 'updated') "
                     "AND (last_run_id IS NULL OR last_run_id <> ?)", (run_id,))
    report = build_report(conn, run_id, source_report, now)
    if attempted == 0:
        final = "success"
        report["note"] = "No source was due yet. Use 'Run now (all sources)' to fetch everything immediately."
    elif ok == 0:
        final = "failed"
    elif failed:
        final = "partial"
    else:
        final = "success"
    conn.execute(
        "UPDATE research_runs SET status = ?, finished_at = ?, sources_attempted = ?, sources_ok = ?, "
        "sources_failed = ?, sources_skipped = ?, items_new = ?, items_duplicate = ?, stories_new = ?, "
        "stories_updated = ?, report_json = ?, error = ? WHERE id = ?",
        (final, _iso(datetime.now(timezone.utc)), attempted, ok, failed, skipped, len(new_item_ids), dup_count,
         len(created), len(updated), json.dumps(report), "" if final != "failed" else "No source could be read.",
         run_id))
    log(conn, "research_run", run_id, final, f"{len(new_item_ids)} new items, {len(created)} new stories")
    conn.commit()
    return run_id


def _insert_item(conn, src, it: dict, run_id, now: datetime) -> int | None:
    canon = canonical_url(it["url"])
    if conn.execute("SELECT 1 FROM research_items WHERE canonical_url = ?", (canon,)).fetchone():
        return None
    cur = conn.execute(
        "INSERT INTO research_items (source_id, canonical_url, url, guid, title, summary, author, published_at, "
        "fetched_at, first_run_id, title_fingerprint, extra_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (src["id"], canon, it["url"], it.get("guid", ""), it["title"][:500], it.get("summary", ""),
         it.get("author", "")[:200], it.get("published_at"), _iso(now), run_id, fingerprint(it["title"]),
         json.dumps(it.get("extra", {}))))
    return cur.lastrowid


def add_manual_item(conn: sqlite3.Connection, business: str, title: str, url: str = "", notes: str = "",
                    now: datetime | None = None) -> int:
    """Something you found yourself: a link, a transcript excerpt, a creator's post."""
    from .sources import manual_source_id

    if business not in ("on-the-road-media", "gta6-network"):
        raise ValueError("Choose a business.")
    title = (title or "").strip()
    if not title:
        raise ValueError("Give it a headline or short description.")
    url = (url or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise ValueError("Links must start with https://")
    now = now or datetime.now(timezone.utc)
    src = conn.execute("SELECT * FROM research_sources WHERE id = ?", (manual_source_id(conn),)).fetchone()
    item = {"url": url or f"manual:{uuid.uuid4().hex}", "title": title, "summary": notes.strip()[:600],
            "published_at": _iso(now), "extra": {"business": business}}
    iid = _insert_item(conn, src, item, None, now)
    if iid is None:
        raise ValueError("That link is already in your research.")
    process_items(conn, [iid], None, now)
    log(conn, "research_item", iid, "added manually", title[:80])
    conn.commit()
    return iid


def _item_rows(conn, where: str, params) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT i.*, s.name AS source_name, s.reliability, s.business AS source_business, s.category AS source_category "
        f"FROM research_items i JOIN research_sources s ON s.id = i.source_id WHERE {where}", params)]


def _business_for(item: dict) -> str | None:
    text = f"{item['title']} {item['summary']}"
    extra = json.loads(item.get("extra_json") or "{}")
    biz = extra.get("business") or item["source_business"]
    if biz == "gta6-network" or (biz == "both" and classify.is_gta(text)):
        return "gta6-network" if (classify.is_gta(text) or extra.get("business") or item["reliability"] == "creator") else None
    if biz == "on-the-road-media":
        return "on-the-road-media"
    return None


def process_items(conn: sqlite3.Connection, item_ids: list[int], run_id, now: datetime) -> tuple[set, set]:
    """Group new items into stories. Returns (created story ids, updated story ids)."""
    if not item_ids:
        return set(), set()
    items = _item_rows(conn, f"i.id IN ({', '.join('?' * len(item_ids))})", item_ids)
    items.sort(key=lambda i: i["published_at"] or i["fetched_at"])
    since = _iso(now - timedelta(days=STORY_WINDOW_DAYS))
    candidates: dict[int, dict] = {}
    for st in conn.execute("SELECT id, business FROM research_stories WHERE last_seen_at >= ? AND dismissed = 0",
                           (since,)).fetchall():
        titles = [r["title"] for r in conn.execute("SELECT title FROM research_items WHERE story_id = ?", (st["id"],))]
        candidates[st["id"]] = _sig(st["business"], titles)
    created, touched = set(), set()
    ts = _iso(now)
    for it in items:
        business = _business_for(it)
        if business is None:
            conn.execute("UPDATE research_items SET relevant = 0 WHERE id = ?", (it["id"],))
            continue
        kw, names = keyword_set(it["title"]), set(named_phrases(it["title"]))
        match, best = None, -1.0
        for sid, sig in candidates.items():
            if sig["business"] == business and related(kw, names, sig["kw"], sig["names"]):
                strength = jaccard(kw, sig["kw"]) + 0.25 * len(names & sig["names"])
                if strength > best:  # attach to the closest story, not merely the first related one
                    match, best = sid, strength
        if match is None:
            match = conn.execute(
                "INSERT INTO research_stories (business, title, first_seen_at, last_seen_at, last_run_id, created_at, "
                "updated_at) VALUES (?,?,?,?,?,?,?)", (business, it["title"], ts, ts, run_id, ts, ts)).lastrowid
            created.add(match)
            candidates[match] = {"business": business, "kw": set(), "names": set()}
        candidates[match]["kw"] |= kw
        candidates[match]["names"] |= names
        conn.execute("UPDATE research_items SET story_id = ? WHERE id = ?", (match, it["id"]))
        touched.add(match)
    ctxs: dict[str, dict] = {}
    for sid in touched:
        status = "new" if sid in created else "updated"
        refresh_story(conn, sid, now, ctxs, status=status, run_id=run_id)
    conn.commit()
    return created, touched - created


def _sig(business, titles):
    kw, names = set(), set()
    for t in titles:
        kw |= keyword_set(t)
        names |= set(named_phrases(t))
    return {"business": business, "kw": kw, "names": names}


def _similar_idea(conn, business: str, title: str) -> int | None:
    kw = keyword_set(title)
    for r in conn.execute("SELECT i.id, i.title FROM ideas i JOIN businesses b ON b.id = i.business_id "
                          "WHERE b.slug = ? AND i.status <> 'archived'", (business,)):
        if jaccard(kw, keyword_set(r["title"])) >= 0.4:
            return r["id"]
    return None


def refresh_story(conn, story_id: int, now: datetime, ctxs: dict | None = None, status: str | None = None,
                  run_id=None) -> None:
    """Recompute aggregates, classification, verification, recommendation and score."""
    ctxs = ctxs if ctxs is not None else {}
    st = dict(conn.execute("SELECT * FROM research_stories WHERE id = ?", (story_id,)).fetchone())
    items = _item_rows(conn, "i.story_id = ?", (story_id,))
    if not items:
        return
    items.sort(key=lambda i: (i["reliability"] != "official", -(len(i["summary"]) > 0),
                              i["published_at"] or ""), reverse=False)
    items_by_time = sorted(items, key=lambda i: i["published_at"] or i["fetched_at"], reverse=True)
    titles = [i["title"] for i in items]
    text = " ".join(f"{i['title']} {i['summary']}" for i in items)
    business = st["business"]
    if business == "on-the-road-media":
        category = classify.detect_sport(text, [i["source_category"] for i in items])
        topic = classify.detect_topic(" ".join(titles) + " " + items[0]["summary"], st["title"] or titles[0])
    else:
        topic = classify.detect_gta_concept(" ".join(titles) + " " + items[0]["summary"], st["title"] or titles[0])
        category = topic
    verification, vnote = classify.verify(items, business)
    ents = classify.entities(titles)
    published = [i["published_at"] for i in items if i["published_at"]]
    agg = {
        "business": business, "category": category, "topic_type": topic,
        # keep the headline that started the story so it doesn't jump around between runs
        "title": st["title"] or items_by_time[-1]["title"],
        "summary": next((i["summary"] for i in items if i["summary"]), ""),
        "items": items, "entities": ents, "verification": verification,
        "major_event": classify.is_major_event(text),
        "source_count": len({i["source_id"] for i in items}), "item_count": len(items),
        "reliabilities": [i["reliability"] for i in items],
        "latest_published_at": max(published) if published else None,
        "first_seen_at": st["first_seen_at"],
    }
    if business not in ctxs:
        ctxs[business] = scoring.analytics_context(conn, business)
    rec = recommend.build(agg, ctxs[business])
    similar = st["idea_id"] is None and _similar_idea(conn, business, agg["title"]) is not None
    sc = scoring.score_story(agg, rec, ctxs[business], now, similar_idea=similar)
    rec["similar_idea_exists"] = similar
    conn.execute(
        "UPDATE research_stories SET category = ?, topic_type = ?, summary = ?, keywords_json = ?, entities_json = ?, "
        "last_seen_at = CASE WHEN ? IS NOT NULL THEN ? ELSE last_seen_at END, latest_published_at = ?, "
        "item_count = ?, source_count = ?, verification = ?, verification_note = ?, "
        "status = COALESCE(?, status), last_run_id = COALESCE(?, last_run_id), score = ?, score_json = ?, "
        "rec_json = ?, updated_at = ? WHERE id = ?",
        (category, topic, agg["summary"], json.dumps(sorted(keyword_set(" ".join(titles)))[:40]), json.dumps(ents),
         status, _iso(now), agg["latest_published_at"], agg["item_count"], agg["source_count"], verification, vnote,
         status, run_id, sc["total"], json.dumps(sc), json.dumps(rec), now_iso(), story_id))


def rescore_recent(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Freshness decays and analytics change, so recent stories are re-scored on every run."""
    now = now or datetime.now(timezone.utc)
    since = _iso(now - timedelta(days=RESCORE_WINDOW_DAYS))
    ids = [r[0] for r in conn.execute("SELECT id FROM research_stories WHERE last_seen_at >= ?", (since,))]
    ctxs: dict = {}
    for sid in ids:
        refresh_story(conn, sid, now, ctxs)
    conn.commit()
    return len(ids)


def build_report(conn, run_id, source_report, now) -> dict:
    report = {"generated_at": _iso(now), "sources": source_report, "businesses": {}}
    for biz in ("on-the-road-media", "gta6-network"):
        rows = conn.execute(
            "SELECT id, title, score, status, verification, topic_type, category FROM research_stories "
            "WHERE business = ? AND dismissed = 0 AND last_seen_at >= ? ORDER BY score DESC LIMIT 10",
            (biz, _iso(now - timedelta(days=3)))).fetchall()
        report["businesses"][biz] = {
            "top": [dict(r) for r in rows],
            "new_count": conn.execute("SELECT COUNT(*) FROM research_stories WHERE business = ? AND last_run_id = ? "
                                      "AND status = 'new'", (biz, run_id)).fetchone()[0],
            "updated_count": conn.execute("SELECT COUNT(*) FROM research_stories WHERE business = ? AND last_run_id = ? "
                                          "AND status = 'updated'", (biz, run_id)).fetchone()[0],
        }
    return report
