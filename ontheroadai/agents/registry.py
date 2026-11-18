"""The nine agents and their honest implementation status."""

from __future__ import annotations

from .base import IMPLEMENTED, PARTIAL, PLANNED, AgentSpec

SPECS: dict[str, AgentSpec] = {
    s.key: s
    for s in [
        AgentSpec(
            "orchestrator", "Orchestrator", IMPLEMENTED,
            "Breaks each idea into a pipeline of agent tasks, runs implemented agents, "
            "routes everything else to a human, and tracks status.",
            works_today=(
                "Creates the production pipeline for an idea",
                "Runs implemented agents automatically",
                "Marks tasks for planned agents as 'manual_required'",
                "Moves ideas to 'ready_for_review' when all steps are done",
                "Creates post-publish analytics/growth tasks",
            ),
            planned=("Scheduling and priority queues across accounts", "Retry/backoff policies"),
        ),
        AgentSpec(
            "trend_scout", "Trend Scout", PLANNED,
            "Researches what is trending in sports and GTA 6 communities.",
            planned=("Free-source trend collection (RSS, public pages, Google Trends exports)",
                     "Auto-fill trend momentum and timeliness scores"),
            target_milestone="Milestone 3",
        ),
        AgentSpec(
            "analytics_agent", "Analytics Agent", PLANNED,
            "Turns real post performance into insights.",
            works_today=(),
            planned=("CSV import of platform analytics exports",
                     "Per-format / per-hook performance analysis",
                     "Feed results back into scoring"),
            target_milestone="Milestone 2",
        ),
        AgentSpec(
            "scriptwriter", "Scriptwriter", PLANNED,
            "Drafts hooks, scripts, captions and interview questions.",
            planned=("Hook variants from proven templates", "Optional local open-source LLM"),
            target_milestone="Milestone 3",
        ),
        AgentSpec(
            "clip_finder", "Clip Finder", PLANNED,
            "Finds usable moments in authorized footage only.",
            planned=("Transcript search of our own/authorized footage (open-source speech-to-text)",
                     "Podcast highlight detection"),
            target_milestone="Milestone 4",
        ),
        AgentSpec(
            "video_editor", "Video Editor", PLANNED,
            "Produces vertical cuts, captions and branding.",
            planned=("FFmpeg-based 9:16 reframing, burned-in captions, brand templates",),
            target_milestone="Milestone 4",
        ),
        AgentSpec(
            "quality_control", "Quality Control", PARTIAL,
            "Checks content is complete and legally safe before human review.",
            works_today=(
                "Blocks ideas whose external footage lacks documented permission",
                "Checks hook, title and target platforms are present",
                "Checks earlier pipeline steps are complete",
            ),
            planned=("Video checks: resolution, aspect ratio, loudness, caption accuracy",),
            target_milestone="Milestone 4",
        ),
        AgentSpec(
            "publishing_coordinator", "Publishing Coordinator", PLANNED,
            "Prepares publish packages (captions, hashtags, schedule) for a human to post.",
            planned=("Per-platform caption/hashtag packages", "Posting calendar",
                     "Never auto-publishes: a human always posts"),
            target_milestone="Milestone 3",
        ),
        AgentSpec(
            "growth_optimizer", "Growth Optimizer", PLANNED,
            "Recommends changes that increase follower conversion.",
            planned=("Follower-conversion recommendations from analytics", "A/B hook suggestions"),
            target_milestone="Milestone 5",
        ),
    ]
}


def implementation_for(agent_key: str):
    """Return the agent instance that really does this work, or None if it is not built yet."""
    if agent_key == "quality_control":
        from .quality_control import QualityControlAgent

        return QualityControlAgent()
    return None
