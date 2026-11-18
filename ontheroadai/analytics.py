"""Manually entered performance metrics and simple, honest summaries.

There is no platform API integration. Numbers come from a person copying them
from each platform's insights screen (CSV import is planned for Milestone 2).
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .db import log, now

METRICS = ("views", "likes", "comments", "shares", "saves", "follows_gained")


def record_metrics(conn: sqlite3.Connection, account_id: int, recorded_on: str | None = None,
                   idea_id: int | None = None, post_url: str = "", avg_watch_seconds=None, **metrics) -> int:
    if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
        raise ValueError("Unknown account.")
    unknown = set(metrics) - set(METRICS)
    if unknown:
        raise ValueError(f"Unknown metric(s): {', '.join(sorted(unknown))}")
    values = {}
    for m in METRICS:
        raw = metrics.get(m, 0)
        try:
            v = int(raw or 0)
        except (TypeError, ValueError):
            raise ValueError(f"{m} must be a whole number.") from None
        if v < 0:
            raise ValueError(f"{m} cannot be negative.")
        values[m] = v
    if avg_watch_seconds in ("", None):
        avg_watch_seconds = None
    else:
        avg_watch_seconds = float(avg_watch_seconds)
        if avg_watch_seconds < 0:
            raise ValueError("Average watch time cannot be negative.")
    recorded_on = recorded_on or date.today().isoformat()
    try:
        date.fromisoformat(recorded_on)
    except ValueError:
        raise ValueError("Date must be YYYY-MM-DD.") from None
    if idea_id in ("", None):
        idea_id = None
    cur = conn.execute(
        "INSERT INTO analytics (account_id, idea_id, post_url, recorded_on, views, likes, comments, shares, saves, "
        "follows_gained, avg_watch_seconds, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, idea_id, post_url.strip(), recorded_on, *values.values(), avg_watch_seconds, now()),
    )
    log(conn, "analytics", cur.lastrowid, "recorded", f"account {account_id}")
    conn.commit()
    return cur.lastrowid


def _rates(row: dict) -> dict:
    views = row["views"] or 0
    eng = (row["likes"] or 0) + (row["comments"] or 0) + (row["shares"] or 0) + (row["saves"] or 0)
    row["engagement_rate"] = round(eng / views * 100, 2) if views else None
    row["follows_per_1k_views"] = round((row["follows_gained"] or 0) / views * 1000, 2) if views else None
    return row


def summary(conn: sqlite3.Connection, business_slug: str) -> list[dict]:
    """Totals per brand/platform for one business."""
    rows = conn.execute(
        "SELECT br.name AS brand_name, a.platform, COUNT(an.id) AS entries, "
        "COALESCE(SUM(an.views),0) AS views, COALESCE(SUM(an.likes),0) AS likes, "
        "COALESCE(SUM(an.comments),0) AS comments, COALESCE(SUM(an.shares),0) AS shares, "
        "COALESCE(SUM(an.saves),0) AS saves, COALESCE(SUM(an.follows_gained),0) AS follows_gained "
        "FROM analytics an JOIN accounts a ON a.id = an.account_id JOIN brands br ON br.id = a.brand_id "
        "JOIN businesses b ON b.id = br.business_id WHERE b.slug = ? "
        "GROUP BY br.name, a.platform ORDER BY views DESC", (business_slug,)
    ).fetchall()
    return [_rates(dict(r)) for r in rows]


def recent(conn: sqlite3.Connection, business_slug: str | None = None, limit: int = 50) -> list[dict]:
    sql = ("SELECT an.*, a.platform, br.name AS brand_name, b.slug AS business_slug, i.title AS idea_title "
           "FROM analytics an JOIN accounts a ON a.id = an.account_id JOIN brands br ON br.id = a.brand_id "
           "JOIN businesses b ON b.id = br.business_id LEFT JOIN ideas i ON i.id = an.idea_id ")
    params: list = []
    if business_slug:
        sql += "WHERE b.slug = ? "
        params.append(business_slug)
    sql += "ORDER BY an.recorded_on DESC, an.id DESC LIMIT ?"
    params.append(limit)
    return [_rates(dict(r)) for r in conn.execute(sql, params).fetchall()]
