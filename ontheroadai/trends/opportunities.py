"""Researched opportunities: listing, filtering, saving and converting into ideas."""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

from .. import ideas as ideas_mod
from .. import rights
from ..db import log, now
from .classify import UNVERIFIED, VERIFICATION
from .sources import RELIABILITY_RANK
from .text import jaccard, keyword_set, tokens

FRESHNESS = {"6h": 6, "24h": 24, "72h": 72, "7d": 168}


def _decode(row) -> dict:
    d = dict(row)
    d["score_detail"] = json.loads(d.pop("score_json") or "{}")
    d["rec"] = json.loads(d.pop("rec_json") or "{}")
    d["entities"] = json.loads(d.pop("entities_json") or "{}")
    d["keywords"] = json.loads(d.pop("keywords_json") or "[]")
    d["verification_label"], d["verification_tone"] = VERIFICATION.get(d["verification"], (d["verification"], "warn"))
    d["unverified"] = d["verification"] in UNVERIFIED
    return d


def get(conn: sqlite3.Connection, story_id: int) -> dict:
    row = conn.execute("SELECT s.*, i.status AS idea_status, i.title AS idea_title FROM research_stories s "
                       "LEFT JOIN ideas i ON i.id = s.idea_id WHERE s.id = ?", (story_id,)).fetchone()
    if row is None:
        raise LookupError(f"Opportunity {story_id} not found")
    d = _decode(row)
    d["items"] = [dict(r) for r in conn.execute(
        "SELECT i.*, s.name AS source_name, s.reliability FROM research_items i JOIN research_sources s "
        "ON s.id = i.source_id WHERE i.story_id = ? ORDER BY i.published_at DESC", (story_id,))]
    for it in d["items"]:
        it["extra"] = json.loads(it.get("extra_json") or "{}")
    return d


def list_opportunities(conn: sqlite3.Connection, filters: dict | None = None, limit: int = 200,
                       now: datetime | None = None) -> list[dict]:
    f = filters or {}
    now = now or datetime.now(timezone.utc)
    sql = ("SELECT s.*, i.status AS idea_status FROM research_stories s LEFT JOIN ideas i ON i.id = s.idea_id "
           "WHERE 1=1")
    params: list = []
    if f.get("business"):
        sql += " AND s.business = ?"
        params.append(f["business"])
    if f.get("category"):
        sql += " AND s.category = ?"
        params.append(f["category"])
    if f.get("topic"):
        sql += " AND s.topic_type = ?"
        params.append(f["topic"])
    if f.get("verification"):
        if f["verification"] == "__verified__":
            sql += f" AND s.verification NOT IN ({', '.join('?' * len(UNVERIFIED))})"
            params += sorted(UNVERIFIED)
        elif f["verification"] == "__unverified__":
            sql += f" AND s.verification IN ({', '.join('?' * len(UNVERIFIED))})"
            params += sorted(UNVERIFIED)
        else:
            sql += " AND s.verification = ?"
            params.append(f["verification"])
    if f.get("freshness") in FRESHNESS:
        cutoff = (now - timedelta(hours=FRESHNESS[f["freshness"]])).isoformat(timespec="seconds")
        sql += " AND COALESCE(s.latest_published_at, s.first_seen_at) >= ?"
        params.append(cutoff)
    if f.get("min_score"):
        sql += " AND s.score >= ?"
        params.append(int(f["min_score"]))
    if f.get("saved"):
        sql += " AND s.saved = 1"
    if f.get("production") == "none":
        sql += " AND s.idea_id IS NULL"
    elif f.get("production") == "idea":
        sql += " AND s.idea_id IS NOT NULL AND i.status IN ('draft', 'ready_for_review', 'approved', 'rejected')"
    elif f.get("production") == "in_production":
        sql += " AND i.status = 'in_production'"
    elif f.get("production") == "published":
        sql += " AND i.status = 'published'"
    if not f.get("include_dismissed"):
        sql += " AND s.dismissed = 0"
    if f.get("status") == "new":
        sql += " AND s.status IN ('new', 'updated')"
    sql += " ORDER BY s.score DESC, s.last_seen_at DESC LIMIT ?"
    params.append(limit)
    rows = [_decode(r) for r in conn.execute(sql, params)]
    if f.get("account"):
        rows = [r for r in rows if r["rec"].get("account_slug") == f["account"]]
    if f.get("min_reliability"):
        floor = RELIABILITY_RANK.get(f["min_reliability"], 0)
        keep = []
        for r in rows:
            best = conn.execute("SELECT s.reliability FROM research_items i JOIN research_sources s ON s.id = i.source_id "
                                "WHERE i.story_id = ?", (r["id"],)).fetchall()
            if max((RELIABILITY_RANK.get(b[0], 0) for b in best), default=0) >= floor:
                keep.append(r)
        rows = keep
    return rows


def set_flag(conn: sqlite3.Connection, story_id: int, saved: bool | None = None, dismissed: bool | None = None) -> None:
    get(conn, story_id)
    if saved is not None:
        conn.execute("UPDATE research_stories SET saved = ? WHERE id = ?", (1 if saved else 0, story_id))
    if dismissed is not None:
        conn.execute("UPDATE research_stories SET dismissed = ? WHERE id = ?", (1 if dismissed else 0, story_id))
    conn.commit()


def acknowledge_claims(conn: sqlite3.Connection, story_id: int, who: str) -> None:
    """The person confirms the video will present the claim as unconfirmed. Lets QC pass on that check."""
    who = (who or "").strip()
    if not who:
        raise ValueError("Enter your name to acknowledge.")
    st = get(conn, story_id)
    if not st["unverified"]:
        raise ValueError("This opportunity is already verified; nothing to acknowledge.")
    conn.execute("UPDATE research_stories SET claims_acknowledged_by = ? WHERE id = ?", (who, story_id))
    log(conn, "research_story", story_id, "unverified claims acknowledged", who)
    conn.commit()


SCORE_MAP = {  # opportunity factor -> idea scoring input
    "trend_momentum": ("freshness", "story_strength"), "audience_fit": ("audience_relevance",),
    "hook_strength": ("hook_potential",), "conversion_potential": ("follower_conversion", "share_potential"),
    "timeliness": ("freshness",), "production_ease": ("production_feasibility",),
}


def convert_to_idea(conn: sqlite3.Connection, story_id: int) -> tuple[int, bool]:
    """Create a draft idea from an opportunity. Returns (idea_id, created).

    Never duplicates: an opportunity converts once, and an existing idea with a near-identical title
    in the same business is linked instead of creating a second one.
    """
    st = get(conn, story_id)
    if st["idea_id"]:
        return st["idea_id"], False
    rec = st["rec"]
    if rec.get("sensitive"):
        raise ValueError("This is sensitive news (e.g. legal trouble or a death). It isn't converted automatically. "
                         "Create an idea by hand if you want to cover it respectfully.")
    biz = st["business"]
    kw = keyword_set(st["title"])
    for r in conn.execute("SELECT i.id, i.title FROM ideas i JOIN businesses b ON b.id = i.business_id "
                          "WHERE b.slug = ? AND i.status <> 'archived'", (biz,)):
        if jaccard(kw, keyword_set(r["title"])) >= 0.6:
            conn.execute("UPDATE research_stories SET idea_id = ? WHERE id = ?", (r["id"], story_id))
            log(conn, "research_story", story_id, "linked to existing idea", str(r["id"]))
            conn.commit()
            return r["id"], False
    factors = {f["key"]: f["input"] for f in st["score_detail"].get("factors", [])}
    inputs = {k: max(0, min(10, round(sum(factors.get(x, 5) for x in src) / len(src)))) for k, src in SCORE_MAP.items()}
    brand_id = None
    slug = rec.get("account_slug") or ("on-the-road-media" if biz == "on-the-road-media" else None)
    if slug:
        row = conn.execute("SELECT id FROM brands WHERE slug = ?", (slug,)).fetchone()
        brand_id = row[0] if row else None
    lines = [rec.get("summary", ""), "", f"Verification: {st['verification_label']}. {st['verification_note']}"]
    if rec.get("why_it_matters"):
        lines.append(f"Why: {rec['why_it_matters']}")
    if rec.get("hooks"):
        lines.append("Hook options: " + " | ".join(rec["hooks"]))
    if rec.get("interview_question"):
        lines.append(f"Interview question: {rec['interview_question']}")
    if rec.get("transformation"):
        lines.append(f"Original-content plan: {rec['transformation']}")
    lines.append("Sources: " + " ; ".join(f"{s['source']}: {s['url']}" for s in rec.get("sources", [])[:5]))
    lines.append(f"(From Trend Scout opportunity #{story_id}. {rec.get('generator_note', '')})")
    needs_footage = bool(rec.get("uses_third_party_footage"))
    idea_id = ideas_mod.create_idea(
        conn, biz, st["title"][:200], rec["format"], description="\n".join(lines),
        hook=(rec.get("hooks") or [""])[0], brand_id=brand_id, target_platforms=rec.get("platforms", []),
        needs_external_footage=needs_footage, **inputs)
    if needs_footage:
        creator = rec.get("creator") or rec.get("original_source") or "Third-party creator"
        existing = conn.execute("SELECT rights_source_id FROM creators WHERE name = ? AND rights_source_id IS NOT NULL",
                                (creator,)).fetchone()
        src_id = existing[0] if existing else rights.create_source(
            conn, biz, f"{creator}: footage for '{st['title'][:60]}'", "creator_permission", "unknown",
            source_url=(rec.get("sources") or [{}])[0].get("url", ""), rights_holder=creator,
            notes="Created by Trend Scout. Permission is NOT assumed; record written permission before use.")
        rights.link_source(conn, idea_id, src_id)
    conn.execute("UPDATE research_stories SET idea_id = ?, saved = 1 WHERE id = ?", (idea_id, story_id))
    log(conn, "research_story", story_id, "converted to idea", str(idea_id))
    conn.commit()
    return idea_id, True


def story_for_idea(conn: sqlite3.Connection, idea_id: int) -> dict | None:
    row = conn.execute("SELECT id FROM research_stories WHERE idea_id = ? ORDER BY id LIMIT 1", (idea_id,)).fetchone()
    return get(conn, row[0]) if row else None


def trending_topics(conn: sqlite3.Connection, business: str | None = None, hours: int = 48, limit: int = 30,
                    now: datetime | None = None) -> list[dict]:
    """Most-mentioned names and keywords across recent headlines, with how many sources mention them."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    sql = ("SELECT i.title, i.source_id, st.business FROM research_items i JOIN research_stories st "
           "ON st.id = i.story_id WHERE COALESCE(i.published_at, i.fetched_at) >= ?")
    params: list = [cutoff]
    if business:
        sql += " AND st.business = ?"
        params.append(business)
    from .text import named_phrases

    names, words = Counter(), Counter()
    name_sources: dict[str, set] = {}
    for r in conn.execute(sql, params):
        for p in named_phrases(r["title"]):
            names[p] += 1
            name_sources.setdefault(p, set()).add(r["source_id"])
        for w in set(tokens(r["title"])):
            words[w] += 1
    out = [{"term": n, "kind": "name", "mentions": c, "sources": len(name_sources[n])}
           for n, c in names.most_common(limit) if c >= 2]
    out += [{"term": w, "kind": "keyword", "mentions": c, "sources": None} for w, c in words.most_common(limit) if c >= 3]
    return sorted(out, key=lambda x: -x["mentions"])[:limit]
