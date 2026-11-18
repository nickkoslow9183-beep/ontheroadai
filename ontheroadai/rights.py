"""Footage rights and permission tracking.

Nothing is assumed to be cleared. A source is only 'granted' when a human
records where the evidence of permission lives (email, contract, DM link...).
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .constants import SOURCE_STATUSES, SOURCE_TYPES
from .db import get_business, log, now
from .ideas import get_idea, rescore
from .scoring import effective_source_status


def _check_date(value):
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Expiry date must be YYYY-MM-DD.") from None


def create_source(conn: sqlite3.Connection, business_slug: str, source_name: str, source_type: str = "unknown",
                  status: str = "unknown", source_url: str = "", rights_holder: str = "", evidence: str = "",
                  expires_on: str | None = None, notes: str = "") -> int:
    business = get_business(conn, business_slug)
    source_name = (source_name or "").strip()
    if not source_name:
        raise ValueError("Source name is required.")
    if source_type not in SOURCE_TYPES:
        raise ValueError(f"Invalid source type {source_type!r}.")
    if status not in SOURCE_STATUSES:
        raise ValueError(f"Invalid permission status {status!r}.")
    evidence = (evidence or "").strip()
    if status == "granted" and not evidence:
        raise ValueError("Record the evidence of permission before marking a source granted.")
    ts = now()
    cur = conn.execute(
        "INSERT INTO source_permissions (business_id, source_name, source_type, source_url, rights_holder, "
        "status, evidence, expires_on, notes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (business["id"], source_name, source_type, source_url.strip(), rights_holder.strip(), status,
         evidence, _check_date(expires_on), notes.strip(), ts, ts),
    )
    log(conn, "source", cur.lastrowid, "created", f"{source_name} ({status})")
    conn.commit()
    return cur.lastrowid


def get_source(conn: sqlite3.Connection, source_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT s.*, b.slug AS business_slug FROM source_permissions s "
        "JOIN businesses b ON b.id = s.business_id WHERE s.id = ?", (source_id,)
    ).fetchone()
    if row is None:
        raise LookupError(f"Source {source_id} not found")
    return row


def update_source_status(conn: sqlite3.Connection, source_id: int, status: str, evidence: str | None = None,
                         expires_on: str | None = None, notes: str | None = None) -> None:
    src = get_source(conn, source_id)
    if status not in SOURCE_STATUSES:
        raise ValueError(f"Invalid permission status {status!r}.")
    new_evidence = src["evidence"] if evidence is None else evidence.strip()
    if status == "granted" and not new_evidence:
        raise ValueError("Record the evidence of permission before marking a source granted.")
    conn.execute(
        "UPDATE source_permissions SET status = ?, evidence = ?, expires_on = ?, notes = ?, updated_at = ? "
        "WHERE id = ?",
        (status, new_evidence,
         src["expires_on"] if expires_on is None else _check_date(expires_on),
         src["notes"] if notes is None else notes.strip(), now(), source_id),
    )
    for (idea_id,) in conn.execute("SELECT idea_id FROM idea_sources WHERE source_id = ?", (source_id,)).fetchall():
        rescore(conn, idea_id)
    log(conn, "source", source_id, f"status -> {status}", src["source_name"])
    conn.commit()


def list_sources(conn: sqlite3.Connection, business_slug: str | None = None) -> list[dict]:
    sql = ("SELECT s.*, b.slug AS business_slug, b.name AS business_name, "
           "(SELECT COUNT(*) FROM idea_sources l WHERE l.source_id = s.id) AS idea_count "
           "FROM source_permissions s JOIN businesses b ON b.id = s.business_id ")
    params = []
    if business_slug:
        sql += "WHERE b.slug = ? "
        params.append(business_slug)
    sql += "ORDER BY s.updated_at DESC"
    out = []
    for row in conn.execute(sql, params).fetchall():
        d = dict(row)
        d["effective_status"] = effective_source_status(row)
        out.append(d)
    return out


def link_source(conn: sqlite3.Connection, idea_id: int, source_id: int) -> None:
    idea = get_idea(conn, idea_id)
    src = get_source(conn, source_id)
    if src["business_slug"] != idea["business_slug"]:
        raise ValueError("Source and idea belong to different businesses.")
    conn.execute("INSERT OR IGNORE INTO idea_sources (idea_id, source_id) VALUES (?,?)", (idea_id, source_id))
    rescore(conn, idea_id)
    log(conn, "idea", idea_id, "source linked", src["source_name"])
    conn.commit()


def unlink_source(conn: sqlite3.Connection, idea_id: int, source_id: int) -> None:
    conn.execute("DELETE FROM idea_sources WHERE idea_id = ? AND source_id = ?", (idea_id, source_id))
    rescore(conn, idea_id)
    log(conn, "idea", idea_id, "source unlinked", str(source_id))
    conn.commit()


def sources_for_idea(conn: sqlite3.Connection, idea_id: int) -> list[dict]:
    rows = conn.execute(
        "SELECT s.* FROM source_permissions s JOIN idea_sources l ON l.source_id = s.id "
        "WHERE l.idea_id = ? ORDER BY s.source_name", (idea_id,)
    ).fetchall()
    out = []
    for row in rows:
        d = dict(row)
        d["effective_status"] = effective_source_status(row)
        out.append(d)
    return out


def rights_check(conn: sqlite3.Connection, idea_id: int) -> tuple[bool, list[str]]:
    """True only when every piece of external footage has documented permission."""
    idea = get_idea(conn, idea_id)
    if not idea["needs_external_footage"]:
        return True, []
    sources = sources_for_idea(conn, idea_id)
    if not sources:
        return False, ["Idea needs external footage but no source is linked."]
    problems = [
        f"{s['source_name']}: permission is {s['effective_status']}"
        for s in sources if s["effective_status"] != "granted"
    ]
    return not problems, problems
