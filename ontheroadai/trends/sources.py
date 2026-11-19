"""Research sources: the default feed list, user-added feeds, and platforms we cannot use for free.

Every default is a feed the publisher itself offers for automated reading (RSS/Atom).
Feed URLs change over time, so a default counts as unverified until its first successful
fetch. The dashboard shows each source's real status.
"""

from __future__ import annotations

import re
import uuid
import sqlite3
from urllib.parse import urlparse

from ..db import log, now

OTRM, GTA = "on-the-road-media", "gta6-network"

RELIABILITY = {
    "official": ("Official", "League, team, athlete or publisher's own announcement"),
    "major_publisher": ("Major sports/news publisher", "Established newsroom with editorial standards"),
    "publisher": ("Publisher", "Established outlet; check claims before stating them as fact"),
    "creator": ("Creator channel", "A creator's own posts; true for what they posted, not for wider claims"),
    "community": ("Community", "Fan discussion; never treat as confirmed"),
    "user": ("Added by you", "Something you submitted"),
}
RELIABILITY_RANK = {"official": 10, "major_publisher": 8, "publisher": 6, "user": 5, "creator": 4, "community": 3}

_PUBLIC_FEED = "Publisher-provided public RSS feed, made for automated reading. Headlines and links only."

# key, name, business, url, category, reliability, interval minutes, enabled
DEFAULT_SOURCES = [
    ("espn-top", "ESPN: top headlines", OTRM, "https://www.espn.com/espn/rss/news", "", "major_publisher", 60, 1),
    ("espn-nba", "ESPN: NBA", OTRM, "https://www.espn.com/espn/rss/nba/news", "nba", "major_publisher", 60, 1),
    ("espn-nfl", "ESPN: NFL", OTRM, "https://www.espn.com/espn/rss/nfl/news", "nfl", "major_publisher", 60, 1),
    ("espn-mlb", "ESPN: MLB", OTRM, "https://www.espn.com/espn/rss/mlb/news", "mlb", "major_publisher", 120, 1),
    ("espn-nhl", "ESPN: NHL", OTRM, "https://www.espn.com/espn/rss/nhl/news", "nhl", "major_publisher", 120, 1),
    ("cbs-nba", "CBS Sports: NBA", OTRM, "https://www.cbssports.com/rss/headlines/nba/", "nba", "major_publisher", 60, 1),
    ("cbs-nfl", "CBS Sports: NFL", OTRM, "https://www.cbssports.com/rss/headlines/nfl/", "nfl", "major_publisher", 60, 1),
    ("cbs-mlb", "CBS Sports: MLB", OTRM, "https://www.cbssports.com/rss/headlines/mlb/", "mlb", "major_publisher", 120, 1),
    ("cbs-nhl", "CBS Sports: NHL", OTRM, "https://www.cbssports.com/rss/headlines/nhl/", "nhl", "major_publisher", 120, 1),
    ("espn-mma", "ESPN: MMA / UFC", OTRM, "https://www.espn.com/espn/rss/mma/news", "ufc", "major_publisher", 120, 1),
    ("cbs-mma", "CBS Sports: MMA / UFC", OTRM, "https://www.cbssports.com/rss/headlines/mma/", "ufc", "major_publisher", 120, 1),
    ("mmafighting", "MMA Fighting (UFC/MMA)", OTRM, "https://www.mmafighting.com/rss/index.xml", "ufc", "publisher", 120, 1),
    ("gamespot-news", "GameSpot: news", GTA, "https://www.gamespot.com/feeds/news/", "gaming", "publisher", 90, 1),
    ("pcgamer", "PC Gamer", GTA, "https://www.pcgamer.com/rss/", "gaming", "publisher", 90, 1),
    ("polygon", "Polygon", GTA, "https://www.polygon.com/rss/index.xml", "gaming", "publisher", 90, 1),
    ("kotaku", "Kotaku", GTA, "https://kotaku.com/rss", "gaming", "publisher", 90, 1),
    ("ign-games", "IGN: games", GTA, "https://feeds.feedburner.com/ign/games-all", "gaming", "publisher", 90, 1),
    ("reddit-gta6", "Reddit r/GTA6 (community)", GTA, "https://www.reddit.com/r/GTA6/.rss", "gaming", "community", 120, 0),
]

URL_FIXES = [("mmafighting", "https://www.mmafighting.com/rss/current", "https://www.mmafighting.com/rss/index.xml")]

DEFAULT_NOTES = {
    "reddit-gta6": "Reddit offers this RSS feed publicly, but its robots.txt and API terms restrict automated "
                   "access. Off by default. If enabled, the robots.txt check decides whether it may be read. "
                   "Community posts are never treated as confirmed.",
}

# Platforms people ask about that cannot be used here without paid plans, app review or logins.
UNAVAILABLE = [
    ("X (Twitter)", "requires_credentials", "Reading posts needs a paid X API plan. Not used. Add links by hand instead."),
    ("TikTok", "unavailable", "TikTok's Research API is limited to approved academic researchers, and there is no public "
                              "feed. Add links by hand."),
    ("Instagram", "requires_credentials", "The Instagram Graph API only covers business accounts you own, after app "
                                          "review. Competitors' posts can't be read. Add links by hand."),
    ("YouTube Data API", "requires_credentials", "Needs a free Google API key. Not needed: channels are followed through "
                                                 "YouTube's public channel feeds (add a channel ID in Watchlists)."),
    ("Twitch / Kick", "requires_credentials", "Twitch's API needs a registered app and token, and Kick has no public "
                                              "feed. Streamers are tracked by hand in Watchlists."),
    ("Official league sites (NBA.com, NFL.com…)", "unavailable", "No public RSS feeds found. Official news reaches "
                                                                 "the system through the publishers above and "
                                                                 "your own submitted links."),
    ("Rockstar Newswire", "unavailable", "No working public feed (newswire/rss returned HTTP 500 when tested on "
                                         "2026-11-19). Add Rockstar's official YouTube channel ID under Watchlists as "
                                         "'Gaming news source' (marked official), or paste Newswire links as submissions."),
]


def seed_sources(conn: sqlite3.Connection) -> None:
    """Insert defaults that are missing. Never overwrites a person's edits."""
    ts = now()
    for key, name, biz, url, cat, rel, interval, enabled in DEFAULT_SOURCES:
        conn.execute(
            "INSERT OR IGNORE INTO research_sources (key, name, business, kind, url, category, reliability, "
            "access_note, enabled, min_interval_minutes, is_builtin, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,?)",
            (key, name, biz, "rss", url, cat, rel, DEFAULT_NOTES.get(key, _PUBLIC_FEED), enabled, interval, ts, ts))
    # Feed URLs that moved, fixed only if the person hasn't edited them.
    for key, old_url, new_url in URL_FIXES:
        conn.execute("UPDATE research_sources SET url = ?, last_status = 'never', last_error = '', etag = NULL, "
                     "last_modified = NULL WHERE key = ? AND url = ?", (new_url, key, old_url))
    conn.execute(
        "INSERT OR IGNORE INTO research_sources (key, name, business, kind, url, category, reliability, access_note, "
        "enabled, is_builtin, last_status, created_at, updated_at) VALUES "
        "('manual', 'Your submitted links and notes', 'both', 'manual', '', '', 'user', "
        "'Things you add by hand. Nothing is fetched.', 1, 1, 'manual', ?, ?)", (ts, ts))


def manual_source_id(conn) -> int:
    return conn.execute("SELECT id FROM research_sources WHERE key = 'manual'").fetchone()[0]


def list_sources(conn: sqlite3.Connection) -> list[dict]:
    rows = [dict(r) for r in conn.execute(
        "SELECT s.*, (SELECT COUNT(*) FROM research_items i WHERE i.source_id = s.id) AS total_items "
        "FROM research_sources s ORDER BY s.kind = 'manual', s.business, s.name")]
    for r in rows:
        r["health"] = health(r)
    return rows


def health(src: dict) -> str:
    """working | stale | error | requires_credentials | disabled | never | manual"""
    if src["kind"] == "manual":
        return "manual"
    if src["requires_credentials"]:
        return "requires_credentials"
    if not src["enabled"]:
        return "disabled"
    if src["last_status"] == "never":
        return "never"
    if src["last_status"] not in ("ok", "not_modified"):
        return "error"
    if src["last_success_at"]:
        from datetime import datetime, timezone

        age_min = (datetime.now(timezone.utc) - datetime.fromisoformat(src["last_success_at"])).total_seconds() / 60
        if age_min > max(3 * src["min_interval_minutes"], 24 * 60):
            return "stale"
    return "working"


_YT_CHANNEL = re.compile(r"(UC[A-Za-z0-9_-]{22})")


def youtube_feed_url(value: str) -> str | None:
    """Accept a channel ID, a /channel/UC... URL or a feed URL. @handles can't be resolved without
    scraping the page, so they return None and the person is told how to find the ID."""
    value = (value or "").strip()
    if "feeds/videos.xml" in value:
        return value
    m = _YT_CHANNEL.search(value)
    return f"https://www.youtube.com/feeds/videos.xml?channel_id={m.group(1)}" if m else None


def add_source(conn: sqlite3.Connection, name: str, url: str, business: str, reliability: str = "publisher",
               category: str = "", kind: str = "rss", interval: int = 120, key: str | None = None) -> int:
    name = (name or "").strip()
    url = (url or "").strip()
    if not name:
        raise ValueError("Give the source a name.")
    if business not in (OTRM, GTA, "both"):
        raise ValueError("Choose a business.")
    if reliability not in RELIABILITY:
        raise ValueError("Choose a reliability level.")
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError("Enter a full feed URL starting with https://")
    if conn.execute("SELECT 1 FROM research_sources WHERE url = ?", (url,)).fetchone():
        raise ValueError("That feed is already in the source list.")
    ts = now()
    key = key or f"user-{uuid.uuid4().hex[:10]}"
    cur = conn.execute(
        "INSERT INTO research_sources (key, name, business, kind, url, category, reliability, access_note, enabled, "
        "min_interval_minutes, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,1,?,?,?)",
        (key, name, business, kind, url, category, reliability,
         "Added by you. Only add feeds the site offers for automated reading; robots.txt is checked before every fetch.",
         max(15, int(interval)), ts, ts))
    log(conn, "research_source", cur.lastrowid, "added", name)
    conn.commit()
    return cur.lastrowid


def update_source(conn: sqlite3.Connection, source_id: int, enabled: bool | None = None, url: str | None = None,
                  interval: int | None = None) -> None:
    src = conn.execute("SELECT * FROM research_sources WHERE id = ?", (source_id,)).fetchone()
    if src is None:
        raise LookupError("Source not found")
    if src["kind"] == "manual":
        raise ValueError("The submissions source can't be changed.")
    sets, params = [], []
    if enabled is not None:
        sets.append("enabled = ?")
        params.append(1 if enabled else 0)
    if url is not None and url.strip() != src["url"]:
        p = urlparse(url.strip())
        if p.scheme not in ("http", "https") or not p.netloc:
            raise ValueError("Enter a full feed URL starting with https://")
        sets += ["url = ?", "etag = NULL", "last_modified = NULL", "last_status = 'never'", "last_error = ''"]
        params.append(url.strip())
    if interval is not None:
        if int(interval) < 15:
            raise ValueError("Check a source at most every 15 minutes.")
        sets.append("min_interval_minutes = ?")
        params.append(int(interval))
    if sets:
        conn.execute(f"UPDATE research_sources SET {', '.join(sets)}, updated_at = ? WHERE id = ?",
                     [*params, now(), source_id])
        log(conn, "research_source", source_id, "updated", src["name"])
        conn.commit()


def delete_source(conn: sqlite3.Connection, source_id: int) -> None:
    src = conn.execute("SELECT * FROM research_sources WHERE id = ?", (source_id,)).fetchone()
    if src is None:
        raise LookupError("Source not found")
    if src["is_builtin"]:
        raise ValueError("Built-in sources can be turned off but not deleted.")
    conn.execute("DELETE FROM research_sources WHERE id = ?", (source_id,))
    conn.commit()
