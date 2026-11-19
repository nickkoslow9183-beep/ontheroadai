"""Quality Control agent (rule-based compliance checks only, for now)."""

from __future__ import annotations

from ..rights import rights_check
from .base import Agent, AgentResult
from .registry import SPECS

MIN_HOOK_CHARS = 10


class QualityControlAgent(Agent):
    spec = SPECS["quality_control"]

    def run(self, conn, task: dict, idea: dict) -> AgentResult:
        checks: list[tuple[str, bool, str]] = []

        checks.append(("Title present", bool(idea["title"].strip()), ""))
        hook = (idea["hook"] or "").strip()
        checks.append((
            "Hook written",
            len(hook) >= MIN_HOOK_CHARS,
            f"Hook must be at least {MIN_HOOK_CHARS} characters." if len(hook) < MIN_HOOK_CHARS else "",
        ))
        checks.append((
            "Target platform chosen",
            bool(idea["target_platforms_list"]),
            "Pick at least one platform." if not idea["target_platforms_list"] else "",
        ))
        ok, problems = rights_check(conn, idea["id"])
        checks.append(("Footage rights cleared", ok, "; ".join(problems)))

        from ..trends.opportunities import story_for_idea

        story = story_for_idea(conn, idea["id"])
        if story is not None:
            claims_ok = not story["unverified"] or bool(story["claims_acknowledged_by"])
            checks.append((
                "Research claims verified",
                claims_ok,
                "" if claims_ok else f"Source status is '{story['verification_label']}'. Confirm it with an official "
                                     f"or major outlet, or acknowledge on opportunity #{story['id']} that the video "
                                     "presents it as unconfirmed.",
            ))

        earlier = conn.execute(
            "SELECT agent_key, status FROM agent_tasks WHERE idea_id = ? AND phase = 'pre_publish' "
            "AND sequence < ? AND status NOT IN ('done', 'skipped')", (idea["id"], task["sequence"])
        ).fetchall()
        checks.append((
            "Earlier pipeline steps complete",
            not earlier,
            ", ".join(f"{r['agent_key']} is {r['status']}" for r in earlier),
        ))

        lines = [f"[{'PASS' if passed else 'FAIL'}] {name}" + (f" - {why}" if why and not passed else "")
                 for name, passed, why in checks]
        failed = [f"{name}: {why}" if why else name for name, passed, why in checks if not passed]
        lines.append("Note: video/audio quality is NOT inspected yet (planned).")
        return AgentResult(success=not failed, output="\n".join(lines), problems=failed)
