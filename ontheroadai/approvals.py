"""Human review decisions. Only a named person can approve content."""

from __future__ import annotations

import sqlite3

from .constants import APPROVAL_DECISIONS
from .db import log, now
from .ideas import get_idea, set_status

REVIEWABLE = ("ready_for_review", "approved")


def record_decision(conn: sqlite3.Connection, idea_id: int, decision: str, reviewer: str, notes: str = "") -> int:
    idea = get_idea(conn, idea_id)
    reviewer = (reviewer or "").strip()
    if decision not in APPROVAL_DECISIONS:
        raise ValueError(f"Decision must be one of: {', '.join(APPROVAL_DECISIONS)}")
    if not reviewer:
        raise ValueError("Reviewer name is required.")
    if idea["status"] not in REVIEWABLE:
        raise ValueError(f"Idea is '{idea['status']}'; it can only be reviewed once production is complete "
                         "(status 'ready_for_review').")
    cur = conn.execute(
        "INSERT INTO approvals (idea_id, decision, reviewer, notes, created_at) VALUES (?,?,?,?,?)",
        (idea_id, decision, reviewer, (notes or "").strip(), now()),
    )
    new_status = {"approved": "approved", "rejected": "rejected", "changes_requested": "in_production"}[decision]
    set_status(conn, idea_id, new_status, f"{decision} by {reviewer}")
    log(conn, "approval", cur.lastrowid, decision, reviewer)
    conn.commit()
    if decision == "changes_requested":
        from .agents.orchestrator import Orchestrator

        orch = Orchestrator(conn)
        orch.reopen_for_changes(idea_id)
        orch.advance(idea_id)
    return cur.lastrowid


def history(conn: sqlite3.Connection, idea_id: int) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM approvals WHERE idea_id = ? ORDER BY id DESC", (idea_id,)).fetchall()]


def latest(conn: sqlite3.Connection, idea_id: int) -> dict | None:
    h = history(conn, idea_id)
    return h[0] if h else None
