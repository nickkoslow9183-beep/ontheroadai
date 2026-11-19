"""Content recommendations drafted from headlines with fixed templates (no AI model).

Hooks are short questions in a plain, conversational style. They only use names and
facts that appear in the source headlines, never invented details. Always edit before
filming.
"""

from __future__ import annotations

import re

from ..constants import CONTENT_TYPES
from .classify import GTA_BRAND, SPORT_LABELS, TOPIC_LABELS, VERIFICATION

GENERATOR_NOTE = "Drafted from the headlines with fixed templates (no AI model). Edit before filming."

SPORT_FORMAT = {
    "fan_debate": "street_interview", "rivalry": "street_interview", "trade_signing": "street_interview",
    "controversy": "fan_reaction", "unexpected_performance": "fan_reaction", "game_day": "game_day",
    "press_conference": "podcast_clip", "athlete_story": "storytelling", "injury": "podcast_clip",
    "general": "fan_reaction", "sensitive": "podcast_clip", "roundup": "street_interview",
}
DIFFICULTY = {
    "street_interview": ("medium", "Go out with a mic and ask fans on camera; you own the footage."),
    "fan_reaction": ("low", "Quick fan reactions on location or from your community; you own the footage."),
    "game_day": ("medium", "Needs venue/tailgate access on game day."),
    "podcast_clip": ("low", "Talk it through on your podcast; clip the segment."),
    "storytelling": ("high", "Needs research, a script and B-roll you're allowed to use (no broadcast footage)."),
    "gta_moment": ("low", "Capture your own gameplay of the situation."),
    "gta_discovery": ("medium", "Test it yourself in-game and record your own footage."),
    "news_explainer": ("low", "Voice-over with your own gameplay or official media (link the source)."),
    "streamer_story": ("high", "Needs the streamer's written permission before any of their footage is used."),
}
DEFAULT_PLATFORMS = {
    "street_interview": ["tiktok", "instagram"], "fan_reaction": ["tiktok", "instagram"],
    "game_day": ["instagram", "tiktok"], "podcast_clip": ["youtube_shorts", "x"],
    "storytelling": ["youtube_shorts", "instagram"], "gta_moment": ["tiktok", "youtube_shorts"],
    "gta_discovery": ["youtube_shorts", "tiktok"], "news_explainer": ["youtube_shorts", "x"],
    "streamer_story": ["youtube_shorts", "tiktok"],
}
EVERGREEN = {
    "nba": ["Who would you trust with the last shot?", "Who's the most overrated player in the NBA?",
            "Would you rather have prime Melo or prime T-Mac?"],
    "nfl": ["Which QB would you trust in a two-minute drill?", "Who's the most overrated team in the NFL?",
            "Name a player you'd build a team around."],
    "mlb": ["Who's the best hitter in baseball right now?", "Which team is overrated this year?",
            "Who would you want up with the game on the line?"],
    "nhl": ["Who's the best player in the NHL right now?", "Which team is going all the way?",
            "Who would you want on a breakaway?"],
    "ufc": ["Who's the best pound-for-pound fighter right now?", "Which fight do you want to see next?",
            "Who's the most overrated fighter in the UFC?"],
    "general": ["Who's the best athlete in the world right now?", "Which team is most overrated right now?",
                "Name a player you'd pay to watch."],
}


def _short(name: str) -> str:
    return name.strip()


def _single_question(title: str, max_words: int) -> str | None:
    """A headline that is itself one short question makes a natural hook. Compound headlines don't."""
    t = (title or "").strip()
    if t.endswith("?") and t.count("?") == 1 and ":" not in t and len(t.split()) <= max_words:
        return t
    return None


def _team_hooks(topic: str, t: str, t2: str | None, league: str) -> list[str]:
    """Hooks when the subject is a team ('Are the Heat…', not 'Is Heat…')."""
    if topic in ("fan_debate", "roundup"):
        return [f"Are the {t} overrated?", f"Are the {t} a real contender?",
                f"Who's better right now: the {t} or the {t2}?" if t2 else f"Who's the best team in the {league} right now?"]
    if topic == "unexpected_performance":
        return [f"Are the {t} for real?", f"Did you see what the {t} just did?", f"Real or fluke: the {t} right now?"]
    if topic == "game_day":
        return [f"{t} or {t2}: who you got?" if t2 else f"Are you picking the {t} tonight?",
                "Give me your score prediction.", f"Who's the X-factor for the {t}?"]
    if topic == "rivalry":
        return [f"{t} or {t2}: who takes it?" if t2 else f"Who's the {t}' biggest rival right now?",
                "Which side are you on?", f"Is this the best rivalry in the {league} right now?"]
    if topic == "trade_signing":
        return [f"Good move or panic move by the {t}?", f"Did the {t} get better or worse?", "Grade this move: A to F."]
    if topic == "injury":
        return [f"How big is this loss for the {t}?", f"Can the {t} survive this?", "Who needs to step up now?"]
    return [f"What's your honest take on the {t} right now?", f"Are the {t} better or worse than last year?",
            f"Who's the best player on the {t}?"]


def sports_hooks(topic: str, sport: str, people: list[str], teams: list[str], title: str = "") -> list[str]:
    y = _short(people[1]) if len(people) > 1 else None
    t = teams[0] if teams else None
    t2 = teams[1] if len(teams) > 1 else None
    if topic == "sensitive":
        return []
    league = SPORT_LABELS.get(sport, "league").split(" ")[0]
    question = _single_question(title, 12)
    if not people:
        hooks = _team_hooks(topic, t, t2, league) if t else list(EVERGREEN.get(sport, EVERGREEN["general"]))
        if topic == "roundup" and not t:
            hooks = [f"Who's number one in the {league} right now?"] + hooks[:2]
        return _dedupe(([question] if question else []) + hooks)
    x = _short(people[0])
    if topic in ("fan_debate", "roundup"):
        hooks = [f"Is {x} overrated?", f"Be honest: is {x} top five right now?",
                 f"Who would you rather have: {x} or {y}?" if y else f"Who would you take over {x}?"]
    elif topic == "rivalry":
        a, b = (x, y) if y else (t, t2) if t and t2 else (x, None)
        hooks = [f"{a} or {b}: who takes it?" if b else f"Who wins this one, {a} or the other side?",
                 f"Which side are you on?", f"Is this the best rivalry in the {league} right now?"]
    elif topic == "unexpected_performance":
        hooks = [f"Did you see what {x} just did?", f"Is {x} for real, or is this a fluke?",
                 f"One word for {x} right now. Go."]
    elif topic == "trade_signing":
        hooks = [f"Good move or panic move by the {t}?" if t else f"Good move or bad move for {x}?",
                 f"Would you want {x} on your team?", f"Grade the {x} move: A to F."]
    elif topic == "controversy":
        hooks = [f"Is the reaction to {x} fair?", "Who's in the wrong here?", "Was that the right call?"]
    elif topic == "press_conference":
        hooks = [f"What did you think of what {x} said?", f"Was {x} right to say that?",
                 f"Real talk or just talk from {x}?"]
    elif topic == "game_day":
        hooks = [f"Who you got: {t} or {t2}?" if t and t2 else f"Who you got tonight?",
                 "Give me your score prediction.", f"Who's the X-factor for {t or x}?"]
    elif topic == "athlete_story":
        hooks = [f"Do you know {x}'s story?", f"Name a better comeback story than {x}.",
                 f"What's the first thing you think of with {x}?"]
    elif topic == "injury":
        hooks = [f"How big is this loss for {t}?" if t else f"How big is losing {x}?",
                 f"Can {t} survive without {x}?" if t and people else "Who needs to step up now?",
                 "Who needs to step up now?"]
    else:
        hooks = [f"What's your honest take on {x}?", f"Is {x} getting enough credit?",
                 f"Who would you trust with the last shot?" if sport == "nba" else f"Hot or not: {x} right now?"]
    return _dedupe(([question] if question else []) + hooks)


def _dedupe(hooks: list[str]) -> list[str]:
    out = []
    for h in hooks:
        h = re.sub(r"\s+", " ", h).strip()
        if h and h not in out:
            out.append(h)
    return out[:3]


def _gta_topic(title: str) -> str:
    t = re.sub(r"(?i)\b(gta 6|gta vi|grand theft auto (vi|6))('s|’s)?\b", "", title)
    t = re.sub(r"\s+", " ", t).strip(" :-–—,.!?")
    return t[:1].upper() + t[1:] if t else title


def gta_hooks(concept: str, creator: str | None, speculative: bool, title: str = "", confirmed: bool = False) -> list[str]:
    question = _single_question(title, 14)
    topic = _gta_topic(title) if title else ""
    if title.lower().startswith("how to "):
        return _dedupe([f"{title.rstrip('.')}, fast.", "Most players are doing this wrong.",
                        "Here's the quickest way."])
    if question and not speculative:
        return _dedupe([question, "Here's my answer.", "Tell me yours in the comments."])
    headline = title.strip().rstrip(".!") if title and len(title.split()) <= 12 else None
    if headline and not headline.endswith("?"):
        headline += "."
    if concept == "news_explainer" and not speculative and topic:
        return _dedupe([headline or "Big GTA 6 news today.",
                        "Here's what Rockstar actually confirmed." if confirmed else "Here's what we actually know.",
                        "What this means for players."])
    if speculative:
        return ["Is this GTA 6 rumor actually true?", "What's confirmed and what's just a rumor?",
                "Let's check this GTA 6 claim."]
    if concept == "gta_moment":
        return ["This is the most GTA thing that's happened so far.", "Nobody expected this to work.",
                "Watch what happens next."]
    if concept == "gta_discovery":
        return _dedupe([headline or "Did you know GTA 6 does this?",
                        "Most players walk right past this.", "I tested this so you don't have to."])
    if concept == "streamer_story":
        return [f"{creator} wasn't ready for this." if creator else "This run went sideways fast.",
                f"How {creator}'s run fell apart." if creator else "The story behind this clip.",
                "Here's how it actually happened."]
    return ["Here's what Rockstar actually confirmed.", "What this means for GTA 6 players.",
            "Confirmed vs rumor: here's the difference."]


def gta_transformation(concept: str) -> str:
    return {
        "gta_moment": "Recreate or capture the situation in your own gameplay, then add your own commentary, "
                      "captions and pacing. Do not repost someone else's clip.",
        "gta_discovery": "Verify the claim yourself in-game. Record your own footage of the test and explain the "
                         "result, including if it doesn't work.",
        "streamer_story": "Narrate the story in your own words over your own gameplay. Use the streamer's clips only "
                          "after written permission is recorded in Footage rights, and credit them.",
        "news_explainer": "Explain what's confirmed (link the official source) over your own gameplay or "
                          "official media, labelling anything unconfirmed.",
    }.get(concept, "Add your own commentary and footage.")


def build(story: dict, ctx: dict) -> dict:
    """story: aggregated dict with business, category, topic_type, title, items (list), entities, verification."""
    business = story["business"]
    verification_label, tone = VERIFICATION.get(story["verification"], (story["verification"], "warn"))
    items = story["items"]
    sources = [{"title": i["title"], "url": i["url"], "source": i["source_name"], "reliability": i["reliability"],
                "published_at": i.get("published_at")} for i in items[:8]]
    summary = next((i["summary"] for i in items if i.get("summary")), story["title"])
    summary = summary if len(summary) <= 320 else summary[:320].rsplit(" ", 1)[0] + "…"
    ents = story.get("entities", {"people": [], "teams": [], "all": []})
    rec: dict = {"generator_note": GENERATOR_NOTE, "summary": summary, "sources": sources,
                 "verification_label": verification_label, "verification_tone": tone}

    if business == "on-the-road-media":
        topic, sport = story["topic_type"], story["category"]
        fmt = SPORT_FORMAT.get(topic, "fan_reaction")
        hooks = sports_hooks(topic, sport, ents.get("people", []), ents.get("teams", []), story["title"])
        subject = (ents.get("people") or ents.get("teams") or [None])[0]
        why = {
            "fan_debate": "Easy for any fan to have an opinion on; good for quick street answers.",
            "rivalry": "Rivalries make people pick a side on camera.",
            "unexpected_performance": "A surprise result people are already talking about.",
            "trade_signing": "Roster moves make every fan a GM: good for quick takes.",
            "controversy": "People already disagree about this, based on the reporting. Keep it fair; don't inflate it.",
            "press_conference": "Comments people can react to directly.",
            "game_day": "Tied to an upcoming or current game: short shelf life, high relevance.",
            "athlete_story": "A human story that works as short-form storytelling.",
            "injury": "Big news for fans of the team; cover respectfully.",
            "sensitive": "Serious news. Not suited to street-interview debate or punchy hooks.",
            "general": "In the news now; check whether there's a clear angle for fans.",
            "roundup": "A roundup or rankings piece: pick one angle and turn it into a quick question for fans.",
        }.get(topic, "")
        if story.get("major_event"):
            why += " Connected to a major event."
        interview = hooks[0] if hooks and fmt in ("street_interview", "fan_reaction", "game_day") else None
        broll = ["Fans in team gear outside the venue or a sports bar (your own footage)",
                 "Your own crowd and street shots for cutaways"]
        if subject:
            broll.append(f"Link {subject}'s official team/league posts in the caption. Don't re-upload them.")
        broll.append("Do not use broadcast or league highlight footage without a license.")
        recommended_platforms, platform_note = _platforms(fmt, ctx)
        difficulty, diff_note = DIFFICULTY[fmt]
        rec.update({
            "why_it_matters": why, "format": fmt, "format_label": CONTENT_TYPES["on-the-road-media"][fmt],
            "hooks": hooks, "interview_question": interview, "broll": broll, "subject": subject,
            "platforms": recommended_platforms, "platform_note": platform_note,
            "difficulty": difficulty, "difficulty_note": diff_note,
            "sport_label": SPORT_LABELS.get(sport, sport), "topic_label": TOPIC_LABELS.get(topic, topic),
            "sensitive": topic == "sensitive",
        })
    else:
        concept = story["topic_type"]
        fmt = concept if concept in CONTENT_TYPES["gta6-network"] else "news_explainer"
        creator = next((i["author"] for i in items if i.get("author") and i["reliability"] in ("creator", "community")), None) \
            or next((i["source_name"] for i in items if i["reliability"] == "creator"), None)
        speculative = story["verification"] in ("rumor_speculation", "reported_unconfirmed")
        recommended_platforms, platform_note = _platforms(fmt, ctx)
        difficulty, diff_note = DIFFICULTY[fmt]
        # A streamer story is about someone else's stream, so it always involves third-party footage.
        third_party = concept == "streamer_story" or (concept == "gta_moment" and any(
            i["reliability"] in ("creator", "community", "user") for i in items))
        dup = "high" if story["source_count"] >= 4 else "medium" if story["source_count"] >= 2 else "low"
        rec.update({
            "why_it_matters": "GTA 6 audience topic in current coverage." + (
                " It's speculation, so frame it as 'is this true?' rather than stating it." if speculative else ""),
            "format": fmt, "format_label": CONTENT_TYPES["gta6-network"][fmt],
            "account_slug": GTA_BRAND.get(concept, "gta6-discoveries"),
            "hooks": gta_hooks(concept, creator, speculative, story["title"],
                               # only official Rockstar material justifies "Rockstar confirmed"
                               confirmed=story["verification"] == "confirmed_official"),
            "creator": creator,
            "original_source": items[0]["source_name"] if items else "",
            "transformation": gta_transformation(concept), "subject": creator or (ents.get("all") or [None])[0],
            "platforms": recommended_platforms, "platform_note": platform_note,
            "difficulty": difficulty, "difficulty_note": diff_note,
            "footage_permission": "not_cleared",
            "footage_note": ("Third-party footage is never treated as cleared. Use your own gameplay, or record the "
                             "creator's written permission in Footage rights first.") if third_party else
                            "Plan uses your own gameplay capture; no third-party footage needed.",
            "uses_third_party_footage": third_party,
            "duplication_risk": dup,
            "duplication_note": {"high": "Widely covered already. You need a distinct angle.",
                                 "medium": "A few outlets have it. Move fast or add something new.",
                                 "low": "Little coverage found so far."}[dup],
            "topic_label": TOPIC_LABELS.get(concept, concept), "sensitive": False,
        })
        rec["interview_question"] = None
        rec["broll"] = ["Your own gameplay capture", "Official Rockstar media only where its terms allow (link the source)"]

    rec["urgency"] = _urgency(story)
    rec["handoff"] = handoff(story, rec, ctx)
    return rec


def _platforms(fmt: str, ctx: dict) -> tuple[list[str], str]:
    fc = ctx["formats"].get(fmt, {})
    best = fc.get("best_platform")
    if best:
        p, per1k, n = best
        rest = [x for x in DEFAULT_PLATFORMS.get(fmt, []) if x != p]
        return [p] + rest[:1], f"Your data: {per1k:.2f} follows per 1,000 views on this platform ({n} videos)."
    return DEFAULT_PLATFORMS.get(fmt, ["tiktok"]), "Default for this format (not enough of your own data yet)."


def _urgency(story: dict) -> str:
    topic = story["topic_type"]
    if topic in ("game_day", "unexpected_performance", "controversy", "press_conference"):
        return "high: post within 24 hours or it goes stale"
    if topic in ("trade_signing", "injury", "rivalry", "news_explainer", "gta_moment"):
        return "medium: best within 2 to 3 days"
    return "low: evergreen-ish, can wait a week"


def handoff(story: dict, rec: dict, ctx: dict) -> dict:
    """Structured inputs prepared for future agents (Scriptwriter, Clip Finder, Video Editor, Growth Optimizer).
    Those agents are still planned. This is data only."""
    return {
        "scriptwriter": {"hooks": rec.get("hooks", []), "interview_question": rec.get("interview_question"),
                         "key_facts": [{"text": s["title"], "source_url": s["url"]} for s in rec["sources"][:4]],
                         "verification": story["verification"],
                         "style": "natural, punchy, plain words, specific stakes, fast pacing; no invented facts"},
        "clip_finder": {"footage_allowed": "own footage only" if not rec.get("uses_third_party_footage")
                        else "third-party footage only with recorded written permission",
                        "search_terms": (story.get("entities", {}).get("all") or [])[:5]},
        "video_editor": {"format": rec["format"], "aspect": "9:16", "platforms": rec["platforms"],
                         "target_length": "15–30 s" if rec["format"] in ("fan_reaction", "street_interview",
                                                                         "gta_moment") else "30–60 s"},
        "growth_optimizer": {"format": rec["format"], "platform_note": rec.get("platform_note"),
                             "format_data": ctx["formats"].get(rec["format"])},
    }
