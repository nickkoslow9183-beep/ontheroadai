"""End-to-end dashboard tests through Flask's test client."""

import re

from ontheroadai.db import connect

SCORE_FIELDS = {"trend_momentum": "8", "audience_fit": "7", "hook_strength": "9",
                "conversion_potential": "6", "timeliness": "5", "production_ease": "7"}


def _idea_id(resp):
    m = re.search(r"/ideas/(\d+)$", resp.headers["Location"])
    assert m, resp.headers["Location"]
    return int(m.group(1))


def test_pages_render(client):
    for url in ["/", "/business/on-the-road-media", "/business/gta6-network", "/rights", "/analytics", "/agents",
                "/health"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert client.get("/business/nope").status_code == 404
    assert client.get("/ideas/999").status_code == 404


def test_sections_are_separate(client):
    otrm = client.get("/business/on-the-road-media").get_data(as_text=True)
    gta = client.get("/business/gta6-network").get_data(as_text=True)
    assert "Street interview" in otrm and "GTA 6 moment" not in otrm
    assert "GTA Streamer Stories" in gta and "Street interview" not in gta
    assert "Facebook" in otrm and 'value="facebook"' not in gta


def test_agents_page_shows_status(client):
    html = client.get("/agents").get_data(as_text=True)
    assert html.count("a-planned") >= 5  # 5 agents still planned after Milestone 3
    assert "a-implemented" in html and "a-partial" in html


def test_full_workflow_via_dashboard(client, app):
    # 1. submit idea
    r = client.post("/business/on-the-road-media/ideas", data={
        "title": "Fans predict the final score", "content_type": "street_interview",
        "hook": "Every fan said the same wrong score", "target_platforms": ["tiktok", "instagram"], **SCORE_FIELDS})
    assert r.status_code == 302
    idea_id = _idea_id(r)
    page = client.get(f"/ideas/{idea_id}").get_data(as_text=True)
    assert "Opportunity score" in page and "Rights clarity" in page
    assert "Publishing is locked" in page

    # 2. orchestrate
    client.post(f"/ideas/{idea_id}/orchestrate")
    conn = connect(app.config["DB_PATH"])
    # 3. complete manual steps through the UI
    for _ in range(10):
        row = conn.execute("SELECT id FROM agent_tasks WHERE idea_id=? AND status='manual_required'",
                           (idea_id,)).fetchone()
        if row is None:
            break
        assert client.post(f"/tasks/{row['id']}/complete", data={"completed_by": "Nick"}).status_code == 302
    assert conn.execute("SELECT status FROM ideas WHERE id=?", (idea_id,)).fetchone()[0] == "ready_for_review"

    # 4. publishing still refused before approval
    client.post(f"/ideas/{idea_id}/publish", data={"confirmed_by": "Nick", "platform": "tiktok",
                                                   "confirm_manual_post": "yes"})
    assert conn.execute("SELECT status FROM ideas WHERE id=?", (idea_id,)).fetchone()[0] == "ready_for_review"

    # 5. approve, then publish (with manual-post confirmation)
    client.post(f"/ideas/{idea_id}/review", data={"decision": "approved", "reviewer": "Nick"})
    client.post(f"/ideas/{idea_id}/publish", data={"confirmed_by": "Nick", "platform": "tiktok"})
    assert conn.execute("SELECT status FROM ideas WHERE id=?", (idea_id,)).fetchone()[0] == "approved"
    client.post(f"/ideas/{idea_id}/publish", data={"confirmed_by": "Nick", "platform": "tiktok",
                                                   "confirm_manual_post": "yes"})
    assert conn.execute("SELECT status FROM ideas WHERE id=?", (idea_id,)).fetchone()[0] == "published"
    conn.close()


def test_invalid_idea_shows_error(client):
    r = client.post("/business/gta6-network/ideas", data={"title": "", "content_type": "gta_moment"},
                    follow_redirects=True)
    assert "Title is required" in r.get_data(as_text=True)


def test_rights_and_analytics_forms(client, app):
    r = client.post("/rights", data={"business_slug": "gta6-network", "source_name": "Clip A",
                                     "source_type": "creator_permission", "status": "granted"},
                    follow_redirects=True)
    assert "evidence" in r.get_data(as_text=True).lower()
    r = client.post("/rights", data={"business_slug": "gta6-network", "source_name": "Clip A",
                                     "source_type": "creator_permission", "status": "granted",
                                     "evidence": "Email 2026-11-10"}, follow_redirects=True)
    assert "Clip A" in r.get_data(as_text=True)

    conn = connect(app.config["DB_PATH"])
    acc = conn.execute("SELECT id FROM accounts LIMIT 1").fetchone()[0]
    conn.close()
    r = client.post("/analytics", data={"account_id": acc, "views": "5000", "follows_gained": "25"},
                    follow_redirects=True)
    assert "Metrics recorded" in r.get_data(as_text=True)
    assert "5,000" in client.get("/business/on-the-road-media").get_data(as_text=True)


def test_html_is_escaped(client):
    r = client.post("/business/gta6-network/ideas", data={"title": "<script>alert(1)</script>",
                                                           "content_type": "gta_moment"})
    page = client.get(f"/ideas/{_idea_id(r)}").get_data(as_text=True)
    assert "<script>alert(1)</script>" not in page
