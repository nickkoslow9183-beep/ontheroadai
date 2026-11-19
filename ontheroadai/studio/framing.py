"""Vertical framing: crop math, layouts, and the timeline of pieces a draft is built from.

Framing is manual (with presets). There is NO automatic speaker tracking: the hosts share one
audio track, so who is talking can't be detected reliably. You choose the focus, and can change
it at specific moments in a clip ("keyframes").

Modes
  left / right / center   9:16 crop centred on that horizontal position
  manual                  9:16 crop centred at x (0 = far left, 1 = far right)
  both                    whole frame, scaled to fit, over a blurred copy of itself (shows both people)
  split                   top half = one position, bottom half = another (stacked two-shot)
"""

from __future__ import annotations

OUT_W, OUT_H = 1080, 1920
MODES = {"left": "Focus left host", "right": "Focus right host", "center": "Center",
         "manual": "Manual position", "both": "Center both (full frame, blurred background)",
         "split": "Top/bottom split (left on top, right below)"}
DEFAULT = {"mode": "center", "x": 0.5, "left_x": 0.27, "right_x": 0.73, "keyframes": []}


def _even(v: float) -> int:
    return max(2, int(v) // 2 * 2)


def normalize(framing: dict | None) -> dict:
    f = {**DEFAULT, **(framing or {})}
    if f["mode"] not in MODES:
        raise ValueError(f"Unknown framing mode {f['mode']!r}")
    for k in ("x", "left_x", "right_x"):
        f[k] = min(1.0, max(0.0, float(f[k])))
    kfs = []
    for kf in f.get("keyframes") or []:
        mode = kf.get("mode", "center")
        if mode not in MODES:
            raise ValueError(f"Unknown framing mode {mode!r} in a framing change")
        kfs.append({"t": max(0.0, float(kf["t"])), "mode": mode, "x": min(1.0, max(0.0, float(kf.get("x", 0.5))))})
    f["keyframes"] = sorted(kfs, key=lambda k: k["t"])
    return f


def position(mode: str, x: float, f: dict) -> float:
    return {"left": f["left_x"], "right": f["right_x"], "center": 0.5}.get(mode, x)


def crop_box(src_w: int, src_h: int, cx: float, aspect: float = OUT_W / OUT_H, max_w_frac: float = 1.0,
             cy: float = 0.5) -> tuple[int, int, int, int]:
    """Largest crop of the given aspect, centred at horizontal fraction cx, clamped to the frame.
    `max_w_frac` limits the width (e.g. 0.5 = never wider than half the frame, so a split panel shows
    one person). Returns (w, h, x, y)."""
    if src_h * aspect > src_w * max_w_frac:  # width-limited: zoom in, keep the aspect
        w = _even(src_w * max_w_frac)
        h = _even(w / aspect)
        x = max(0, min(src_w - w, int(round(cx * src_w - w / 2))))
        y = max(0, min(src_h - h, int(round(cy * src_h - h / 2))))
        return w, h, x // 2 * 2, y // 2 * 2
    if src_w / src_h > aspect:   # landscape source: full height, crop width
        h = _even(src_h)
        w = _even(src_h * aspect)
        x = int(round(cx * src_w - w / 2))
        x = max(0, min(src_w - w, x))
        return w, h, x // 2 * 2, 0
    w = _even(src_w)              # tall source: full width, crop height around the middle
    h = _even(src_w / aspect)
    y = max(0, (src_h - h) // 2)
    return w, h, 0, y // 2 * 2


def piece_filter(mode: str, x: float, f: dict, src_w: int, src_h: int, inp: str, out: str, uid: str) -> str:
    """FFmpeg filter chain turning stream label `inp` into a 1080x1920 stream labelled `out`."""
    if mode == "both":
        return (f"[{inp}]split=2[bg{uid}][fg{uid}];"
                f"[bg{uid}]scale={OUT_W}:{OUT_H}:force_original_aspect_ratio=increase,crop={OUT_W}:{OUT_H},"
                f"boxblur=24:2,eq=brightness=-0.08[bgb{uid}];"
                f"[fg{uid}]scale={OUT_W}:-2:flags=lanczos[fgs{uid}];"
                f"[bgb{uid}][fgs{uid}]overlay=(W-w)/2:(H-h)/2,setsar=1[{out}]")
    if mode == "split":
        half = OUT_H // 2
        # each panel is limited to its half of the frame so it shows one person; faces sit a bit above centre
        tw, th, tx, ty = crop_box(src_w, src_h, f["left_x"], OUT_W / half, max_w_frac=0.5, cy=0.45)
        bw, bh, bx, by = crop_box(src_w, src_h, f["right_x"], OUT_W / half, max_w_frac=0.5, cy=0.45)
        return (f"[{inp}]split=2[t{uid}][b{uid}];"
                f"[t{uid}]crop={tw}:{th}:{tx}:{ty},scale={OUT_W}:{half}:flags=lanczos[ts{uid}];"
                f"[b{uid}]crop={bw}:{bh}:{bx}:{by},scale={OUT_W}:{half}:flags=lanczos[bs{uid}];"
                f"[ts{uid}][bs{uid}]vstack=inputs=2,setsar=1[{out}]")
    w, h, cx, cy = crop_box(src_w, src_h, position(mode, x, f))
    return f"[{inp}]crop={w}:{h}:{cx}:{cy},scale={OUT_W}:{OUT_H}:flags=lanczos,setsar=1[{out}]"


def keep_intervals(start: float, end: float, silences: list[tuple[float, float]] | None, tighten: bool,
                   min_gap: float = 0.7, pad: float = 0.15) -> list[tuple[float, float]]:
    """The parts of [start, end] to keep. With `tighten`, pauses longer than min_gap are shortened to
    2 x pad (jump cuts). Speech is never removed: only detected silence is cut."""
    if not tighten or not silences:
        return [(start, end)]
    cuts = []
    for s, e in sorted(silences):
        s, e = max(s, start), min(e, end)
        if e - s >= min_gap:
            cuts.append((s + pad, e - pad))
    out, cur = [], start
    for s, e in cuts:
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if end > cur:
        out.append((cur, end))
    return [(round(a, 3), round(b, 3)) for a, b in out if b - a >= 0.1]


def build_pieces(start: float, end: float, framing: dict, keep: list[tuple[float, float]]) -> list[dict]:
    """Split the kept intervals at framing changes. Each piece: src_start, src_end, out_start, mode, x."""
    f = normalize(framing)
    changes = [(start + k["t"], k["mode"], k["x"]) for k in f["keyframes"] if start + k["t"] < end]
    pieces, out_t = [], 0.0
    for a, b in keep:
        bounds = sorted({a, b, *[t for t, _, _ in changes if a < t < b]})
        for s, e in zip(bounds, bounds[1:]):
            mode, x = f["mode"], f["x"]
            for t, m, kx in changes:
                if t <= s + 1e-6:
                    mode, x = m, kx
            pieces.append({"src_start": round(s, 3), "src_end": round(e, 3), "out_start": round(out_t, 3),
                           "mode": mode, "x": x})
            out_t += e - s
    return pieces


def map_time(t: float, pieces: list[dict]) -> float | None:
    """Source time -> output time, or None if that moment was cut out."""
    for p in pieces:
        if p["src_start"] - 1e-6 <= t <= p["src_end"] + 1e-6:
            return round(p["out_start"] + (t - p["src_start"]), 3)
    return None


def output_duration(pieces: list[dict]) -> float:
    return round(sum(p["src_end"] - p["src_start"] for p in pieces), 3)
