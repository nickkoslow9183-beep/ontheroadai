"""Trend Scout dashboard, using TEST FIXTURE feeds served by a fake transport (no internet)."""

import io
import re
from datetime import datetime, timezone

import pytest

from ontheroadai.db import connect
from trend_fixtures import FakeTransport, install_test_sources


@pytest.fixture
def tapp(app):
    conn = connect(app.config["DB_PATH"])
    install_test_sources(conn)
    conn.close()
    app.config["TREND_TRANSPORT"] = FakeTransport(now=datetime.now(timezone.utc))
    return app


def run(client):
    return client.post("/trends/run", data={"force": "1"}, follow_redirects=True).get_data(as_text=True)


def test_pages_render_before_any_run(client):
    for url in ["/trends", "/trends?view=gta", "/trends?view=saved", "/trends?view=new", "/trends/topics",
                "/trends/sources", "/trends/watchlist", "/trends/runs"]:
        r = client.get(url)
        assert r.status_code == 200, url
    html = client.get("/trends").get_data(as_text=True)
    assert "Run Trend Scout" in html and "nothing runs" in html and "GitHub" in html
    assert client.get("/trends/o/999").status_code == 404
    assert client.get("/trends/runs/999").status_code == 404
    src = client.get("/trends/sources").get_data(as_text=True)
    assert "ESPN: NBA" in src and "requires credentials" in src.replace("_", " ") and "X (Twitter)" in src


def test_run_button_report_and_opportunities(client, tapp):
    html = run(client)
    assert "Research run #1: success" in html and "Research report" in html
    assert "New &amp; updated opportunities" in html and "TEST Sports A" in html
    sports = client.get("/trends?view=sports").get_data(as_text=True)
    assert "Jalen Brooks drops career-high 51" in sports and "Create idea" in sports
    assert "Nintendo" not in client.get("/trends?view=gta").get_data(as_text=True)
    hist = client.get("/trends/runs").get_data(as_text=True)
    assert "#1" in hist and "success" in hist
    # filters
    ufc = client.get("/trends?view=sports&category=ufc").get_data(as_text=True)
    assert "Rivera vs. Okafor" in ufc and "Jalen Brooks" not in ufc
    unv = client.get("/trends?view=gta&verification=__unverified__").get_data(as_text=True)
    assert "leak suggests" in unv and "Rockstar confirms" not in unv


def test_opportunity_detail_and_one_click_idea(client, tapp):
    run(client)
    page = client.get("/trends?view=sports").get_data(as_text=True)
    sid = re.search(r'/trends/o/(\d+)">\s*<b>Jalen Brooks drops', page).group(1)
    detail = client.get(f"/trends/o/{sid}").get_data(as_text=True)
    for text in ("Why it matters", "Hook options", "Interview question", "B-roll ideas", "Urgency",
                 "Platforms", "Production effort", "Sources (2)", "Prepared for future agents", "your data" if False else "heuristic"):
        assert text in detail, text
    r = client.post(f"/trends/o/{sid}/idea", follow_redirects=True)
    assert "Idea created from this opportunity" in r.get_data(as_text=True)
    r = client.post(f"/trends/o/{sid}/idea", follow_redirects=True)
    assert "no duplicate was created" in r.get_data(as_text=True)
    conn = connect(tapp.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM ideas").fetchone()[0] == 1
    conn.close()


def test_acknowledge_save_and_hide(client, tapp):
    run(client)
    page = client.get("/trends?view=gta").get_data(as_text=True)
    sid = re.search(r'/trends/o/(\d+)">\s*<b>GTA 6 leak', page).group(1)
    detail = client.get(f"/trends/o/{sid}").get_data(as_text=True)
    assert "Unverified." in detail
    r = client.post(f"/trends/o/{sid}/ack", data={"who": "Nick"}, follow_redirects=True)
    assert "Acknowledged by" in r.get_data(as_text=True)
    client.post(f"/trends/o/{sid}/save", data={"value": "1"})
    assert "GTA 6 leak" in client.get("/trends?view=saved").get_data(as_text=True)
    client.post(f"/trends/o/{sid}/dismiss", data={"value": "1"})
    assert "GTA 6 leak" not in client.get("/trends?view=gta").get_data(as_text=True)


def test_sources_add_toggle_test(client, tapp):
    r = client.post("/trends/sources", data={"name": "Bad", "url": "not-a-url", "business": "on-the-road-media",
                                             "reliability": "publisher"}, follow_redirects=True)
    assert "full feed URL" in r.get_data(as_text=True)
    conn = connect(tapp.config["DB_PATH"])
    sid = conn.execute("SELECT id FROM research_sources WHERE name = 'TEST Sports A'").fetchone()[0]
    r = client.post(f"/trends/sources/{sid}", data={"action": "test"}, follow_redirects=True)
    assert "Works: ok, 4 item(s)" in r.get_data(as_text=True)
    client.post(f"/trends/sources/{sid}", data={"action": "save", "url": conn.execute(
        "SELECT url FROM research_sources WHERE id = ?", (sid,)).fetchone()[0], "interval": "90"})
    assert conn.execute("SELECT enabled, min_interval_minutes FROM research_sources WHERE id = ?", (sid,)).fetchone()[:] == (0, 90)
    bad = client.post("/trends/sources", data={"name": "Broken feed", "url": "https://nowhere.test/rss",
                                               "business": "gta6-network", "reliability": "publisher"})
    bid = conn.execute("SELECT id FROM research_sources WHERE name = 'Broken feed'").fetchone()[0]
    r = client.post(f"/trends/sources/{bid}", data={"action": "test"}, follow_redirects=True)
    assert "Not working: network error" in r.get_data(as_text=True)
    conn.close()


def test_watchlist_and_submissions(client, tapp):
    r = client.post("/trends/watchlist", data={"name": "TestCreator", "category": "gta_creator",
                                               "feed": "UCtestchannel00000000001"}, follow_redirects=True)
    assert "Added to the watchlist" in r.get_data(as_text=True)
    r = client.post("/trends/watchlist", data={"name": "Handle", "category": "gta_streamer", "feed": "@nope"},
                    follow_redirects=True)
    assert "channel ID" in r.get_data(as_text=True)
    run(client)
    wl = client.get("/trends/watchlist").get_data(as_text=True)
    assert "hidden easter egg" in wl and "views (YouTube feed)" in wl
    r = client.post("/trends/submit", data={"business": "on-the-road-media",
                                            "bulk": "Fans boo the refs in Philly | https://mine.test/1\nSecond thing I saw"},
                    follow_redirects=True)
    assert "Added 2 item(s)" in r.get_data(as_text=True)
    r = client.post("/trends/submit", data={"business": "gta6-network",
                                            "file": (io.BytesIO(b"GTA 6 photo mode tip | https://mine.test/2"), "notes.txt")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "Added 1 item(s)" in r.get_data(as_text=True)
    r = client.post("/trends/submit", data={"business": "gta6-network",
                                            "file": (io.BytesIO(b"x"), "evil.exe")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "must be .txt" in r.get_data(as_text=True)


def test_schedule_setting_and_topics(client, tapp):
    r = client.post("/trends/schedule", data={"hours": "6"}, follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "only happen while this app is open" in html and "scheduler not running in this process" in html
    run(client)
    topics = client.get("/trends/topics").get_data(as_text=True)
    assert "Jalen Brooks" in topics


def test_idea_page_shows_trend_scout_and_agents_page(client, tapp):
    run(client)
    agents = client.get("/agents").get_data(as_text=True)
    block = agents[agents.index("Trend Scout"):agents.index("Trend Scout") + 300]
    assert "a-implemented" in block
    for name in ("Scriptwriter", "Publishing Coordinator", "Growth Optimizer"):  # still planned after Milestone 4
        b = agents[agents.index(name):agents.index(name) + 200]
        assert "a-planned" in b, name
