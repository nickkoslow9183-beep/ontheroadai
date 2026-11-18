"""Idea submission, editing, listing and scoring."""

from __future__ import annotations

import json
import sqlite3

from .constants import BUSINESS_PLATFORMS, CONTENT_TYPES, SCORE_INPUTS
from .db import get_business, log, now
from .scoring import compute_score

EDITABLE_FIELDS = {"title", "description", "content_type", "hook", "brand_id",
                   "target_platforms", "needs_external_footage", *SCORE_INPUTS}


def _validate(conn, business_slug: str, fields: dict) -> dict:
    clean = dict(fields)
    if "title" in clean:
        clean["title"] = (clean["title"] or "").strip()
        if not clean["title"]:
            raise ValueError("Title is required.")
    if "content_type" in clean and clean["content_type"] not in CONTENT_TYPES[business_slug]:
        raise ValueError(f"Invalid content type {clean['content_type']!r} for this business.")
    if "target_platforms" in clean:
        platforms = clean["target_platforms"]
        if isinstance(platforms, str):
            platforms = [p for p in platforms.split(",") if p]
        allowed = BUSINESS_PLATFORMS[business_slug]
        bad = [p for p in platforms if p not in allowed]
        if bad:
            raise ValueError(f"Platform(s) not used by this business: {', '.join(bad)}")
        clean["target_platforms"] = ",".join(p for p in allowed if p in platforms)
    if "needs_external_footage" in clean:
        clean["needs_external_footage"] = 1 if clean["needs_external_footage"] else 0
    if clean.get("brand_id") in ("", None):
        clean["brand_id"] = None
    elif "brand_id" in clean:
        clean["brand_id"] = int(clean["brand_id"])
        row = conn.execute(
            "SELECT 1 FROM brands b JOIN businesses x ON x.id = b.business_id "
            "WHERE b.id = ? AND x.slug = ?", (clean["brand_id"], business_slug)
        ).fetchone()
        if row is None:
            raise ValueError("That brand does not belong to this business.")
    for key in SCORE_INPUTS:
        if key in clean:
            try:
                value = int(clean[key])
            except (TypeError, ValueError):
                raise ValueError(f"{key} must be a whole number from 0 to 10.") from None
            if not 0 <= value <= 10:
                raise ValueError(f"{key} must be between 0 and 10.")
            clean[key] = value
    for key in ("description", "hook"):
        if key in clean:
            clean[key] = (clean[key] or "").strip()
    return clean


def create_idea(conn: sqlite3.Connection, business_slug: str, title: str, content_type: str, **fields) -> int:
    business = get_business(conn, business_slug)
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Unknown idea field(s): {', '.join(sorted(unknown))}")
    data = _validate(conn, business_slug, {"title": title, "content_type": content_type, **fields})
    ts = now()
    cols = ["business_id", *data.keys(), "created_at", "updated_at"]
    vals = [business["id"], *data.values(), ts, ts]
    cur = conn.execute(
        f"INSERT INTO ideas ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals
    )
    idea_id = cur.lastrowid
    rescore(conn, idea_id)
    log(conn, "idea", idea_id, "created", data["title"])
    conn.commit()
    return idea_id


def update_idea(conn: sqlite3.Connection, idea_id: int, **fields) -> None:
    idea = get_idea(conn, idea_id)
    if idea["status"] in ("published", "archived"):
        raise ValueError(f"Cannot edit an idea that is {idea['status']}.")
    unknown = set(fields) - EDITABLE_FIELDS
    if unknown:
        raise ValueError(f"Field(s) cannot be edited here: {', '.join(sorted(unknown))}")
    if not fields:
        return
    data = _validate(conn, idea["business_slug"], fields)
    sets = ", ".join(f"{k} = ?" for k in data)
    conn.execute(f"UPDATE ideas SET {sets}, updated_at = ? WHERE id = ?", [*data.values(), now(), idea_id])
    rescore(conn, idea_id)
    log(conn, "idea", idea_id, "updated", ", ".join(sorted(data)))
    conn.commit()


def set_status(conn: sqlite3.Connection, idea_id: int, status: str, detail: str = "") -> None:
    """Internal status transition. Publishing goes through publishing.mark_published."""
    if status == "published":
        raise ValueError("Use publishing.mark_published to publish (it enforces approval).")
    conn.execute("UPDATE ideas SET status = ?, updated_at = ? WHERE id = ?", (status, now(), idea_id))
    log(conn, "idea", idea_id, f"status -> {status}", detail)


def archive_idea(conn: sqlite3.Connection, idea_id: int) -> None:
    get_idea(conn, idea_id)
    set_status(conn, idea_id, "archived")
    conn.commit()


def rescore(conn: sqlite3.Connection, idea_id: int) -> int:
    row = conn.execute("SELECT * FROM ideas WHERE id = ?", (idea_id,)).fetchone()
    sources = conn.execute(
        "SELECT s.status, s.expires_on FROM source_permissions s "
        "JOIN idea_sources l ON l.source_id = s.id WHERE l.idea_id = ?", (idea_id,)
    ).fetchall()
    result = compute_score({k: row[k] for k in SCORE_INPUTS}, bool(row["needs_external_footage"]), sources)
    conn.execute(
        "UPDATE ideas SET score = ?, score_breakdown = ? WHERE id = ?",
        (result["total"], json.dumps(result), idea_id),
    )
    return result["total"]


def _to_dict(row) -> dict:
    d = dict(row)
    d["target_platforms_list"] = [p for p in (d.get("target_platforms") or "").split(",") if p]
    d["breakdown"] = json.loads(d["score_breakdown"]) if d.get("score_breakdown") else None
    return d


_SELECT = (
    "SELECT i.*, b.slug AS business_slug, b.name AS business_name, br.name AS brand_name "
    "FROM ideas i JOIN businesses b ON b.id = i.business_id "
    "LEFT JOIN brands br ON br.id = i.brand_id "
)


def get_idea(conn: sqlite3.Connection, idea_id: int) -> dict:
    row = conn.execute(_SELECT + "WHERE i.id = ?", (idea_id,)).fetchone()
    if row is None:
        raise LookupError(f"Idea {idea_id} not found")
    return _to_dict(row)


def list_ideas(conn: sqlite3.Connection, business_slug: str | None = None, status: str | None = None,
               include_archived: bool = False) -> list[dict]:
    where, params = [], []
    if business_slug:
        where.append("b.slug = ?")
        params.append(business_slug)
    if status:
        where.append("i.status = ?")
        params.append(status)
    elif not include_archived:
        where.append("i.status <> 'archived'")
    sql = _SELECT + (("WHERE " + " AND ".join(where)) if where else "") + \
        " ORDER BY i.score DESC, i.created_at DESC"
    return [_to_dict(r) for r in conn.execute(sql, params).fetchall()]
