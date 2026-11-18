"""Shared vocabulary for the whole system.

Keeping these in one place makes it easy to add platforms, content types or
new businesses later without hunting through the code.
"""

PLATFORMS = {
    "instagram": "Instagram",
    "tiktok": "TikTok",
    "youtube_shorts": "YouTube Shorts",
    "facebook": "Facebook",
    "x": "X",
}

OTRM = "on-the-road-media"
GTA = "gta6-network"

BUSINESS_PLATFORMS = {
    OTRM: ["instagram", "tiktok", "youtube_shorts", "facebook", "x"],
    GTA: ["tiktok", "instagram", "youtube_shorts", "x"],
}

CONTENT_TYPES = {
    OTRM: {
        "street_interview": "Street interview",
        "fan_reaction": "Fan reaction",
        "game_day": "Game-day coverage",
        "podcast_clip": "Podcast clip",
        "storytelling": "Sports storytelling",
    },
    GTA: {
        "gta_moment": "GTA 6 moment",
        "gta_discovery": "GTA 6 discovery",
        "streamer_story": "Streamer story",
        "news_explainer": "News / trailer explainer",
    },
}

IDEA_STATUSES = (
    "draft",
    "in_production",
    "ready_for_review",
    "approved",
    "rejected",
    "published",
    "archived",
)

TASK_STATUSES = (
    "pending",
    "manual_required",
    "in_progress",
    "done",
    "blocked",
    "failed",
    "skipped",
)

SOURCE_TYPES = {
    "own_footage": "Our own footage",
    "creator_permission": "Creator / streamer permission",
    "licensed": "Licensed footage",
    "publisher_press_kit": "Publisher press kit / official media",
    "public_domain": "Public domain / CC0",
    "unknown": "Unknown origin",
}

SOURCE_STATUSES = ("unknown", "requested", "granted", "denied", "expired")

APPROVAL_DECISIONS = ("approved", "rejected", "changes_requested")

# The 0-10 inputs a human (later: agents) provides for each idea.
SCORE_INPUTS = (
    "trend_momentum",
    "audience_fit",
    "hook_strength",
    "conversion_potential",
    "timeliness",
    "production_ease",
)
