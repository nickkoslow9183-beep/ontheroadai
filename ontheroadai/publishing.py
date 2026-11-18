"""Publishing gate.

This system NEVER posts anything. 'Mark published' records that a person has
already posted the content manually, and it is refused unless a human has
approved it and every footage permission is in place.
"""

from __future__ import annotations

import sqlite3

from .approvals import latest
from .constants import PLATFORMS
from .db import log, now
from .ideas import get_idea
from .rights import rights_check


def readiness(conn: sqlite3.Connection, idea_id: int) -> tuple[bool, list[str]]:
    idea = get_idea(conn, idea_id)
    reasons = []
    if idea["status"] == "published":
        return False, ["Already marked published."]
    decision = latest(conn, idea_id)
    if decision is None or decision["decision"] != "approved":
        reasons.append("No current human approval.")
    if idea["status"] != "approved":
        reasons.append(f"Idea status is '{idea['status']}', must be 'approved'.")
    ok, problems = rights_check(conn, idea_id)
    if not ok:
        reasons.extend(problems)
    qc = conn.execute(
        "SELECT status FROM agent_tasks WHERE idea_id = ? AND agent_key = 'quality_control' AND phase = 'pre_publish'",
        (idea_id,),
    ).fetchone()
    if qc is None or qc["status"] != "done":
        reasons.append("Quality Control check has not passed.")
    return not reasons, reasons


def mark_published(conn: sqlite3.Connection, idea_id: int, confirmed_by: str, platform: str,
                   published_url: str = "") -> None:
    confirmed_by = (confirmed_by or "").strip()
    if not confirmed_by:
        raise ValueError("Name the person who posted it.")
    idea = get_idea(conn, idea_id)
    if platform not in PLATFORMS or platform not in idea["target_platforms_list"]:
        raise ValueError("Platform must be one of this idea's target platforms.")
    ok, reasons = readiness(conn, idea_id)
    if not ok:
        raise PermissionError("Cannot mark published: " + " ".join(reasons))
    conn.execute(
        "UPDATE ideas SET status = 'published', published_platform = ?, published_url = ?, published_by = ?, "
        "published_at = ?, updated_at = ? WHERE id = ?",
        (platform, (published_url or "").strip(), confirmed_by, now(), now(), idea_id),
    )
    log(conn, "idea", idea_id, "marked published (manual post)", f"{platform} by {confirmed_by}")
    conn.commit()
    from .agents.orchestrator import Orchestrator

    Orchestrator(conn).on_published(idea_id)
