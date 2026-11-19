"""Deterministic text helpers: tokens, named phrases, fingerprints and similarity."""

from __future__ import annotations

import hashlib
import re

STOPWORDS = set("""
a an the and or but if then than so to of in on at by for with from into over under about after before as is are
was were be been being it its this that these those he she they them his her their our we you your i me my
not no yes do does did done has have had having will would can could should may might must just also more most
very new now up down out off vs via per amid says said say report reports reported sources source per according
how why what when where who which while still again back get gets got make makes made one two three first last
year years day days week weeks night tonight today game games season team teams player players star stars time
""".split())
# Leading words that are capitalised only because they start a headline.
HEADLINE_LEAD = set("""
the a an breaking watch report reports sources source why how what when where who which inside live video
analysis opinion exclusive update updates first last best worst top here
""".split())
LEAGUE_WORDS = {"NBA", "NFL", "MLB", "NHL", "UFC", "MMA", "WNBA", "NCAA", "ESPN", "CBS", "GTA", "PS5", "PC"}

_WORD = re.compile(r"[A-Za-z0-9][A-Za-z0-9'’.\-]*")


def tokens(text: str) -> list[str]:
    out = []
    for w in _WORD.findall(text or ""):
        w = w.lower().strip(".'’-")
        w = re.sub(r"['’]s$", "", w)
        if len(w) > 2 and w not in STOPWORDS and not w.isdigit():
            out.append(w)
    return out


def keyword_set(text: str) -> set[str]:
    return set(tokens(text))


def named_phrases(title: str) -> list[str]:
    """Runs of capitalised words, e.g. 'LeBron James', 'Golden State Warriors'. Order kept, no duplicates."""
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9'’.\-]*|[,:;!?\"()|]", title or "")
    phrases, cur = [], []

    def flush():
        if cur:
            phrase = " ".join(cur).strip(".'’")
            if phrase and phrase not in phrases:
                phrases.append(phrase)
        cur.clear()

    for i, w in enumerate(words):
        is_cap = w[:1].isupper() and w.upper() not in LEAGUE_WORDS
        if is_cap and not (i == 0 and w.lower() in HEADLINE_LEAD) and w.lower() not in STOPWORDS:
            cur.append(re.sub(r"['’]s$", "", w))
            if re.search(r"['’]s$", w):
                flush()
        else:
            flush()
    flush()
    return phrases


def fingerprint(title: str) -> str:
    return hashlib.sha1(" ".join(sorted(keyword_set(title))).encode()).hexdigest()[:16]


# Words from recurring templated headlines ("Today's top games to watch, best bets, odds: ...")
BOILERPLATE = set("""top games watch best bets odds picks today today's preview predictions prediction lines spread
                     parlay updated rankings power week mailbag takeaways grades tips guide""".split())
# Names shared by countless unrelated articles; not evidence of the same story.
GENERIC_NAMES = {"grand theft auto", "grand theft auto vi", "grand theft auto 6", "gta", "gta 6", "vice city",
                 "rockstar", "rockstar games", "ps5", "xbox", "playstation", "nfl draft", "power rankings",
                 "super bowl", "world series", "stanley cup", "nba finals"}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def related(kw_a: set, names_a: set, kw_b: set, names_b: set) -> bool:
    """Same story? Team or league names alone are weak evidence (many different articles mention the
    same team), so a shared person-style name (two or more words) or several shared names plus real
    keyword overlap is required."""
    shared = {n for n in names_a & names_b if n.lower() not in GENERIC_NAMES}
    strong = {n for n in shared if " " in n}
    sim = jaccard(kw_a - BOILERPLATE, kw_b - BOILERPLATE)
    if strong and sim >= 0.2:
        return True
    if len(shared) >= 2 and sim >= 0.3:
        return True
    return sim >= 0.5
