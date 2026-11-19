"""Transparent 0-100 opportunity score for researched stories.

This ranks opportunities for you to review. It does NOT predict views and does not know
how any platform's algorithm ranks videos. Two factors use your own stored analytics
(Milestone 2) when there are enough videos of the recommended format; otherwise they
stay neutral and the score is labelled a heuristic.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .sources import RELIABILITY_RANK

WEIGHTS = {
    "freshness": 14, "audience_relevance": 12, "story_strength": 9, "debate_potential": 10, "share_potential": 8,
    "hook_potential": 10, "originality": 7, "production_feasibility": 8, "source_reliability": 10,
    "format_history": 6, "follower_conversion": 6,
}
assert sum(WEIGHTS.values()) == 100
LABELS = {
    "freshness": "Topic freshness", "audience_relevance": "Audience relevance", "story_strength": "Story strength",
    "debate_potential": "Debate potential", "share_potential": "Share potential", "hook_potential": "Hook potential",
    "originality": "Originality", "production_feasibility": "Production feasibility",
    "source_reliability": "Source reliability", "format_history": "Your format's past performance",
    "follower_conversion": "Your format's follower conversion",
}
MIN_FORMAT_VIDEOS = 3
DISCLAIMER = ("Ranks opportunities for review. It is not a prediction of views or virality, and it does not know how "
              "any platform's algorithm ranks content.")

SPORT_RELEVANCE = {"nba": 9, "nfl": 9, "ufc": 7, "mlb": 6, "nhl": 5, "general": 5}
DEBATE_BASE = {"fan_debate": 9, "rivalry": 9, "controversy": 7, "trade_signing": 7, "unexpected_performance": 6,
               "press_conference": 6, "game_day": 6, "athlete_story": 4, "injury": 3, "general": 4, "sensitive": 0,
               "gta_moment": 5, "gta_discovery": 6, "streamer_story": 5, "news_explainer": 6, "roundup": 6}
SHARE_BASE = {"unexpected_performance": 8, "controversy": 7, "rivalry": 7, "fan_debate": 7, "athlete_story": 6,
              "trade_signing": 6, "game_day": 5, "press_conference": 5, "injury": 4, "general": 4, "sensitive": 1,
              "gta_moment": 8, "gta_discovery": 7, "streamer_story": 7, "news_explainer": 6, "roundup": 4}
FEASIBILITY = {"low": 9, "medium": 6, "high": 3}
SURPRISE = ["record", "historic", "first-ever", "stuns", "wild", "insane", "viral", "shock", "unbelievable",
            "buzzer", "walk-off", "upset", "never", "secret", "hidden"]


def age_hours(iso: str | None, now: datetime) -> float | None:
    if not iso:
        return None
    try:
        return max(0.0, (now - datetime.fromisoformat(iso)).total_seconds() / 3600)
    except ValueError:
        return None


def freshness_points(hours: float | None) -> tuple[int, str]:
    if hours is None:
        return 4, "No publish time in the feed; treated as moderately fresh."
    for limit, pts in ((6, 10), (12, 9), (24, 8), (48, 6), (72, 4), (168, 2)):
        if hours <= limit:
            return pts, f"Newest item is {hours:.0f} h old."
    return 0, f"Newest item is {hours / 24:.0f} days old."


def _ratio_points(ratio: float) -> int:
    for limit, pts in ((1.5, 9), (1.15, 7), (0.85, 5), (0.6, 3)):
        if ratio >= limit:
            return pts
    return 1


def analytics_context(conn, business_slug: str) -> dict:
    """Per-format performance from Milestone 2 data, computed once per run."""
    from ..analytics import insights

    videos = insights.load_videos(conn, business_slug)
    conv = insights.conversion_report(videos)
    overall = insights.kpis(videos)
    by_format: dict[str, dict] = {}
    for v in videos:
        if v.get("format"):
            by_format.setdefault(v["format"], []).append(v)
    out = {}
    views_all = sorted(v["views"] for v in videos if v.get("views") is not None)
    med_all = views_all[len(views_all) // 2] if views_all else None
    for fmt, vs in by_format.items():
        with_views = sorted(v["views"] for v in vs if v.get("views") is not None)
        usable = [v for v in vs if v.get("views") and v.get("follows_gained") is not None]
        views = sum(v["views"] for v in usable)
        follows = sum(v["follows_gained"] for v in usable)
        plat: dict[str, list] = {}
        for v in usable:
            plat.setdefault(v["platform"], []).append(v)
        best_platform = None
        cands = [(p, sum(x["follows_gained"] for x in vs_) / sum(x["views"] for x in vs_) * 1000, len(vs_))
                 for p, vs_ in plat.items() if len(vs_) >= MIN_FORMAT_VIDEOS and sum(x["views"] for x in vs_)]
        if cands:
            best_platform = max(cands, key=lambda c: c[1])
        out[fmt] = {
            "n_views": len(with_views), "median_views": with_views[len(with_views) // 2] if with_views else None,
            "n_conv": len(usable), "per_1k": follows / views * 1000 if views else None,
            "best_platform": best_platform,
        }
    return {"formats": out, "overall_per_1k": overall["per_1k"], "overall_median_views": med_all,
            "n_videos": len(videos), "conversion_by_format": conv["by_format"]}


def score_story(story: dict, rec: dict, ctx: dict, now: datetime | None = None,
                similar_idea: bool = False) -> dict:
    """story: aggregated story dict; rec: recommendation (format, difficulty, hooks...)."""
    now = now or datetime.now(timezone.utc)
    f: dict[str, tuple[int, str]] = {}
    topic = story["topic_type"]
    text = story["title"] + " " + story.get("summary", "")

    f["freshness"] = freshness_points(age_hours(story.get("latest_published_at") or story.get("first_seen_at"), now))

    if story["business"] == "on-the-road-media":
        base = SPORT_RELEVANCE.get(story["category"], 5)
        extra = 1 if story.get("major_event") else 0
        f["audience_relevance"] = (min(10, base + extra),
                                   f"{story['category'].upper() if story['category'] != 'general' else 'General sports'} "
                                   f"has {'broad' if base >= 8 else 'solid' if base >= 6 else 'narrower'} general-fan reach"
                                   + ("; major event" if extra else "") + ". Set by fixed rules, not your audience data.")
    else:
        f["audience_relevance"] = (8 if topic != "news_explainer" else 9,
                                   "Directly about GTA 6, which is the network's audience.")

    s, it = story["source_count"], story["item_count"]
    f["story_strength"] = (min(10, 3 + 2 * (s - 1) + max(0, it - s)),
                           f"{it} item(s) from {s} source(s).")

    d = DEBATE_BASE.get(topic, 4) + (1 if "?" in story["title"] else 0)
    f["debate_potential"] = (min(10, d) if topic != "sensitive" else 0,
                             "Sensitive news is not treated as debate material." if topic == "sensitive"
                             else f"Topic type: {topic.replace('_', ' ')}.")

    sh = SHARE_BASE.get(topic, 4) + (1 if any(w in text.lower() for w in SURPRISE) else 0) + \
        (1 if story.get("major_event") else 0)
    f["share_potential"] = (min(10, sh), "Based on topic type and surprise/major-event wording.")

    if topic == "sensitive":
        f["hook_potential"] = (2, "Sensitive story: no punchy hooks are drafted.")
    else:
        has_name = bool(rec.get("subject"))
        hp = 4 + (3 if has_name else 0) + (1 if len(story["title"]) <= 80 else 0) + (2 if rec.get("hooks") else 0)
        f["hook_potential"] = (min(10, hp), ("Names a specific person/team" if has_name else "No clear named subject")
                               + "; short, specific headlines make better hooks.")

    orig = 7 if s == 1 else 6 if s <= 3 else 4
    if rec.get("format") in ("street_interview", "fan_reaction", "gta_discovery", "gta_moment"):
        orig += 2
    note = f"Covered by {s} source(s); your own angle ({rec.get('format_label', '')}) adds originality."
    if similar_idea:
        orig -= 3
        note += " You already have a similar idea."
    f["originality"] = (max(0, min(10, orig)), note)

    f["production_feasibility"] = (FEASIBILITY.get(rec.get("difficulty", "medium"), 6),
                                   f"{rec.get('difficulty', 'medium').capitalize()} effort: {rec.get('difficulty_note', '')}")

    best = max((RELIABILITY_RANK.get(r, 3) for r in story.get("reliabilities", [])), default=3)
    if story["verification"] in ("rumor_speculation", "reported_unconfirmed", "community_unverified"):
        best = max(0, best - 3)
    f["source_reliability"] = (best, f"Best source: {', '.join(sorted(set(story.get('reliabilities', []))))}; "
                                     f"verification: {story['verification'].replace('_', ' ')}.")

    fmt = rec.get("format")
    fc = ctx["formats"].get(fmt, {})
    data_used = []
    if fc.get("n_views", 0) >= MIN_FORMAT_VIDEOS and ctx.get("overall_median_views") and fc.get("median_views") is not None:
        ratio = fc["median_views"] / ctx["overall_median_views"] if ctx["overall_median_views"] else 1
        f["format_history"] = (_ratio_points(ratio), f"Your {fc['n_views']} {rec.get('format_label')} videos: median "
                               f"{fc['median_views']:,} views vs {ctx['overall_median_views']:,} for all formats.")
        data_used.append("format_history")
    else:
        f["format_history"] = (5, f"Not enough of your own {rec.get('format_label', 'format')} videos yet "
                                  f"({fc.get('n_views', 0)} of {MIN_FORMAT_VIDEOS} needed). Neutral value used.")
    if fc.get("n_conv", 0) >= MIN_FORMAT_VIDEOS and ctx.get("overall_per_1k") and fc.get("per_1k") is not None:
        ratio = fc["per_1k"] / ctx["overall_per_1k"] if ctx["overall_per_1k"] else 1
        f["follower_conversion"] = (_ratio_points(ratio), f"{fc['per_1k']:.2f} follows per 1,000 views for this "
                                    f"format ({fc['n_conv']} videos) vs {ctx['overall_per_1k']:.2f} overall.")
        data_used.append("follower_conversion")
    else:
        f["follower_conversion"] = (5, f"Not enough follower data for this format yet ({fc.get('n_conv', 0)} of "
                                       f"{MIN_FORMAT_VIDEOS} videos). Neutral value used.")

    factors, total = [], 0.0
    for key, weight in WEIGHTS.items():
        val, why = f[key]
        pts = val / 10 * weight
        total += pts
        factors.append({"key": key, "label": LABELS[key], "input": val, "weight": weight,
                        "points": round(pts, 1), "explanation": why})
    mode = "heuristic" if not data_used else "heuristic + your analytics"
    return {"total": int(round(total)), "factors": factors, "mode": mode, "analytics_factors": data_used,
            "disclaimer": DISCLAIMER}
