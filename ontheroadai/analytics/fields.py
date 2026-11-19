"""The metrics we track, how platform export columns map onto them, and value parsers.

Parsers return None for "not available" (blank, '-', 'N/A'...). They never
substitute a default value: a missing metric stays missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str        # identity | context | metric
    value_type: str  # text | datetime | duration | count | percent
    synonyms: tuple[str, ...]
    help: str = ""


FIELDS: tuple[Field, ...] = (
    Field("post_id", "Post / video ID", "identity", "text",
          ("video id", "post id", "content id", "media id", "tweet id", "item id", "id"),
          "The platform's ID for the post. Best way to recognise the same video in later imports."),
    Field("post_url", "Post link", "identity", "text",
          ("permalink", "post link", "video link", "share url", "link", "url"),
          "Link to the post. The ID is extracted from TikTok/Instagram/YouTube/X/Facebook links."),
    Field("title", "Title / caption", "identity", "text",
          ("video title", "title", "caption", "description", "video description", "post text", "text", "content"),
          "Used to name the video. Used as an identifier only if there is no ID or link."),
    Field("published_at", "Publish date & time", "context", "datetime",
          ("publish time", "video publish time", "post time", "publish date", "published", "date posted",
           "posted", "create time", "created", "date", "time"),
          "When the video was posted."),
    Field("duration_seconds", "Video length", "context", "duration",
          ("duration", "video duration", "duration sec", "length", "video length"),
          "Seconds or mm:ss."),
    Field("topic", "Topic", "context", "text", ("topic", "subject")),
    Field("format", "Format", "context", "text", ("format", "content type", "series")),
    Field("views", "Views", "metric", "count", ("video views", "views", "plays", "total plays")),
    Field("reach", "Reach", "metric", "count",
          ("reach", "accounts reached", "unique viewers", "reached audience")),
    Field("likes", "Likes", "metric", "count", ("likes", "like", "reactions", "hearts")),
    Field("comments", "Comments", "metric", "count", ("comments", "replies")),
    Field("shares", "Shares", "metric", "count", ("shares", "reposts", "retweets", "sends")),
    Field("saves", "Saves", "metric", "count", ("saves", "saved", "favorites", "add to favorites", "bookmarks")),
    Field("follows_gained", "Followers gained", "metric", "count",
          ("followers gained", "new followers", "follows gained", "subscribers gained", "follows", "subscribers"),
          "Followers/subscribers gained from this video."),
    Field("avg_watch_seconds", "Average watch time", "metric", "duration",
          ("average watch time", "avg watch time", "average view duration", "avg view duration",
           "average time watched", "avg time watched"),
          "Seconds or mm:ss."),
    Field("completion_rate", "Completion rate %", "metric", "percent",
          ("completion rate", "watched full video", "full video watch rate", "watched full video %",
           "completed views"),
          "Share of viewers who watched to the end."),
    Field("skip_rate", "Skip rate %", "metric", "percent",
          ("skip rate", "swipe away rate", "swipe away", "skipped"),
          "Share of viewers who scrolled away in the first seconds."),
)

FIELD_BY_KEY = {f.key: f for f in FIELDS}
IDENTITY_KEYS = tuple(f.key for f in FIELDS if f.kind == "identity")
CONTEXT_KEYS = tuple(f.key for f in FIELDS if f.kind == "context")
METRIC_KEYS = tuple(f.key for f in FIELDS if f.kind == "metric")
COUNT_METRICS = tuple(f.key for f in FIELDS if f.kind == "metric" and f.value_type == "count")

# Specific fields are matched before generic ones (e.g. "average watch time" before "time").
MATCH_ORDER = ("avg_watch_seconds", "completion_rate", "skip_rate", "follows_gained", "duration_seconds",
               "post_id", "post_url", "published_at", "views", "reach", "likes", "comments", "shares",
               "saves", "title", "topic", "format")

MISSING = {"", "-", "--", "—", "–", "n/a", "na", "null", "none", "nan", "not available"}


def norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9%]+", " ", (h or "").lower()).strip()


def auto_map(headers: list[str]) -> dict[str, str | None]:
    """Suggest a column for each field from common export names. Always shown to the user to confirm."""
    normed = {h: norm_header(h) for h in headers}
    used: set[str] = set()
    mapping: dict[str, str | None] = {k: None for k in FIELD_BY_KEY}
    # pass 1: exact names; pass 2: header contains a distinctive (multi-word or long) name
    for exact in (True, False):
        for key in MATCH_ORDER:
            if mapping[key]:
                continue
            for syn in FIELD_BY_KEY[key].synonyms:
                if not exact and " " not in syn and len(syn) < 5:
                    continue
                for h in headers:
                    if h in used:
                        continue
                    n = normed[h]
                    hit = n == syn if exact else re.search(rf"(^| ){re.escape(syn)}( |$)", n)
                    if hit:
                        mapping[key] = h
                        used.add(h)
                        break
                if mapping[key]:
                    break
    return mapping


# ---- value parsers ----------------------------------------------------------

def is_missing(raw) -> bool:
    return raw is None or str(raw).strip().lower() in MISSING


def parse_count(raw) -> int | None:
    if is_missing(raw):
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
    else:
        s = str(raw).strip().replace(",", "").replace(" ", "").replace(" ", "")
        mult = 1.0
        if s[-1:].lower() in ("k", "m", "b"):
            mult = {"k": 1e3, "m": 1e6, "b": 1e9}[s[-1].lower()]
            s = s[:-1]
        try:
            value = float(s) * mult
        except ValueError:
            raise ValueError(f"'{raw}' is not a number") from None
        if mult == 1.0 and value != int(value):
            raise ValueError(f"'{raw}' should be a whole number")
    if value < 0:
        raise ValueError(f"'{raw}' cannot be negative")
    return int(round(value))


def parse_percent(raw, fraction: bool = False) -> float | None:
    """'45%' or '45' -> 45.0. With fraction=True, plain numbers are 0-1 fractions ('0.45' -> 45.0)."""
    if is_missing(raw):
        return None
    s = str(raw).strip()
    has_pct = s.endswith("%")
    s = s.rstrip("%").strip()
    if "," in s and "." not in s:
        s = s.replace(",", ".")
    try:
        value = float(s)
    except ValueError:
        raise ValueError(f"'{raw}' is not a percentage") from None
    if fraction and not has_pct:
        value *= 100
    if not 0 <= value <= 100:
        raise ValueError(f"'{raw}' is outside 0-100%")
    return round(value, 2)


_DUR_UNITS = re.compile(r"^(?:(\d+(?:\.\d+)?)\s*h(?:rs?|ours?)?)?\s*(?:(\d+(?:\.\d+)?)\s*m(?:in(?:utes?)?)?)?\s*"
                        r"(?:(\d+(?:\.\d+)?)\s*s(?:ec(?:onds?)?)?)?$")


def parse_duration(raw) -> float | None:
    """Seconds from '7', '7.5', '7s', '0:07', '00:01:05', '1m 5s'."""
    if is_missing(raw):
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
    else:
        s = str(raw).strip().lower()
        try:
            if ":" in s:
                parts = [float(p) for p in s.split(":")]
                if len(parts) > 3:
                    raise ValueError
                value = 0.0
                for p in parts:
                    value = value * 60 + p
            else:
                try:
                    value = float(s)
                except ValueError:
                    m = _DUR_UNITS.match(s)
                    if not m or not any(m.groups()):
                        raise ValueError from None
                    h, mi, se = (float(x) if x else 0.0 for x in m.groups())
                    value = h * 3600 + mi * 60 + se
        except ValueError:
            raise ValueError(f"'{raw}' is not a duration (use seconds or mm:ss)") from None
    if value < 0:
        raise ValueError(f"'{raw}' cannot be negative")
    return round(value, 2)


_DATE_FORMATS = [
    ("%m/%d/%Y %H:%M:%S", True), ("%m/%d/%Y %H:%M", True), ("%m/%d/%Y %I:%M %p", True),
    ("%m/%d/%Y %I:%M:%S %p", True), ("%m/%d/%Y", False), ("%m/%d/%y %H:%M", True), ("%m/%d/%y", False),
    ("%Y/%m/%d %H:%M:%S", True), ("%Y/%m/%d %H:%M", True), ("%Y/%m/%d", False),
    ("%b %d, %Y %I:%M %p", True), ("%b %d, %Y %H:%M", True), ("%b %d, %Y", False), ("%B %d, %Y", False),
    ("%d %b %Y %H:%M", True), ("%d %b %Y", False), ("%d %B %Y", False),
]


def parse_datetime(raw, day_first: bool = False) -> str | None:
    """Return 'YYYY-MM-DD' or 'YYYY-MM-DDTHH:MM' (no time is invented when the export has none)."""
    if is_missing(raw):
        return None
    s = re.sub(r"\s+", " ", str(raw).strip())
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        has_time = len(s) > 10
    except ValueError:
        dt, has_time = None, False
        for fmt, with_time in _DATE_FORMATS:
            if day_first:
                fmt = fmt.replace("%m/%d", "%d/%m")
            try:
                dt = datetime.strptime(s, fmt)
                has_time = with_time
                break
            except ValueError:
                continue
        if dt is None:
            raise ValueError(f"'{raw}' is not a recognised date") from None
    dt = dt.replace(tzinfo=None)
    return dt.strftime("%Y-%m-%dT%H:%M") if has_time else dt.strftime("%Y-%m-%d")


def parse_value(key: str, raw, fraction_percent: bool = False, day_first: bool = False):
    f = FIELD_BY_KEY[key]
    if f.value_type == "count":
        return parse_count(raw)
    if f.value_type == "percent":
        return parse_percent(raw, fraction_percent)
    if f.value_type == "duration":
        return parse_duration(raw)
    if f.value_type == "datetime":
        return parse_datetime(raw, day_first)
    return None if is_missing(raw) else str(raw).strip()
