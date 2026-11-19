import pytest

from conftest import make_ready_idea
from ontheroadai import approvals, ideas, publishing, rights
from ontheroadai.agents import SPECS, implementation_for
from ontheroadai.agents.orchestrator import Orchestrator


def test_agent_registry_is_honest():
    assert len(SPECS) == 9
    statuses = {k: s.status for k, s in SPECS.items()}
    assert statuses["orchestrator"] == "implemented"
    assert statuses["quality_control"] == "partial"
    # Milestone 2: the Analytics Agent is implemented (and tested in test_analytics_agent.py).
    assert statuses["analytics_agent"] == "implemented"
    assert implementation_for("analytics_agent") is not None
    # Milestone 3: Trend Scout is implemented (tested in test_trends_*.py).
    assert statuses["trend_scout"] == "implemented"
    planned = {"scriptwriter", "clip_finder", "video_editor", "publishing_coordinator", "growth_optimizer"}
    assert {k for k, v in statuses.items() if v == "planned"} == planned
    for key in planned:
        assert implementation_for(key) is None, f"{key} is marked planned but has an implementation"
    assert implementation_for("quality_control") is not None


def test_plan_creates_pipeline_and_routes_planned_agents_to_humans(conn):
    idea_id = ideas.create_idea(conn, "on-the-road-media", "Game-day vlog", "game_day", hook="Inside the loudest stadium")
    orch = Orchestrator(conn)
    tasks = orch.plan(idea_id)
    assert [t["agent_key"] for t in tasks] == ["trend_scout", "scriptwriter", "clip_finder", "video_editor",
                                               "quality_control", "publishing_coordinator"]
    by_agent = {t["agent_key"]: t for t in tasks}
    assert by_agent["clip_finder"]["status"] == "skipped"  # own footage only
    # Milestone 3: Trend Scout runs, finds no related research, and hands the step to a person.
    assert by_agent["trend_scout"]["status"] == "manual_required"
    assert "No related research found" in by_agent["trend_scout"]["output"]
    assert by_agent["scriptwriter"]["status"] == "pending"
    assert ideas.get_idea(conn, idea_id)["status"] == "in_production"
    # idempotent
    orch.plan(idea_id)
    assert len(orch.tasks(idea_id)) == 6


def test_manual_completion_requires_name(conn):
    idea_id = ideas.create_idea(conn, "on-the-road-media", "x", "game_day")
    orch = Orchestrator(conn)
    task = orch.plan(idea_id)[0]
    with pytest.raises(ValueError):
        orch.complete_manual_task(task["id"], "  ")


def test_full_flow_to_ready_for_review(conn):
    idea_id = make_ready_idea(conn)
    tasks = Orchestrator(conn).tasks(idea_id)
    qc = next(t for t in tasks if t["agent_key"] == "quality_control")
    assert qc["status"] == "done" and qc["completed_by"] == "agent:quality_control"
    assert "[PASS] Footage rights cleared" in qc["output"]
    assert ideas.get_idea(conn, idea_id)["status"] == "ready_for_review"


def test_qc_blocks_uncleared_footage_then_passes_after_grant(conn):
    idea_id = make_ready_idea(conn, needs_footage=True, business="gta6-network", content_type="streamer_story")
    orch = Orchestrator(conn)
    qc = next(t for t in orch.tasks(idea_id) if t["agent_key"] == "quality_control")
    assert qc["status"] == "blocked"
    assert "[FAIL] Footage rights cleared" in qc["output"]
    assert ideas.get_idea(conn, idea_id)["status"] == "in_production"

    src = rights.create_source(conn, "gta6-network", "StreamerY clip", "creator_permission", "granted",
                               evidence="Signed release form 2026-11-12")
    rights.link_source(conn, idea_id, src)
    orch.advance(idea_id)
    qc = next(t for t in orch.tasks(idea_id) if t["agent_key"] == "quality_control")
    assert qc["status"] == "done"
    pub = next(t for t in orch.tasks(idea_id) if t["agent_key"] == "publishing_coordinator")
    orch.complete_manual_task(pub["id"], "Tester")
    assert ideas.get_idea(conn, idea_id)["status"] == "ready_for_review"


def test_cannot_publish_without_approval(conn):
    idea_id = make_ready_idea(conn)
    ok, reasons = publishing.readiness(conn, idea_id)
    assert not ok and any("approval" in r for r in reasons)
    with pytest.raises(PermissionError):
        publishing.mark_published(conn, idea_id, "Nick", "tiktok")
    assert ideas.get_idea(conn, idea_id)["status"] == "ready_for_review"


def test_review_required_before_production_complete(conn):
    idea_id = ideas.create_idea(conn, "on-the-road-media", "x", "game_day")
    with pytest.raises(ValueError, match="ready_for_review"):
        approvals.record_decision(conn, idea_id, "approved", "Nick")


def test_approve_then_publish_creates_post_publish_tasks(conn):
    idea_id = make_ready_idea(conn)
    with pytest.raises(ValueError, match="Reviewer"):
        approvals.record_decision(conn, idea_id, "approved", "")
    approvals.record_decision(conn, idea_id, "approved", "Nick", "Great hook")
    assert publishing.readiness(conn, idea_id) == (True, [])
    with pytest.raises(ValueError, match="target platforms"):
        publishing.mark_published(conn, idea_id, "Nick", "facebook")
    publishing.mark_published(conn, idea_id, "Nick", "tiktok", "https://www.tiktok.com/@example/video/1")
    idea = ideas.get_idea(conn, idea_id)
    assert idea["status"] == "published" and idea["published_by"] == "Nick"
    post = Orchestrator(conn).tasks(idea_id, phase="post_publish")
    # Milestone 2: the Analytics Agent runs itself and waits (pending) until numbers are imported.
    assert {t["agent_key"]: t["status"] for t in post} == {"analytics_agent": "pending",
                                                           "growth_optimizer": "manual_required"}
    assert "Waiting for data" in next(t for t in post if t["agent_key"] == "analytics_agent")["output"]
    with pytest.raises(ValueError):
        ideas.update_idea(conn, idea_id, title="changed after publishing")


def test_rights_revoked_after_approval_blocks_publish(conn):
    idea_id = ideas.create_idea(conn, "gta6-network", "Heist fail", "gta_moment", hook="This heist went so wrong",
                                target_platforms=["tiktok"], needs_external_footage=True)
    src = rights.create_source(conn, "gta6-network", "Licensed pack", "licensed", "granted", evidence="Invoice #42")
    rights.link_source(conn, idea_id, src)
    orch = Orchestrator(conn)
    orch.plan(idea_id)
    while (m := [t for t in orch.tasks(idea_id) if t["status"] == "manual_required"]):
        orch.complete_manual_task(m[0]["id"], "Tester")
    approvals.record_decision(conn, idea_id, "approved", "Nick")
    rights.update_source_status(conn, src, "expired")
    ok, reasons = publishing.readiness(conn, idea_id)
    assert not ok and any("expired" in r for r in reasons)


def test_changes_requested_reruns_qc(conn):
    idea_id = make_ready_idea(conn)
    approvals.record_decision(conn, idea_id, "changes_requested", "Nick", "Tighter hook")
    orch = Orchestrator(conn)
    by_agent = {t["agent_key"]: t["status"] for t in orch.tasks(idea_id)}
    assert by_agent["quality_control"] == "done"          # re-ran automatically
    assert by_agent["publishing_coordinator"] == "manual_required"
    assert ideas.get_idea(conn, idea_id)["status"] == "in_production"


def test_rejected_idea_cannot_be_restarted(conn):
    idea_id = make_ready_idea(conn)
    approvals.record_decision(conn, idea_id, "rejected", "Nick")
    with pytest.raises(ValueError):
        Orchestrator(conn).plan(idea_id)
