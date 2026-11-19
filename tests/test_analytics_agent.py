"""The Analytics Agent working inside the Orchestrator."""

from conftest import make_ready_idea
from ontheroadai import approvals, publishing
from ontheroadai.agents.orchestrator import Orchestrator
from ontheroadai.analytics import csv_import, store


def publish(conn, url="https://www.tiktok.com/@otrm/video/7301000000000000001"):
    idea_id = make_ready_idea(conn)
    approvals.record_decision(conn, idea_id, "approved", "Nick")
    publishing.mark_published(conn, idea_id, "Nick", "tiktok", url)
    return idea_id


def analytics_task(conn, idea_id):
    return next(t for t in Orchestrator(conn).tasks(idea_id, "post_publish") if t["agent_key"] == "analytics_agent")


def test_publishing_creates_linked_video_and_agent_waits(conn):
    idea_id = publish(conn)
    v = conn.execute("SELECT * FROM videos WHERE idea_id = ?", (idea_id,)).fetchone()
    assert v["video_key"] == "id:7301000000000000001" and v["format"] == "street_interview"
    t = analytics_task(conn, idea_id)
    assert t["status"] == "pending" and "no performance numbers yet" in t["output"]


def test_csv_import_completes_the_analytics_task(conn):
    idea_id = publish(conn)
    acc = conn.execute("SELECT account_id FROM videos WHERE idea_id = ?", (idea_id,)).fetchone()[0]
    data = (b"Video ID,Video title,Duration,Total views,New followers,Average watch time\n"
            b"7301000000000000001,From export,0:20,8000,40,0:09\n")
    imp = csv_import.start_import(conn, acc, "t.csv", data, "2026-11-10")
    csv_import.commit_import(conn, imp, {"post_id": "Video ID", "title": "Video title", "duration_seconds": "Duration",
                                         "views": "Total views", "follows_gained": "New followers",
                                         "avg_watch_seconds": "Average watch time"}, {})
    t = analytics_task(conn, idea_id)
    assert t["status"] == "done" and t["completed_by"] == "agent:analytics_agent"
    assert "8,000 views" in t["output"] and "5.0 follows per 1,000 views" in t["output"]
    assert "watched 45% of its length" in t["output"]
    assert "Not enough other videos" in t["output"]
    # growth optimizer is still planned -> still a human task
    growth = next(x for x in Orchestrator(conn).tasks(idea_id, "post_publish") if x["agent_key"] == "growth_optimizer")
    assert growth["status"] == "manual_required"


def test_agent_reports_missing_metrics_and_updates_on_new_numbers(conn):
    idea_id = publish(conn)
    vid = conn.execute("SELECT id FROM videos WHERE idea_id = ?", (idea_id,)).fetchone()[0]
    store.add_snapshot(conn, vid, "2026-11-10", {"views": "1000"})
    conn.commit()
    store.after_data_change(conn, [vid])
    t = analytics_task(conn, idea_id)
    assert t["status"] == "done" and "Not available: avg watch time, video length, followers gained" in t["output"]
    store.add_snapshot(conn, vid, "2026-11-17", {"views": "2500", "follows_gained": "12"})
    conn.commit()
    store.after_data_change(conn, [vid])
    assert "2,500 views" in analytics_task(conn, idea_id)["output"]


def test_idea_without_link_waits_for_video(conn):
    idea_id = publish(conn, url="")
    # OTRM has one brand, so a video record is still created and linked
    assert conn.execute("SELECT COUNT(*) FROM videos WHERE idea_id = ?", (idea_id,)).fetchone()[0] == 1
    conn.execute("DELETE FROM videos WHERE idea_id = ?", (idea_id,))
    conn.commit()
    Orchestrator(conn).advance(idea_id)
    assert "no video is linked" in analytics_task(conn, idea_id)["output"]
