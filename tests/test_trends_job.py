"""Trend Scout research runs end to end on TEST FIXTURE feeds (not live data)."""

import json
from datetime import timedelta

import pytest

from conftest import make_ready_idea
from ontheroadai import approvals, ideas, publishing
from ontheroadai.agents.orchestrator import Orchestrator
from ontheroadai.analytics import store
from ontheroadai.db import connect, init_db
from ontheroadai.trends import opportunities as opp
from ontheroadai.trends import scheduler, sources, watchlist
from ontheroadai.trends.fetcher import Response, TransportError
from ontheroadai.trends.job import RunInProgress, add_manual_item, rescore_recent, run_trend_scout
from trend_fixtures import COMMUNITY, GAMING, NOW, SPORTS_A, SPORTS_B, UFC_ATOM, YOUTUBE, FakeTransport, \
    install_test_sources


@pytest.fixture
def ran(conn):
    install_test_sources(conn)
    t = FakeTransport()
    run_id = run_trend_scout(conn, "test", transport=t, now=NOW)
    return conn, run_id, t


def stories(conn, business=None):
    return {s["title"]: s for s in opp.list_opportunities(conn, {"business": business} if business else {}, now=NOW)}


def test_defaults_seeded_and_honest(conn):
    rows = sources.list_sources(conn)
    names = {r["name"] for r in rows}
    assert "ESPN: NBA" in names and "MMA Fighting (UFC/MMA)" in names and "Reddit r/GTA6 (community)" in names
    reddit = next(r for r in rows if r["key"] == "reddit-gta6")
    assert reddit["enabled"] == 0 and "robots" in reddit["access_note"]
    assert all(r["health"] in ("never", "disabled", "manual") for r in rows)  # unverified until fetched


def test_run_counts_dedupes_and_groups(ran):
    conn, run_id, _ = ran
    run = conn.execute("SELECT * FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "success" and run["sources_ok"] == 5 and run["sources_failed"] == 0
    assert run["items_duplicate"] == 1  # same article with tracking params from another source
    s = stories(conn, "on-the-road-media")
    brooks = s["Jalen Brooks drops career-high 51 as Lakers stun Celtics"]
    assert brooks["source_count"] == 2 and brooks["item_count"] == 2  # two outlets grouped into one story
    assert brooks["verification"] == "multiple_reliable" and brooks["category"] == "nba"
    assert brooks["topic_type"] == "unexpected_performance"
    trade = s["Warriors agree to trade Marcus Hale to Knicks"]
    assert trade["source_count"] == 2 and trade["topic_type"] == "trade_signing"
    assert s["UFC 330: Rivera vs. Okafor rematch set after controversial decision"]["category"] == "ufc"
    assert s["Report: Nets reportedly could pursue Darius Cole"]["verification"] == "reported_unconfirmed"
    # non-GTA gaming news is stored but not turned into an opportunity
    gta = stories(conn, "gta6-network")
    assert not any("Nintendo" in t for t in gta)
    assert conn.execute("SELECT relevant FROM research_items WHERE title LIKE 'Nintendo%'").fetchone()[0] == 0
    assert gta["GTA 6 leak suggests online mode might include heists"]["verification"] == "rumor_speculation"
    assert gta["Theory: the GTA 6 map is bigger than we think"]["verification"] == "community_unverified"


def test_scores_have_explanations_and_heuristic_label(ran):
    conn, _, _ = ran
    for s in opp.list_opportunities(conn, now=NOW):
        sc = s["score_detail"]
        assert 0 <= s["score"] <= 100 and len(sc["factors"]) == 11
        assert round(sum(f["points"] for f in sc["factors"])) == s["score"]
        assert all(f["explanation"] for f in sc["factors"])
        assert sc["mode"] == "heuristic"  # no analytics stored yet
        assert "not a prediction" in sc["disclaimer"]


def test_freshness_drops_with_age(ran):
    conn, _, _ = ran
    s = stories(conn, "on-the-road-media")
    fresh = next(f for f in s["Jalen Brooks drops career-high 51 as Lakers stun Celtics"]["score_detail"]["factors"]
                 if f["key"] == "freshness")
    older = next(f for f in s["Is Jalen Brooks overrated? Fans debate after big night"]["score_detail"]["factors"]
                 if f["key"] == "freshness")
    assert fresh["input"] > older["input"]
    rescore_recent(conn, NOW + timedelta(days=5))
    later = opp.get(conn, s["Jalen Brooks drops career-high 51 as Lakers stun Celtics"]["id"])
    assert next(f for f in later["score_detail"]["factors"] if f["key"] == "freshness")["input"] < fresh["input"]


def test_recommendations_content(ran):
    conn, _, _ = ran
    s = stories(conn, "on-the-road-media")
    rec = s["Jalen Brooks drops career-high 51 as Lakers stun Celtics"]["rec"]
    assert rec["format"] == "fan_reaction" and len(rec["hooks"]) == 3
    assert any("Jalen Brooks" in h for h in rec["hooks"])
    assert rec["interview_question"] and rec["sources"] and rec["broll"] and rec["urgency"].startswith("high")
    assert "no AI model" in rec["generator_note"]
    sensitive = s["Former guard Tom Reyes arrested after incident"]
    assert sensitive["topic_type"] == "sensitive" and sensitive["rec"]["hooks"] == []
    gta = stories(conn, "gta6-network")
    streamer = gta["Streamer's GTA 6 bank heist ends in hilarious fail"]["rec"]
    assert streamer["footage_permission"] == "not_cleared" and streamer["transformation"]
    assert streamer["account_slug"] in ("gta-streamer-stories", "gta6-moments")
    assert gta["GTA 6 leak suggests online mode might include heists"]["rec"]["hooks"][0].startswith("Is this GTA 6 rumor")


def test_second_run_new_vs_updated_and_conditional_get(ran):
    conn, run1, t = ran
    t.pages[SPORTS_B] = Response(200, {"etag": '"v2"'}, t.pages[SPORTS_B].body.replace(
        b"</channel>", b"<item><title>Lakers star Jalen Brooks says the 51 was for his late coach</title>"
                       b"<link>https://sportb.test/nba/brooks-coach</link><pubDate>Wed, 19 Nov 2026 17:00:00 GMT"
                       b"</pubDate></item><item><title>Bruins top Rangers in shootout</title>"
                       b"<link>https://sportb.test/nhl/bruins</link><pubDate>Wed, 19 Nov 2026 17:30:00 GMT</pubDate>"
                       b"</item></channel>"))
    run2 = run_trend_scout(conn, "test", transport=t, now=NOW + timedelta(hours=1), force=True)
    r = conn.execute("SELECT * FROM research_runs WHERE id = ?", (run2,)).fetchone()
    assert r["items_new"] == 2 and r["stories_updated"] == 1 and r["stories_new"] == 1
    brooks = conn.execute("SELECT * FROM research_stories WHERE title LIKE 'Jalen Brooks drops%'").fetchone()
    assert brooks["status"] == "updated" and brooks["item_count"] == 3
    other = conn.execute("SELECT status FROM research_stories WHERE title LIKE 'Warriors agree%'").fetchone()
    assert other["status"] == "seen"
    # unchanged feeds were requested with If-None-Match and answered 304
    statuses = {row[0] for row in conn.execute("SELECT status FROM research_run_sources WHERE run_id = ?", (run2,))}
    assert "not_modified" in statuses


def test_interval_skips_sources_not_due(ran):
    conn, _, t = ran
    run2 = run_trend_scout(conn, "test", transport=t, now=NOW + timedelta(minutes=10))
    r = conn.execute("SELECT * FROM research_runs WHERE id = ?", (run2,)).fetchone()
    assert r["sources_attempted"] == 0 and r["sources_skipped"] == 5 and r["status"] == "success"
    assert "No source was due" in json.loads(r["report_json"])["note"]


def test_failures_are_recorded_not_fatal(conn):
    ids = install_test_sources(conn)
    t = FakeTransport(overrides={
        SPORTS_B: Response(500), GAMING: Response(200, {}, b"<html><body>not a feed</body></html>"),
        UFC_ATOM: TransportError("blocked"), "https://community.test/robots.txt": Response(200, {}, b"User-agent: *\nDisallow: /")})
    run_id = run_trend_scout(conn, "test", transport=t, now=NOW)
    run = conn.execute("SELECT * FROM research_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["status"] == "partial" and run["sources_ok"] == 1 and run["sources_failed"] == 4
    st = {r["source_id"]: r for r in conn.execute("SELECT * FROM research_run_sources WHERE run_id = ?", (run_id,))}
    assert st[ids[SPORTS_B]]["status"] == "http_error" and st[ids[SPORTS_B]]["http_status"] == 500
    assert st[ids[GAMING]]["status"] == "parse_error" and "web page" in st[ids[GAMING]]["error"]
    assert st[ids[UFC_ATOM]]["status"] == "network_error"
    assert st[ids[COMMUNITY]]["status"] == "robots_disallowed"
    src = conn.execute("SELECT * FROM research_sources WHERE id = ?", (ids[SPORTS_B],)).fetchone()
    assert src["consecutive_failures"] == 1 and src["last_success_at"] is None
    assert sources.health(dict(src)) == "error"


def test_all_sources_failing_marks_run_failed(conn):
    install_test_sources(conn, [SPORTS_A])
    t = FakeTransport(overrides={SPORTS_A: TransportError("offline")})
    run_id = run_trend_scout(conn, "test", transport=t, now=NOW)
    assert conn.execute("SELECT status FROM research_runs WHERE id = ?", (run_id,)).fetchone()[0] == "failed"


def test_run_lock(conn):
    conn.execute("INSERT INTO research_runs (trigger, status, started_at) VALUES ('test', 'running', ?)",
                 (NOW.isoformat(),))
    conn.commit()
    with pytest.raises(RunInProgress):
        run_trend_scout(conn, "test", transport=FakeTransport(), now=NOW + timedelta(minutes=5))
    run_trend_scout(conn, "test", transport=FakeTransport(), now=NOW + timedelta(hours=2))  # stale lock released
    assert conn.execute("SELECT status FROM research_runs WHERE id = 1").fetchone()[0] == "failed"


def test_manual_submission(conn):
    add_manual_item(conn, "on-the-road-media", "Fans chanting MVP for Jalen Brooks at the arena",
                    "https://myclip.test/1", "saw it live", now=NOW)
    s = opp.list_opportunities(conn, now=NOW)[0]
    assert s["verification"] == "user_submitted" and s["unverified"]
    with pytest.raises(ValueError, match="already"):
        add_manual_item(conn, "on-the-road-media", "dup", "https://myclip.test/1", now=NOW)
    with pytest.raises(ValueError):
        add_manual_item(conn, "nope", "x")


def test_analytics_inform_scoring(ran):
    conn, _, _ = ran
    acc = conn.execute("SELECT a.id FROM accounts a JOIN brands b ON b.id = a.brand_id "
                       "WHERE b.slug = 'on-the-road-media' AND a.platform = 'tiktok'").fetchone()[0]
    for i in range(3):  # fan reactions convert well...
        store.add_manual_entry(conn, acc, {"title": f"FR {i}", "format": "fan_reaction"}, "2026-11-10",
                               {"views": "10000", "follows_gained": "120"})
    for i in range(3):  # ...street interviews less so
        store.add_manual_entry(conn, acc, {"title": f"SI {i}", "format": "street_interview"}, "2026-11-10",
                               {"views": "10000", "follows_gained": "20"})
    rescore_recent(conn, NOW)
    s = stories(conn, "on-the-road-media")
    brooks = s["Jalen Brooks drops career-high 51 as Lakers stun Celtics"]
    sc = brooks["score_detail"]
    conv = next(f for f in sc["factors"] if f["key"] == "follower_conversion")
    assert sc["mode"] == "heuristic + your analytics" and "follower_conversion" in sc["analytics_factors"]
    assert conv["input"] >= 7 and "3 videos" in conv["explanation"]
    assert "Your data" in brooks["rec"]["platform_note"]
    trade = s["Warriors agree to trade Marcus Hale to Knicks"]  # street interview format
    tconv = next(f for f in trade["score_detail"]["factors"] if f["key"] == "follower_conversion")
    assert tconv["input"] < conv["input"]


def test_convert_to_idea_without_duplicates(ran):
    conn, _, _ = ran
    s = stories(conn, "on-the-road-media")["Jalen Brooks drops career-high 51 as Lakers stun Celtics"]
    idea_id, created = opp.convert_to_idea(conn, s["id"])
    assert created
    idea = ideas.get_idea(conn, idea_id)
    assert idea["content_type"] == "fan_reaction" and idea["hook"] in s["rec"]["hooks"]
    assert "sporta.test" in idea["description"] and idea["brand_name"] == "On The Road Media"
    assert opp.convert_to_idea(conn, s["id"]) == (idea_id, False)  # second click: same idea
    assert conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 1
    # an existing idea with the same title gets linked instead of duplicated
    other = stories(conn, "on-the-road-media")["Warriors agree to trade Marcus Hale to Knicks"]
    existing = ideas.create_idea(conn, "on-the-road-media", "Warriors agree to trade Marcus Hale to the Knicks",
                                 "street_interview")
    assert opp.convert_to_idea(conn, other["id"]) == (existing, False)
    sensitive = stories(conn, "on-the-road-media")["Former guard Tom Reyes arrested after incident"]
    with pytest.raises(ValueError, match="sensitive"):
        opp.convert_to_idea(conn, sensitive["id"])


def test_gta_conversion_never_clears_footage_and_qc_blocks(ran):
    conn, _, _ = ran
    s = stories(conn, "gta6-network")["Theory: the GTA 6 map is bigger than we think"]
    idea_id, _ = opp.convert_to_idea(conn, s["id"])
    idea = ideas.get_idea(conn, idea_id)
    orch = Orchestrator(conn)
    orch.plan(idea_id)
    while (m := [t for t in orch.tasks(idea_id) if t["status"] == "manual_required"]):
        orch.complete_manual_task(m[0]["id"], "Tester")
    ts = next(t for t in orch.tasks(idea_id) if t["agent_key"] == "trend_scout")
    assert ts["completed_by"] == "agent:trend_scout" and "Research brief" in ts["output"]
    qc = next(t for t in orch.tasks(idea_id) if t["agent_key"] == "quality_control")
    assert qc["status"] == "blocked" and "[FAIL] Research claims verified" in qc["output"]
    if idea["needs_external_footage"]:
        perm = conn.execute("SELECT p.status FROM source_permissions p JOIN idea_sources l ON l.source_id = p.id "
                            "WHERE l.idea_id = ?", (idea_id,)).fetchone()
        assert perm["status"] == "unknown"
    # acknowledging the claim lets that check pass (rights still apply separately)
    opp.acknowledge_claims(conn, s["id"], "Nick")
    orch.advance(idea_id)
    qc = next(t for t in orch.tasks(idea_id) if t["agent_key"] == "quality_control")
    assert "[PASS] Research claims verified" in qc["output"]


def test_streamer_story_gets_unknown_rights_record(ran):
    conn, _, _ = ran
    add_manual_item(conn, "gta6-network", "Streamer TestCreator's GTA 6 heist reaction goes wrong",
                    "https://twitch.test/clip/1", now=NOW)
    s = next(x for x in opp.list_opportunities(conn, {"business": "gta6-network"}, now=NOW)
             if "TestCreator" in x["title"])
    idea_id, _ = opp.convert_to_idea(conn, s["id"])
    idea = ideas.get_idea(conn, idea_id)
    assert idea["needs_external_footage"] == 1
    perm = conn.execute("SELECT p.* FROM source_permissions p JOIN idea_sources l ON l.source_id = p.id "
                        "WHERE l.idea_id = ?", (idea_id,)).fetchone()
    assert perm["status"] == "unknown" and "NOT assumed" in perm["notes"]


def test_trend_scout_agent_matches_research_or_hands_to_human(ran):
    conn, _, _ = ran
    conn.execute("UPDATE research_stories SET last_seen_at = datetime('now')")
    conn.commit()
    related = ideas.create_idea(conn, "on-the-road-media", "Jalen Brooks career-high reaction", "fan_reaction")
    Orchestrator(conn).plan(related)
    t = next(x for x in Orchestrator(conn).tasks(related) if x["agent_key"] == "trend_scout")
    assert t["status"] == "done" and "Matched by headline similarity" in t["output"]
    unrelated = ideas.create_idea(conn, "on-the-road-media", "Best tailgate food in Ohio", "street_interview")
    Orchestrator(conn).plan(unrelated)
    t = next(x for x in Orchestrator(conn).tasks(unrelated) if x["agent_key"] == "trend_scout")
    assert t["status"] == "manual_required" and "No related research" in t["output"]


def test_published_research_idea_still_publishes_normally(ran):
    conn, _, _ = ran
    s = stories(conn, "on-the-road-media")["Jalen Brooks drops career-high 51 as Lakers stun Celtics"]
    idea_id, _ = opp.convert_to_idea(conn, s["id"])
    orch = Orchestrator(conn)
    orch.plan(idea_id)
    while (m := [t for t in orch.tasks(idea_id) if t["status"] == "manual_required"]):
        orch.complete_manual_task(m[0]["id"], "Tester")
    assert ideas.get_idea(conn, idea_id)["status"] == "ready_for_review"  # verified story: QC passes
    approvals.record_decision(conn, idea_id, "approved", "Nick")
    publishing.mark_published(conn, idea_id, "Nick", ideas.get_idea(conn, idea_id)["target_platforms_list"][0])
    assert ideas.get_idea(conn, idea_id)["status"] == "published"


def test_filters(ran):
    conn, _, _ = ran
    assert all(s["category"] == "ufc" for s in opp.list_opportunities(conn, {"category": "ufc"}, now=NOW))
    unverified = opp.list_opportunities(conn, {"verification": "__unverified__"}, now=NOW)
    assert unverified and all(s["unverified"] for s in unverified)
    fresh = opp.list_opportunities(conn, {"freshness": "6h"}, now=NOW)
    assert fresh and all("debate" not in s["title"].lower() for s in fresh)
    assert all(s["score"] >= 60 for s in opp.list_opportunities(conn, {"min_score": "60"}, now=NOW))
    official_only = opp.list_opportunities(conn, {"min_reliability": "major_publisher"}, now=NOW)
    assert official_only and all(s["business"] == "on-the-road-media" for s in official_only)
    sid = official_only[0]["id"]
    opp.set_flag(conn, sid, dismissed=True)
    assert sid not in [s["id"] for s in opp.list_opportunities(conn, now=NOW)]
    opp.set_flag(conn, sid, saved=True)
    assert [s["id"] for s in opp.list_opportunities(conn, {"saved": True, "include_dismissed": True}, now=NOW)] == [sid]


def test_trending_topics(ran):
    conn, _, _ = ran
    topics = opp.trending_topics(conn, "on-the-road-media", 48, now=NOW)
    jb = next(t for t in topics if t["term"] == "Jalen Brooks")
    assert jb["mentions"] >= 3 and jb["sources"] == 2


def test_watchlist_youtube_feed_and_patterns(conn):
    cid = watchlist.add_creator(conn, "TestCreator", "gta_creator", "https://www.youtube.com/@testcreator",
                                "UCtestchannel00000000001")
    c = conn.execute("SELECT * FROM creators WHERE id = ?", (cid,)).fetchone()
    src = conn.execute("SELECT * FROM research_sources WHERE id = ?", (c["source_id"],)).fetchone()
    assert src["url"] == YOUTUBE and src["kind"] == "youtube_channel" and src["reliability"] == "creator"
    conn.execute("UPDATE research_sources SET enabled = 0 WHERE id <> ? AND kind <> 'manual'", (src["id"],))
    conn.commit()
    run_trend_scout(conn, "test", transport=FakeTransport(), now=NOW)
    row = next(r for r in watchlist.list_creators(conn, NOW) if r["id"] == cid)
    assert row["patterns"]["items"] == 1 and row["patterns"]["latest"][0]["views"] == 48210
    with pytest.raises(ValueError, match="channel ID"):
        watchlist.add_creator(conn, "Handle only", "gta_streamer", feed="@somebody")
    tw = watchlist.add_creator(conn, "Twitch Person", "gta_streamer", "https://www.twitch.tv/person")
    tw_row = next(r for r in watchlist.list_creators(conn, NOW) if r["id"] == tw)
    assert tw_row["source_id"] is None and "Twitch" in tw_row["tracking"]
    rid = watchlist.link_rights_record(conn, tw)
    assert conn.execute("SELECT status FROM source_permissions WHERE id = ?", (rid,)).fetchone()[0] == "unknown"


def test_scheduler_due_logic(conn):
    assert scheduler.next_due(conn) is None and not scheduler.is_due(conn, NOW)
    scheduler.set_interval(conn, 6)
    assert scheduler.is_due(conn, NOW)  # never run -> due
    conn.execute("INSERT INTO research_runs (trigger, status, started_at) VALUES ('manual', 'success', ?)",
                 (NOW.isoformat(),))
    conn.commit()
    assert not scheduler.is_due(conn, NOW + timedelta(hours=5))
    assert scheduler.is_due(conn, NOW + timedelta(hours=6))
    with pytest.raises(ValueError):
        scheduler.set_interval(conn, 5)


def test_scheduler_tick_runs_through_orchestrator(tmp_path):
    db = str(tmp_path / "s.db")
    c = connect(db)
    init_db(c)
    install_test_sources(c, [SPORTS_A])
    scheduler.set_interval(c, 1)
    c.close()
    sch = scheduler.Scheduler(db, transport=FakeTransport())
    run_id = sch.tick()
    c = connect(db)
    assert c.execute("SELECT trigger FROM research_runs WHERE id = ?", (run_id,)).fetchone()[0] == "scheduled"
    assert sch.tick() is None  # not due again yet
    c.close()


def test_upgrade_from_milestone_2_database(tmp_path):
    """A Milestone 2 database (user_version 2, no research tables) upgrades in place."""
    import sqlite3

    db = tmp_path / "m2.db"
    c = connect(db)
    init_db(c)
    for t in ("research_run_sources", "research_items", "research_stories", "research_runs", "research_sources",
              "creators", "app_settings"):
        c.execute(f"DROP TABLE {t}")
    c.execute("PRAGMA user_version = 2")
    iid = ideas.create_idea(c, "on-the-road-media", "Kept idea", "fan_reaction")
    c.commit()
    c.close()
    c = connect(db)
    init_db(c)
    assert c.execute("PRAGMA user_version").fetchone()[0] >= 3  # 4 since Milestone 4
    assert ideas.get_idea(c, iid)["title"] == "Kept idea"
    assert c.execute("SELECT COUNT(*) FROM research_sources WHERE is_builtin = 1").fetchone()[0] >= 16
    c.close()
    assert isinstance(sqlite3.sqlite_version, str)
