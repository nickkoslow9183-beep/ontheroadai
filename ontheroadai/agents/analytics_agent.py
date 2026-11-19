"""Analytics Agent: reports how a published idea actually performed."""

from __future__ import annotations

from ..analytics.insights import load_videos
from .base import Agent, AgentResult
from .registry import SPECS


class AnalyticsAgent(Agent):
    spec = SPECS["analytics_agent"]

    def run(self, conn, task: dict, idea: dict) -> AgentResult:
        if task["task_type"] != "collect_performance":
            return AgentResult(False, f"Analytics Agent does not handle task type {task['task_type']!r}.")
        everything = load_videos(conn, idea["business_slug"])
        mine = [v for v in everything if v.get("idea_id") == idea["id"]]
        if not mine:
            return AgentResult(False, "Waiting for data: no video is linked to this idea yet. Import the platform's "
                               "CSV export (videos with the same post link/ID link automatically) or link the "
                               "video on its page in Analytics.", waiting=True)
        measured = [v for v in mine if v.get("snapshot_id")]
        if not measured:
            return AgentResult(False, "Waiting for data: the video is linked but has no performance numbers yet. "
                               "Import a CSV or enter the numbers by hand.", waiting=True)

        conv = [v for v in everything if v.get("views") and v.get("follows_per_1k") is not None]
        lines = []
        for v in measured:
            name = v["title"] or v["post_url"] or f"video #{v['id']}"
            lines.append(f"{name} ({v['platform_label']}), numbers as of {v['captured_on']}:")
            facts = []
            for key, label in (("views", "views"), ("follows_gained", "followers gained")):
                if v.get(key) is not None:
                    facts.append(f"{v[key]:,} {label}")
            if v.get("follows_per_1k") is not None:
                facts.append(f"{v['follows_per_1k']} follows per 1,000 views")
            if v.get("engagement_rate") is not None:
                facts.append(f"{v['engagement_rate']}% engagement"
                             + (" (partial)" if v.get("engagement_partial") else ""))
            if v.get("watch_ratio") is not None:
                facts.append(f"watched {v['watch_ratio']:.0%} of its length on average")
            lines.append("  " + ("; ".join(facts) if facts else "metrics present but none of the headline ones"))
            others = [o["follows_per_1k"] for o in conv if o["id"] != v["id"]]
            if v.get("follows_per_1k") is not None and len(others) >= 3:
                others.sort()
                mid = others[len(others) // 2] if len(others) % 2 else (others[len(others) // 2 - 1] + others[len(others) // 2]) / 2
                rel = "above" if v["follows_per_1k"] > mid else "below" if v["follows_per_1k"] < mid else "at"
                lines.append(f"  Follower conversion is {rel} the business median ({mid:.2f}, {len(others)} other videos).")
            else:
                lines.append("  Not enough other videos with follower data to compare against yet.")
            if v.get("retention_note"):
                lines.append(f"  Retention: {v['retention_note']}.")
            missing = [label for key, label in (("avg_watch_seconds", "avg watch time"),
                                                ("duration_seconds", "video length"),
                                                ("follows_gained", "followers gained"))
                       if v.get(key) is None]
            if missing:
                lines.append(f"  Not available: {', '.join(missing)} (not estimated).")
        lines.append("Re-runs automatically when newer numbers are imported.")
        return AgentResult(True, "\n".join(lines))
