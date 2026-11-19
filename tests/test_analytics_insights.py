"""Calculations, retention context, conversion, comparison and recommendations."""

import pytest

from ontheroadai.analytics import insights, store

SLUG = "on-the-road-media"


def acc(conn, platform="tiktok", brand="on-the-road-media"):
    return conn.execute("SELECT a.id FROM accounts a JOIN brands b ON b.id = a.brand_id "
                        "WHERE b.slug = ? AND a.platform = ?", (brand, platform)).fetchone()[0]


def add(conn, title, fmt, length, watch, views, follows, platform="tiktok", skip=None, published="2026-11-01T18:00",
        captured="2026-11-10", **extra):
    vid, _ = store.add_manual_entry(
        conn, acc(conn, platform),
        {"title": title, "format": fmt, "duration_seconds": length, "published_at": published},
        captured, {"views": views, "follows_gained": follows, "avg_watch_seconds": watch, "skip_rate": skip, **extra})
    return vid


def seed(conn):
    """15 s clips watched ~7 s, 60 s clips watched ~30 s, plus one 60 s clip watched only 7 s."""
    ids = {}
    for i in range(4):
        ids[f"short{i}"] = add(conn, f"Short {i}", "fan_reaction", 15, 7 + i * 0.2, 10000, 50)
    for i in range(4):
        ids[f"long{i}"] = add(conn, f"Long {i}", "street_interview", 60, 30 + i, 10000, 150)
    ids["long_drop"] = add(conn, "Long but loses people", "street_interview", 60, 7, 10000, 20)
    return ids


def by_title(videos):
    return {v["title"]: v for v in videos}


def test_derived_metrics_and_missing_inputs(conn):
    add(conn, "Full", "fan_reaction", 20, 10, 4000, 12, likes=300, comments=20, shares=40, saves=40)
    add(conn, "Partial", "fan_reaction", None, 10, 4000, None, likes=300)
    v = by_title(insights.load_videos(conn, SLUG))
    assert v["Full"]["follows_per_1k"] == 3.0
    assert v["Full"]["engagement_rate"] == 10.0 and v["Full"]["engagement_partial"] is False
    assert v["Full"]["watch_ratio"] == 0.5
    # nothing estimated when inputs are missing
    assert v["Partial"]["follows_per_1k"] is None
    assert v["Partial"]["watch_ratio"] is None and v["Partial"]["length_bucket"] is None
    assert v["Partial"]["engagement_partial"] is True


def test_seven_seconds_means_different_things_at_different_lengths(conn):
    seed(conn)
    v = by_title(insights.load_videos(conn, SLUG))
    short, drop = v["Short 0"], v["Long but loses people"]
    assert short["avg_watch_seconds"] == drop["avg_watch_seconds"] == 7
    assert short["watch_ratio"] == pytest.approx(0.467, abs=0.001)
    assert drop["watch_ratio"] == pytest.approx(0.117, abs=0.001)
    # each judged against its own length group
    assert short["length_bucket"] == "≤15 s" and drop["length_bucket"] == "31–60 s"
    assert short["retention_flag"] == "typical"
    assert drop["retention_flag"] == "early_drop"
    assert "similar-length" in drop["retention_note"]
    assert v["Long 3"]["retention_flag"] in ("typical", "strong_hold")


def test_no_peers_means_no_relative_judgement(conn):
    add(conn, "Lonely", "fan_reaction", 120, 10, 1000, 1)
    v = insights.load_videos(conn, SLUG)[0]
    assert v["relative_retention"] is None
    assert v["retention_flag"] == "low_no_peers" and "not enough similar-length" in v["retention_note"]


def test_skip_rate_flags_early_drop(conn):
    for i in range(3):
        add(conn, f"Peer {i}", "fan_reaction", 20, None, 1000, 5, skip=30 + i)
    add(conn, "Skipped a lot", "fan_reaction", 20, None, 1000, 5, skip=55)
    v = by_title(insights.load_videos(conn, SLUG))
    assert v["Skipped a lot"]["retention_flag"] == "early_drop"
    assert "skip rate 55%" in v["Skipped a lot"]["retention_note"]


def test_conversion_by_format_is_pooled(conn):
    seed(conn)
    rep = insights.conversion_report(insights.load_videos(conn, SLUG))
    fmts = {g["label"]: g for g in rep["by_format"]}
    assert fmts["Fan reaction"]["pooled_per_1k"] == 5.0 and fmts["Fan reaction"]["n"] == 4
    # (4*150 + 20) / 50,000 * 1000 = 12.4
    assert fmts["Street interview"]["pooled_per_1k"] == 12.4
    assert rep["by_format"][0]["label"] == "Street interview"
    assert fmts["Fan reaction"]["sample"].startswith("Anecdotal")


def test_filters(conn):
    seed(conn)
    add(conn, "On Instagram", "fan_reaction", 15, 7, 1000, 1, platform="instagram", published="2026-10-01T09:00")
    assert len(insights.load_videos(conn, SLUG, {"platform": "instagram"})) == 1
    assert len(insights.load_videos(conn, SLUG, {"format": "street_interview"})) == 5
    assert len(insights.load_videos(conn, SLUG, {"length": "≤15 s"})) == 5
    assert len(insights.load_videos(conn, SLUG, {"date_to": "2026-10-15"})) == 1
    store.add_manual_entry(conn, acc(conn), {"title": "No format"}, "2026-11-10", {"views": "5"})
    assert [v["title"] for v in insights.load_videos(conn, SLUG, {"format": "__untagged__"})] == ["No format"]
    assert insights.load_videos(conn, "gta6-network") == []


def test_latest_snapshot_is_used(conn):
    vid = add(conn, "Growing", "fan_reaction", 15, 7, 1000, 5, captured="2026-11-10")
    store.add_snapshot(conn, vid, "2026-11-17", {"views": "4000", "follows_gained": "30"})
    conn.commit()
    v = insights.load_videos(conn, SLUG)[0]
    assert v["views"] == 4000 and v["captured_on"] == "2026-11-17" and v["snapshot_count"] == 2
    assert v["avg_watch_seconds"] is None  # not carried over from an older snapshot


def test_compare_separates_observed_from_possible(conn):
    ids = seed(conn)
    c = insights.compare(conn, SLUG, [ids["short0"], ids["long0"]])
    assert [v["letter"] for v in c["videos"]] == ["A", "B"]
    text = " ".join(c["observed"])
    assert "follows per 1,000 views" in text and "15 s long" in text and "different formats" in text
    assert any("Not proven" in p for p in c["possible"])
    assert any("not measured here" in p for p in c["possible"])
    assert any("anecdotal" in x for x in c["caveats"])
    row = next(r for r in c["rows"] if r["label"] == "Follows per 1,000 views")
    assert row["values"] == ["5", "15"] and row["best"] == [1]
    skip = next(r for r in c["rows"] if r["label"] == "Skip rate %")
    assert skip["values"] == ["—", "—"] and skip["best"] == []


def test_compare_cross_platform_and_age_caveats(conn):
    a = add(conn, "TT", "fan_reaction", 15, 7, 1000, 5, published="2026-11-01T18:00", captured="2026-11-03")
    b = add(conn, "IG", "fan_reaction", 15, 7, 1000, 5, platform="instagram", published="2026-11-01T18:00",
            captured="2026-11-20")
    caveats = " ".join(insights.compare(conn, SLUG, [a, b])["caveats"])
    assert "Different platforms" in caveats and "different ages" in caveats


def test_compare_validation(conn):
    ids = seed(conn)
    with pytest.raises(ValueError, match="at least two"):
        insights.compare(conn, SLUG, [ids["short0"]])
    with pytest.raises(ValueError, match="at most six"):
        insights.compare(conn, SLUG, list(ids.values())[:7])
    with pytest.raises(ValueError, match="not found"):
        insights.compare(conn, "gta6-network", [ids["short0"], ids["long0"]])


def test_recommendations_use_real_data_and_hedge(conn):
    assert insights.recommendations([], SLUG)[0]["key"] == "no_data"
    seed(conn)
    recs = insights.recommendations(insights.load_videos(conn, SLUG), SLUG)
    keys = {r["key"] for r in recs}
    assert "format_conversion" in keys
    fc = next(r for r in recs if r["key"] == "format_conversion")
    assert "Street interview" in fc["title"] and "12.4" in " ".join(fc["evidence"])
    assert fc["confidence"].startswith("Anecdotal")
    for r in recs:
        assert r["confidence"] and "not a controlled experiment" in r["caution"]
        blob = (r["title"] + r["action"] + " ".join(r["evidence"])).lower()
        assert "significant" not in blob and "proves" not in blob and "guarantee" not in blob


def test_early_drop_recommendation_is_business_specific(conn):
    for i in range(3):
        add(conn, f"Good {i}", "street_interview", 60, 30, 1000, 5)
    add(conn, "Drop A", "street_interview", 60, 8, 1000, 5)
    add(conn, "Drop B", "street_interview", 60, 9, 1000, 5)
    recs = insights.recommendations(insights.load_videos(conn, SLUG), SLUG)
    rec = next(r for r in recs if r["key"] == "early_drop")
    assert "athlete" in rec["action"] and "Shorten the introduction" in rec["action"]


def test_data_gap_recommendation(conn):
    for i in range(3):
        store.add_manual_entry(conn, acc(conn), {"title": f"Bare {i}"}, "2026-11-10", {"views": "100"})
    recs = insights.recommendations(insights.load_videos(conn, SLUG), SLUG)
    gaps = next(r for r in recs if r["key"] == "data_gaps")
    assert any("video length is missing" in e.lower() for e in gaps["evidence"])


def test_sample_labels():
    assert insights.sample_label(3).startswith("Anecdotal")
    assert insights.sample_label(10).startswith("Directional")
    assert insights.sample_label(20).startswith("Moderate")
    assert "not a controlled test" in insights.sample_label(50)
