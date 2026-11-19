"""Video Studio units: validation, transcripts, clip finding, framing, captions (no rendering)."""

import json

import pytest

from ontheroadai.studio import captions, clipfinder, framing, media, transcript
from studio_fixtures import SRT, make_video, words_json


# ---- upload validation & inspection (real ffprobe on generated files) ----------------------

def test_validate_good_video(tmp_path):
    v = make_video(tmp_path / "a.mp4", dur=3)
    pr = media.validate_upload(v, "My Episode.MP4")
    assert (pr.width, pr.height) == (1280, 720) and pr.has_audio and pr.video_codec == "h264"
    assert 2.9 <= pr.duration <= 3.2


def test_validate_rejects_bad_files(tmp_path):
    v = make_video(tmp_path / "a.mp4", dur=3)
    with pytest.raises(media.MediaError, match="MP4, MOV or M4V"):
        media.validate_upload(v, "clip.avi")
    fake = tmp_path / "fake.mp4"
    fake.write_bytes(b"this is just text pretending to be a video" * 10)
    with pytest.raises(media.MediaError, match="isn't really"):
        media.validate_upload(fake, "fake.mp4")
    empty = tmp_path / "empty.mp4"
    empty.write_bytes(b"")
    with pytest.raises(media.MediaError, match="empty"):
        media.validate_upload(empty, "empty.mp4")
    audio_only = tmp_path / "audio.mp4"
    import subprocess
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=d=2", "-c:a", "aac", str(audio_only)],
                   check=True)
    with pytest.raises(media.MediaError, match="no video stream"):
        media.validate_upload(audio_only, "audio.mp4")
    short = make_video(tmp_path / "short.mp4", dur=0.5)
    with pytest.raises(media.MediaError, match="shorter than one second"):
        media.validate_upload(short, "short.mp4")


def test_silence_and_black_detection(tmp_path):
    v = make_video(tmp_path / "b.mp4", dur=4, black_from=2.5)
    black = media.detect_black(v)
    assert black and black[0][0] == pytest.approx(2.5, abs=0.1)
    silent = make_video(tmp_path / "s.mp4", dur=3, audio=False)
    assert media.detect_silence(silent) == []  # no audio stream: nothing reported, not invented


# ---- transcripts -----------------------------------------------------------------------

def test_parse_srt_vtt_json():
    src, segs = transcript.parse_subtitles(SRT, "ep.srt")
    assert src == "import_srt" and len(segs) == 3 and segs[0]["words"] is None
    assert segs[1]["start"] == 2.3 and segs[1]["end"] == 4.6
    vtt = "WEBVTT\n\n00:00.500 --> 00:02.000\nRight host: Steph all day.\n"
    src, segs = transcript.parse_subtitles(vtt, "ep.vtt")
    assert src == "import_vtt" and segs[0]["speaker"] == "Right host" and segs[0]["text"] == "Steph all day."
    src, segs = transcript.parse_subtitles(words_json(), "ep.json")
    assert src == "import_json" and segs[0]["words"][0]["word"] == "Who"
    with pytest.raises(ValueError, match="srt"):
        transcript.parse_subtitles("x", "ep.txt")
    with pytest.raises(ValueError, match="No timed lines"):
        transcript.parse_subtitles("hello", "ep.srt")
    with pytest.raises(ValueError, match="ends before it starts"):
        transcript.parse_subtitles("1\n00:00:05,000 --> 00:00:04,000\nbad\n", "ep.srt")


def test_inconsistent_word_timings_are_dropped_not_repaired():
    data = json.dumps({"segments": [{"start": 1.0, "end": 2.0, "text": "hi there",
                                     "words": [{"start": 1.0, "end": 1.2, "word": "hi"},
                                               {"start": 5.0, "end": 5.3, "word": "there"}]}]})
    _, segs = transcript.parse_subtitles(data, "x.json")
    assert segs[0]["words"] is None


def test_editing_keeps_or_drops_word_timings(conn):
    from ontheroadai.studio import service  # noqa: F401  (ensures tables exist via init)
    conn.execute("INSERT INTO studio_recordings (business, kind, title, original_filename, stored_path, sha256, size_bytes, "
                 "created_at, updated_at) VALUES ('on-the-road-media','podcast','t','t.mp4','x','abc',1,'n','n')")
    _, segs = transcript.parse_subtitles(words_json(), "x.json")
    transcript.save(conn, 1, "import_json", segs)
    # punctuation/capitalisation only: timings kept
    transcript.edit_segment(conn, 1, 0, text="who would you trust with the LAST shot?!")
    t = transcript.get(conn, 1)
    assert t["segments"][0]["words"] and t["segments"][0]["words"][6]["word"] == "LAST"
    # words changed: timings for that line removed, with a note
    res = transcript.edit_segment(conn, 1, 1, text="Level one I'm taking Chick-fil-A, easy pick.")
    t = transcript.get(conn, 1)
    assert t["segments"][1]["words"] is None and "removed" in res["note"] and t["edited"] == 1
    transcript.edit_segment(conn, 1, 2, speaker="Right host")
    assert transcript.get(conn, 1)["segments"][2]["speaker_source"] == "manual"
    with pytest.raises(ValueError):
        transcript.edit_segment(conn, 1, 2, text="   ")


# ---- clip finder ------------------------------------------------------------------------

def _segs(lines, gap=0.4, wps=2.6):
    out, t = [], 0.0
    for i, text in enumerate(lines):
        d = max(1.0, len(text.split()) / wps)
        out.append({"id": i, "start": round(t, 2), "end": round(t + d, 2), "text": text, "words": None})
        t += d + gap
    return out


DEBATE = ["Welcome back, okay let's get into it.",
          "Who would you trust with the last shot in a game seven?",
          "Honestly it has to be Steph, nobody is more automatic.",
          "No way, I'm taking Kobe every single time because he wanted that moment.",
          "That's a hot take and I think it's overrated.",
          "The numbers say Steph is the best shooter ever.",
          "Fair, but that is my final answer.",
          "Alright, next segment, the food draft.",
          "Level one is fast food, what's your pick?",
          "I'm taking Chick fil A, easy pick.",
          "Level two is pizza, I'm going New York style.",
          "That's a crazy good draft, I can't argue with that."]


def test_finds_debate_and_respects_boundaries():
    res = clipfinder.find_candidates(_segs(DEBATE), "podcast_clean", max_candidates=5)
    cands = res["candidates"]
    assert cands, res
    best = cands[0]
    assert best["start"] == pytest.approx(_segs(DEBATE)[1]["start"])          # starts at the question, not the intro
    assert "final answer" in best["excerpt"]                                   # keeps the payoff
    assert "next segment" not in best["excerpt"]                               # never crosses a topic change
    assert best["category"] == "debate" and best["hook"].startswith("Who would you trust")
    assert len(best["factors"]) == 10 and round(sum(f["points"] for f in best["factors"])) == best["score"]
    for a in cands:
        for b in cands:
            if a is not b:
                assert min(a["end"], b["end"]) - max(a["start"], b["start"]) <= 0.3 * min(a["duration"], b["duration"])


def test_never_starts_mid_thought_and_flags_context():
    lines = ["And that's why he left the team.", "He never talked to them again after that.",
             "Like I said earlier, it was about money.", "That's the whole story, period."] * 2
    res = clipfinder.find_candidates(_segs(lines, wps=1.0), "podcast_clean")
    for c in res["candidates"]:
        assert not c["excerpt"].startswith("And ")
    flagged = [f for c in res["candidates"] for f in c["flags"]]
    assert any("refer" in f.lower() for f in flagged)


def test_question_answer_payoff_is_kept():
    segs = _segs(["What's the worst trade in league history?", "Easy.",
                  "It's the one where they gave up three firsts for a backup, no question at all.",
                  "Ok."], wps=0.6)
    w = clipfinder.score_window(segs[:1], clipfinder.PRESETS["podcast_clean"], [], [], None, segs[1])
    assert any("question" in f for f in w["flags"]) and w["factors"][7]["input"] == 3  # payoff factor low


def test_no_speech_and_gameplay_notes():
    assert "nothing to find" in clipfinder.find_candidates([], "gta_gameplay")["notes"][0]
    res = clipfinder.find_candidates(_segs(["Watch this.", "Oh no, the car flipped!"]), "gta_gameplay")
    assert any("Visual moments" in n for n in res["notes"])


def test_trend_scout_topics_boost():
    from ontheroadai.trends.text import keyword_set, named_phrases
    trend = [{"id": 7, "title": "Steph Curry hits game-winner", "kw": keyword_set("Steph Curry hits game-winner shot"),
              "names": set(named_phrases("Steph Curry hits game-winner"))}]
    segs = _segs(DEBATE[1:7])
    w = clipfinder.score_window(segs, clipfinder.PRESETS["podcast_clean"], [], trend, None, None)
    topical = next(f for f in w["factors"] if f["key"] == "topical")
    assert w["trend_story_id"] == 7 and topical["input"] > 5


# ---- framing -----------------------------------------------------------------------------

def test_crop_math():
    assert framing.crop_box(1920, 1080, 0.5) == (606, 1080, 656, 0)
    w, h, x, y = framing.crop_box(1920, 1080, 0.0)
    assert x == 0                                                    # clamped at the left edge
    w, h, x, y = framing.crop_box(1920, 1080, 1.0)
    assert x + w <= 1920                                             # clamped at the right edge
    assert framing.crop_box(1080, 1920, 0.5) == (1080, 1920, 0, 0)   # already vertical: whole frame
    w, h, x, y = framing.crop_box(1920, 1080, 0.27, 1080 / 960, max_w_frac=0.5, cy=0.45)
    assert w == 960 and abs(w / h - 1080 / 960) < 0.01                # split panel = half the frame


def test_pieces_keyframes_and_tightening():
    f = {"mode": "left", "keyframes": [{"t": 2.0, "mode": "right"}, {"t": 5.0, "mode": "both"}]}
    pieces = framing.build_pieces(10.0, 18.0, f, [(10.0, 18.0)])
    assert [(p["src_start"], p["mode"]) for p in pieces] == [(10.0, "left"), (12.0, "right"), (15.0, "both")]
    keep = framing.keep_intervals(10.0, 18.0, [(12.0, 13.5), (16.0, 16.3)], tighten=True)
    assert keep == [(10.0, 12.15), (13.35, 18.0)]                    # 1.5s pause cut to 0.3s; short pause kept
    p2 = framing.build_pieces(10.0, 18.0, f, keep)
    assert framing.output_duration(p2) == pytest.approx(8.0 - 1.2, abs=0.01)
    assert framing.map_time(12.5, p2) is None                        # inside the removed pause
    assert framing.map_time(14.0, p2) == pytest.approx(2.15 + 0.65, abs=0.01)
    with pytest.raises(ValueError):
        framing.normalize({"mode": "auto-track"})


# ---- captions ------------------------------------------------------------------------------

def test_word_captions_never_start_before_the_word():
    _, segs = transcript.parse_subtitles(words_json(), "x.json")
    words = transcript.words_in(segs, 0, 8)
    pieces = framing.build_pieces(0, 8, {"mode": "center"}, [(0, 8)])
    cues = captions.build_cues(words, segs, 0, 8, pieces, {})
    assert cues["timing"] == "word" and cues["cues"]
    for c in cues["cues"]:
        assert c["start"] >= c["words"][0]["start"] - 1e-9
        assert len(c["text"]) <= 22 * 2 + 15
    for a, b in zip(cues["cues"], cues["cues"][1:]):
        assert a["end"] <= b["start"] + 1e-9                       # no overlap


def test_line_captions_when_no_word_timings():
    _, segs = transcript.parse_subtitles(SRT, "x.srt")
    pieces = framing.build_pieces(0, 8, {}, [(0, 8)])
    cues = captions.build_cues([], segs, 0, 8, pieces, {})
    assert cues["timing"] == "line" and [c["start"] for c in cues["cues"]] == [0.2, 2.3, 4.9]
    assert captions.build_cues([], segs, 0, 8, pieces, {"enabled": False})["timing"] == "none"


def test_board_overlay_reveals_only_when_said(tmp_path):
    _, segs = transcript.parse_subtitles(words_json(), "x.json")
    words = transcript.words_in(segs, 0, 8)
    pieces = framing.build_pieces(0, 8, {}, [(0, 8)])
    ov = [{"type": "board", "title": "DRAFT", "lines": [
        {"label": "Level 1", "pick": "Chick-fil-A", "phrase": "chick-fil-a"},
        {"label": "Level 2", "pick": "Pizza", "phrase": "deep dish"}]}]
    events, problems = captions.resolve_overlays(ov, words, pieces, 8.0)
    chick = next(w for w in words if w["word"].lower().startswith("chick"))
    assert events[0]["reveal"] == pytest.approx(chick["start"])
    assert events[1]["reveal"] is None and "deep dish" in problems[0]   # not said: reported, not guessed
    cues = captions.build_cues(words, segs, 0, 8, pieces, {})
    p = tmp_path / "c.ass"
    captions.write_ass(p, cues, {"font": "DejaVu Sans"}, events, hook_text="Who would you trust?")
    ass = p.read_text()
    assert "PlayResX: 1080" in ass and "Level 1: ?" in ass and "Chick-fil-A" in ass and "Level 2: ?" in ass
    assert "Who would you trust?" in ass


def test_caption_style_validation():
    with pytest.raises(ValueError):
        captions.normalize_style({"color": "red"})
    with pytest.raises(ValueError):
        captions.normalize_style({"position": "sideways"})
    s = captions.normalize_style({"size": 500, "font": "Evil{\\font}"})
    assert s["size"] == 120 and "{" not in s["font"]
