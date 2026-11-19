"""Creator & competitor watchlists.

A creator entry is a profile link plus, where a permitted feed exists (YouTube channel
feeds, RSS), a research source that Trend Scout reads. Patterns are calculated only from
items those feeds actually returned. No view counts or private analytics are guessed. The
only numbers shown are view counts YouTube itself includes in its public channel feed.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .. import rights
from ..db import log, now
from .sources import add_source, youtube_feed_url
from .text import tokens

CATEGORIES = {
    "sports_creator": ("Sports creator", "on-the-road-media"),
    "sports_media": ("Sports media account", "on-the-road-media"),
    "gta_streamer": ("GTA streamer", "gta6-network"),
    "gta_creator": ("GTA content creator", "gta6-network"),
    "gaming_news": ("Gaming news source", "gta6-network"),
}
NO_FEED = {
    "tiktok.com": "TikTok has no public feed. Tracked by hand.",
    "instagram.com": "Instagram has no public feed for other accounts. Tracked by hand.",
    "twitch.tv": "Twitch needs API credentials. Tracked by hand.",
    "kick.com": "Kick has no public feed. Tracked by hand.",
    "x.com": "X needs a paid API. Tracked by hand.",
    "twitter.com": "X needs a paid API. Tracked by hand.",
}


def add_creator(conn: sqlite3.Connection, name: str, category: str, profile_url: str = "", feed: str = "",
                notes: str = "") -> int:
    name = (name or "").strip()
    if not name:
        raise ValueError("Enter the creator's name.")
    if category not in CATEGORIES:
        raise ValueError("Choose a category.")
    business = CATEGORIES[category][1]
    profile_url = (profile_url or "").strip()
    if profile_url and not profile_url.startswith(("http://", "https://")):
        raise ValueError("Profile links must start with https://")
    feed = (feed or "").strip()
    host = urlparse(profile_url).netloc.lower().removeprefix("www.") if profile_url else ""
    platform = host.split(".")[0] if host else ""
    feed_url = None
    kind = "rss"
    if feed:
        feed_url = youtube_feed_url(feed)
        if feed_url:
            kind = "youtube_channel"
        elif feed.startswith(("http://", "https://")):
            feed_url = feed
        else:
            raise ValueError("For YouTube, paste the channel ID (starts with UC…) or a /channel/UC… link. "
                             "@handles can't be looked up automatically: on the channel page, choose "
                             "More → Share channel → Copy channel ID.")
    elif "youtube.com" in host or "youtu.be" in host:
        feed_url = youtube_feed_url(profile_url)
        kind = "youtube_channel" if feed_url else "rss"
    source_id = None
    if feed_url:
        existing = conn.execute("SELECT id FROM research_sources WHERE url = ?", (feed_url,)).fetchone()
        source_id = existing[0] if existing else add_source(
            conn, f"{name} ({'YouTube' if kind == 'youtube_channel' else 'feed'})", feed_url, business,
            "official" if category == "gaming_news" and "rockstar" in name.lower() else "creator",
            category=category, kind=kind, interval=180)
    ts = now()
    cur = conn.execute(
        "INSERT INTO creators (name, business, category, platform, profile_url, source_id, notes, created_at, "
        "updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (name, business, category, platform or ("youtube" if kind == "youtube_channel" else ""), profile_url,
         source_id, notes.strip(), ts, ts))
    log(conn, "creator", cur.lastrowid, "added to watchlist", name)
    conn.commit()
    return cur.lastrowid


def delete_creator(conn: sqlite3.Connection, creator_id: int) -> None:
    row = conn.execute("SELECT * FROM creators WHERE id = ?", (creator_id,)).fetchone()
    if row is None:
        raise LookupError("Creator not found")
    conn.execute("DELETE FROM creators WHERE id = ?", (creator_id,))
    if row["source_id"]:
        conn.execute("UPDATE research_sources SET enabled = 0 WHERE id = ? AND is_builtin = 0", (row["source_id"],))
    conn.commit()


def link_rights_record(conn: sqlite3.Connection, creator_id: int) -> int:
    """Create (or return) a footage-rights record for this creator. Always starts as 'unknown'."""
    c = conn.execute("SELECT * FROM creators WHERE id = ?", (creator_id,)).fetchone()
    if c is None:
        raise LookupError("Creator not found")
    if c["rights_source_id"]:
        return c["rights_source_id"]
    sid = rights.create_source(conn, c["business"], f"{c['name']}: footage", "creator_permission", "unknown",
                               source_url=c["profile_url"], rights_holder=c["name"],
                               notes="From the watchlist. Record their written permission before using any clip.")
    conn.execute("UPDATE creators SET rights_source_id = ? WHERE id = ?", (sid, creator_id))
    conn.commit()
    return sid


def list_creators(conn: sqlite3.Connection, now_dt: datetime | None = None) -> list[dict]:
    now_dt = now_dt or datetime.now(timezone.utc)
    rows = [dict(r) for r in conn.execute(
        "SELECT c.*, s.last_status, s.last_success_at, s.enabled AS source_enabled, p.status AS rights_status "
        "FROM creators c LEFT JOIN research_sources s ON s.id = c.source_id "
        "LEFT JOIN source_permissions p ON p.id = c.rights_source_id ORDER BY c.category, c.name")]
    for r in rows:
        r["category_label"] = CATEGORIES[r["category"]][0]
        host = urlparse(r["profile_url"]).netloc.lower().removeprefix("www.") if r["profile_url"] else ""
        r["tracking"] = "feed" if r["source_id"] else NO_FEED.get(host, "No feed. Tracked by hand.")
        r["patterns"] = patterns(conn, r["source_id"], now_dt) if r["source_id"] else None
    return rows


def patterns(conn: sqlite3.Connection, source_id: int, now_dt: datetime) -> dict:
    """Simple, observable patterns from the creator's own feed items."""
    since = (now_dt - timedelta(days=30)).isoformat(timespec="seconds")
    items = [dict(r) for r in conn.execute(
        "SELECT title, url, published_at, extra_json FROM research_items WHERE source_id = ? "
        "ORDER BY published_at DESC", (source_id,))]
    recent = [i for i in items if (i["published_at"] or "") >= since]
    if not items:
        return {"items": 0}
    words = Counter(w for i in recent or items for w in set(tokens(i["title"])))
    titles = [i["title"] for i in (recent or items)]
    views = [json.loads(i["extra_json"] or "{}").get("views_reported_by_feed") for i in items[:15]]
    return {
        "items": len(items), "last_30_days": len(recent), "per_week": round(len(recent) / 30 * 7, 1),
        "question_titles": round(sum(1 for t in titles if "?" in t) / len(titles) * 100),
        "number_titles": round(sum(1 for t in titles if re.search(r"\d", t)) / len(titles) * 100),
        "avg_title_words": round(sum(len(t.split()) for t in titles) / len(titles), 1),
        "top_words": [w for w, _ in words.most_common(8)],
        "latest": [{"title": i["title"], "url": i["url"], "published_at": i["published_at"],
                    "views": v} for i, v in zip(items[:5], views[:5])],
    }
