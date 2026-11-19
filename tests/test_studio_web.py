"""Video Studio dashboard: upload API, pages, clip editor form, rendering, streaming, approvals.
Uses generated fixture videos and imported transcripts; renders are real FFmpeg output."""

import io
import json
import re

import pytest

from ontheroadai.db import connect
from ontheroadai.studio import jobs
from studio_fixtures import make_video, words_json


@pytest.fixture(autouse=True)
def _no_whisper(monkeypatch):
    from ontheroadai.studio import transcript

    def unavailable(*a, **k):
        raise transcript.TranscriptionUnavailable("disabled in fixture tests")
    monkeypatch.setattr(transcript, "transcribe", unavailable)


def run_jobs(app):
    conn = connect(app.config["DB_PATH"])
    try:
        return jobs.run_until_idle(conn)
    finally:
        conn.close()


def upload(client, path, name="episode.mp4", **meta):
    data = path.read_bytes()
    r = client.post("/studio/upload/init", json={"filename": name, "size": len(data), "business": "on-the-road-media",
                                                 "kind": "podcast", "title": "Web fixture", **meta})
    assert r.status_code == 200, r.get_json()
    token, chunk = r.get_json()["token"], 1024 * 64
    for off in range(0, len(data), chunk):
        r = client.put(f"/studio/upload/{token}?offset={off}", data=data[off:off + chunk])
        assert r.status_code == 200, r.get_json()
    return client.post(f"/studio/upload/{token}/complete")


def test_pages_render_empty(client):
    for url in ["/studio", "/studio/recordings", "/studio/jobs", "/studio/drafts", "/studio/approvals",
                "/studio/jobs.json"]:
        assert client.get(url).status_code == 200, url
    html = client.get("/studio").get_data(as_text=True)
    assert "Upload a recording" in html and "faster-whisper" in html and "not saved to GitHub" in html
    for url in ["/studio/recordings/9", "/studio/clips/9", "/studio/drafts/9", "/studio/media/draft/9"]:
        assert client.get(url).status_code == 404, url


def test_upload_api_rejects_bad_input(client, tmp_path):
    r = client.post("/studio/upload/init", json={"filename": "x.avi", "size": 10})
    assert r.status_code == 400 and "MP4" in r.get_json()["error"]
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"not a video at all " * 100)
    r = upload(client, fake, "fake.mp4")
    assert r.status_code == 400 and "isn't really" in r.get_json()["error"]


def test_full_workflow_in_the_browser_routes(client, app, tmp_path):
    r = upload(client, make_video(tmp_path / "ep.mp4", dur=8))
    assert r.status_code == 200
    rid = r.get_json()["recording_id"]
    run_jobs(app)
    page = client.get(f"/studio/recordings/{rid}").get_data(as_text=True)
    assert "Import a transcript" in page or "Import .srt" in page
    # range request support so the browser can seek
    r = client.get(f"/studio/media/recording/{rid}", headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and len(r.data) == 100
    # import a transcript -> candidates
    r = client.post(f"/studio/recordings/{rid}/import", data={"file": (io.BytesIO(words_json().encode()), "t.json")},
                    content_type="multipart/form-data", follow_redirects=True)
    assert "Transcript imported" in r.get_data(as_text=True)
    run_jobs(app)
    tpage = client.get(f"/studio/recordings/{rid}/transcript").get_data(as_text=True)
    assert "Who would you trust" in tpage and "not</b> detected automatically" in tpage
    client.post(f"/studio/recordings/{rid}/transcript/0", data={"text": "Who would you trust with the last shot?",
                                                                 "speaker": "Left host"})
    assert "Left host" in client.get(f"/studio/recordings/{rid}/transcript").get_data(as_text=True)
    srt = client.get(f"/studio/recordings/{rid}/transcript.srt").get_data(as_text=True)
    assert "Left host: Who would you trust" in srt
    # manual clip -> clip editor -> render
    r = client.post(f"/studio/recordings/{rid}/manual_clip", data={"start": "0", "end": "7"})
    cid = int(re.search(r"/studio/clips/(\d+)", r.headers["Location"]).group(1))
    editor = client.get(f"/studio/clips/{cid}").get_data(as_text=True)
    assert "Framing is manual" in editor and "Focus left host" in editor and "Top/bottom split" in editor
    form = {"start": "0", "end": "7", "preset": "podcast_clean", "title": "Last shot", "hook": "Who do you trust?",
            "mode": "left", "x": "0.5", "left_x": "0.3", "right_x": "0.7", "kf_t": ["3", ""], "kf_mode": ["right", "right"],
            "kf_x": ["0.5", "0.5"], "cap_enabled": "1", "font": "DejaVu Sans", "size": "64", "color": "#ffffff",
            "outline_color": "#000000", "outline": "5", "highlight": "word", "highlight_color": "#ffd400",
            "position": "lower", "max_chars": "22", "overlays": "[]", "quality": "fast", "action": "render"}
    r = client.post(f"/studio/clips/{cid}", data=form)
    did = int(re.search(r"/studio/drafts/(\d+)", r.headers["Location"]).group(1))
    assert "Queued" in client.get(f"/studio/drafts/{did}").get_data(as_text=True)
    run_jobs(app)
    dpage = client.get(f"/studio/drafts/{did}").get_data(as_text=True)
    assert "Download MP4" in dpage and "Quality control" in dpage and "1080x1920" in dpage
    assert client.get(f"/studio/media/draft/{did}?download=1").headers["Content-Disposition"].startswith("attachment")
    assert "Last shot" in client.get("/studio/approvals").get_data(as_text=True)
    # approval needs a name; with QC warnings, an explicit acknowledgement
    r = client.post(f"/studio/drafts/{did}/review", data={"decision": "approved", "reviewer": ""}, follow_redirects=True)
    assert "Enter your name" in r.get_data(as_text=True)
    r = client.post(f"/studio/drafts/{did}/review", data={"decision": "approved", "reviewer": "Nick", "ack": "1"},
                    follow_redirects=True)
    assert "Approved by" in r.get_data(as_text=True)
    r = client.post(f"/studio/drafts/{did}/idea", follow_redirects=True)
    assert "Idea created and linked" in r.get_data(as_text=True)
    assert "Video Studio" in client.get("/").get_data(as_text=True)


def test_bad_clip_form_and_job_retry(client, app, tmp_path):
    rid = upload(client, make_video(tmp_path / "e.mp4", dur=4)).get_json()["recording_id"]
    run_jobs(app)
    r = client.post(f"/studio/recordings/{rid}/manual_clip", data={"start": "3", "end": "1"}, follow_redirects=True)
    assert "inside the recording" in r.get_data(as_text=True)
    conn = connect(app.config["DB_PATH"])
    jid = conn.execute("SELECT id FROM studio_jobs WHERE kind = 'transcribe'").fetchone()[0]
    conn.execute("UPDATE studio_jobs SET status = 'failed' WHERE id = ?", (jid,))
    conn.commit()
    r = client.post(f"/studio/jobs/{jid}/retry", follow_redirects=True)
    assert "queued again" in r.get_data(as_text=True)
    assert json.loads(client.get("/studio/jobs.json").get_data())[0]["status"] in ("queued", "needs_review", "completed")
    conn.close()


def test_agents_page_reflects_milestone_4(client):
    html = client.get("/agents").get_data(as_text=True)
    for name in ("Clip Finder", "Video Editor", "Quality Control"):
        block = html[html.index(name):html.index(name) + 200]
        assert "a-implemented" in block, name
    for name in ("Scriptwriter", "Publishing Coordinator", "Growth Optimizer"):
        block = html[html.index(name):html.index(name) + 200]
        assert "a-planned" in block, name
