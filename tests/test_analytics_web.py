"""Analytics dashboard pages through Flask's test client."""

import io
import re
from pathlib import Path

from ontheroadai.db import connect

FIX = Path(__file__).parent / "fixtures"


def account_id(app, platform="tiktok", brand="on-the-road-media"):
    conn = connect(app.config["DB_PATH"])
    try:
        return conn.execute("SELECT a.id FROM accounts a JOIN brands b ON b.id = a.brand_id "
                            "WHERE b.slug = ? AND a.platform = ?", (brand, platform)).fetchone()[0]
    finally:
        conn.close()


def upload(client, app, name="example_tiktok_export.csv", data=None, platform="tiktok", slug="on-the-road-media"):
    data = data if data is not None else (FIX / name).read_bytes()
    return client.post(f"/analytics/{slug}/import", data={
        "account_id": str(account_id(app, platform, "on-the-road-media" if slug == "on-the-road-media" else "gta6-moments")),
        "captured_on": "2026-11-10", "file": (io.BytesIO(data), name)}, content_type="multipart/form-data")


def mapping_form(page_html):
    """Read the selected option for every map_* select, like a browser would submit it."""
    form = {}
    for name, body in re.findall(r'<select name="(map_\w+)"[^>]*>(.*?)</select>', page_html, re.S):
        m = re.search(r'<option value="([^"]*)" selected>', body)
        form[name] = m.group(1) if m else ""
    return form


def import_fixture(client, app, **kw):
    r = upload(client, app, **kw)
    assert r.status_code == 302, r.get_data(as_text=True)[:500]
    detail = r.headers["Location"]
    page = client.get(detail).get_data(as_text=True)
    r2 = client.post(detail, data={**mapping_form(page), "header_row": "1", "action": "import"}, follow_redirects=True)
    return detail, r2.get_data(as_text=True)


def test_pages_render_empty(client):
    for url in ["/analytics", "/analytics/on-the-road-media", "/analytics/gta6-network",
                "/analytics/on-the-road-media/import", "/analytics/gta6-network/import"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/analytics/nope").status_code == 404
    assert client.get("/analytics/videos/999").status_code == 404
    assert client.get("/analytics/imports/999").status_code == 404
    assert "No videos yet" in client.get("/analytics/gta6-network").get_data(as_text=True)


def test_upload_map_preview_import(client, app):
    r = upload(client, app)
    detail = r.headers["Location"]
    page = client.get(detail).get_data(as_text=True)
    assert "Step 2: match the columns" in page and "auto-detected" in page
    form = mapping_form(page)
    assert form["map_views"] == "Total views" and form["map_post_id"] == "Video ID"
    assert form["map_reach"] == ""
    prev = client.post(detail, data={**form, "header_row": "1", "action": "preview"}).get_data(as_text=True)
    assert "Will import" in prev and "nothing is saved" in prev
    conn = connect(app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM video_snapshots").fetchone()[0] == 0  # preview wrote nothing
    done = client.post(detail, data={**form, "header_row": "1", "action": "import"}, follow_redirects=True)
    html = done.get_data(as_text=True)
    assert "Imported 4 row(s)" in html and "Undo this import" in html
    assert conn.execute("SELECT COUNT(*) FROM video_snapshots").fetchone()[0] == 4
    conn.close()


def test_rejections_show_messages(client, app):
    html = upload(client, app, name="x.xlsx", data=b"PK\x03\x04zz").get_data(as_text=True)
    assert "Please upload a .csv file" in html
    html = upload(client, app, name="x.csv", data=b"PK\x03\x04zz").get_data(as_text=True)
    assert "Excel" in html
    import_fixture(client, app)
    html = upload(client, app).get_data(as_text=True)
    assert "already imported" in html


def test_bad_mapping_shows_error(client, app):
    detail = upload(client, app).headers["Location"]
    html = client.post(detail, data={"map_views": "Total views", "header_row": "1", "action": "import"},
                       follow_redirects=True).get_data(as_text=True)
    assert "identify videos" in html


def test_dashboard_after_import(client, app):
    import_fixture(client, app)
    import_fixture(client, app, name="example_instagram_export.csv", platform="instagram")
    html = client.get("/analytics/on-the-road-media").get_data(as_text=True)
    assert "Recommendations" in html and "Follower conversion" in html and "Retention" in html
    assert "EXAMPLE Fans guess the final score" in html
    assert 'class="sortable videos"' in html and "Compare selected" in html
    assert "bar-row" in html  # charts rendered
    assert "loses viewers early" in html or "low, no peers" in html or "typical" in html
    # filter by platform
    html = client.get("/analytics/on-the-road-media?platform=instagram").get_data(as_text=True)
    assert "EXAMPLE Fans react to the comeback" in html and "EXAMPLE Fans guess the final score" not in html
    # the M1 business page still shows the summary
    assert "Open On The Road Media analytics" in client.get("/business/on-the-road-media").get_data(as_text=True)


def test_compare_bulk_tag_and_video_edit(client, app):
    import_fixture(client, app)
    conn = connect(app.config["DB_PATH"])
    ids = [r[0] for r in conn.execute("SELECT id FROM videos ORDER BY id")]
    r = client.get(f"/analytics/on-the-road-media/compare?ids={ids[0]}&ids={ids[1]}")
    html = r.get_data(as_text=True)
    assert r.status_code == 200 and "What the numbers show" in html and "Possible reasons (not proven)" in html
    r = client.get(f"/analytics/on-the-road-media/compare?ids={ids[0]}", follow_redirects=True)
    assert "at least two" in r.get_data(as_text=True)

    r = client.post("/analytics/on-the-road-media/videos/bulk",
                    data={"ids": [str(ids[0]), str(ids[1])], "bulk_format": "fan_reaction"}, follow_redirects=True)
    assert "Updated 2 video(s)" in r.get_data(as_text=True)
    assert conn.execute("SELECT COUNT(*) FROM videos WHERE format = 'fan_reaction'").fetchone()[0] == 2

    page = client.get(f"/analytics/videos/{ids[0]}").get_data(as_text=True)
    assert "Snapshots" in page and "Latest numbers" in page
    sid = conn.execute("SELECT id FROM video_snapshots WHERE video_id = ?", (ids[0],)).fetchone()[0]
    client.post(f"/analytics/snapshots/{sid}", data={"captured_on": "2026-11-10", "views": "12500", "likes": ""})
    s = conn.execute("SELECT views, likes FROM video_snapshots WHERE id = ?", (sid,)).fetchone()
    assert s["views"] == 12500 and s["likes"] is None
    client.post(f"/analytics/videos/{ids[0]}/snapshots", data={"captured_on": "2026-11-17", "views": "14000"})
    assert "Views over time" in client.get(f"/analytics/videos/{ids[0]}").get_data(as_text=True)
    client.post(f"/analytics/snapshots/{sid}/delete")
    assert conn.execute("SELECT COUNT(*) FROM video_snapshots WHERE id = ?", (sid,)).fetchone()[0] == 0
    client.post(f"/analytics/videos/{ids[1]}/delete")
    assert conn.execute("SELECT COUNT(*) FROM videos WHERE id = ?", (ids[1],)).fetchone()[0] == 0
    conn.close()


def test_undo_from_dashboard(client, app):
    detail, _ = import_fixture(client, app)
    import_id = detail.rsplit("/", 1)[-1]
    html = client.post(f"/analytics/imports/{import_id}/undo", follow_redirects=True).get_data(as_text=True)
    assert "Import undone" in html
    conn = connect(app.config["DB_PATH"])
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
    conn.close()


def test_manual_entry_form(client, app):
    r = client.post("/analytics", data={"account_id": account_id(app), "title": "Typed in", "views": "1,500",
                                        "follows_gained": "", "duration_seconds": "0:30", "avg_watch_seconds": "12"},
                    follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Metrics recorded" in html and "Typed in" in html
    conn = connect(app.config["DB_PATH"])
    s = conn.execute("SELECT * FROM video_snapshots").fetchone()
    assert s["views"] == 1500 and s["follows_gained"] is None
    conn.close()
    r = client.post("/analytics", data={"account_id": account_id(app), "title": "Bad", "views": "-3"},
                    follow_redirects=True)
    assert "cannot be negative" in r.get_data(as_text=True)


def test_agents_page_shows_analytics_implemented(client):
    html = client.get("/agents").get_data(as_text=True)
    block = html[html.index("Analytics Agent"):html.index("Analytics Agent") + 300]
    assert "a-implemented" in block
