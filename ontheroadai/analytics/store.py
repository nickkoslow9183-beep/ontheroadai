"""Videos and their performance snapshots: create, correct, remove."""

from __future__ import annotations

import re
import sqlite3
import uuid
from datetime import date
from urllib.parse import parse_qs, urlparse

from ..db import log, now
from .fields import FIELD_BY_KEY, METRIC_KEYS, parse_value

VIDEO_FIELDS = ("post_url", "title", "topic", "format", "published_at", "duration_seconds", "idea_id",
                "external_id")

_ID_PATTERNS = [
    re.compile(r"tiktok\.com/.*/video/(\d+)"),
    re.compile(r"tiktok\.com/.*/photo/(\d+)"),
    re.compile(r"instagram\.com/(?:[^/]+/)?(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)"),
    re.compile(r"(?:youtube\.com/shorts/|youtu\.be/)([A-Za-z0-9_-]{6,})"),
    re.compile(r"(?:twitter|x)\.com/[^/]+/status/(\d+)"),
    re.compile(r"facebook\.com/.*(?:/videos/|/reel/|story_fbid=|v=)(\d+)"),
]


def extract_post_id(url: str) -> str | None:
    if not url:
        return None
    for pat in _ID_PATTERNS:
        m = pat.search(url)
        if m:
            return m.group(1)
    parsed = urlparse(url)
    if "youtube.com" in parsed.netloc:
        v = parse_qs(parsed.query).get("v")
        if v:
            return v[0]
    return None


def normalize_url(url: str) -> str:
    p = urlparse(url.strip())
    host = p.netloc.lower().removeprefix("www.").removeprefix("m.")
    return f"{host}{p.path.rstrip('/')}"


def video_key(post_id: str | None = None, url: str | None = None, title: str | None = None,
              published_at: str | None = None) -> str | None:
    """Stable identity used to recognise the same video across imports."""
    post_id = (post_id or "").strip()
    if post_id:
        return f"id:{post_id}"
    url = (url or "").strip()
    if url:
        pid = extract_post_id(url)
        return f"id:{pid}" if pid else f"url:{normalize_url(url)}"
    title = re.sub(r"\s+", " ", (title or "").strip().lower())
    if title:
        return f"title:{title[:200]}|{(published_at or '')[:10]}"
    return None


def _clean_video_fields(fields: dict) -> dict:
    out = {}
    for k, v in fields.items():
        if k not in VIDEO_FIELDS:
            raise ValueError(f"Unknown video field {k!r}")
        if k == "duration_seconds":
            v = parse_value("duration_seconds", v)
            if v is not None and v <= 0:
                raise ValueError("Video length must be more than 0 seconds.")
        elif k == "published_at":
            v = parse_value("published_at", v)
        elif k == "idea_id":
            v = int(v) if v not in (None, "") else None
        else:
            v = (v or "").strip() if isinstance(v, str) or v is None else v
        out[k] = v
    return out


def find_or_create_video(conn: sqlite3.Connection, account_id: int, key: str, fields: dict,
                         created_via: str = "manual", import_id: int | None = None) -> tuple[int, bool]:
    """Return (video_id, created). Existing videos only get *empty* fields filled in, so
    corrections a person made are never overwritten by a later import."""
    fields = _clean_video_fields(fields)
    row = conn.execute("SELECT * FROM videos WHERE account_id = ? AND video_key = ?", (account_id, key)).fetchone()
    if row:
        fill = {k: v for k, v in fields.items() if v not in (None, "") and row[k] in (None, "")}
        if fill:
            sets = ", ".join(f"{k} = ?" for k in fill)
            conn.execute(f"UPDATE videos SET {sets}, updated_at = ? WHERE id = ?", [*fill.values(), now(), row["id"]])
        return row["id"], False
    if key.startswith("id:") and not fields.get("external_id"):
        fields["external_id"] = key[3:]
    ts = now()
    cols = ["account_id", "video_key", *fields.keys(), "created_via", "import_id", "created_at", "updated_at"]
    vals = [account_id, key, *fields.values(), created_via, import_id, ts, ts]
    cur = conn.execute(f"INSERT INTO videos ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    return cur.lastrowid, True


def validate_metrics(metrics: dict, fraction_percent: bool = False) -> dict:
    """Parse raw metric values. Blank -> None (missing). Never fills in a guess."""
    unknown = set(metrics) - set(METRIC_KEYS)
    if unknown:
        raise ValueError(f"Unknown metric(s): {', '.join(sorted(unknown))}")
    out = {}
    for key in METRIC_KEYS:
        try:
            out[key] = parse_value(key, metrics.get(key), fraction_percent=fraction_percent)
        except ValueError as exc:
            raise ValueError(f"{FIELD_BY_KEY[key].label}: {exc}") from None
    return out


def _check_date(value: str | None) -> str:
    value = value or date.today().isoformat()
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError("Date must be YYYY-MM-DD.") from None


def add_snapshot(conn: sqlite3.Connection, video_id: int, captured_on: str | None, metrics: dict,
                 source: str = "manual", import_id: int | None = None, notes: str = "",
                 already_parsed: bool = False) -> int:
    captured_on = _check_date(captured_on)
    values = metrics if already_parsed else validate_metrics(metrics)
    if all(values.get(k) is None for k in METRIC_KEYS):
        raise ValueError("Enter at least one metric.")
    if conn.execute("SELECT 1 FROM video_snapshots WHERE video_id = ? AND captured_on = ?",
                    (video_id, captured_on)).fetchone():
        raise ValueError(f"This video already has numbers for {captured_on}. Edit that entry instead.")
    ts = now()
    cols = ["video_id", "captured_on", *METRIC_KEYS, "source", "import_id", "notes", "created_at", "updated_at"]
    vals = [video_id, captured_on, *(values.get(k) for k in METRIC_KEYS), source, import_id, notes.strip(), ts, ts]
    cur = conn.execute(f"INSERT INTO video_snapshots ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
    return cur.lastrowid


def add_manual_entry(conn: sqlite3.Connection, account_id: int, video: dict, captured_on: str | None,
                     metrics: dict) -> tuple[int, int]:
    """Manual entry from the dashboard: find/create the video, then add one snapshot."""
    if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
        raise ValueError("Unknown account.")
    video = {k: v for k, v in video.items() if v not in (None, "")}
    published = parse_value("published_at", video.get("published_at"))
    key = video_key(video.get("external_id"), video.get("post_url"), video.get("title"), published)
    if key is None:
        key = f"manual:{uuid.uuid4().hex}"
    metrics_clean = validate_metrics(metrics)
    video_id, _ = find_or_create_video(conn, account_id, key, video, created_via="manual")
    snap_id = add_snapshot(conn, video_id, captured_on, metrics_clean, "manual", already_parsed=True)
    log(conn, "video", video_id, "metrics entered manually", captured_on or "")
    conn.commit()
    after_data_change(conn, [video_id])
    return video_id, snap_id


def update_snapshot(conn: sqlite3.Connection, snapshot_id: int, metrics: dict, captured_on: str | None = None,
                    notes: str | None = None) -> None:
    snap = conn.execute("SELECT * FROM video_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if snap is None:
        raise LookupError("Snapshot not found")
    values = validate_metrics(metrics)
    if all(v is None for v in values.values()):
        raise ValueError("A snapshot needs at least one metric. Delete it instead.")
    captured_on = _check_date(captured_on) if captured_on else snap["captured_on"]
    sets = ", ".join(f"{k} = ?" for k in METRIC_KEYS)
    conn.execute(
        f"UPDATE video_snapshots SET {sets}, captured_on = ?, notes = ?, updated_at = ? WHERE id = ?",
        [*(values[k] for k in METRIC_KEYS), captured_on, snap["notes"] if notes is None else notes.strip(),
         now(), snapshot_id],
    )
    log(conn, "snapshot", snapshot_id, "corrected")
    conn.commit()
    after_data_change(conn, [snap["video_id"]])


def delete_snapshot(conn: sqlite3.Connection, snapshot_id: int) -> int:
    snap = conn.execute("SELECT video_id FROM video_snapshots WHERE id = ?", (snapshot_id,)).fetchone()
    if snap is None:
        raise LookupError("Snapshot not found")
    conn.execute("DELETE FROM video_snapshots WHERE id = ?", (snapshot_id,))
    log(conn, "snapshot", snapshot_id, "deleted")
    conn.commit()
    return snap["video_id"]


def get_video(conn: sqlite3.Connection, video_id: int) -> dict:
    row = conn.execute(
        "SELECT v.*, a.platform, a.brand_id, br.name AS brand_name, b.slug AS business_slug, "
        "b.name AS business_name, i.title AS idea_title FROM videos v JOIN accounts a ON a.id = v.account_id "
        "JOIN brands br ON br.id = a.brand_id JOIN businesses b ON b.id = br.business_id "
        "LEFT JOIN ideas i ON i.id = v.idea_id WHERE v.id = ?", (video_id,)).fetchone()
    if row is None:
        raise LookupError(f"Video {video_id} not found")
    return dict(row)


def snapshots(conn: sqlite3.Connection, video_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM video_snapshots WHERE video_id = ? ORDER BY captured_on, id", (video_id,))]


def update_video(conn: sqlite3.Connection, video_id: int, **fields) -> None:
    video = get_video(conn, video_id)
    clean = _clean_video_fields(fields)
    if clean.get("idea_id"):
        idea = conn.execute("SELECT b.slug FROM ideas i JOIN businesses b ON b.id = i.business_id WHERE i.id = ?",
                            (clean["idea_id"],)).fetchone()
        if idea is None or idea["slug"] != video["business_slug"]:
            raise ValueError("That idea belongs to a different business.")
    if not clean:
        return
    sets = ", ".join(f"{k} = ?" for k in clean)
    conn.execute(f"UPDATE videos SET {sets}, updated_at = ? WHERE id = ?", [*clean.values(), now(), video_id])
    log(conn, "video", video_id, "edited", ", ".join(sorted(clean)))
    conn.commit()
    after_data_change(conn, [video_id])


def bulk_tag(conn: sqlite3.Connection, video_ids: list[int], business_slug: str, fmt: str | None = None,
             topic: str | None = None) -> int:
    sets, params = [], []
    if fmt is not None:
        sets.append("format = ?")
        params.append(fmt.strip())
    if topic is not None:
        sets.append("topic = ?")
        params.append(topic.strip())
    if not sets or not video_ids:
        raise ValueError("Choose videos and a format or topic to apply.")
    n = 0
    for vid in video_ids:
        if get_video(conn, vid)["business_slug"] != business_slug:
            raise ValueError("Selected videos belong to a different business.")
        conn.execute(f"UPDATE videos SET {', '.join(sets)}, updated_at = ? WHERE id = ?", [*params, now(), vid])
        n += 1
    log(conn, "video", None, "bulk tagged", f"{n} videos")
    conn.commit()
    return n


def delete_video(conn: sqlite3.Connection, video_id: int) -> None:
    get_video(conn, video_id)
    conn.execute("DELETE FROM videos WHERE id = ?", (video_id,))
    log(conn, "video", video_id, "deleted with its snapshots")
    conn.commit()


def link_published_idea(conn: sqlite3.Connection, idea_id: int) -> int | None:
    """When a person marks an idea published, create (or link) the matching video record so
    later CSV imports with the same post link/ID attach to it automatically."""
    idea = conn.execute(
        "SELECT i.*, b.id AS biz_id FROM ideas i JOIN businesses b ON b.id = i.business_id WHERE i.id = ?",
        (idea_id,)).fetchone()
    brand_id = idea["brand_id"]
    if brand_id is None:
        brands = conn.execute("SELECT id FROM brands WHERE business_id = ?", (idea["biz_id"],)).fetchall()
        if len(brands) != 1:
            return None  # ambiguous: the person links it on the video page
        brand_id = brands[0]["id"]
    acc = conn.execute("SELECT id FROM accounts WHERE brand_id = ? AND platform = ?",
                       (brand_id, idea["published_platform"])).fetchone()
    if acc is None:
        return None
    key = video_key(None, idea["published_url"]) or f"idea:{idea_id}:{idea['published_platform']}"
    video_id, _ = find_or_create_video(
        conn, acc["id"], key,
        {"post_url": idea["published_url"] or "", "title": idea["title"], "format": idea["content_type"],
         "published_at": (idea["published_at"] or "")[:16] or None, "idea_id": idea_id},
        created_via="publish")
    conn.execute("UPDATE videos SET idea_id = ? WHERE id = ? AND idea_id IS NULL", (idea_id, video_id))
    conn.commit()
    return video_id


def after_data_change(conn: sqlite3.Connection, video_ids) -> None:
    """New numbers arrived: let the Orchestrator re-run post-publish tasks for linked, published ideas."""
    ids = list(video_ids)
    if not ids:
        return
    rows = conn.execute(
        f"SELECT DISTINCT v.idea_id FROM videos v JOIN ideas i ON i.id = v.idea_id "
        f"WHERE i.status = 'published' AND v.id IN ({', '.join('?' * len(ids))})", ids).fetchall()
    if not rows:
        return
    from ..agents.orchestrator import Orchestrator

    orch = Orchestrator(conn)
    for r in rows:
        orch.advance_post(r["idea_id"])
