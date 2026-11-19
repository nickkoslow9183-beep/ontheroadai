"""Captions and timed text overlays, written as an ASS subtitle file that FFmpeg burns in.

Timing rules:
- With word timings (Whisper), each caption appears at its first word's real start time and
  never earlier. A "word highlight" colours each word exactly while it is spoken.
- Without word timings (imported subtitles, or edited lines), captions use the line's own
  timing and are reported as "line timing". Word timings are never estimated.
- Overlays that should change "when something is said" are pinned to the real time the phrase
  is spoken in the transcript. If the phrase can't be found, they are reported, not guessed.
"""

from __future__ import annotations

import re

from .framing import OUT_H, OUT_W, map_time

NEWLINE = "\\N"  # ASS line break
POSITIONS = {  # (ASS alignment, vertical margin): kept clear of the platform buttons at the bottom
    "lower": (2, 560), "middle": (5, 0), "upper": (8, 360),
}
DEFAULT_STYLE = {"font": "DejaVu Sans", "size": 64, "color": "#FFFFFF", "outline_color": "#000000", "outline": 5,
                 "highlight": "word", "highlight_color": "#FFD400", "position": "lower", "max_chars": 22,
                 "uppercase": False, "enabled": True}


def normalize_style(style: dict | None) -> dict:
    s = {**DEFAULT_STYLE, **(style or {})}
    s["size"] = max(28, min(120, int(s["size"])))
    s["outline"] = max(0, min(12, int(s["outline"])))
    s["max_chars"] = max(10, min(40, int(s["max_chars"])))
    if s["position"] not in POSITIONS:
        raise ValueError("Caption position must be lower, middle or upper.")
    if s["highlight"] not in ("none", "word"):
        raise ValueError("Highlight must be 'none' or 'word'.")
    for k in ("color", "outline_color", "highlight_color"):
        if not re.fullmatch(r"#[0-9A-Fa-f]{6}", str(s[k])):
            raise ValueError(f"{k} must be a colour like #FFFFFF")
    s["font"] = re.sub(r"[,{}\\]", "", str(s["font"]))[:60] or DEFAULT_STYLE["font"]
    s["enabled"] = bool(s["enabled"])
    s["uppercase"] = bool(s["uppercase"])
    return s


def ass_color(hex_rgb: str, alpha: int = 0) -> str:
    r, g, b = hex_rgb[1:3], hex_rgb[3:5], hex_rgb[5:7]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def ass_time(t: float) -> str:
    cs = int(round(max(0.0, t) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def esc(text: str) -> str:
    return text.replace("\\", "/").replace("{", "(").replace("}", ")").replace("\n", " ")


def wrap(words: list[str], max_chars: int, max_lines: int = 2) -> list[list[str]]:
    lines, cur = [], []
    for w in words:
        if cur and len(" ".join(cur + [w])) > max_chars:
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def build_cues(words: list[dict], segments: list[dict], clip_start: float, clip_end: float, pieces: list[dict],
               style: dict) -> dict:
    """Return {"timing": "word"|"line"|"none", "cues": [{start, end, text, words:[{start,end,word}]}]} in
    OUTPUT time."""
    st = normalize_style(style)
    if not st["enabled"]:
        return {"timing": "none", "cues": []}
    mx = st["max_chars"]
    if words:
        mapped = []
        for w in words:
            a, b = map_time(w["start"], pieces), map_time(w["end"], pieces)
            if a is None or b is None:
                continue  # word fell inside a removed pause (shouldn't happen: only silence is cut)
            mapped.append({"start": a, "end": max(b, a + 0.05), "word": w["word"]})
        cues, cur = [], []

        def flush():
            if cur:
                cues.append({"start": cur[0]["start"], "end": cur[-1]["end"], "words": list(cur),
                             "text": " ".join(x["word"] for x in cur)})
                cur.clear()
        for w in mapped:
            if cur:
                gap = w["start"] - cur[-1]["end"]
                line_len = len(" ".join(x["word"] for x in cur + [w]))
                if gap > 0.45 or line_len > mx * 2 or len(cur) >= 7 or cur[-1]["word"].endswith((".", "?", "!")):
                    flush()
            cur.append(w)
        flush()
        for i, c in enumerate(cues):  # short hold so captions don't flash, without overlapping the next one
            nxt = cues[i + 1]["start"] if i + 1 < len(cues) else c["end"] + 0.3
            c["end"] = round(min(c["end"] + 0.25, nxt), 3)
        return {"timing": "word", "cues": cues}
    cues = []
    for s in segments:
        if s["end"] <= clip_start or s["start"] >= clip_end:
            continue
        a = map_time(max(s["start"], clip_start), pieces)
        b = map_time(min(s["end"], clip_end), pieces)
        if a is None or b is None or b <= a:
            continue
        cues.append({"start": a, "end": b, "text": s["text"], "words": None})
    return {"timing": "line", "cues": cues}


def _find_phrase(words: list[dict], phrase: str) -> float | None:
    """Source-time start of the first occurrence of `phrase` in the real word timings.

    Letters and digits are compared with spaces and punctuation removed, so "chick-fil-a" matches
    whether the transcriber produced one word or "chick" / "-fil" / "-a". The time returned is
    always a real word's start time."""
    norm = lambda x: re.sub(r"[^a-z0-9]", "", x.lower())  # noqa: E731
    target = norm(phrase)
    if not target:
        return None
    toks = [norm(w["word"]) for w in words]
    for i in range(len(words)):
        if not toks[i] or not target.startswith(toks[i]):
            continue
        acc = ""
        for j in range(i, len(words)):
            acc += toks[j]
            if acc == target:
                return words[i]["start"]
            if not target.startswith(acc):
                break
    return None


def resolve_overlays(overlays: list[dict], words: list[dict], pieces: list[dict], duration: float) -> tuple[list, list]:
    """Turn overlay definitions into timed events (output time). Returns (events, problems)."""
    events, problems = [], []
    for o in overlays or []:
        pos = o.get("position", "upper")
        if o.get("type") == "board":
            lines = o.get("lines") or []
            for i, ln in enumerate(lines):
                reveal = None
                if ln.get("phrase"):
                    src = _find_phrase(words, ln["phrase"])
                    if src is None:
                        problems.append(f"“{ln['phrase']}” was not found in this clip's word timings. "
                                        "Set its time by hand.")
                    else:
                        reveal = map_time(src, pieces)
                elif ln.get("time") not in (None, ""):
                    reveal = float(ln["time"])
                events.append({"kind": "board", "row": i, "rows": len(lines), "title": o.get("title", ""),
                               "label": ln.get("label", ""), "pick": ln.get("pick", ""), "reveal": reveal,
                               "position": pos, "end": duration})
        else:
            a = float(o.get("start", 0))
            b = float(o.get("end", min(duration, a + 3)))
            if b > a:
                events.append({"kind": "text", "text": o.get("text", ""), "start": a, "end": min(b, duration),
                               "position": pos})
    return events, problems


def write_ass(path, cues: dict, style: dict, overlay_events: list[dict], hook_text: str | None = None,
              hook_seconds: float = 3.0) -> None:
    st = normalize_style(style)
    align, margin_v = POSITIONS[st["position"]]
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {OUT_W}
PlayResY: {OUT_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{st['font']},{st['size']},{ass_color(st['color'])},{ass_color(st['highlight_color'])},{ass_color(st['outline_color'])},&H80000000,-1,0,0,0,100,100,0,0,1,{st['outline']},1,{align},70,70,{margin_v},1
Style: Hook,{st['font']},{int(st['size'] * 1.05)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H99000000,-1,0,0,0,100,100,0,0,3,14,0,8,80,80,220,1
Style: Board,{st['font']},{int(st['size'] * 0.75)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H99000000,-1,0,0,0,100,100,0,0,3,10,0,7,90,90,300,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    hi = ass_color(st["highlight_color"])
    base = ass_color(st["color"])
    lines = []
    up = (lambda t: t.upper()) if st["uppercase"] else (lambda t: t)  # noqa: E731
    for c in cues.get("cues", []):
        if c.get("words") and st["highlight"] == "word":
            ws = c["words"]
            rows = wrap([w["word"] for w in ws], st["max_chars"], 3)
            for k, w in enumerate(ws):
                a = w["start"]
                b = ws[k + 1]["start"] if k + 1 < len(ws) else c["end"]
                parts, idx = [], 0
                for r, row in enumerate(rows):
                    toks = []
                    for tok in row:
                        t = esc(up(tok))
                        toks.append(f"{{\\c{hi}}}{t}{{\\c{base}}}" if idx == k else t)
                        idx += 1
                    parts.append(" ".join(toks))
                text = NEWLINE.join(parts)
                lines.append(f"Dialogue: 1,{ass_time(a)},{ass_time(b)},Cap,,0,0,0,,{text}")
        else:
            rows = wrap(c["text"].split(), st["max_chars"], 4)
            text = NEWLINE.join(esc(up(" ".join(r))) for r in rows)
            lines.append(f"Dialogue: 1,{ass_time(c['start'])},{ass_time(c['end'])},Cap,,0,0,0,,{text}")
    if hook_text:
        lines.append(f"Dialogue: 2,{ass_time(0)},{ass_time(hook_seconds)},Hook,,0,0,0,,{esc(hook_text)}")
    for e in overlay_events:
        if e["kind"] == "text":
            al = {"upper": 8, "middle": 5, "lower": 2}.get(e["position"], 8)
            lines.append(f"Dialogue: 3,{ass_time(e['start'])},{ass_time(e['end'])},Board,,0,0,0,,"
                         f"{{\\an{al}}}{esc(e['text'])}")
        else:
            y = 300 + e["row"] * int(st["size"] * 0.95) + (int(st["size"] * 1.0) if e["title"] else 0)
            pos = f"{{\\an7\\pos(90,{y})}}"
            label = esc(e["label"])
            if e["row"] == 0 and e["title"]:
                lines.append(f"Dialogue: 3,{ass_time(0)},{ass_time(e['end'])},Board,,0,0,0,,"
                             f"{{\\an7\\pos(90,300)}}{esc(e['title'])}")
            if e["reveal"] is None or e["reveal"] > 0:
                until = e["reveal"] if e["reveal"] is not None else e["end"]
                lines.append(f"Dialogue: 3,{ass_time(0)},{ass_time(until)},Board,,0,0,0,,"
                             f"{pos}{{\\alpha&H60&}}{label}: ?")
            if e["reveal"] is not None:
                lines.append(f"Dialogue: 3,{ass_time(e['reveal'])},{ass_time(e['end'])},Board,,0,0,0,,"
                             f"{pos}{label}: {{\\c{hi}}}{esc(e['pick'])}")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(header + "\n".join(lines) + "\n")
