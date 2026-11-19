"""TEST FIXTURES: fictional feeds for Trend Scout tests. Not real news.

People named here (Jalen Brooks, Marcus Hale, ...) are invented. URLs use the reserved
.test domain. Dates are generated relative to the test clock so freshness can be tested.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from xml.sax.saxutils import escape

from ontheroadai.trends.fetcher import Response, TransportError

NOW = datetime(2026, 11, 19, 18, 0, tzinfo=timezone.utc)

SPORTS_A = "https://feeds.sporta.test/nba.xml"
SPORTS_B = "https://feeds.sportb.test/nba/"
UFC_ATOM = "https://feeds.fightnews.test/atom"
GAMING = "https://feeds.gamenews.test/rss"
YOUTUBE = "https://www.youtube.com/feeds/videos.xml?channel_id=UCtestchannel00000000001"
COMMUNITY = "https://community.test/r/gta6.rss"


def _rfc(now, hours):
    return format_datetime(now - timedelta(hours=hours))


def _iso(now, hours):
    return (now - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def rss(title, items):
    body = "".join(
        f"<item><title>{escape(t)}</title><link>{escape(link)}</link><description><![CDATA[<p>{d}</p>]]></description>"
        f"<pubDate>{date}</pubDate><guid>{escape(link)}</guid></item>" for t, link, d, date in items)
    return (f'<?xml version="1.0"?><!-- TEST FIXTURE: not real news --><rss version="2.0"><channel>'
            f"<title>{title}</title>{body}</channel></rss>").encode()


def feeds(now: datetime = NOW) -> dict[str, bytes]:
    a = rss("TEST Sports A", [
        ("Jalen Brooks drops career-high 51 as Lakers stun Celtics", "https://sporta.test/nba/brooks-51",
         "Jalen Brooks scored a career-high 51 points as the Lakers beat the Celtics 118-112.", _rfc(now, 3)),
        ("Warriors agree to trade Marcus Hale to Knicks", "https://sporta.test/nba/hale-trade",
         "The Warriors traded guard Marcus Hale to the Knicks for two picks.", _rfc(now, 5)),
        ("Is Jalen Brooks overrated? Fans debate after big night", "https://sporta.test/nba/brooks-debate",
         "Fans argue about whether Brooks belongs in the top five.", _rfc(now, 30)),
        ("Former guard Tom Reyes arrested after incident", "https://sporta.test/nba/reyes",
         "Police say the former guard was arrested.", _rfc(now, 8)),
    ])
    b = rss("TEST Sports B", [
        ("Jalen Brooks scores career-high 51 points in Lakers win over Celtics",
         "https://www.sportb.test/nba/news/brooks-career-high?utm_source=rss&utm_medium=feed",
         "Brooks went off for 51 in Boston.", _rfc(now, 2)),
        # same article as Sports A, just with tracking parameters -> duplicate
        ("Jalen Brooks drops career-high 51 as Lakers stun Celtics",
         "https://www.sporta.test/nba/brooks-51/?utm_source=partner#comments", "dupe", _rfc(now, 3)),
        ("Knicks land Marcus Hale in trade with Warriors", "https://sportb.test/nba/knicks-hale",
         "New York acquires Marcus Hale.", _rfc(now, 4)),
        ("Report: Nets reportedly could pursue Darius Cole", "https://sportb.test/nba/cole-rumor",
         "Per sources, the Nets may make a move.", _rfc(now, 6)),
    ])
    ufc = (f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>TEST Fight News</title>'
           f'<entry><title>UFC 330: Rivera vs. Okafor rematch set after controversial decision</title>'
           f'<link rel="alternate" href="https://fightnews.test/ufc-330-rematch"/><id>tag:fightnews.test,1</id>'
           f'<published>{_iso(now, 10)}</published><summary>The rematch is official.</summary>'
           f'<author><name>Staff</name></author></entry></feed>').encode()
    gaming = rss("TEST Game News", [
        ("Rockstar confirms new GTA 6 trailer details for Vice City", "https://gamenews.test/gta6-trailer",
         "Rockstar Games announced the details in a post.", _rfc(now, 7)),
        ("GTA 6 leak suggests online mode might include heists", "https://gamenews.test/gta6-leak",
         "An insider claims...", _rfc(now, 12)),
        ("Streamer's GTA 6 bank heist ends in hilarious fail", "https://gamenews.test/gta6-streamer-fail",
         "The stream clip went viral.", _rfc(now, 9)),
        ("Nintendo announces a new handheld bundle", "https://gamenews.test/nintendo",
         "Not GTA related.", _rfc(now, 5)),
    ])
    yt = (f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" '
          f'xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/">'
          f"<title>TEST Creator</title><entry><id>yt:video:abc123</id><yt:videoId>abc123</yt:videoId>"
          f"<title>I found a hidden easter egg in GTA 6 Vice City</title>"
          f'<link rel="alternate" href="https://www.youtube.com/watch?v=abc123"/>'
          f"<author><name>TestCreator</name></author><published>{_iso(now, 20)}</published>"
          f'<media:group><media:description>Secret spot.</media:description><media:community>'
          f'<media:statistics views="48210"/></media:community></media:group></entry></feed>').encode()
    community = (f'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>TEST community</title>'
                 f'<entry><title>Theory: the GTA 6 map is bigger than we think</title>'
                 f'<link href="https://community.test/r/gta6/comments/1"/><id>c1</id>'
                 f'<updated>{_iso(now, 15)}</updated><author><name>u/fan</name></author></entry></feed>').encode()
    return {SPORTS_A: a, SPORTS_B: b, UFC_ATOM: ufc, GAMING: gaming, YOUTUBE: yt, COMMUNITY: community}


class FakeTransport:
    """Serves fixture feeds. robots.txt is 404 (allowed) unless overridden."""

    def __init__(self, now: datetime = NOW, overrides: dict | None = None):
        self.pages = {u: Response(200, {"etag": f'"{abs(hash(u))}"', "content-type": "application/rss+xml"}, b)
                      for u, b in feeds(now).items()}
        self.overrides = overrides or {}
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, headers, timeout=20):
        self.requests.append((url, dict(headers)))
        if url in self.overrides:
            o = self.overrides[url]
            if isinstance(o, Exception):
                raise o
            return o
        if url.endswith("/robots.txt"):
            return Response(404, {}, b"")
        if url in self.pages:
            page = self.pages[url]
            if headers.get("If-None-Match") and headers["If-None-Match"] == page.headers.get("etag"):
                return Response(304, {}, b"")
            return page
        raise TransportError("Name or service not known")


def install_test_sources(conn, urls=None):
    """Disable the real defaults and add the fixture feeds as sources (test DB only)."""
    from ontheroadai.trends import sources

    conn.execute("UPDATE research_sources SET enabled = 0 WHERE kind <> 'manual'")
    conn.commit()
    spec = [
        ("TEST Sports A", SPORTS_A, "on-the-road-media", "major_publisher", "nba"),
        ("TEST Sports B", SPORTS_B, "on-the-road-media", "major_publisher", "nba"),
        ("TEST Fight News", UFC_ATOM, "on-the-road-media", "publisher", "ufc"),
        ("TEST Game News", GAMING, "gta6-network", "publisher", "gaming"),
        ("TEST Community", COMMUNITY, "gta6-network", "community", "gaming"),
    ]
    ids = {}
    for name, url, biz, rel, cat in spec:
        if urls is None or url in urls:
            ids[url] = sources.add_source(conn, name, url, biz, rel, cat, interval=60)
    return ids
