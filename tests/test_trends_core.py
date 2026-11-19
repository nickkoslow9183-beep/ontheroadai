"""Trend Scout: fetching rules, parsing, normalisation, classification, verification, hooks.
All data here is TEST FIXTURE data (see trend_fixtures.py), not live results."""

from datetime import datetime, timezone

import pytest

from ontheroadai.trends import classify, recommend
from ontheroadai.trends.fetcher import Fetcher, Response, TransportError
from ontheroadai.trends.parser import FeedError, canonical_url, parse_date, parse_feed, strip_html
from ontheroadai.trends.text import named_phrases, related, keyword_set
from trend_fixtures import GAMING, NOW, SPORTS_A, UFC_ATOM, YOUTUBE, FakeTransport, feeds

F = feeds()


# ---- parsing & normalisation ---------------------------------------------------------

def test_parse_rss():
    feed = parse_feed(F[SPORTS_A], SPORTS_A, NOW)
    assert feed["title"] == "TEST Sports A" and len(feed["items"]) == 4
    it = feed["items"][0]
    assert it["title"].startswith("Jalen Brooks drops career-high 51")
    assert it["summary"].startswith("Jalen Brooks scored")  # HTML stripped
    assert it["published_at"] == "2026-11-19T15:00:00+00:00"


def test_parse_atom_and_youtube():
    ufc = parse_feed(F[UFC_ATOM], UFC_ATOM, NOW)["items"][0]
    assert ufc["url"] == "https://fightnews.test/ufc-330-rematch" and ufc["author"] == "Staff"
    yt = parse_feed(F[YOUTUBE], YOUTUBE, NOW)["items"][0]
    assert yt["extra"]["youtube_video_id"] == "abc123"
    assert yt["extra"]["views_reported_by_feed"] == 48210  # only because the feed itself reports it


def test_parse_errors():
    with pytest.raises(FeedError, match="web page"):
        parse_feed(b"<!DOCTYPE html><html><body>hi</body></html>")
    with pytest.raises(FeedError, match="Not valid XML"):
        parse_feed(b"<rss><channel><item>")
    with pytest.raises(FeedError, match="Empty"):
        parse_feed(b"   ")
    with pytest.raises(FeedError, match="Unrecognised"):
        parse_feed(b"<svg></svg>")


def test_canonical_url_strips_tracking():
    a = canonical_url("https://www.sporta.test/nba/brooks-51/?utm_source=partner#comments")
    assert a == canonical_url("https://sporta.test/nba/brooks-51") == "https://sporta.test/nba/brooks-51"
    assert canonical_url("http://example.test/x?b=2&a=1&fbclid=z") == "https://example.test/x?a=1&b=2"


def test_dates():
    assert parse_date("Wed, 19 Nov 2026 15:00:00 GMT") == "2026-11-19T15:00:00+00:00"
    assert parse_date("2026-11-19T10:00:00-05:00") == "2026-11-19T15:00:00+00:00"
    assert parse_date("not a date") is None
    assert parse_date("Sat, 01 Jan 2050 00:00:00 GMT", NOW) is None  # implausible future


def test_strip_html_and_names():
    assert strip_html("<p>Hi &amp; <b>bye</b></p><script>x()</script>") == "Hi & bye"
    assert named_phrases("Jalen Brooks drops career-high 51 as Lakers stun Celtics") == ["Jalen Brooks", "Lakers", "Celtics"]
    assert "Breaking" not in named_phrases("Breaking: Marcus Hale traded")


def test_story_relatedness():
    a = "Jalen Brooks drops career-high 51 as Lakers stun Celtics"
    b = "Jalen Brooks scores career-high 51 points in Lakers win over Celtics"
    c = "Warriors agree to trade Marcus Hale to Knicks"
    assert related(keyword_set(a), set(named_phrases(a)), keyword_set(b), set(named_phrases(b)))
    assert not related(keyword_set(a), set(named_phrases(a)), keyword_set(c), set(named_phrases(c)))


# ---- fetching rules ---------------------------------------------------------------

def test_fetcher_respects_robots():
    t = FakeTransport(overrides={"https://feeds.sporta.test/robots.txt":
                                 Response(200, {}, b"User-agent: *\nDisallow: /")})
    res = Fetcher(t, sleep=lambda s: None).fetch(SPORTS_A)
    assert res.status == "robots_disallowed"
    assert not any(u == SPORTS_A for u, _ in t.requests)  # the feed itself was never requested


def test_fetcher_robots_server_error_means_no():
    t = FakeTransport(overrides={"https://feeds.sporta.test/robots.txt": Response(503, {}, b"")})
    assert Fetcher(t, sleep=lambda s: None).fetch(SPORTS_A).status == "robots_disallowed"


def test_fetcher_status_mapping_and_headers():
    t = FakeTransport(overrides={
        "https://a.test/401": Response(401), "https://a.test/429": Response(429, {"retry-after": "120"}),
        "https://a.test/500": Response(500), "https://a.test/down": TransportError("blocked by proxy")})
    f = Fetcher(t, sleep=lambda s: None)
    assert f.fetch("https://a.test/401").status == "needs_credentials"
    r = f.fetch("https://a.test/429")
    assert r.status == "rate_limited" and r.retry_after == "120"
    assert f.fetch("https://a.test/500").status == "http_error"
    assert f.fetch("https://a.test/down").status == "network_error"
    assert f.fetch("ftp://a.test/x").status == "http_error"
    ua = t.requests[-1][1]["User-Agent"]
    assert "OnTheRoadAI-TrendScout" in ua and "Cookie" not in t.requests[-1][1]


def test_fetcher_conditional_get_and_host_spacing():
    t = FakeTransport()
    slept = []
    clock = iter([0.0, 0.0, 0.5, 0.5, 0.6, 0.6, 5.0, 5.0])
    f = Fetcher(t, sleep=slept.append, clock=lambda: next(clock), min_host_interval=2.0)
    first = f.fetch(SPORTS_A)
    assert first.status == "ok" and first.etag
    again = f.fetch(SPORTS_A, etag=first.etag)
    assert again.status == "not_modified"
    assert slept and all(s > 0 for s in slept)  # waited between requests to the same host


# ---- classification & verification -----------------------------------------------------

def test_sport_and_topic_detection():
    assert classify.detect_sport("Chiefs QB throws four touchdowns", []) == "nfl"
    assert classify.detect_sport("Rangers win in overtime", ["nhl"]) == "nhl"     # ambiguous name + feed hint
    assert classify.detect_sport("Rangers walk-off homer", ["mlb"]) == "mlb"
    assert classify.detect_sport("UFC 330 rematch booked", []) == "ufc"
    assert classify.detect_topic("Jalen Brooks drops career-high 51") == "unexpected_performance"
    assert classify.detect_topic("Warriors agree to trade Marcus Hale") == "trade_signing"
    assert classify.detect_topic("Is Brooks overrated? Fans debate") == "fan_debate"
    assert classify.detect_topic("Former guard arrested after incident") == "sensitive"


def test_gta_detection():
    assert classify.is_gta("GTA 6 trailer") and classify.is_gta("Grand Theft Auto VI news")
    assert not classify.is_gta("Nintendo announces handheld")
    assert classify.detect_gta_concept("Streamer's GTA 6 heist ends in fail") == "streamer_story"
    assert classify.detect_gta_concept("hidden easter egg found in GTA 6") == "gta_discovery"
    assert classify.detect_gta_concept("Rockstar confirms trailer release date") == "news_explainer"


def _it(rel, sid, title):
    return {"reliability": rel, "source_id": sid, "title": title, "summary": ""}


def test_verification_labels():
    v = classify.verify
    assert v([_it("official", 1, "Team announces signing")], "on-the-road-media")[0] == "confirmed_official"
    assert v([_it("major_publisher", 1, "X signs"), _it("major_publisher", 2, "X signs deal")],
             "on-the-road-media")[0] == "multiple_reliable"
    assert v([_it("major_publisher", 1, "X signs")], "on-the-road-media")[0] == "single_reliable_report"
    assert v([_it("major_publisher", 1, "Nets reportedly could pursue Cole")], "on-the-road-media")[0] == "reported_unconfirmed"
    assert v([_it("community", 1, "Theory: map is huge")], "gta6-network")[0] == "community_unverified"
    assert v([_it("publisher", 1, "GTA 6 leak suggests heists")], "gta6-network")[0] == "rumor_speculation"
    assert v([_it("publisher", 1, "Rockstar confirms trailer details")], "gta6-network")[0] == "credible_report"
    assert v([_it("user", 1, "something I saw")], "on-the-road-media")[0] == "user_submitted"


# ---- hooks ---------------------------------------------------------------------------

def test_sports_hooks_are_short_specific_and_safe():
    hooks = recommend.sports_hooks("unexpected_performance", "nba", ["Jalen Brooks"], ["Lakers"])
    assert len(hooks) == 3 and all("Jalen Brooks" in h for h in hooks)
    for topic in ("fan_debate", "rivalry", "trade_signing", "controversy", "press_conference", "game_day",
                  "athlete_story", "injury", "general"):
        for h in recommend.sports_hooks(topic, "nba", ["Jalen Brooks"], ["Lakers", "Celtics"]):
            assert len(h.split()) <= 12, h
            assert "!!" not in h and "you won't believe" not in h.lower() and "None" not in h
    assert recommend.sports_hooks("sensitive", "nba", ["Tom Reyes"], []) == []
    assert recommend.sports_hooks("general", "nba", [], [])[0] == "Who would you trust with the last shot?"


def test_gta_hooks_hedge_speculation():
    assert "rumor" in recommend.gta_hooks("gta_discovery", None, True)[0].lower()
    assert recommend.gta_hooks("streamer_story", "TestCreator", False)[0].startswith("TestCreator")


# ---- regressions found on live feeds (2026-11-19) -------------------------------------

def test_entities_from_real_world_headline_shapes():
    e = classify.entities(["Ravens CB Marlon Humphrey apologizes after comments"])
    assert e["people"] == ["Marlon Humphrey"] and "Ravens" in e["teams"]
    e = classify.entities(["Why Jaguars' Trevor Lawrence has legitimate MVP case"])
    assert e["people"] == ["Trevor Lawrence"] and "Jaguars" in e["teams"]
    e = classify.entities(["Prisco's NFL Week 11 Power Rankings: Patrick Mahomes returns to form"])
    assert "Power Rankings" not in e["people"] and "Patrick Mahomes" in e["people"]
    e = classify.entities(["Eric Karabell's notebook: Trade Walker Kessler? Klay Thompson worth rostering?"])
    assert e["people"][0] == "Walker Kessler" and "Eric Karabell" not in e["people"]
    e = classify.entities(["At MLB GM Meetings, the Red Sox weigh options"])
    assert "Red Sox" in e["teams"] and "Red Sox" not in e["people"]
    assert classify.entities(["DE Hunter sacks the quarterback twice"])["people"] == []


def test_team_hooks_read_naturally():
    hooks = recommend.sports_hooks("fan_debate", "nba", [], ["Heat"], "Updated MVP odds: Heat on the rise")
    assert hooks[0] == "Are the Heat overrated?"
    assert recommend.sports_hooks("trade_signing", "mlb", [], ["Phillies"])[0] == "Good move or panic move by the Phillies?"


def test_question_headline_becomes_hook_only_when_simple():
    assert recommend.sports_hooks("general", "nfl", [], [], "Who wins the NFC this year?")[0] == "Who wins the NFC this year?"
    hooks = recommend.sports_hooks("trade_signing", "nba", ["Walker Kessler"], [], "Notebook: Trade Kessler? Klay worth it?")
    assert not hooks[0].startswith("Notebook")


def test_gta_concept_uses_headline_and_never_overclaims_confirmation():
    assert classify.detect_gta_concept("KFC Announces GTA 6 Promo  you can get the best map", "KFC Announces GTA 6 Promo") == "news_explainer"
    hooks = recommend.gta_hooks("news_explainer", None, False, "KFC Announces GTA 6 Promo", confirmed=False)
    assert not any("Rockstar actually confirmed" in h for h in hooks)
    assert classify.detect_topic("Concerns for four NFL teams; Cowboys at a crossroads",
                                 "Concerns for four NFL teams; Cowboys at a crossroads") == "roundup"
