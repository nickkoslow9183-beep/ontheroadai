"""Rule-based classification of stories: sport, topic type, GTA concept, verification.

Everything here is keyword logic you can read and change. No AI model is used.
"""

from __future__ import annotations

import re

from .text import named_phrases

TEAMS = {
    "nba": """76ers sixers bucks bulls cavaliers cavs celtics clippers grizzlies hawks heat hornets jazz kings knicks
              lakers magic mavericks mavs nets nuggets pacers pelicans pistons raptors rockets spurs suns thunder
              timberwolves wolves blazers trail-blazers warriors wizards""",
    "nfl": """49ers niners bears bengals bills broncos browns buccaneers bucs cardinals chargers chiefs colts commanders
              cowboys dolphins eagles falcons giants jaguars jags jets lions packers panthers patriots pats raiders
              rams ravens saints seahawks steelers texans titans vikings""",
    "mlb": """angels astros athletics a's blue-jays braves brewers cardinals cubs diamondbacks d-backs dodgers giants
              guardians mariners marlins mets nationals orioles padres phillies pirates rangers rays reds red-sox
              rockies royals tigers twins white-sox yankees""",
    "nhl": """avalanche blackhawks blues bruins blue-jackets canadiens habs canucks capitals coyotes devils ducks flames
              flyers golden-knights hurricanes islanders jets kings kraken lightning maple-leafs leafs oilers panthers
              penguins predators rangers red-wings sabres senators sharks stars utah wild""",
}
SPORT_WORDS = {
    "nba": "nba basketball dunk three-pointer triple-double playoffs finals all-star",
    "nfl": "nfl football touchdown quarterback qb super-bowl kickoff interception sack",
    "mlb": "mlb baseball homer home-run pitcher inning world-series no-hitter walk-off",
    "nhl": "nhl hockey goalie stanley-cup hat-trick shutout power-play puck",
    "ufc": "ufc mma octagon fight fighter bout knockout submission title-fight pfl bellator",
}
SPORT_LABELS = {"nba": "NBA", "nfl": "NFL", "mlb": "MLB", "nhl": "NHL", "ufc": "UFC / MMA", "general": "General sports"}
TEAM_TO_SPORTS: dict[str, set[str]] = {}
for _sport, _names in TEAMS.items():
    for _n in _names.split():
        TEAM_TO_SPORTS.setdefault(_n.replace("-", " "), set()).add(_sport)

MAJOR_EVENTS = ["super bowl", "world series", "stanley cup", "nba finals", "all-star", "playoff", "playoffs", "draft",
                "trade deadline", "opening day", "march madness", "olympics", "thanksgiving", "christmas day",
                "conference finals", "wild card", "ufc 3"]

# Ordered: the first type with the most hits wins ties.
TOPIC_TYPES = [
    ("roundup", ["power rankings", "rankings", "takeaways", "things we learned", "what we learned", "winners and losers",
                 "grades", "mailbag", "mock draft", "storylines", "biggest questions", "things", "concerns for",
                 "observations", "overreactions", "trade candidates", "bold predictions"]),
    ("sensitive", ["arrested", "charged", "lawsuit", "dies", "died", "death", "passed away", "shooting", "domestic",
                   "assault", "hospitalized", "funeral", "crash victim"]),
    ("injury", ["injury", "injured", "torn", "acl", "achilles", "surgery", "sprain", "concussion", "ruled out",
                "out for season", "day-to-day", "fracture", "strain", "will miss"]),
    ("controversy", ["controversy", "controversial", "fined", "suspended", "ejected", "slams", "blasts", "rips",
                     "criticizes", "criticized", "apologizes", "backlash", "investigation", "officiating", "missed call",
                     "refs", "referee", "heated", "fires back", "calls out"]),
    ("trade_signing", ["trade", "traded", "trades", "signs", "signing", "agrees", "contract", "extension",
                       "free agent", "free agency", "waived", "released", "acquire", "acquires", "deal with", "max deal"]),
    ("unexpected_performance", ["career-high", "career high", "record", "historic", "first-ever", "first ever",
                                "stuns", "stunning", "upset", "comeback", "buzzer-beater", "buzzer beater", "walk-off",
                                "triple-double", "hat trick", "no-hitter", "knockout", "ko", "submission", "explodes",
                                "erupts", "dominates", "50-point", "40-point", "shocks", "unbeaten", "streak"]),
    ("rivalry", ["rivalry", "rival", "rivals", "feud", "beef", "rematch", "showdown", "trash talk", "trash-talk"]),
    ("press_conference", ["press conference", "presser", "told reporters", "postgame", "media day", "comments",
                          "said", "says", "speaks", "opens up", "interview", "responds"]),
    ("fan_debate", ["best", "worst", "overrated", "underrated", "goat", "mvp", "ranking", "rankings", "top 10",
                    "top 5", "debate", "better than", "should", "power rankings", "awards"]),
    ("game_day", ["tonight", "preview", "matchup", "odds", "prediction", "predictions", "picks", "kickoff",
                  "tip-off", "lineup", "how to watch", "live updates", "best bets", "games to watch", "top games",
                  "parlay", "spread", "start time", "tv channel"]),
    ("athlete_story", ["story", "journey", "emotional", "tribute", "inspired", "overcame", "legacy", "rookie",
                       "retire", "retires", "retirement", "milestone", "family"]),
]
TOPIC_LABELS = {
    "sensitive": "Sensitive news", "injury": "Injury news", "controversy": "Controversy",
    "trade_signing": "Trade / signing", "unexpected_performance": "Unexpected performance",
    "rivalry": "Rivalry", "press_conference": "Press conference / comments", "fan_debate": "Fan debate",
    "game_day": "Game-day storyline", "athlete_story": "Athlete story", "general": "General news",
    "roundup": "Roundup / rankings",
    "gta_moment": "GTA 6 moment", "gta_discovery": "GTA 6 discovery", "streamer_story": "Streamer story",
    "news_explainer": "GTA 6 news",
}

GTA_TERMS = ["gta 6", "gta vi", "gta6", "grand theft auto vi", "grand theft auto 6", "rockstar games", "rockstar",
             "vice city", "leonida", "gta online", "grand theft auto"]
GTA_CONCEPTS = [
    ("streamer_story", ["streamer", "stream", "streaming", "twitch", "kick", "youtuber", "reacts", "reaction",
                        "roleplay", "nopixel", "speedrun", "speedrunner", "content creator"]),
    ("gta_discovery", ["easter egg", "secret", "hidden", "discover", "discovered", "found", "mechanic", "mechanics",
                       "detail", "details", "map", "physics", "feature", "features", "challenge", "theory", "spotted",
                       "analysis", "breakdown", "how to", "guide", "tips", "where to find", "location", "locations",
                       "unlock", "you can", "has a", "has some", "animations", "song", "radio", "best"]),
    ("gta_moment", ["funny", "chaos", "chaotic", "fail", "wild", "moment", "moments", "clip", "insane", "glitch",
                    "bug", "hilarious", "viral"]),
    ("news_explainer", ["trailer", "release", "release date", "release day", "delay", "delayed", "price", "pre-order",
                        "preorder", "announce", "announces", "announced", "confirms", "confirmed", "official", "launch",
                        "ps5", "xbox", "pc", "promo", "partnership", "sales", "outage", "servers", "patch", "update"]),
]
GTA_BRAND = {"gta_moment": "gta6-moments", "gta_discovery": "gta6-discoveries", "news_explainer": "gta6-discoveries",
             "streamer_story": "gta-streamer-stories"}

SPECULATION = ["rumor", "rumour", "rumors", "leak", "leaked", "leaks", "insider", "allegedly", "reportedly",
               "could", "might", "may be", "theory", "theories", "speculation", "speculate", "fans think",
               "predict", "predicted", "wishlist", "possible", "potential", "unconfirmed", "hint", "hints",
               "expected to", "per sources", "sources say"]
CONFIRM_WORDS = ["confirms", "confirmed", "announces", "announced", "official", "officially", "reveals", "revealed"]

VERIFICATION = {
    "confirmed_official": ("Confirmed: official source", "good"),
    "multiple_reliable": ("Reported by 2+ independent reliable outlets", "good"),
    "single_reliable_report": ("Single reliable report", "ok"),
    "credible_report": ("Credible report, not officially confirmed", "ok"),
    "reported_unconfirmed": ("Reported, not confirmed (uses 'reportedly' / sources)", "warn"),
    "community_unverified": ("Community or creator post: unverified", "warn"),
    "rumor_speculation": ("Rumor / speculation", "bad"),
    "user_submitted": ("Added by you: verify before stating as fact", "warn"),
}
UNVERIFIED = {"reported_unconfirmed", "community_unverified", "rumor_speculation", "user_submitted"}


def _hits(text: str, words) -> int:
    t = f" {text.lower()} "
    n = 0
    for w in words:
        if re.search(rf"(?<![a-z0-9]){re.escape(w)}(?![a-z0-9])", t):
            n += 1
    return n


def detect_sport(text: str, hints: list[str]) -> str:
    """hints: source categories of the items (e.g. ['nba', 'nba', ''])."""
    score = {s: 0.0 for s in SPORT_WORDS}
    for h in hints:
        if h in score:
            score[h] += 3
    low = f" {text.lower()} "
    for team, sports in TEAM_TO_SPORTS.items():
        if re.search(rf"(?<![a-z]){re.escape(team)}(?![a-z])", low):
            for s in sports:
                score[s] += 1.5 / len(sports)
    for s, words in SPORT_WORDS.items():
        score[s] += _hits(text, [w.replace("-", " ") for w in words.split()] + words.split())
    best = max(score, key=score.get)
    return best if score[best] >= 1.5 else "general"


def _weighted_hits(text: str, words) -> float:
    """Multi-word phrases are stronger evidence than single words."""
    return sum((2 if " " in w else 1) for w in words if _hits(text, [w]))


def detect_topic(text: str, title: str | None = None) -> str:
    if _hits(text, dict(TOPIC_TYPES)["sensitive"]):
        return "sensitive"
    title = title if title is not None else text
    if ";" in title or re.match(r"^(\d+|two|three|four|five|six|seven|eight|nine|ten)\b", title.strip().lower()):
        return "roundup"
    best, best_hits = "general", 0.0
    for name, words in TOPIC_TYPES:
        if name == "sensitive":
            continue
        h = _weighted_hits(text, words)
        if h > best_hits:
            best, best_hits = name, h
    return best


def is_major_event(text: str) -> bool:
    return _hits(text, MAJOR_EVENTS) > 0


def is_gta(text: str) -> bool:
    return _hits(text, GTA_TERMS) > 0


def detect_gta_concept(text: str, title: str = "") -> str:
    """Headline words count double: the summary often mentions side details."""
    best, best_hits = "news_explainer", 0.0
    for name, words in GTA_CONCEPTS:
        h = _weighted_hits(text, words) + 2 * _weighted_hits(title, words)
        if h > best_hits:
            best, best_hits = name, h
    return best


def is_speculative(text: str) -> bool:
    return _hits(text, SPECULATION) > 0


POSITIONS = set("""QB RB WR TE OL OT OG C DE DT DL LB ILB OLB CB S FS SS K P PG SG SF PF G F OF IF SP RP DH LW RW D
                   GM HC OC DC Coach Rookie Star Guard Forward Center Quarterback Pitcher Goalie Fighter Champ""".split())
NOT_A_PERSON = set("""rankings ranking odds bets cup draft week bowl series final finals take things report mock picks
                      game games state st university college league conference championship trophy award awards stadium
                      arena hall fame edition version update season playoff playoffs team teams fantasy podcast show live
                      news today takeaways grades power top best worst nfl nba mlb nhl ufc espn cbs day night world
                      sports media agent tickets odds preview recap watch""".split())
LEADING_VERBS = set("""trade sign start bench draft cut keep buy sell watch fire hire pay extend release waive
                       stash drop add pick fade""".split())
MULTIWORD_TEAMS = {"red sox", "white sox", "blue jays", "maple leafs", "golden knights", "trail blazers",
                   "red wings", "blue jackets"}


def _team_in(phrase: str) -> str | None:
    words = phrase.split()
    for i in range(len(words) - 1):
        pair = f"{words[i]} {words[i + 1]}"
        if pair.lower() in MULTIWORD_TEAMS:
            return pair
    for w in words:
        base = _bare(re.sub(r"^[a-z]+-less\s*", "", w, flags=re.I))
        if base.lower() in TEAM_TO_SPORTS and base.lower() != "utah":
            return base
    return None


def _bare(word: str) -> str:
    return re.sub(r"(['’]s|['’])$", "", word)


def _person_in(phrase: str) -> str | None:
    words = [_bare(w) for w in phrase.replace("-less", "").split()]
    # drop leading team names, cities and positions: "Ravens CB Marlon Humphrey" -> "Marlon Humphrey"
    while words and (words[0] in POSITIONS or words[0].lower() in TEAM_TO_SPORTS or words[0].isupper()
                     or words[0].lower() in LEADING_VERBS):
        words.pop(0)
    if len(words) >= 3 and words[0].lower() not in NOT_A_PERSON and _team_in(" ".join(words[:2])):
        words = words[2:]
    if not 2 <= len(words) <= 3 or _team_in(" ".join(words)) == " ".join(words):
        return None
    if any(w.lower().strip(".") in NOT_A_PERSON or w.lower() in TEAM_TO_SPORTS or not w[:1].isupper()
           or any(ch.isdigit() for ch in w) for w in words):
        return None
    return " ".join(words)


def entities(titles: list[str]) -> dict:
    """People and teams named in headlines, most frequent first. Headline jargon
    ('Power Rankings', 'World Cup') and position abbreviations are filtered out."""
    counts: dict[str, int] = {}
    for t in titles:
        for p in named_phrases(t):
            counts[p] = counts.get(p, 0) + 1
    ordered = sorted(counts, key=lambda p: (-counts[p], titles[0].find(p) if p in titles[0] else 999))
    teams, people = [], []
    joined = " ".join(titles)
    for p in ordered:
        person = None if is_gta(p) else _person_in(p)
        if person and re.search(re.escape(person) + r"['’]s (notebook|mailbag|take|column|picks|mock|rankings|thoughts|"
                                r"takeaways|power rankings|podcast|show)", joined, re.I):
            person = None  # a columnist's byline, not the subject
        if person and person not in people:
            people.append(person)
        team = _team_in(p)
        if team and team not in teams:
            teams.append(team)
    return {"all": ordered, "teams": teams, "people": people}


def verify(items: list[dict], business: str) -> tuple[str, str]:
    """items: dicts with reliability, source_id, title, summary. Returns (label key, explanation)."""
    rel = [i["reliability"] for i in items]
    sources = {i["source_id"] for i in items}
    text = " ".join(f"{i['title']} {i.get('summary', '')}" for i in items)
    titles = " ".join(i["title"] for i in items)
    reliable_sources = {i["source_id"] for i in items if i["reliability"] in ("official", "major_publisher", "publisher")}
    if "official" in rel:
        return "confirmed_official", "At least one item comes from an official source."
    if set(rel) <= {"user"}:
        return "user_submitted", "Only your own submissions. Check a primary source before filming."
    if set(rel) <= {"community", "creator", "user"}:
        return "community_unverified", "Only community or creator posts. Nothing from an established outlet yet."
    speculative = is_speculative(titles)
    if business == "gta6-network":
        if speculative:
            return "rumor_speculation", "Headline language signals rumor, leak or speculation. Not confirmed by Rockstar."
        if _hits(titles, CONFIRM_WORDS):
            return "credible_report", ("A publisher reports it as confirmed or announced, but no official Rockstar "
                                       "source is linked. Check Rockstar's own channels.")
        return "single_reliable_report" if len(reliable_sources) <= 1 else "multiple_reliable", \
            f"Covered by {len(reliable_sources)} publisher source(s). Not an official confirmation."
    if speculative and len(reliable_sources) <= 1:
        return "reported_unconfirmed", "The report relies on unnamed sources or 'reportedly'. Say 'reportedly' on camera."
    if len(reliable_sources) >= 2:
        return "multiple_reliable", f"{len(reliable_sources)} independent reliable outlets report it."
    return "single_reliable_report", "One reliable outlet so far. Fine to discuss; attribute it to that outlet."
