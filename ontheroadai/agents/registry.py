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
                "Starts Trend Scout research runs (on demand or scheduled) and records them",
            ),
            planned=("Scheduling and priority queues across accounts", "Retry/backoff policies"),
        ),
        AgentSpec(
            "trend_scout", "Trend Scout", IMPLEMENTED,
            "Researches sports and GTA 6 news from permitted public feeds and turns it into verified, scored "
            "opportunities.",
            works_today=(
                "Reads public RSS/Atom feeds and YouTube channel feeds (robots.txt, rate limits and caching respected)",
                "Normalises, de-duplicates and groups related headlines into stories; flags new developments",
                "Classifies by sport, topic and GTA concept; labels confirmed vs reported vs rumor",
                "Transparent opportunity score that uses your analytics when there is enough data",
                "Drafts hooks, formats and production notes from templates (no AI model)",
                "One-click conversion to ideas; completes the 'trend context' step for researched ideas",
                "Runs on demand, or on a schedule while the app is open",
            ),
            planned=("Optional local AI model for richer summaries (only if free and local)",
                     "More official sources as they publish feeds"),
            target_milestone="",
        ),
        AgentSpec(
            "analytics_agent", "Analytics Agent", IMPLEMENTED,
            "Turns real post performance into insights.",
            works_today=(
                "Imports platform CSV exports with a column-mapping screen (duplicates and bad files rejected)",
                "Manual entry, correction and deletion of performance snapshots",
                "Retention analysis against similar-length videos",
                "Follower conversion (follows per 1,000 views) by format, platform and length",
                "Side-by-side video comparison separating observed differences from unproven causes",
                "Data-backed recommendations with sample-size labels",
                "Post-publish task: reports how a published idea performed once its numbers are imported",
            ),
            planned=("Direct platform API sync (only if free and officially supported)",
                     "Automatically suggest scoring inputs from history"),
            target_milestone="",
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
                "Blocks researched ideas whose claims are unverified until someone acknowledges them",
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
    if agent_key == "analytics_agent":
        from .analytics_agent import AnalyticsAgent

        return AnalyticsAgent()
    if agent_key == "trend_scout":
        from .trend_scout_agent import TrendScoutAgent

        return TrendScoutAgent()
    return None
