"""RSS 2.0 / RSS 1.0 (RDF) / Atom parsing and normalisation, standard library only."""

from __future__ import annotations

import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "media": "http://search.yahoo.com/mrss/",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "dc": "http://purl.org/dc/elements/1.1/",
    "content": "http://purl.org/rss/1.0/modules/content/",
    "rss1": "http://purl.org/rss/1.0/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
}
TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|mc_cid|mc_eid|cmpid|ref|ref_src|src|source|ex_cid|xid|"
                             r"partner|ito|icid|sr_share|feature|si)$", re.I)
MAX_SUMMARY = 600


class FeedError(ValueError):
    """The response is not a readable RSS/Atom feed."""


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_SUMMARY:
        text = text[:MAX_SUMMARY].rsplit(" ", 1)[0] + "…"
    return text


def parse_date(value: str | None, now: datetime | None = None) -> str | None:
    """Return UTC ISO 8601, or None if missing/unreadable/implausibly far in the future."""
    if not value or not value.strip():
        return None
    v = value.strip()
    dt = None
    try:
        dt = parsedate_to_datetime(v)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    now = now or datetime.now(timezone.utc)
    if dt > now + timedelta(days=2) or dt.year < 2000:
        return None
    return dt.isoformat(timespec="seconds")


def canonical_url(url: str) -> str:
    """Normalise for de-duplication: https, no www, no fragment, no tracking parameters."""
    p = urlparse((url or "").strip())
    if not p.netloc:
        return (url or "").strip()
    host = p.netloc.lower()
    host = host[4:] if host.startswith("www.") else host
    host = host[2:] if host.startswith("m.") else host
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
                             if not TRACKING_PARAMS.match(k)))
    path = p.path.rstrip("/") or "/"
    return urlunparse(("https", host, path, "", query, ""))


def _text(el, path, ns=NS) -> str:
    found = el.find(path, ns)
    return (found.text or "").strip() if found is not None and found.text else ""


def parse_feed(body: bytes, base_url: str = "", now: datetime | None = None) -> dict:
    """Return {"title": str, "items": [ {title, url, guid, summary, author, published_at, extra} ]}."""
    if not body or not body.strip():
        raise FeedError("Empty response")
    head = body[:600].lstrip().lower()
    if head.startswith(b"<!doctype html") or head.startswith(b"<html"):
        raise FeedError("The URL returned a web page, not an RSS/Atom feed")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise FeedError(f"Not valid XML: {exc}") from None
    tag = root.tag.split("}")[-1].lower()
    items = []
    if tag == "rss" or tag == "rdf":
        channel = root.find("channel")
        feed_title = _text(channel, "title") if channel is not None else ""
        if tag == "rss":
            nodes = channel.findall("item") if channel is not None else []
        else:
            feed_title = _text(root, "rss1:channel/rss1:title")
            nodes = root.findall("rss1:item", NS)
        for it in nodes:
            def g(name):
                return _text(it, name) or _text(it, f"rss1:{name}")
            link = g("link") or _text(it, "guid")
            items.append({
                "title": strip_html(g("title")),
                "url": urljoin(base_url, link) if link else "",
                "guid": _text(it, "guid"),
                "summary": strip_html(g("description") or _text(it, "content:encoded")),
                "author": _text(it, "dc:creator") or g("author"),
                "published_at": parse_date(g("pubDate") or _text(it, "dc:date"), now),
                "extra": {"categories": [c.text.strip() for c in it.findall("category") if c.text][:8]},
            })
    elif tag == "feed":
        feed_title = _text(root, "atom:title")
        for e in root.findall("atom:entry", NS):
            link = ""
            for ln in e.findall("atom:link", NS):
                if ln.get("rel", "alternate") == "alternate" and ln.get("href"):
                    link = ln.get("href")
                    break
            summary = _text(e, "atom:summary") or _text(e, "atom:content") or _text(e, "media:group/media:description")
            extra = {}
            vid = _text(e, "yt:videoId")
            if vid:
                extra["youtube_video_id"] = vid
                stats = e.find("media:group/media:community/media:statistics", NS)
                if stats is not None and stats.get("views", "").isdigit():
                    extra["views_reported_by_feed"] = int(stats.get("views"))
            items.append({
                "title": strip_html(_text(e, "atom:title")),
                "url": urljoin(base_url, link) if link else "",
                "guid": _text(e, "atom:id"),
                "summary": strip_html(summary),
                "author": _text(e, "atom:author/atom:name"),
                "published_at": parse_date(_text(e, "atom:published") or _text(e, "atom:updated"), now),
                "extra": extra,
            })
    else:
        raise FeedError(f"Unrecognised feed format <{tag}>")
    items = [i for i in items if i["title"] and i["url"]]
    return {"title": feed_title, "items": items}
