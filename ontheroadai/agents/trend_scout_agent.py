"""Trend Scout agent: supplies the 'trend context' step of an idea's production pipeline."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .base import Agent, AgentResult
from .registry import SPECS


class TrendScoutAgent(Agent):
    spec = SPECS["trend_scout"]

    def run(self, conn, task: dict, idea: dict) -> AgentResult:
        from ..trends.opportunities import get, story_for_idea
        from ..trends.text import jaccard, keyword_set, named_phrases

        if task["task_type"] != "trend_context":
            return AgentResult(False, f"Trend Scout does not handle task type {task['task_type']!r}.")
        story = story_for_idea(conn, idea["id"])
        how = "This idea was created from that opportunity."
        if story is None:
            # look for related research in the last 14 days
            kw, names = keyword_set(idea["title"]), set(named_phrases(idea["title"]))
            since = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
            best, best_sim = None, 0.0
            for r in conn.execute("SELECT id, title FROM research_stories WHERE business = ? AND last_seen_at >= ? "
                                  "AND dismissed = 0", (idea["business_slug"], since)):
                sim = jaccard(kw, keyword_set(r["title"])) + 0.3 * len(names & set(named_phrases(r["title"])))
                if sim > best_sim:
                    best, best_sim = r["id"], sim
            if best is None or best_sim < 0.3:
                return AgentResult(False, "No related research found in the last 14 days. Check the trend by hand "
                                   "(or run Trend Scout first), then mark this step done.", needs_human=True)
            story = get(conn, best)
            how = f"Matched by headline similarity ({best_sim:.2f}); check it's the same topic."
        rec = story["rec"]
        lines = [f"Research brief from opportunity #{story['id']}: {story['title']}", how,
                 f"Verification: {story['verification_label']}. {story['verification_note']}",
                 f"Opportunity score: {story['score']} ({story['score_detail'].get('mode', 'heuristic')})"]
        if rec.get("why_it_matters"):
            lines.append(f"Why it matters: {rec['why_it_matters']}")
        if rec.get("hooks"):
            lines.append("Hook options: " + " | ".join(rec["hooks"]))
        lines.append(f"Urgency: {rec.get('urgency', '?')}")
        for s in rec.get("sources", [])[:4]:
            lines.append(f"- {s['source']}: {s['url']}")
        if story["unverified"] and not story["claims_acknowledged_by"]:
            lines.append("WARNING: unverified. Quality Control will block this until it's confirmed or acknowledged.")
        lines.append(rec.get("generator_note", ""))
        return AgentResult(True, "\n".join(x for x in lines if x))
