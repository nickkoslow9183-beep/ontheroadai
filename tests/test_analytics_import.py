"""CSV import, parsing, duplicates, missing data, corrections, migration."""

from pathlib import Path

import pytest

from ontheroadai.analytics import csv_import, store
from ontheroadai.analytics.csv_import import ImportRejected
from ontheroadai.analytics.fields import (auto_map, parse_count, parse_datetime, parse_duration,
                                          parse_percent)
from ontheroadai.db import init_db

FIX = Path(__file__).parent / "fixtures"
TIKTOK = (FIX / "example_tiktok_export.csv").read_bytes()
INSTAGRAM = (FIX / "example_instagram_export.csv").read_bytes()


def account(conn, brand="on-the-road-media", platform="tiktok"):
    return conn.execute("SELECT a.id FROM accounts a JOIN brands b ON b.id = a.brand_id "
                        "WHERE b.slug = ? AND a.platform = ?", (brand, platform)).fetchone()[0]


def do_import(conn, data, platform="tiktok", captured_on="2026-11-10", name="export.csv", brand="on-the-road-media"):
    imp_id = csv_import.start_import(conn, account(conn, brand, platform), name, data, captured_on)
    imp = csv_import.get_import(conn, imp_id)
    headers, _ = csv_import.load_rows(imp)
    mapping, options, _ = csv_import.suggest_mapping(conn, headers)
    return imp_id, csv_import.commit_import(conn, imp_id, mapping, options)


# ---- parsers ---------------------------------------------------------------------

def test_parse_count():
    assert parse_count("1,234") == 1234
    assert parse_count("1.2K") == 1200
    assert parse_count("3.4M") == 3_400_000
    assert parse_count(" 15 ") == 15
    for missing in ("", "-", "--", "N/A", None, "—"):
        assert parse_count(missing) is None
    with pytest.raises(ValueError):
        parse_count("abc")
    with pytest.raises(ValueError):
        parse_count("-5")
    with pytest.raises(ValueError):
        parse_count("12.5")


def test_parse_percent():
    assert parse_percent("45%") == 45.0
    assert parse_percent("45") == 45.0
    assert parse_percent("0.45", fraction=True) == 45.0
    assert parse_percent("45%", fraction=True) == 45.0
    assert parse_percent("12,5%") == 12.5
    assert parse_percent("") is None
    with pytest.raises(ValueError):
        parse_percent("140%")


def test_parse_duration():
    assert parse_duration("0:15") == 15
    assert parse_duration("00:01:05") == 65
    assert parse_duration("1:05") == 65
    assert parse_duration("7.5") == 7.5
    assert parse_duration("15s") == 15
    assert parse_duration("1m 5s") == 65
    assert parse_duration("-") is None
    with pytest.raises(ValueError):
        parse_duration("soon")


def test_parse_datetime():
    assert parse_datetime("2026-11-01 18:30:00") == "2026-11-01T18:30"
    assert parse_datetime("2026-11-01") == "2026-11-01"           # no time invented
    assert parse_datetime("11/02/2026 18:00") == "2026-11-02T18:00"
    assert parse_datetime("02/11/2026 18:00", day_first=True) == "2026-11-02T18:00"
    assert parse_datetime("Nov 5, 2026") == "2026-11-05"
    assert parse_datetime("2026-11-01T18:30:00Z") == "2026-11-01T18:30"
    with pytest.raises(ValueError):
        parse_datetime("yesterday")


def test_auto_map_common_exports():
    tt = auto_map(["Video ID", "Video title", "Post time", "Duration", "Total views", "Total likes",
                   "Total comments", "Total shares", "Add to Favorites", "Average watch time",
                   "Watched full video", "New followers"])
    assert tt["post_id"] == "Video ID" and tt["title"] == "Video title" and tt["views"] == "Total views"
    assert tt["saves"] == "Add to Favorites" and tt["avg_watch_seconds"] == "Average watch time"
    assert tt["completion_rate"] == "Watched full video" and tt["follows_gained"] == "New followers"
    assert tt["published_at"] == "Post time" and tt["duration_seconds"] == "Duration"
    assert tt["reach"] is None and tt["skip_rate"] is None
    ig = auto_map(["Permalink", "Description", "Publish time", "Duration (sec)", "Views", "Reach", "Follows"])
    assert ig["post_url"] == "Permalink" and ig["title"] == "Description" and ig["reach"] == "Reach"
    assert ig["follows_gained"] == "Follows" and ig["duration_seconds"] == "Duration (sec)"


def test_post_id_extraction():
    assert store.extract_post_id("https://www.tiktok.com/@otrm/video/7301000000000000001?lang=en") == "7301000000000000001"
    assert store.extract_post_id("https://www.instagram.com/reel/Cx12_ab/") == "Cx12_ab"
    assert store.extract_post_id("https://youtube.com/shorts/abcDEF12345") == "abcDEF12345"
    assert store.extract_post_id("https://www.youtube.com/watch?v=abcDEF12345") == "abcDEF12345"
    assert store.extract_post_id("https://x.com/otrm/status/1234567890") == "1234567890"
    assert store.video_key(None, "https://www.tiktok.com/@a/video/99") == store.video_key("99")


# ---- rejecting bad files -----------------------------------------------------------

@pytest.mark.parametrize("data,name,msg", [
    (b"", "a.csv", "empty"),
    (b"PK\x03\x04rest-of-zip", "a.csv", "Excel"),
    (b"Views,Likes\n1,2\n", "report.pdf", ".csv"),
    (b"\x00\x01\x02binary", "a.csv", "binary"),
    (b"Views,Likes\n", "a.csv", "no data rows"),
    (b"just one column\n5\n", "a.csv", "at least two columns"),
])
def test_rejects_invalid_files(conn, data, name, msg):
    with pytest.raises(ImportRejected, match=msg):
        csv_import.start_import(conn, account(conn), name, data, "2026-11-10")
    assert conn.execute("SELECT COUNT(*) FROM analytics_imports").fetchone()[0] == 0


def test_rejects_future_date_and_unknown_account(conn):
    with pytest.raises(ImportRejected, match="future"):
        csv_import.start_import(conn, account(conn), "a.csv", TIKTOK, "2999-01-01")
    with pytest.raises(ImportRejected, match="account"):
        csv_import.start_import(conn, 99999, "a.csv", TIKTOK, "2026-11-10")


def test_same_file_cannot_be_imported_twice(conn):
    do_import(conn, TIKTOK)
    with pytest.raises(ImportRejected, match="already imported"):
        csv_import.start_import(conn, account(conn), "again.csv", TIKTOK, "2026-11-12")


def test_mapping_validation(conn):
    imp_id = csv_import.start_import(conn, account(conn), "a.csv", TIKTOK, "2026-11-10")
    imp = csv_import.get_import(conn, imp_id)
    with pytest.raises(ValueError, match="identify"):
        csv_import.plan_import(conn, imp, {"views": "Total views"}, {})
    with pytest.raises(ValueError, match="metric"):
        csv_import.plan_import(conn, imp, {"post_id": "Video ID"}, {})
    with pytest.raises(ValueError, match="once"):
        csv_import.plan_import(conn, imp, {"post_id": "Video ID", "views": "Video ID"}, {})
    with pytest.raises(ValueError, match="not in this file"):
        csv_import.plan_import(conn, imp, {"post_id": "Nope", "views": "Total views"}, {})


# ---- importing ----------------------------------------------------------------------

def test_tiktok_import_stores_values_and_keeps_missing_missing(conn):
    imp_id, counts = do_import(conn, TIKTOK)
    assert counts == {"rows": 4, "importable": 4, "new_videos": 4, "duplicates": 0, "skipped": 0, "errors": 0}
    v = conn.execute("SELECT * FROM videos WHERE external_id = '7301000000000000001'").fetchone()
    assert v["title"] == "EXAMPLE Fans guess the final score"
    assert v["duration_seconds"] == 15 and v["published_at"] == "2026-11-01T18:30"
    s = conn.execute("SELECT * FROM video_snapshots WHERE video_id = ?", (v["id"],)).fetchone()
    assert (s["views"], s["saves"], s["follows_gained"], s["avg_watch_seconds"], s["completion_rate"]) == \
        (12000, 60, 96, 7, 45)
    # not in the TikTok export -> stored as NULL, never 0
    assert s["reach"] is None and s["skip_rate"] is None
    assert s["source"] == "csv" and s["import_id"] == imp_id
    assert csv_import.get_import(conn, imp_id)["status"] == "completed"
    assert csv_import.get_import(conn, imp_id)["raw_content"] is None


def test_instagram_import_skips_totals_and_empty_rows(conn):
    imp_id, counts = do_import(conn, INSTAGRAM, platform="instagram")
    assert counts["importable"] == 2 and counts["skipped"] == 1 and counts["errors"] == 1
    errors = csv_import.get_import(conn, imp_id)["errors"]
    assert errors[0]["line"] == 4 and "No metric values" in errors[0]["message"]
    v = conn.execute("SELECT * FROM videos WHERE external_id = 'EXAMPLEa1'").fetchone()
    assert v["published_at"] == "2026-11-02T18:00"
    s = conn.execute("SELECT * FROM video_snapshots WHERE video_id = ?", (v["id"],)).fetchone()
    assert s["reach"] == 11000 and s["avg_watch_seconds"] is None


def test_newer_export_adds_history_and_same_date_is_duplicate(conn):
    do_import(conn, TIKTOK, captured_on="2026-11-10")
    newer = TIKTOK.replace(b"12000,900", b"15000,950")
    _, counts = do_import(conn, newer, captured_on="2026-11-17", name="week2.csv")
    assert counts["importable"] == 4 and counts["new_videos"] == 0
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 4
    vid = conn.execute("SELECT id FROM videos WHERE external_id = '7301000000000000001'").fetchone()[0]
    assert [s["views"] for s in store.snapshots(conn, vid)] == [12000, 15000]
    # a different file with the same videos for an already-imported date: nothing new
    again = TIKTOK.replace(b"EXAMPLE Tailgate", b"EXAMPLE  Tailgate")
    with pytest.raises(ImportRejected, match="Nothing new"):
        do_import(conn, again, captured_on="2026-11-10", name="dupe.csv")


def test_duplicate_rows_within_one_file(conn):
    lines = TIKTOK.decode().strip().split("\n")
    data = ("\n".join(lines + [lines[1]]) + "\n").encode()
    _, counts = do_import(conn, data)
    assert counts["importable"] == 4 and counts["duplicates"] == 1


def test_bad_values_are_reported_not_guessed(conn):
    data = (b"Video ID,Total views,New followers\n"
            b"1,1000,10\n"
            b"2,lots,5\n"
            b"3,500,-2\n"
            b",700,1\n")
    imp_id, counts = do_import(conn, data)
    assert counts["importable"] == 1 and counts["errors"] == 3
    msgs = " | ".join(e["message"] for e in csv_import.get_import(conn, imp_id)["errors"])
    assert "not a number" in msgs and "negative" in msgs and "identify" in msgs


def test_all_rows_bad_rejects_and_saves_nothing(conn):
    data = b"Video ID,Total views\n1,abc\n2,xyz\n"
    imp_id = csv_import.start_import(conn, account(conn), "bad.csv", data, "2026-11-10")
    imp = csv_import.get_import(conn, imp_id)
    with pytest.raises(ImportRejected, match="No rows could be imported"):
        csv_import.commit_import(conn, imp_id, {"post_id": "Video ID", "views": "Total views"}, {})
    assert conn.execute("SELECT COUNT(*) FROM video_snapshots").fetchone()[0] == 0
    assert csv_import.get_import(conn, imp_id)["status"] == "pending"
    assert imp["row_count"] == 2


def test_title_lines_above_headers_are_detected(conn):
    data = b"TikTok analytics export\n\nVideo ID;Total views;New followers\n1;1000;10\n2;2000;5\n"
    imp_id = csv_import.start_import(conn, account(conn), "x.csv", data, "2026-11-10")
    imp = csv_import.get_import(conn, imp_id)
    assert imp["header_row"] == 2 and imp["row_count"] == 2
    headers, rows = csv_import.load_rows(imp)
    assert headers == ["Video ID", "Total views", "New followers"]
    assert rows[0][0] == 4  # real line number in the file, for error messages
    data2 = b"Exported,2026-11-10\nVideo ID,Total views,New followers\n1,1000,10\n"
    imp2 = csv_import.start_import(conn, account(conn), "y.csv", data2, "2026-11-10")
    # a 2-cell title line is skipped because the rows under it are wider
    assert csv_import.get_import(conn, imp2)["header_row"] == 2
    headers, rows = csv_import.load_rows(csv_import.get_import(conn, imp2))
    assert headers == ["Video ID", "Total views", "New followers"] and len(rows) == 1


def test_fraction_percent_option(conn):
    data = b"Video ID,Total views,Watched full video\n1,1000,0.45\n"
    imp_id = csv_import.start_import(conn, account(conn), "f.csv", data, "2026-11-10")
    csv_import.commit_import(conn, imp_id, {"post_id": "Video ID", "views": "Total views",
                                            "completion_rate": "Watched full video"}, {"fraction_percent": True})
    assert conn.execute("SELECT completion_rate FROM video_snapshots").fetchone()[0] == 45.0


def test_mapping_is_remembered(conn):
    data = b"ID thing,Plays count\n1,100\n"
    imp_id = csv_import.start_import(conn, account(conn), "m.csv", data, "2026-11-10")
    csv_import.commit_import(conn, imp_id, {"post_id": "ID thing", "views": "Plays count"}, {}, preset_name="Mine")
    imp2 = csv_import.start_import(conn, account(conn), "m2.csv", b"ID thing,Plays count\n2,200\n", "2026-11-10")
    headers, _ = csv_import.load_rows(csv_import.get_import(conn, imp2))
    mapping, _, source = csv_import.suggest_mapping(conn, headers)
    assert mapping["post_id"] == "ID thing" and mapping["views"] == "Plays count" and "Mine" in source


def test_undo_import_and_reimport(conn):
    imp_id, _ = do_import(conn, TIKTOK)
    result = csv_import.undo_import(conn, imp_id)
    assert result == {"snapshots": 4, "videos": 4}
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
    with pytest.raises(ValueError):
        csv_import.undo_import(conn, imp_id)
    _, counts = do_import(conn, TIKTOK)  # allowed again after undo
    assert counts["importable"] == 4


def test_undo_keeps_videos_with_other_history(conn):
    do_import(conn, TIKTOK, captured_on="2026-11-10")
    imp2, _ = do_import(conn, TIKTOK.replace(b"12000", b"13000"), captured_on="2026-11-17", name="w2.csv")
    csv_import.undo_import(conn, imp2)
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 4
    assert conn.execute("SELECT COUNT(*) FROM video_snapshots").fetchone()[0] == 4


# ---- manual entry & corrections ---------------------------------------------------------

def test_manual_entry_blank_is_missing(conn):
    vid, sid = store.add_manual_entry(conn, account(conn), {"title": "Hand entered", "duration_seconds": "0:30"},
                                      "2026-11-10", {"views": "2,000", "follows_gained": "", "avg_watch_seconds": "0:12"})
    s = conn.execute("SELECT * FROM video_snapshots WHERE id = ?", (sid,)).fetchone()
    assert s["views"] == 2000 and s["follows_gained"] is None and s["avg_watch_seconds"] == 12
    with pytest.raises(ValueError, match="at least one metric"):
        store.add_manual_entry(conn, account(conn), {"title": "Empty"}, "2026-11-10", {})
    with pytest.raises(ValueError, match="already has numbers"):
        store.add_snapshot(conn, vid, "2026-11-10", {"views": "5"})


def test_correct_and_remove_entries(conn):
    vid, sid = store.add_manual_entry(conn, account(conn), {"title": "Typo video"}, "2026-11-10", {"views": "20000"})
    store.update_snapshot(conn, sid, {"views": "2000", "likes": "150"})
    s = conn.execute("SELECT * FROM video_snapshots WHERE id = ?", (sid,)).fetchone()
    assert s["views"] == 2000 and s["likes"] == 150
    with pytest.raises(ValueError):
        store.update_snapshot(conn, sid, {"views": "-1"})
    store.update_video(conn, vid, title="Fixed title", duration_seconds="45", format="fan_reaction")
    assert store.get_video(conn, vid)["title"] == "Fixed title"
    with pytest.raises(ValueError):
        store.update_video(conn, vid, duration_seconds="0")
    store.delete_snapshot(conn, sid)
    assert store.snapshots(conn, vid) == []
    store.delete_video(conn, vid)
    with pytest.raises(LookupError):
        store.get_video(conn, vid)


def test_import_does_not_overwrite_corrections(conn):
    do_import(conn, TIKTOK, captured_on="2026-11-10")
    vid = conn.execute("SELECT id FROM videos WHERE external_id = '7301000000000000001'").fetchone()[0]
    store.update_video(conn, vid, title="My corrected title")
    do_import(conn, TIKTOK.replace(b"12000", b"13000"), captured_on="2026-11-17", name="w2.csv")
    assert store.get_video(conn, vid)["title"] == "My corrected title"


# ---- Milestone 1 compatibility ---------------------------------------------------------

def test_legacy_rows_migrate_once(conn):
    acc = account(conn)
    conn.execute("INSERT INTO analytics (account_id, recorded_on, views, likes, follows_gained, created_at) "
                 "VALUES (?, '2026-10-01', 500, 20, 3, 'x')", (acc,))
    conn.commit()
    init_db(conn)
    init_db(conn)
    snaps = conn.execute("SELECT * FROM video_snapshots WHERE source = 'legacy'").fetchall()
    assert len(snaps) == 1 and snaps[0]["views"] == 500 and snaps[0]["follows_gained"] == 3
    assert conn.execute("PRAGMA user_version").fetchone()[0] >= 2  # 3 since Milestone 3


def test_legacy_row_with_link_matches_later_import(conn):
    acc = account(conn)
    conn.execute("INSERT INTO analytics (account_id, recorded_on, views, post_url, created_at) VALUES "
                 "(?, '2026-11-01', 500, 'https://www.tiktok.com/@otrm/video/7301000000000000001', 'x')", (acc,))
    conn.commit()
    init_db(conn)
    do_import(conn, TIKTOK, captured_on="2026-11-10")
    vid = conn.execute("SELECT id FROM videos WHERE external_id = '7301000000000000001'").fetchone()[0]
    assert [s["source"] for s in store.snapshots(conn, vid)] == ["legacy", "csv"]
