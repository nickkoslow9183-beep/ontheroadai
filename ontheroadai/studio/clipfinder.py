"""Clip Finder: picks short-form moments from a timestamped transcript.

Deterministic and explainable: every candidate gets a 0-100 score built from readable factors,
plus a list of context problems. It works on what was *said*. It can't see the video, so for
gameplay without much speech it will find few or no moments, and it says so.

Payoff protection: a clip only ends at a natural end of a thought (sentence end or a clear
pause). If the next line continues the thought ("because...", "but...") or answers a question
the clip ends on, the clip is extended past the target length up to a hard maximum, rather
than cutting the payoff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..trends.text import jaccard, keyword_set, named_phrases


@dataclass(frozen=True)
class Preset:
    key: str
    label: str
    min_len: float
    ideal_low: float
    ideal_high: float
    max_len: float
    hard_max: float


PRESETS = {
    "podcast_clean": Preset("podcast_clean", "Clean podcast Reel", 15, 20, 45, 60, 90),
    "sports_story": Preset("sports_story", "Sports storytelling Reel", 20, 25, 50, 75, 100),
    "gta_gameplay": Preset("gta_gameplay", "GTA gameplay clip", 8, 12, 30, 45, 60),
}

FILLER_START = re.compile(r"^(um+|uh+|so+|yeah|okay|ok|alright|all right|and|but|like|anyway|well|you know|i mean)\b[, ]",
                          re.I)
CONTINUATION = re.compile(r"^(and|but|because|cause|'cause|so|which|then|or|plus|also)\b", re.I)
DANGLING = re.compile(r"^(he|she|they|it|that|this|those|these|him|her|them|there)\b", re.I)
TRANSITION = re.compile(r"\b(next segment|next topic|moving on|let's move on|switching gears|next up|new topic|"
                        r"let's switch|on to the next|that's it for)\b", re.I)
CONCLUSION = re.compile(r"\b(final answer|that's why|end of story|that's it|period|facts|that's my|i'm done|"
                        r"case closed|no question|can't argue|that settles it)\b", re.I)
BACKREF = re.compile(r"\b(like i said|as i said|like we said|earlier|we talked about|going back to|"
                     r"as you mentioned|what you said before)\b", re.I)
OPINION = ["i think", "overrated", "underrated", "best", "worst", "goat", "no way", "never", "always", "better than",
           "would you rather", "who would you", "facts", "cap", "nah", "hot take", "disagree", "i agree", "trust",
           "top five", "top 5", "number one", "honestly", "be honest", "hands down", "for sure", "no chance",
           "i don't care", "you're wrong", "that's crazy"]
EMOTION = ["haha", "lol", "[laughter]", "(laughs)", "crazy", "insane", "wild", "bro", "oh my god", "dude", "what?",
           "come on", "stop", "no way", "unbelievable", "let's go", "hilarious", "dead"]
STORY = ["one time", "i remember", "when i was", "true story", "back in", "growing up", "the first time", "story"]
GAME = ["draft", "pick", "round", "level", "tier", "first pick", "would you rather", "rank", "ranking", "lineup"]
CATEGORY_LABELS = {"debate": "Debate / hot take", "funny": "Funny / reaction", "story": "Story", "game": "Game / draft",
                   "general": "General"}

WEIGHTS = {"hook": 22, "opinion": 14, "energy": 10, "category_strength": 6, "pacing": 10, "dead_air": 8,
           "self_contained": 12, "payoff": 8, "length_fit": 5, "topical": 5}
assert sum(WEIGHTS.values()) == 100
FACTOR_LABELS = {"hook": "Opening hook", "opinion": "Debate / opinion", "energy": "Energy / humor / surprise",
                 "category_strength": "Story or game structure", "pacing": "Pacing",
                 "dead_air": "Dead air", "self_contained": "Makes sense on its own", "payoff": "Complete payoff",
                 "length_fit": "Length fit", "topical": "Topical (Trend Scout)"}


def _hits(text: str, words) -> int:
    t = f" {text.lower()} "
    return sum(1 for w in words if (f" {w} " in t) or (w.startswith(("[", "(")) and w in t) or
               (w.endswith("?") and w in t))


def _ends_thought(seg: dict, next_seg: dict | None) -> bool:
    if next_seg is None:
        return True
    gap = next_seg["start"] - seg["end"]
    if CONTINUATION.match(next_seg["text"].strip()):
        return False
    if seg["text"].rstrip().endswith("?") and gap < 2.0:
        return False  # a question is followed by its answer: keep going for the payoff
    return seg["text"].rstrip().endswith((".", "!", "?", '"')) or gap >= 0.8


def _strong(sentence: str) -> bool:
    s = sentence.strip()
    return s.endswith("?") or _hits(s, OPINION) > 0 or _hits(s, EMOTION) > 0 or s.endswith("!")


def _hook_text(sentence: str, max_words: int = 12) -> str:
    words = sentence.strip().split()
    words = words[1:] if words and FILLER_START.match(sentence.strip() + " ") and len(words) > 4 else words
    text = " ".join(words[:max_words]).strip(" ,")
    return text[:1].upper() + text[1:] if text else ""


def _length_points(dur: float, p: Preset, preferred: tuple[float, float] | None) -> tuple[int, str]:
    low, high = preferred or (p.ideal_low, p.ideal_high)
    src = "your analytics" if preferred else f"the {p.label} preset"
    if low <= dur <= high:
        return 10, f"{dur:.0f}s is inside the {low:.0f}–{high:.0f}s range from {src}."
    off = (low - dur) if dur < low else (dur - high)
    return max(2, int(10 - off / 4)), f"{dur:.0f}s is outside the {low:.0f}–{high:.0f}s range from {src} (by {off:.0f}s)."


def score_window(segs: list[dict], p: Preset, silences: list[tuple[float, float]], trend: list[dict],
                 preferred: tuple[float, float] | None, next_seg: dict | None) -> dict:
    start, end = segs[0]["start"], segs[-1]["end"]
    dur = end - start
    text = " ".join(s["text"] for s in segs)
    first = segs[0]["text"].strip()
    f: dict[str, tuple[int, str]] = {}
    flags: list[str] = []

    # hook: the first sentence the viewer hears
    hp = 5
    notes = []
    if first.endswith("?"):
        hp += 3
        notes.append("opens with a question")
    if _hits(first, OPINION):
        hp += 2
        notes.append("opens with an opinion")
    if _hits(first, EMOTION) or first.endswith("!"):
        hp += 1
        notes.append("opens with energy")
    if len(first.split()) <= 14:
        hp += 1
    if FILLER_START.match(first + " "):
        hp -= 3
        notes.append("starts with a filler word")
    if not notes:
        notes.append("first line is a plain statement")
    f["hook"] = (max(0, min(10, hp)), "; ".join(notes).capitalize() + f": “{first[:80]}”")

    # time until the first strong line (setup length)
    first_strong = next((s for s in segs if _strong(s["text"])), None)
    setup = (first_strong["start"] - start) if first_strong else dur
    if setup > 6:
        flags.append(f"Slow start: the first strong line comes {setup:.0f}s in. Consider starting later.")
        f["hook"] = (max(0, f["hook"][0] - 2), f["hook"][1] + f" Setup lasts {setup:.0f}s.")

    op = _hits(text, OPINION)
    f["opinion"] = (min(10, op * 3), f"{op} debate/opinion marker(s).")
    en = _hits(text, EMOTION) + text.count("!")
    f["energy"] = (min(10, en * 2 + 2) if en else 2, f"{en} energy/humor/surprise marker(s).")
    st, gm = _hits(text, STORY), _hits(text, GAME)
    f["category_strength"] = (min(10, 3 + 3 * max(st, gm)), f"{st} story and {gm} game/draft marker(s).")

    words = len(text.split())
    wps = words / dur if dur else 0
    if 2.0 <= wps <= 3.6:
        pc = (10, f"{wps:.1f} words/second: lively, natural pace.")
    elif wps < 2.0:
        pc = (max(2, int(10 - (2.0 - wps) * 8)), f"{wps:.1f} words/second: slow; may lose viewers.")
    else:
        pc = (7, f"{wps:.1f} words/second: very fast; check captions stay readable.")
    f["pacing"] = pc

    gaps = sum(max(0, b["start"] - a["end"]) for a, b in zip(segs, segs[1:]) if b["start"] - a["end"] > 1.0)
    sil = sum(min(e, end) - max(s, start) for s, e in silences if e > start and s < end and (e - s) >= 1.0)
    dead = max(gaps, sil)
    f["dead_air"] = (max(0, 10 - int(dead * 2)), f"{dead:.1f}s of pauses longer than 1s." +
                     (" 'Tighten pauses' can remove them." if dead >= 1.5 else ""))
    if dead >= 3:
        flags.append(f"{dead:.0f}s of dead air inside the clip.")

    sc = 10
    if DANGLING.match(first) or CONTINUATION.match(first):
        sc -= 4
        flags.append(f"Starts with “{first.split()[0]}”, which may refer to something said earlier.")
    if BACKREF.search(text):
        sc -= 3
        flags.append("Refers back to earlier in the conversation (“like I said”, “earlier”…).")
    if dur < p.min_len:
        sc -= 2
    f["self_contained"] = (max(0, sc), "Opens cleanly and doesn't rely on earlier context." if sc == 10 else
                           "May need context from earlier in the recording.")

    last = segs[-1]["text"].rstrip()
    complete = last.endswith((".", "!", '"')) or (next_seg is None)
    if last.endswith("?") and next_seg is not None:
        complete = False
        flags.append("Ends on a question; the answer may be cut off.")
    if next_seg is not None and CONTINUATION.match(next_seg["text"].strip()):
        complete = False
        flags.append(f"The next line continues the thought (“{next_seg['text'][:40]}…”).")
    concluded = bool(CONCLUSION.search(last))
    f["payoff"] = ((10 if concluded else 8) if complete else 3,
                   ("Ends on a conclusion (“" + last[-50:] + "”)." if concluded else "Ends at the end of a thought.")
                   if complete else "May end before the payoff.")

    f["length_fit"] = _length_points(dur, p, preferred)

    kw = keyword_set(text)
    # compare individual name words, so "Steph" in conversation matches "Steph Curry" in a headline
    name_words = {w.lower() for n in named_phrases(text) for w in n.split() if len(w) > 2}
    best, best_sim = None, 0.0
    for t in trend:
        t_words = {w.lower() for n in t["names"] for w in n.split() if len(w) > 2}
        sim = jaccard(kw, t["kw"]) + 0.3 * len(name_words & t_words)
        if sim > best_sim:
            best, best_sim = t, sim
    if best and best_sim >= 0.12:
        f["topical"] = (min(10, 5 + int(best_sim * 20)), f"Matches researched story “{best['title'][:60]}”.")
    else:
        f["topical"] = (3 if trend else 5, "No matching Trend Scout story." if trend else
                        "No recent Trend Scout research to compare with (neutral).")
        best = None

    cats = {"debate": op, "funny": en, "story": st, "game": gm}
    category = max(cats, key=cats.get) if max(cats.values()) > 0 else "general"

    factors, total = [], 0.0
    for key, w in WEIGHTS.items():
        val, why = f[key]
        pts = val / 10 * w
        total += pts
        factors.append({"key": key, "label": FACTOR_LABELS[key], "input": val, "weight": w, "points": round(pts, 1),
                        "explanation": why})
    hook_line = first_strong["text"] if first_strong and (first_strong["start"] - start) <= 6 else first
    return {"start": round(start, 3), "end": round(end, 3), "duration": round(dur, 2), "score": int(round(total)),
            "factors": factors, "flags": flags, "category": category, "excerpt": text,
            "hook": _hook_text(hook_line), "title": _title(category, hook_line),
            "trend_story_id": best["id"] if best else None}


def _title(category: str, line: str) -> str:
    return _hook_text(line, 14).rstrip(".!")


def find_candidates(segments: list[dict], preset: str = "podcast_clean", silences=None, trend=None,
                    preferred: tuple[float, float] | None = None, max_candidates: int = 8,
                    min_score: int = 35) -> dict:
    """Return {"candidates": [...], "notes": [...]}."""
    p = PRESETS[preset]
    silences = silences or []
    trend = trend or []
    segs = [s for s in segments if s.get("text", "").strip()]
    notes = []
    if not segs:
        return {"candidates": [], "notes": ["The transcript has no speech, so there's nothing to find clips in. "
                                            "Add clips by hand in the Clip editor."]}
    total = segs[-1]["end"] - segs[0]["start"]
    speech = sum(s["end"] - s["start"] for s in segs)
    if preset == "gta_gameplay" or (total and speech / total < 0.25):
        notes.append("Clip finding uses speech only. Visual moments (chases, crashes, glitches) without talking "
                     "are not detected. Mark those by hand.")
    windows = []
    for i in range(len(segs)):
        if segs[i]["text"].strip() and CONTINUATION.match(segs[i]["text"].strip()) and i > 0:
            continue  # never start mid-thought
        for j in range(i, len(segs)):
            if j > i and TRANSITION.search(segs[j]["text"]):
                break  # never run a clip across a change of topic
            dur = segs[j]["end"] - segs[i]["start"]
            if dur > p.hard_max:
                break
            nxt = segs[j + 1] if j + 1 < len(segs) else None
            if dur < p.min_len:
                continue
            if not _ends_thought(segs[j], nxt):
                continue
            w = score_window(segs[i:j + 1], p, silences, trend, preferred, nxt)
            if dur > p.max_len:
                w["flags"].append(f"Runs {dur:.0f}s (over the usual {p.max_len:.0f}s) so the thought isn't cut "
                                  "off before its payoff.")
            windows.append(w)
            if dur >= p.ideal_high:
                break
    if not windows:
        # the conversation never reaches a clean ending within limits: offer the best partial window, flagged
        notes.append("No clean start/end points within the length limits. Adjust clip times by hand.")
        return {"candidates": [], "notes": notes}
    windows.sort(key=lambda w: (-w["score"], w["start"]))
    chosen: list[dict] = []
    for w in windows:
        if len(chosen) >= max_candidates:
            break
        if w["score"] < min_score and len(chosen) >= 3:
            break
        overlap = any(min(w["end"], c["end"]) - max(w["start"], c["start"]) > 0.3 * min(w["duration"], c["duration"])
                      for c in chosen)
        similar = any(jaccard(keyword_set(w["excerpt"]), keyword_set(c["excerpt"])) >= 0.6 for c in chosen)
        if not overlap and not similar:
            chosen.append(w)
    chosen.sort(key=lambda w: -w["score"])
    return {"candidates": chosen, "notes": notes}

