"""Turn stored snapshots into retention, conversion, comparison and recommendations.

Principles
- Use each video's most recent snapshot.
- A missing input means the derived number is missing too (shown as "—").
- Retention is judged against videos of similar length, never in absolute
  seconds: 7 s average watch on a 15 s video (47%) is very different from 7 s
  on a 60 s video (12%).
- Every group comparison reports its sample size and a plain-language
  confidence label. Nothing here is a statistical significance test.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from datetime import date
from statistics import median

from ..constants import CONTENT_TYPES, PLATFORMS

LENGTH_BUCKETS = [(15, "≤15 s"), (30, "16–30 s"), (60, "31–60 s"), (90, "61–90 s"), (180, "91–180 s"),
                  (float("inf"), "over 3 min")]
MIN_PEERS = 3            # similar-length videos needed before judging retention
MIN_GROUP = 3            # videos per group before comparing groups
EARLY_DROP_RELATIVE = 0.75   # watched <= 75% as much as similar-length peers
STRONG_HOLD_RELATIVE = 1.25
SKIP_MARGIN = 10.0       # percentage points above peers' median skip rate
LOW_ABSOLUTE_RATIO = 0.25
TIME_SLOTS = [(5, 12, "morning (5–12)"), (12, 17, "afternoon (12–17)"), (17, 22, "evening (17–22)"),
              (22, 29, "late night (22–5)")]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

CAUTION = ("Based on your stored data only. This is an observation, not a controlled experiment. "
           "Treat it as something to test, not a proven cause.")


def sample_label(n: int) -> str:
    if n < 5:
        return "Anecdotal: very small sample"
    if n < 15:
        return "Directional: small sample"
    if n < 30:
        return "Moderate sample"
    return "Larger sample (still not a controlled test)"


def length_bucket(seconds) -> str | None:
    if not seconds:
        return None
    for upper, label in LENGTH_BUCKETS:
        if seconds <= upper:
            return label
    return None


def _ratio(a, b, scale=1.0):
    if a is None or b in (None, 0):
        return None
    return a / b * scale


def _med(values):
    vals = [v for v in values if v is not None]
    return median(vals) if vals else None


def format_label(slug: str, fmt: str) -> str:
    if not fmt:
        return "Untagged"
    return CONTENT_TYPES.get(slug, {}).get(fmt, fmt)


def time_slot(hour: int | None) -> str | None:
    if hour is None:
        return None
    h = hour if hour >= 5 else hour + 24
    for lo, hi, label in TIME_SLOTS:
        if lo <= h < hi:
            return label
    return None


def derive(v: dict, slug: str) -> dict:
    """Add calculated metrics. Each is None when any required input is missing."""
    views = v.get("views")
    parts = [v.get(k) for k in ("likes", "comments", "shares", "saves")]
    known = [p for p in parts if p is not None]
    if views and known:
        v["engagement_rate"] = round(sum(known) / views * 100, 2)
        v["engagement_partial"] = len(known) < 4
    else:
        v["engagement_rate"], v["engagement_partial"] = None, False
    per1k = _ratio(v.get("follows_gained"), views, 1000)
    v["follows_per_1k"] = round(per1k, 2) if per1k is not None else None
    wr = _ratio(v.get("avg_watch_seconds"), v.get("duration_seconds"))
    v["watch_ratio"] = round(wr, 3) if wr is not None else None
    v["length_bucket"] = length_bucket(v.get("duration_seconds"))
    v["format_label"] = format_label(slug, v.get("format") or "")
    v["platform_label"] = PLATFORMS.get(v.get("platform"), v.get("platform"))
    pub = v.get("published_at") or ""
    v["publish_date"] = pub[:10] or None
    v["publish_hour"] = int(pub[11:13]) if len(pub) >= 13 else None
    v["time_slot"] = time_slot(v["publish_hour"])
    v["weekday"] = None
    v["days_since_publish"] = None
    if v["publish_date"]:
        try:
            d = date.fromisoformat(v["publish_date"])
            v["weekday"] = WEEKDAYS[d.weekday()]
            if v.get("captured_on"):
                v["days_since_publish"] = (date.fromisoformat(v["captured_on"]) - d).days
        except ValueError:
            pass
    return v


def load_videos(conn: sqlite3.Connection, business_slug: str, filters: dict | None = None) -> list[dict]:
    f = filters or {}
    sql = (
        "SELECT v.*, a.platform, a.brand_id, br.name AS brand_name, i.title AS idea_title, "
        "s.id AS snapshot_id, s.captured_on, s.views, s.reach, s.likes, s.comments, s.shares, s.saves, "
        "s.follows_gained, s.avg_watch_seconds, s.completion_rate, s.skip_rate, "
        "(SELECT COUNT(*) FROM video_snapshots x WHERE x.video_id = v.id) AS snapshot_count "
        "FROM videos v JOIN accounts a ON a.id = v.account_id JOIN brands br ON br.id = a.brand_id "
        "JOIN businesses b ON b.id = br.business_id LEFT JOIN ideas i ON i.id = v.idea_id "
        "LEFT JOIN video_snapshots s ON s.id = (SELECT id FROM video_snapshots y WHERE y.video_id = v.id "
        "ORDER BY y.captured_on DESC, y.id DESC LIMIT 1) WHERE b.slug = ?")
    params: list = [business_slug]
    if f.get("brand_id"):
        sql += " AND a.brand_id = ?"
        params.append(int(f["brand_id"]))
    if f.get("platform"):
        sql += " AND a.platform = ?"
        params.append(f["platform"])
    if f.get("format"):
        sql += " AND v.format = ?"
        params.append("" if f["format"] == "__untagged__" else f["format"])
    if f.get("date_from"):
        sql += " AND substr(v.published_at, 1, 10) >= ?"
        params.append(f["date_from"])
    if f.get("date_to"):
        sql += " AND substr(v.published_at, 1, 10) <= ?"
        params.append(f["date_to"])
    sql += " ORDER BY v.published_at DESC, v.id DESC"
    videos = [derive(dict(r), business_slug) for r in conn.execute(sql, params)]
    if f.get("length"):
        videos = [v for v in videos if v["length_bucket"] == f["length"]]
    annotate_retention(videos)
    return videos


# ---- retention ------------------------------------------------------------------

def _peers(videos, v, metric):
    """Similar-length videos (same platform if enough, else any platform) with `metric` known."""
    if not v["length_bucket"]:
        return [], None
    same = [o for o in videos if o is not v and o["length_bucket"] == v["length_bucket"] and o.get(metric) is not None]
    same_platform = [o for o in same if o["platform"] == v["platform"]]
    if len(same_platform) >= MIN_PEERS:
        return same_platform, f"{len(same_platform)} other {v['length_bucket']} {v['platform_label']} videos"
    if len(same) >= MIN_PEERS:
        return same, f"{len(same)} other {v['length_bucket']} videos (mixed platforms)"
    return [], None


def annotate_retention(videos: list[dict]) -> None:
    for v in videos:
        v.update(peer_watch_ratio=None, relative_retention=None, peer_skip_rate=None,
                 retention_flag=None, retention_note="", peer_group=None)
        notes = []
        if v["watch_ratio"] is not None:
            peers, desc = _peers(videos, v, "watch_ratio")
            if peers:
                pm = median(p["watch_ratio"] for p in peers)
                v["peer_watch_ratio"], v["peer_group"] = round(pm, 3), desc
                v["relative_retention"] = round(v["watch_ratio"] / pm, 2) if pm else None
        if v.get("skip_rate") is not None:
            sp, sdesc = _peers(videos, v, "skip_rate")
            if sp:
                v["peer_skip_rate"] = round(median(p["skip_rate"] for p in sp), 1)
                v["peer_group"] = v["peer_group"] or sdesc
        rel, skip, pskip = v["relative_retention"], v.get("skip_rate"), v["peer_skip_rate"]
        if pskip is not None and skip >= pskip + SKIP_MARGIN:
            v["retention_flag"] = "early_drop"
            notes.append(f"skip rate {skip:.0f}% vs {pskip:.0f}% for similar-length videos")
        if rel is not None and rel <= EARLY_DROP_RELATIVE:
            v["retention_flag"] = "early_drop"
            notes.append(f"watched {v['watch_ratio']:.0%} of its length on average vs {v['peer_watch_ratio']:.0%} "
                         f"for similar-length videos")
        if v["retention_flag"] is None:
            if (rel is not None and rel >= STRONG_HOLD_RELATIVE) or (pskip is not None and skip <= pskip - SKIP_MARGIN):
                v["retention_flag"] = "strong_hold"
                if rel is not None:
                    notes.append(f"watched {v['watch_ratio']:.0%} vs {v['peer_watch_ratio']:.0%} for similar-length videos")
                if pskip is not None and skip <= pskip - SKIP_MARGIN:
                    notes.append(f"skip rate {skip:.0f}% vs {pskip:.0f}%")
            elif rel is not None or pskip is not None:
                v["retention_flag"] = "typical"
            elif v["watch_ratio"] is not None and v["watch_ratio"] < LOW_ABSOLUTE_RATIO:
                v["retention_flag"] = "low_no_peers"
                notes.append(f"watched only {v['watch_ratio']:.0%} of its length on average; not enough "
                             "similar-length videos to compare yet")
        v["retention_note"] = "; ".join(notes)


def _group(videos, keyfn):
    groups = defaultdict(list)
    for v in videos:
        k = keyfn(v)
        if k is not None:
            groups[k].append(v)
    return groups


def retention_report(videos: list[dict]) -> dict:
    with_ratio = [v for v in videos if v["watch_ratio"] is not None]
    by_format = []
    for label, vs in _group(videos, lambda v: v["format_label"]).items():
        r = [v["watch_ratio"] for v in vs if v["watch_ratio"] is not None]
        rel = [v["relative_retention"] for v in vs if v["relative_retention"] is not None]
        comp = [v["completion_rate"] for v in vs if v.get("completion_rate") is not None]
        skip = [v["skip_rate"] for v in vs if v.get("skip_rate") is not None]
        if not (r or comp or skip):
            continue
        by_format.append({
            "label": label, "n": len(vs), "n_ratio": len(r), "median_watch_ratio": _med(r),
            "n_relative": len(rel), "median_relative": _med(rel), "n_completion": len(comp),
            "median_completion": _med(comp), "n_skip": len(skip), "median_skip": _med(skip),
            "sample": sample_label(max(len(r), len(comp), len(skip))),
        })
    by_format.sort(key=lambda g: (g["median_relative"] is None, -(g["median_relative"] or 0),
                                  -(g["median_watch_ratio"] or 0)))
    by_length = []
    for upper, label in LENGTH_BUCKETS:
        vs = [v for v in with_ratio if v["length_bucket"] == label]
        if vs:
            by_length.append({"label": label, "n": len(vs),
                              "median_watch_ratio": median(v["watch_ratio"] for v in vs),
                              "median_avg_watch": median(v["avg_watch_seconds"] for v in vs)})
    early = sorted([v for v in videos if v["retention_flag"] in ("early_drop", "low_no_peers")],
                   key=lambda v: (v["relative_retention"] if v["relative_retention"] is not None else 9))
    strong = sorted([v for v in videos if v["retention_flag"] == "strong_hold"],
                    key=lambda v: -(v["relative_retention"] or 0))
    return {"by_format": by_format, "by_length": by_length, "early": early, "strong": strong,
            "assessable": sum(1 for v in videos if v["retention_flag"] not in (None, "low_no_peers")),
            "with_ratio": len(with_ratio), "missing_length": sum(1 for v in videos if not v.get("duration_seconds")),
            "missing_watch": sum(1 for v in videos if v.get("avg_watch_seconds") is None)}


# ---- follower conversion ---------------------------------------------------------

def _conversion_groups(videos, keyfn):
    rows = []
    for label, vs in _group(videos, keyfn).items():
        usable = [v for v in vs if v.get("views") and v.get("follows_gained") is not None]
        if not usable:
            continue
        views = sum(v["views"] for v in usable)
        follows = sum(v["follows_gained"] for v in usable)
        rows.append({"label": label, "n": len(usable), "views": views, "follows": follows,
                     "pooled_per_1k": round(follows / views * 1000, 2) if views else None,
                     "median_per_1k": round(median(v["follows_per_1k"] for v in usable), 2),
                     "sample": sample_label(len(usable))})
    rows.sort(key=lambda r: -(r["pooled_per_1k"] or 0))
    return rows


def conversion_report(videos: list[dict], min_views: int = 100) -> dict:
    usable = [v for v in videos if v.get("views") and v.get("follows_gained") is not None]
    top = sorted([v for v in usable if v["views"] >= min_views], key=lambda v: -v["follows_per_1k"])[:5]
    return {"by_format": _conversion_groups(videos, lambda v: v["format_label"]),
            "by_platform": _conversion_groups(videos, lambda v: v["platform_label"]),
            "by_length": _conversion_groups(videos, lambda v: v["length_bucket"]),
            "top": top, "usable": len(usable), "min_views": min_views,
            "missing_follows": sum(1 for v in videos if v.get("follows_gained") is None)}


def kpis(videos: list[dict]) -> dict:
    with_views = [v for v in videos if v.get("views") is not None]
    conv = [v for v in videos if v.get("views") and v.get("follows_gained") is not None]
    views = sum(v["views"] for v in with_views)
    cviews = sum(v["views"] for v in conv)
    return {
        "videos": len(videos), "with_data": sum(1 for v in videos if v.get("snapshot_id")),
        "views": views if with_views else None,
        "follows": sum(v["follows_gained"] for v in videos if v.get("follows_gained") is not None)
        if any(v.get("follows_gained") is not None for v in videos) else None,
        "per_1k": round(sum(v["follows_gained"] for v in conv) / cviews * 1000, 2) if cviews else None,
        "per_1k_n": len(conv),
        "median_watch_ratio": _med([v["watch_ratio"] for v in videos]),
        "watch_n": sum(1 for v in videos if v["watch_ratio"] is not None),
    }


# ---- comparison ----------------------------------------------------------------

COMPARE_ROWS = [
    # key, label, higher_is_better, kind
    ("views", "Views", True, "count"), ("reach", "Reach", True, "count"),
    ("follows_gained", "Followers gained", True, "count"), ("follows_per_1k", "Follows per 1,000 views", True, "num"),
    ("engagement_rate", "Engagement rate %", True, "num"), ("likes", "Likes", True, "count"),
    ("comments", "Comments", True, "count"), ("shares", "Shares", True, "count"), ("saves", "Saves", True, "count"),
    ("avg_watch_seconds", "Average watch time (s)", True, "num"), ("duration_seconds", "Video length (s)", None, "num"),
    ("watch_ratio", "Share of length watched", True, "pct"),
    ("relative_retention", "Retention vs similar-length videos", True, "x"),
    ("completion_rate", "Completion rate %", True, "num"), ("skip_rate", "Skip rate %", False, "num"),
]


def _fmt(val, kind):
    if val is None:
        return "—"
    if kind == "count":
        return f"{val:,}"
    if kind == "pct":
        return f"{val:.0%}"
    if kind == "x":
        return f"{val:.2f}×"
    if kind == "rate":
        return f"{val:g}%"
    return f"{val:g}"


def compare(conn: sqlite3.Connection, business_slug: str, ids: list[int]) -> dict:
    ids = list(dict.fromkeys(int(i) for i in ids))
    if len(ids) < 2:
        raise ValueError("Select at least two videos to compare.")
    if len(ids) > 6:
        raise ValueError("Compare at most six videos at a time.")
    pool = load_videos(conn, business_slug)  # full set, so retention peers are the same as elsewhere
    by_id = {v["id"]: v for v in pool}
    missing = [i for i in ids if i not in by_id]
    if missing:
        raise ValueError("Some selected videos were not found in this business.")
    vids = [by_id[i] for i in ids]
    for idx, v in enumerate(vids):
        v["letter"] = chr(65 + idx)
        v["display_title"] = v["title"] or v["post_url"] or f"Video #{v['id']}"

    rows = []
    for key, label, higher, kind in COMPARE_ROWS:
        vals = [v.get(key) for v in vids]
        known = [x for x in vals if x is not None]
        best = None
        if higher is not None and len(known) >= 2 and len(set(known)) > 1:
            target = max(known) if higher else min(known)
            best = [i for i, x in enumerate(vals) if x == target]
        rows.append({"label": label, "values": [_fmt(x, kind) for x in vals], "best": best or [],
                     "missing": len(known) < len(vals)})
    context = [
        ("Platform", [v["platform_label"] for v in vids]), ("Account", [v["brand_name"] for v in vids]),
        ("Format", [v["format_label"] for v in vids]), ("Topic", [v.get("topic") or "—" for v in vids]),
        ("Published", [(v.get("published_at") or "—").replace("T", " ") for v in vids]),
        ("Day / time slot", [f"{v['weekday'] or '—'} · {v['time_slot'] or 'time unknown'}" for v in vids]),
        ("Numbers as of", [f"{v.get('captured_on') or '—'}"
                           + (f" ({v['days_since_publish']} days after posting)" if v["days_since_publish"] is not None else "")
                           for v in vids]),
    ]

    observed, possible, caveats = [], [], []

    def lead(key, label, kind, higher=True):
        have = [v for v in vids if v.get(key) is not None]
        if len(have) < 2:
            return None
        hi = max(have, key=lambda v: v[key]) if higher else min(have, key=lambda v: v[key])
        lo = min(have, key=lambda v: v[key]) if higher else max(have, key=lambda v: v[key])
        if hi[key] == lo[key]:
            return None
        text = f"{hi['letter']} had the {'highest' if higher else 'lowest'} {label} ({_fmt(hi[key], kind)}) and " \
               f"{lo['letter']} the {'lowest' if higher else 'highest'} ({_fmt(lo[key], kind)})"
        if higher and lo[key] and kind not in ("pct", "rate"):
            text += f", {hi[key] / lo[key]:.1f}× higher"
        observed.append(text + ".")
        return hi, lo

    conv = lead("follows_per_1k", "follows per 1,000 views", "num")
    lead("views", "views", "count")
    ret = lead("watch_ratio", "share of the video watched", "pct")
    lead("completion_rate", "completion rate", "rate")
    skip = lead("skip_rate", "skip rate", "rate", higher=False)
    lead("engagement_rate", "engagement rate", "rate")

    lengths = [v for v in vids if v.get("duration_seconds")]
    if len(lengths) >= 2:
        short, long_ = min(lengths, key=lambda v: v["duration_seconds"]), max(lengths, key=lambda v: v["duration_seconds"])
        if long_["duration_seconds"] >= short["duration_seconds"] * 1.2:
            observed.append(f"{short['letter']} is {short['duration_seconds']:g} s long; {long_['letter']} is "
                            f"{long_['duration_seconds']:g} s.")
            if ret and ret[0] is short:
                possible.append(f"The shorter length of {short['letter']} may have helped it keep viewers. Not proven: "
                                "a shorter video is naturally watched for a larger share of its length. Test the same "
                                "format at both lengths.")
    formats = {v["format_label"] for v in vids}
    if len(formats) > 1:
        observed.append("They are different formats: " + ", ".join(f"{v['letter']} = {v['format_label']}" for v in vids) + ".")
        if conv and conv[0]["format_label"] != conv[1]["format_label"]:
            possible.append(f"The {conv[0]['format_label']} format may convert viewers to followers better than "
                            f"{conv[1]['format_label']}. Not proven from a few videos: check the format table, "
                            "which uses all your videos.")
    if skip and skip[1].get("skip_rate") is not None:
        possible.append(f"{skip[1]['letter']}'s opening (first 1–2 seconds) may be weaker: more viewers skipped it. "
                        "The hook, thumbnail frame and first line are all candidates. Not measured directly.")
    slots = {(v["weekday"], v["time_slot"]) for v in vids}
    if len(slots) > 1 and all(v["time_slot"] for v in vids):
        possible.append("They were posted at different days/times, which can change initial distribution.")
    possible.append("Topic, hook wording, trend timing, the people on screen and how the platform's algorithm "
                    "distributed each video are not measured here and could explain the difference.")

    if len({v["platform"] for v in vids}) > 1:
        caveats.append("Different platforms measure views, watch time and follows differently. Compare within one "
                       "platform when you can.")
    ages = [v["days_since_publish"] for v in vids if v["days_since_publish"] is not None]
    if len(ages) >= 2 and max(ages) - min(ages) > 2:
        caveats.append(f"The numbers were captured at different ages ({min(ages)} to {max(ages)} days after posting). "
                       "Older posts have had more time to collect views and follows.")
    if len(ages) < len(vids):
        caveats.append("Some videos have no publish date, so it is unknown how old they were when measured.")
    if any(r["missing"] for r in rows):
        caveats.append("Some metrics are missing for some videos (shown as —). Missing values were not estimated.")
    if any(v.get("engagement_partial") for v in vids):
        caveats.append("Engagement rate for some videos uses only the interactions present in the export.")
    caveats.append(f"This compares {len(vids)} individual videos. That is anecdotal evidence: one video can over- "
                   "or under-perform for reasons unrelated to how it was made.")
    return {"videos": vids, "rows": rows, "context": context, "observed": observed, "possible": possible,
            "caveats": caveats}


# ---- recommendations -----------------------------------------------------------

def _rec(key, title, action, evidence, n, extra_caution=""):
    return {"key": key, "title": title, "action": action, "evidence": evidence, "n": n,
            "confidence": sample_label(n), "caution": (extra_caution + " " + CAUTION).strip()}


def recommendations(videos: list[dict], business_slug: str) -> list[dict]:
    recs: list[dict] = []
    if not videos or not any(v.get("snapshot_id") for v in videos):
        return [_rec("no_data", "Import performance data first",
                     "Upload a CSV export or enter numbers by hand. Recommendations appear once there is data.",
                     ["No performance data is stored for this selection."], 0)]
    otrm = business_slug == "on-the-road-media"
    ret = retention_report(videos)
    conv = conversion_report(videos)

    # 1. Early drop-off -> shorten intros / earlier reveal
    early = [v for v in ret["early"] if v["retention_flag"] == "early_drop"]
    assessable = ret["assessable"]
    if early and assessable >= MIN_PEERS and (len(early) >= 2 or len(early) / assessable >= 0.3):
        fmts = sorted({v["format_label"] for v in early})
        action = ("Shorten the introduction: open on the fan's answer, the reaction or the athlete, then add context. "
                  "Test revealing the athlete or the key person in the first second."
                  if otrm else
                  "Open on the payoff (the crash, glitch, discovery or streamer reaction) before any context. "
                  "Cut the build-up to under 2 seconds.")
        recs.append(_rec(
            "early_drop", f"Viewers leave early on {len(early)} of {assessable} comparable videos", action,
            [f"Formats affected: {', '.join(fmts)}."] +
            [f"“{(v['title'] or v['post_url'] or '#' + str(v['id']))[:60]}”: {v['retention_note']}." for v in early[:3]],
            assessable))

    # 2. Skip rate by format -> change visual hooks
    all_skip = [v["skip_rate"] for v in videos if v.get("skip_rate") is not None]
    if len(all_skip) >= 5:
        overall = median(all_skip)
        for g in ret["by_format"]:
            if g["n_skip"] >= MIN_GROUP and g["median_skip"] >= overall + 8:
                recs.append(_rec(
                    f"skip_{g['label']}", f"Change the visual hook for {g['label']}",
                    "Try a different first frame: movement, a face or a bold on-screen question in the first second. "
                    "Make the first spoken line the most surprising one.",
                    [f"Median skip rate {g['median_skip']:.0f}% for {g['label']} ({g['n_skip']} videos) vs "
                     f"{overall:.0f}% across all {len(all_skip)} videos with skip data."], g["n_skip"]))

    # 3. Conversion by format -> prioritise the stronger format
    groups = [g for g in conv["by_format"] if g["n"] >= MIN_GROUP and g["label"] != "Untagged"]
    if len(groups) >= 2:
        best, worst = groups[0], groups[-1]
        if best["pooled_per_1k"] and (worst["pooled_per_1k"] == 0 or best["pooled_per_1k"] >= 1.5 * worst["pooled_per_1k"]):
            recs.append(_rec(
                "format_conversion", f"Prioritise {best['label']} for follower growth",
                f"Make more {best['label']} videos, and test what makes them work inside the weaker format "
                f"({worst['label']}).",
                [f"{best['label']}: {best['pooled_per_1k']} follows per 1,000 views ({best['n']} videos, "
                 f"{best['views']:,} views).",
                 f"{worst['label']}: {worst['pooled_per_1k']} follows per 1,000 views ({worst['n']} videos, "
                 f"{worst['views']:,} views)."],
                min(best["n"], worst["n"])))

    # 4. High reach, weak conversion -> give a reason to follow
    conv_videos = [v for v in videos if v.get("views") and v.get("follows_per_1k") is not None]
    if len(conv_videos) >= 5:
        mv = median(v["views"] for v in conv_videos)
        mc = median(v["follows_per_1k"] for v in conv_videos)
        weak = [v for v in conv_videos if v["views"] >= mv and v["follows_per_1k"] <= 0.5 * mc]
        if weak and mc > 0:
            recs.append(_rec(
                "reach_no_follow", f"{len(weak)} high-reach video{'s' if len(weak) != 1 else ''} converted few followers",
                "Give viewers a reason to follow: label it as a series (“Part 1”, a recurring segment name), "
                "end with a question that the next video answers, or tease what's next."
                if otrm else
                "Turn one-offs into series (“Day 3 of finding every hidden…”), end on a cliffhanger, and keep the "
                "account's promise obvious in the first line.",
                [f"Median across {len(conv_videos)} videos: {mv:,.0f} views, {mc} follows per 1,000 views."] +
                [f"“{(v['title'] or '#' + str(v['id']))[:60]}”: {v['views']:,} views but {v['follows_per_1k']} "
                 f"follows per 1,000." for v in weak[:3]],
                len(conv_videos)))

    # 5. Length vs conversion
    lgroups = [g for g in conv["by_length"] if g["n"] >= MIN_GROUP]
    if len(lgroups) >= 2:
        best, worst = lgroups[0], lgroups[-1]
        if best["pooled_per_1k"] and (worst["pooled_per_1k"] == 0 or best["pooled_per_1k"] >= 1.5 * worst["pooled_per_1k"]):
            recs.append(_rec(
                "length_conversion", f"Test more {best['label']} videos",
                f"Cut some upcoming videos to {best['label']} and compare follows per 1,000 views.",
                [f"{best['label']}: {best['pooled_per_1k']} follows per 1,000 views ({best['n']} videos).",
                 f"{worst['label']}: {worst['pooled_per_1k']} follows per 1,000 views ({worst['n']} videos)."],
                min(best["n"], worst["n"]),
                "This compares follower conversion, not watch %, because shorter videos are always watched for a "
                "larger share of their length."))

    # 6. Pacing in long videos
    long_vids = [v for v in videos if (v.get("duration_seconds") or 0) > 45 and v["watch_ratio"] is not None]
    if len(long_vids) >= MIN_GROUP:
        m = median(v["watch_ratio"] for v in long_vids)
        if m < 0.35:
            recs.append(_rec(
                "pacing", "Try faster pacing in videos over 45 seconds",
                "Remove pauses and repeated setup, add a cut or on-screen change every 2–3 seconds, and move the "
                "best moment earlier.",
                [f"Videos over 45 s ({len(long_vids)}) are watched for {m:.0%} of their length on average (median)."],
                len(long_vids)))

    # 7. Posting time (low confidence)
    slot_groups = []
    for label, vs in _group([v for v in videos if v.get("views") is not None], lambda v: v["time_slot"]).items():
        if len(vs) >= MIN_GROUP:
            slot_groups.append((label, len(vs), median(v["views"] for v in vs)))
    if len(slot_groups) >= 2:
        slot_groups.sort(key=lambda s: -s[2])
        (bl, bn, bm), (wl, wn, wm) = slot_groups[0], slot_groups[-1]
        if wm and bm >= 1.5 * wm:
            recs.append(_rec(
                "timing", f"Test posting more in the {bl}",
                f"Schedule a few comparable videos in the {bl} and compare against the {wl}.",
                [f"Median views: {bl} {bm:,.0f} ({bn} videos) vs {wl} {wm:,.0f} ({wn} videos)."],
                min(bn, wn), "Posting time is easily confused with topic and account growth over time."))

    # 8. Data gaps that block analysis
    n = len(videos)
    gaps = []
    if ret["missing_length"] / n >= 0.5:
        gaps.append(f"video length is missing for {ret['missing_length']} of {n} videos")
    if ret["missing_watch"] / n >= 0.5:
        gaps.append(f"average watch time is missing for {ret['missing_watch']} of {n} videos")
    if conv["missing_follows"] / n >= 0.5:
        gaps.append(f"followers gained is missing for {conv['missing_follows']} of {n} videos")
    untagged = sum(1 for v in videos if not v.get("format"))
    if untagged / n >= 0.5:
        gaps.append(f"{untagged} of {n} videos have no format tag")
    if gaps:
        recs.append(_rec(
            "data_gaps", "Fill data gaps to unlock more analysis",
            "Include these columns in your exports, or add them on each video's page. Tag formats in bulk from the "
            "video table.", [g[0].upper() + g[1:] + "." for g in gaps], n, ""))
    if not [r for r in recs if r["key"] != "data_gaps"]:
        recs.insert(0, _rec("not_enough", "Not enough comparable data for confident recommendations yet",
                            "Keep importing. Most comparisons need at least 3 videos per group.",
                            [f"{n} videos in this selection."], n))
    return recs


def format_benchmark(conn: sqlite3.Connection, business_slug: str, fmt: str) -> dict | None:
    """Historical performance of one format, shown on idea pages to inform scoring."""
    videos = load_videos(conn, business_slug)
    if not videos:
        return None
    conv = {g["label"]: g for g in conversion_report(videos)["by_format"]}
    ret = {g["label"]: g for g in retention_report(videos)["by_format"]}
    label = format_label(business_slug, fmt)
    overall = kpis(videos)
    if label not in conv and label not in ret:
        return {"label": label, "n": 0, "overall_per_1k": overall["per_1k"]}
    c = conv.get(label, {})
    return {"label": label, "n": c.get("n", 0), "per_1k": c.get("pooled_per_1k"),
            "overall_per_1k": overall["per_1k"], "median_relative": ret.get(label, {}).get("median_relative"),
            "sample": sample_label(c.get("n", 0))}
