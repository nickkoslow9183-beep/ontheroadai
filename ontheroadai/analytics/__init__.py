"""Performance analytics (Milestone 2).

Data enters through CSV uploads (csv_import) or manual entry (store), is kept
as dated snapshots per video, and is analysed in insights. There is still no
platform API integration and nothing is fetched from the internet.

The Milestone 1 functions below (record_metrics, summary, recent) are kept
for compatibility and now read/write the same videos + snapshots tables.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import date

from ..db import log
from .store import add_snapshot, after_data_change, find_or_create_video, video_key

METRICS = ("views", "likes", "comments", "shares", "saves", "follows_gained")


def record_metrics(conn: sqlite3.Connection, account_id: int, recorded_on: str | None = None,
                   idea_id: int | None = None, post_url: str = "", avg_watch_seconds=None, **metrics) -> int:
    """Milestone 1 manual entry. Unlike the newer manual-entry form, metrics left out here count as 0,
    which is how the Milestone 1 form behaved."""
    if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
        raise ValueError("Unknown account.")
    unknown = set(metrics) - set(METRICS)
    if unknown:
        raise ValueError(f"Unknown metric(s): {', '.join(sorted(unknown))}")
    values = {}
    for m in METRICS:
        try:
            v = int(metrics.get(m, 0) or 0)
        except (TypeError, ValueError):
            raise ValueError(f"{m} must be a whole number.") from None
        if v < 0:
            raise ValueError(f"{m} cannot be negative.")
        values[m] = v
    if avg_watch_seconds not in ("", None):
        avg_watch_seconds = float(avg_watch_seconds)
        if avg_watch_seconds < 0:
            raise ValueError("Average watch time cannot be negative.")
        values["avg_watch_seconds"] = avg_watch_seconds
    recorded_on = recorded_on or date.today().isoformat()
    try:
        date.fromisoformat(recorded_on)
    except ValueError:
        raise ValueError("Date must be YYYY-MM-DD.") from None
    post_url = (post_url or "").strip()
    key = video_key(None, post_url) or f"manual:{uuid.uuid4().hex}"
    video_id, _ = find_or_create_video(conn, account_id, key,
                                       {"post_url": post_url, "idea_id": idea_id or None}, "manual")
    full = {k: values.get(k) for k in ("views", "reach", "likes", "comments", "shares", "saves", "follows_gained",
                                       "avg_watch_seconds", "completion_rate", "skip_rate")}
    snap_id = add_snapshot(conn, video_id, recorded_on, full, "manual", already_parsed=True)
    log(conn, "analytics", snap_id, "recorded", f"account {account_id}")
    conn.commit()
    after_data_change(conn, [video_id])
    return snap_id


def _rates(row: dict) -> dict:
    views = row.get("views") or 0
    eng = row.get("interactions")
    row["engagement_rate"] = round(eng / views * 100, 2) if views and eng is not None else None
    follows = row.get("follows_gained")
    row["follows_per_1k_views"] = round(follows / views * 1000, 2) if views and follows is not None else None
    return row


_LATEST = ("JOIN video_snapshots s ON s.id = (SELECT id FROM video_snapshots y WHERE y.video_id = v.id "
           "ORDER BY y.captured_on DESC, y.id DESC LIMIT 1) ")


def summary(conn: sqlite3.Connection, business_slug: str) -> list[dict]:
    """Totals per brand/platform for one business, using each video's latest numbers."""
    rows = conn.execute(
        "SELECT br.name AS brand_name, a.platform, COUNT(v.id) AS entries, "
        "COALESCE(SUM(s.views),0) AS views, COALESCE(SUM(s.likes),0) AS likes, "
        "COALESCE(SUM(s.comments),0) AS comments, COALESCE(SUM(s.shares),0) AS shares, "
        "COALESCE(SUM(s.saves),0) AS saves, SUM(s.follows_gained) AS follows_gained, "
        "SUM(CASE WHEN s.views > 0 AND COALESCE(s.likes, s.comments, s.shares, s.saves) IS NOT NULL "
        "    THEN COALESCE(s.likes,0)+COALESCE(s.comments,0)+COALESCE(s.shares,0)+COALESCE(s.saves,0) END) "
        "    AS interactions "
        "FROM videos v " + _LATEST +
        "JOIN accounts a ON a.id = v.account_id JOIN brands br ON br.id = a.brand_id "
        "JOIN businesses b ON b.id = br.business_id WHERE b.slug = ? "
        "GROUP BY br.name, a.platform ORDER BY views DESC", (business_slug,)).fetchall()
    out = []
    for r in rows:
        d = _rates(dict(r))
        d["follows_gained"] = d["follows_gained"] or 0
        out.append(d)
    return out


def recent(conn: sqlite3.Connection, business_slug: str | None = None, limit: int = 50) -> list[dict]:
    sql = ("SELECT s.*, s.captured_on AS recorded_on, v.title, v.post_url, v.id AS video_id, a.platform, "
           "br.name AS brand_name, b.slug AS business_slug, i.title AS idea_title "
           "FROM video_snapshots s JOIN videos v ON v.id = s.video_id JOIN accounts a ON a.id = v.account_id "
           "JOIN brands br ON br.id = a.brand_id JOIN businesses b ON b.id = br.business_id "
           "LEFT JOIN ideas i ON i.id = v.idea_id ")
    params: list = []
    if business_slug:
        sql += "WHERE b.slug = ? "
        params.append(business_slug)
    sql += "ORDER BY s.captured_on DESC, s.id DESC LIMIT ?"
    params.append(limit)
    out = []
    for r in conn.execute(sql, params).fetchall():
        d = dict(r)
        d["interactions"] = sum(d.get(k) or 0 for k in ("likes", "comments", "shares", "saves"))
        out.append(_rates(d))
    return out
