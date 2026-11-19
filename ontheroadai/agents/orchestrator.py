"""The Orchestrator: plans, routes and tracks agent tasks for each idea."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from ..db import log, now
from ..ideas import get_idea, set_status
from .registry import SPECS, implementation_for


@dataclass(frozen=True)
class Step:
    agent_key: str
    task_type: str
    description: str
    only_if_external_footage: bool = False


PRE_PUBLISH = [
    Step("trend_scout", "trend_context", "Confirm the trend/angle is current and note references."),
    Step("scriptwriter", "hook_and_script", "Write the hook, script or interview questions, and caption."),
    Step("clip_finder", "authorized_footage", "Locate the exact clips in AUTHORIZED footage and note timestamps.",
         only_if_external_footage=True),
    Step("video_editor", "vertical_edit", "Produce the 9:16 edit with captions and branding."),
    Step("quality_control", "compliance_check", "Check completeness and footage rights before human review."),
    Step("publishing_coordinator", "publish_package", "Prepare captions, hashtags and posting time per platform."),
]

POST_PUBLISH = [
    Step("analytics_agent", "collect_performance", "Record views, watch time and follows after 24-72h."),
    Step("growth_optimizer", "growth_review", "Note what to repeat or change to convert more followers."),
]

FINISHED = ("done", "skipped")


class Orchestrator:
    spec = SPECS["orchestrator"]

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- planning -------------------------------------------------------
    def _create_tasks(self, idea: dict, phase: str, steps: list[Step]) -> None:
        ts = now()
        for seq, step in enumerate(steps, start=1):
            skipped = step.only_if_external_footage and not idea["needs_external_footage"]
            self.conn.execute(
                "INSERT OR IGNORE INTO agent_tasks (idea_id, agent_key, task_type, phase, sequence, description, "
                "status, output, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (idea["id"], step.agent_key, step.task_type, phase, seq, step.description,
                 "skipped" if skipped else "pending",
                 "Not needed: idea uses only our own footage." if skipped else "", ts, ts),
            )

    def plan(self, idea_id: int) -> list[dict]:
        """Create the production pipeline (idempotent) and start it."""
        idea = get_idea(self.conn, idea_id)
        if idea["status"] in ("rejected", "published", "archived"):
            raise ValueError(f"Cannot start production for an idea that is {idea['status']}.")
        self._create_tasks(idea, "pre_publish", PRE_PUBLISH)
        if idea["status"] == "draft":
            set_status(self.conn, idea_id, "in_production", "pipeline planned by orchestrator")
        log(self.conn, "orchestrator", idea_id, "planned pipeline")
        self.conn.commit()
        self.advance(idea_id)
        return self.tasks(idea_id)

    # ---- execution ------------------------------------------------------
    def _set_task(self, task_id: int, status: str, output: str | None = None, completed_by: str | None = None):
        sets = ["status = ?", "updated_at = ?"]
        params: list = [status, now()]
        if output is not None:
            sets.append("output = ?")
            params.append(output)
        if status == "done":
            sets += ["completed_at = ?", "completed_by = ?"]
            params += [now(), completed_by]
        self.conn.execute(f"UPDATE agent_tasks SET {', '.join(sets)} WHERE id = ?", [*params, task_id])

    def advance(self, idea_id: int) -> dict:
        """Run every task that is ready, in order, until something needs a human or is blocked."""
        idea = get_idea(self.conn, idea_id)
        if idea["status"] == "published":
            return self.advance_post(idea_id)
        ran, waiting_on = [], None
        for task in self.tasks(idea_id, phase="pre_publish"):
            if task["status"] in FINISHED:
                continue
            if task["status"] in ("manual_required", "in_progress"):
                waiting_on = task
                break
            agent = implementation_for(task["agent_key"])
            if agent is None:
                self._set_task(task["id"], "manual_required",
                               f"{SPECS[task['agent_key']].name} is not implemented yet. "
                               "A person must do this step and mark it done.")
                log(self.conn, "task", task["id"], "routed to human", task["agent_key"])
                waiting_on = task
                break
            result = agent.run(self.conn, task, idea)
            if result.waiting:
                self._set_task(task["id"], "pending", result.output)
                waiting_on = task
                break
            if result.needs_human:
                self._set_task(task["id"], "manual_required", result.output)
                log(self.conn, "task", task["id"], "routed to human", f"{task['agent_key']}: needs a person")
                waiting_on = task
                break
            self._set_task(task["id"], "done" if result.success else "blocked", result.output,
                           completed_by=f"agent:{task['agent_key']}" if result.success else None)
            log(self.conn, "task", task["id"], "agent ran", f"{task['agent_key']}: "
                f"{'passed' if result.success else 'blocked'}")
            ran.append(task["agent_key"])
            if not result.success:
                waiting_on = task
                break

        pre = self.tasks(idea_id, phase="pre_publish")
        if pre and all(t["status"] in FINISHED for t in pre) and idea["status"] == "in_production":
            set_status(self.conn, idea_id, "ready_for_review", "all production steps complete")
        self.conn.commit()
        return {"ran": ran, "waiting_on": waiting_on["agent_key"] if waiting_on else None}

    def complete_manual_task(self, task_id: int, completed_by: str, notes: str = "") -> dict:
        """A human marks a planned-agent task done, then the pipeline continues."""
        task = self.get_task(task_id)
        completed_by = (completed_by or "").strip()
        if not completed_by:
            raise ValueError("Say who completed the task.")
        if task["status"] != "manual_required":
            raise ValueError(f"Only tasks waiting for a person can be completed by hand (this one is {task['status']}).")
        self._set_task(task_id, "done", (task["output"] + "\n" if task["output"] else "") +
                       f"Completed by {completed_by}. {notes.strip()}".strip(), completed_by=completed_by)
        log(self.conn, "task", task_id, "completed by human", completed_by)
        self.conn.commit()
        if task["phase"] == "pre_publish":
            return self.advance(task["idea_id"])
        return {"ran": [], "waiting_on": None}

    def reopen_for_changes(self, idea_id: int) -> None:
        """After 'changes requested', QC and the publish package must be redone."""
        for t in self.tasks(idea_id, phase="pre_publish"):
            if t["agent_key"] in ("quality_control", "publishing_coordinator"):
                self._set_task(t["id"], "pending", "")
        self.conn.commit()

    def on_published(self, idea_id: int) -> None:
        idea = get_idea(self.conn, idea_id)
        self._create_tasks(idea, "post_publish", POST_PUBLISH)
        for t in self.tasks(idea_id, phase="post_publish"):
            if t["status"] == "pending" and implementation_for(t["agent_key"]) is None:
                self._set_task(t["id"], "manual_required",
                               f"{SPECS[t['agent_key']].name} is not implemented yet. Do this by hand.")
        self.conn.commit()
        self.advance_post(idea_id)

    def advance_post(self, idea_id: int) -> dict:
        """Run implemented post-publish agents. They are independent of each other, and the
        Analytics Agent re-runs (even when already done) so its report reflects the newest numbers."""
        idea = get_idea(self.conn, idea_id)
        ran, waiting = [], []
        for task in self.tasks(idea_id, phase="post_publish"):
            agent = implementation_for(task["agent_key"])
            if agent is None or task["status"] in ("skipped", "manual_required", "in_progress"):
                continue
            result = agent.run(self.conn, task, idea)
            if result.waiting:
                self._set_task(task["id"], "pending", result.output)
                waiting.append(task["agent_key"])
            else:
                self._set_task(task["id"], "done" if result.success else "blocked", result.output,
                               completed_by=f"agent:{task['agent_key']}" if result.success else None)
                ran.append(task["agent_key"])
            log(self.conn, "task", task["id"], "agent ran", f"{task['agent_key']}: "
                f"{'waiting for data' if result.waiting else 'done' if result.success else 'blocked'}")
        self.conn.commit()
        return {"ran": ran, "waiting_on": waiting[0] if waiting else None}

    # ---- research -------------------------------------------------------
    def run_research(self, trigger: str = "manual", transport=None, force: bool = False,
                     source_ids: list[int] | None = None) -> int:
        """Start a Trend Scout research run and record it in the activity log."""
        from ..trends.job import run_trend_scout

        run_id = run_trend_scout(self.conn, trigger=trigger, transport=transport, force=force, source_ids=source_ids)
        log(self.conn, "orchestrator", None, "research run routed to Trend Scout", f"run #{run_id} ({trigger})")
        self.conn.commit()
        return run_id

    # ---- queries --------------------------------------------------------
    def get_task(self, task_id: int) -> dict:
        row = self.conn.execute("SELECT * FROM agent_tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise LookupError(f"Task {task_id} not found")
        return dict(row)

    def tasks(self, idea_id: int, phase: str | None = None) -> list[dict]:
        sql = "SELECT * FROM agent_tasks WHERE idea_id = ?"
        params: list = [idea_id]
        if phase:
            sql += " AND phase = ?"
            params.append(phase)
        sql += " ORDER BY phase DESC, sequence"  # pre_publish before post_publish
        rows = self.conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["agent_name"] = SPECS[d["agent_key"]].name
            d["agent_status"] = SPECS[d["agent_key"]].status
            out.append(d)
        return out

    def open_tasks(self, business_slug: str | None = None) -> list[dict]:
        sql = ("SELECT t.*, i.title AS idea_title, b.slug AS business_slug FROM agent_tasks t "
               "JOIN ideas i ON i.id = t.idea_id JOIN businesses b ON b.id = i.business_id "
               "WHERE t.status IN ('manual_required','blocked','failed','in_progress') "
               "AND i.status NOT IN ('archived','rejected')")
        params = []
        if business_slug:
            sql += " AND b.slug = ?"
            params.append(business_slug)
        sql += " ORDER BY t.updated_at"
        out = []
        for r in self.conn.execute(sql, params).fetchall():
            d = dict(r)
            d["agent_name"] = SPECS[d["agent_key"]].name
            out.append(d)
        return out

    def status_counts(self) -> dict[str, dict[str, int]]:
        counts: dict[str, dict[str, int]] = {k: {} for k in SPECS}
        for r in self.conn.execute(
            "SELECT agent_key, status, COUNT(*) AS n FROM agent_tasks GROUP BY agent_key, status"
        ).fetchall():
            counts.setdefault(r["agent_key"], {})[r["status"]] = r["n"]
        return counts
