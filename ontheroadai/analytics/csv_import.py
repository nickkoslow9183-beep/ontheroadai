"""CSV import: upload -> map columns -> preview -> import -> (optionally) undo.

Safety rules:
- Files that are not readable text CSVs, are empty or too big are rejected.
- The exact same file cannot be imported twice (SHA-256 check).
- A video can only have one snapshot per capture date, so overlapping exports
  are reported as duplicates instead of double-counting.
- Rows with unreadable values are skipped and reported. Nothing is guessed.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

from ..db import log, now
from .fields import FIELD_BY_KEY, IDENTITY_KEYS, METRIC_KEYS, auto_map, is_missing, parse_value
from .store import after_data_change, find_or_create_video, video_key

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 20000
MAX_STORED_ERRORS = 200
ALLOWED_EXTENSIONS = (".csv", ".tsv", ".txt")
TOTAL_LABELS = {"total", "totals", "grand total", "sum", "all"}


class ImportRejected(ValueError):
    """The whole file is unusable; nothing was saved."""


# ---- reading files ------------------------------------------------------------

def decode(data: bytes) -> str:
    if not data or not data.strip():
        raise ImportRejected("The file is empty.")
    if len(data) > MAX_BYTES:
        raise ImportRejected(f"The file is larger than {MAX_BYTES // (1024 * 1024)} MB.")
    if data[:4] == b"PK\x03\x04":
        raise ImportRejected("This is an Excel (.xlsx) or zip file, not a CSV. In Excel or Google Sheets use "
                             "File > Save as / Download > CSV, then upload that.")
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if b"\x00" in data[:4096]:
        raise ImportRejected("This looks like a binary file, not a CSV text file.")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return data.decode("cp1252")  # CSVs saved by Excel on Windows
        except UnicodeDecodeError:
            raise ImportRejected("Could not read the file's text encoding. Save it as 'CSV UTF-8'.") from None


def _nonblank_line_starts(text: str) -> list[tuple[int, int]]:
    """[(physical line number, character offset)] for each non-blank line."""
    out, offset = [], 0
    for i, line in enumerate(text.splitlines(keepends=True), start=1):
        if line.strip():
            out.append((i, offset))
        offset += len(line)
    return out


def parse_csv(text: str, header_row: int = 1) -> tuple[list[str], list[tuple[int, dict, bool]]]:
    """Return (headers, [(line_number, {header: value}, has_extra_cells)]).

    header_row counts non-blank lines, so title lines above the column names can be skipped.
    """
    starts = _nonblank_line_starts(text)
    if header_row < 1 or header_row > len(starts):
        raise ImportRejected("The file has no header row at that position.")
    first_line, offset = starts[header_row - 1]
    body = text[offset:]
    try:
        dialect = csv.Sniffer().sniff(body[:20000], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.reader(io.StringIO(body), dialect)
    rows: list[tuple[int, list[str]]] = []
    try:
        for cells in reader:
            rows.append((reader.line_num + first_line - 1, cells))
    except csv.Error as exc:
        raise ImportRejected(f"The CSV is malformed near line {reader.line_num + first_line - 1}: {exc}") from None
    rows = [(n, c) for n, c in rows if any(x.strip() for x in c)]
    raw_headers = [h.strip() for h in rows[0][1]]
    if sum(1 for h in raw_headers if h) < 2:
        raise ImportRejected("The header row must contain column names, with at least two columns. "
                             "If your export has title lines above the column names, change the header row number.")
    headers, seen = [], {}
    for i, h in enumerate(raw_headers):
        h = h or f"Column {i + 1}"
        if h in seen:
            seen[h] += 1
            h = f"{h} ({seen[h]})"
        else:
            seen[h] = 1
        headers.append(h)
    data = []
    for line, cells in rows[1:]:
        extra = len(cells) > len(headers) and any(c.strip() for c in cells[len(headers):])
        padded = (cells + [""] * len(headers))[: len(headers)]
        data.append((line, dict(zip(headers, (c.strip() for c in padded))), extra))
    if not data:
        raise ImportRejected("The file has column names but no data rows.")
    if len(data) > MAX_ROWS:
        raise ImportRejected(f"The file has more than {MAX_ROWS:,} rows. Split it into smaller files.")
    return headers, data


def detect_header_row(text: str, max_scan: int = 10) -> int:
    """First line that looks like column names: >= 2 named columns and a following row of similar width.
    Falls back to 1 so the normal error message explains the problem."""
    for h in range(1, min(max_scan, len(_nonblank_line_starts(text))) + 1):
        try:
            headers, data = parse_csv(text, h)
        except ImportRejected:
            continue
        named = sum(1 for x in headers if not x.startswith("Column "))
        wider = sum(1 for _, _, extra in data if extra)
        if named >= 2 and named >= len(headers) * 0.6 and wider <= len(data) / 2:
            return h
    return 1


def header_signature(headers: list[str]) -> str:
    return hashlib.sha256("\x1f".join(sorted(h.lower() for h in headers)).encode()).hexdigest()


# ---- import lifecycle -----------------------------------------------------------

def start_import(conn: sqlite3.Connection, account_id: int, filename: str, data: bytes,
                 captured_on: str | None = None) -> int:
    filename = (filename or "").strip() or "upload.csv"
    if not filename.lower().endswith(ALLOWED_EXTENSIONS):
        raise ImportRejected("Please upload a .csv file (exported from the platform's analytics).")
    if conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
        raise ImportRejected("Choose which account this export belongs to.")
    captured_on = captured_on or date.today().isoformat()
    try:
        cap = date.fromisoformat(captured_on)
    except ValueError:
        raise ImportRejected("The 'numbers as of' date must be YYYY-MM-DD.") from None
    if cap > date.today() + timedelta(days=1):
        raise ImportRejected("The 'numbers as of' date is in the future.")
    sha = hashlib.sha256(data or b"").hexdigest()
    dup = conn.execute("SELECT id, completed_at FROM analytics_imports WHERE file_sha256 = ? AND status = 'completed'",
                       (sha,)).fetchone()
    if dup:
        raise ImportRejected(f"This exact file was already imported (import #{dup['id']}, "
                             f"{(dup['completed_at'] or '')[:10]}). Undo that import first if you need to redo it.")
    text = decode(data)
    header_row = detect_header_row(text)
    headers, rows = parse_csv(text, header_row)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(timespec="seconds")
    conn.execute("UPDATE analytics_imports SET status = 'discarded', raw_content = NULL "
                 "WHERE status = 'pending' AND created_at < ?", (cutoff,))
    cur = conn.execute(
        "INSERT INTO analytics_imports (account_id, filename, file_sha256, captured_on, raw_content, header_row, "
        "row_count, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (account_id, filename, sha, cap.isoformat(), text, header_row, len(rows), now()))
    log(conn, "import", cur.lastrowid, "uploaded", filename)
    conn.commit()
    return cur.lastrowid


def get_import(conn: sqlite3.Connection, import_id: int) -> dict:
    row = conn.execute(
        "SELECT im.*, a.platform, br.name AS brand_name, b.slug AS business_slug, b.name AS business_name "
        "FROM analytics_imports im JOIN accounts a ON a.id = im.account_id JOIN brands br ON br.id = a.brand_id "
        "JOIN businesses b ON b.id = br.business_id WHERE im.id = ?", (import_id,)).fetchone()
    if row is None:
        raise LookupError(f"Import {import_id} not found")
    d = dict(row)
    d["mapping"] = json.loads(d["mapping_json"]) if d["mapping_json"] else None
    d["options"] = json.loads(d["options_json"] or "{}")
    d["errors"] = json.loads(d["errors_json"] or "[]")
    return d


def list_imports(conn: sqlite3.Connection, business_slug: str | None = None, limit: int = 50) -> list[dict]:
    sql = ("SELECT im.id, im.filename, im.status, im.captured_on, im.row_count, im.imported_count, "
           "im.new_video_count, im.duplicate_count, im.skipped_count, im.error_count, im.created_at, "
           "im.completed_at, a.platform, br.name AS brand_name, b.slug AS business_slug "
           "FROM analytics_imports im JOIN accounts a ON a.id = im.account_id JOIN brands br ON br.id = a.brand_id "
           "JOIN businesses b ON b.id = br.business_id WHERE im.status <> 'discarded' ")
    params: list = []
    if business_slug:
        sql += "AND b.slug = ? "
        params.append(business_slug)
    sql += "ORDER BY im.id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params)]


def load_rows(imp: dict, header_row: int | None = None):
    if imp["raw_content"] is None:
        raise ImportRejected("This upload is no longer available. Please upload the file again.")
    return parse_csv(imp["raw_content"], header_row or imp["header_row"])


def suggest_mapping(conn: sqlite3.Connection, headers: list[str]) -> tuple[dict, dict, str]:
    """Return (mapping, options, source) - a remembered mapping for identical headers, else auto-detected."""
    preset = conn.execute("SELECT * FROM mapping_presets WHERE header_signature = ?",
                          (header_signature(headers),)).fetchone()
    if preset:
        mapping = json.loads(preset["mapping_json"])
        mapping = {k: (v if v in headers else None) for k, v in mapping.items()}
        return mapping, json.loads(preset["options_json"]), f"saved mapping '{preset['name']}'"
    return auto_map(headers), {}, "auto-detected from column names (please check)"


def validate_mapping(mapping: dict, headers: list[str]) -> dict:
    clean = {}
    for key in FIELD_BY_KEY:
        col = mapping.get(key) or None
        if col is not None and col not in headers:
            raise ValueError(f"Column '{col}' is not in this file.")
        clean[key] = col
    used = [c for c in clean.values() if c]
    dupes = {c for c in used if used.count(c) > 1}
    if dupes:
        raise ValueError(f"Each column can only be used once: {', '.join(sorted(dupes))}")
    if not any(clean[k] for k in IDENTITY_KEYS):
        raise ValueError("Map at least one way to identify videos: Post / video ID, Post link, or Title.")
    if not any(clean[k] for k in METRIC_KEYS):
        raise ValueError("Map at least one metric column (for example Views).")
    return clean


def plan_import(conn: sqlite3.Connection, imp: dict, mapping: dict, options: dict,
                header_row: int | None = None) -> dict:
    """Dry run: work out what would happen to every row without writing anything."""
    headers, rows = load_rows(imp, header_row)
    mapping = validate_mapping(mapping, headers)
    fraction = bool(options.get("fraction_percent"))
    day_first = bool(options.get("day_first"))
    planned, seen = [], set()
    counts = {"rows": len(rows), "importable": 0, "new_videos": 0, "duplicates": 0, "skipped": 0, "errors": 0}
    for line, row, extra in rows:
        item = {"line": line, "status": "", "message": "", "title": ""}
        raw = {k: row.get(col) for k, col in mapping.items() if col}
        idents = {(raw.get(k) or "").strip().lower() for k in IDENTITY_KEYS}
        if idents & TOTAL_LABELS:
            item.update(status="skipped", message="Looks like a totals row")
            counts["skipped"] += 1
            planned.append(item)
            continue
        problems, values = [], {}
        if extra:
            problems.append("Row has more cells than there are column names")
        for key, val in raw.items():
            try:
                values[key] = parse_value(key, val, fraction_percent=fraction, day_first=day_first)
            except ValueError as exc:
                problems.append(f"{mapping[key]}: {exc}")
        if values.get("duration_seconds") == 0:
            values["duration_seconds"] = None
            problems.append(f"{mapping['duration_seconds']}: video length cannot be 0")
        metrics = {k: values.get(k) for k in METRIC_KEYS}
        if not problems and all(v is None for v in metrics.values()):
            problems.append("No metric values in this row")
        key = video_key(values.get("post_id"), values.get("post_url"), values.get("title"),
                        values.get("published_at"))
        if key is None:
            problems.append("No post ID, link or title to identify the video")
        item["title"] = values.get("title") or values.get("post_url") or values.get("post_id") or ""
        if problems:
            item.update(status="error", message="; ".join(problems))
            counts["errors"] += 1
            planned.append(item)
            continue
        existing = conn.execute("SELECT id FROM videos WHERE account_id = ? AND video_key = ?",
                                (imp["account_id"], key)).fetchone()
        if key in seen:
            item.update(status="duplicate", message="Same video appears earlier in this file")
            counts["duplicates"] += 1
        elif existing and conn.execute("SELECT 1 FROM video_snapshots WHERE video_id = ? AND captured_on = ?",
                                       (existing["id"], imp["captured_on"])).fetchone():
            item.update(status="duplicate", message=f"Already have numbers for this video as of {imp['captured_on']}")
            counts["duplicates"] += 1
        else:
            item.update(status="new_video" if not existing else "new_snapshot",
                        message="New video" if not existing else "New numbers for a known video")
            counts["importable"] += 1
            counts["new_videos"] += 0 if existing else 1
            item["_key"] = key
            item["_video"] = {
                "external_id": values.get("post_id") or "",
                "post_url": values.get("post_url") or "", "title": values.get("title") or "",
                "topic": values.get("topic") or "", "format": _format_key(conn, imp, values.get("format")),
                "published_at": values.get("published_at"), "duration_seconds": values.get("duration_seconds"),
            }
            item["_metrics"] = metrics
        seen.add(key)
        planned.append(item)
    return {"rows": planned, "counts": counts, "mapping": mapping, "headers": headers}


def _format_key(conn, imp, value):
    """Turn 'Street interview' / 'street_interview' into the business's content-type key when it matches."""
    if not value:
        return ""
    from ..constants import CONTENT_TYPES

    types = CONTENT_TYPES.get(imp["business_slug"], {})
    v = value.strip().lower().replace(" ", "_").replace("-", "_")
    for k, label in types.items():
        if v == k or v == label.lower().replace(" ", "_").replace("-", "_"):
            return k
    return value.strip()


def commit_import(conn: sqlite3.Connection, import_id: int, mapping: dict, options: dict,
                  header_row: int | None = None, preset_name: str | None = None) -> dict:
    imp = get_import(conn, import_id)
    if imp["status"] != "pending":
        raise ImportRejected(f"This import is already {imp['status']}.")
    plan = plan_import(conn, imp, mapping, options, header_row)
    c = plan["counts"]
    errors = [{"line": r["line"], "message": r["message"]} for r in plan["rows"] if r["status"] == "error"]
    if c["importable"] == 0:
        if c["errors"]:
            raise ImportRejected(f"No rows could be imported: {c['errors']} row(s) have problems "
                                 f"(first: line {errors[0]['line']}: {errors[0]['message']}). Check the column mapping.")
        conn.execute("UPDATE analytics_imports SET status = 'discarded', raw_content = NULL WHERE id = ?", (import_id,))
        conn.commit()
        raise ImportRejected(f"Nothing new to import: all {c['duplicates']} row(s) were already imported.")
    touched = []
    try:
        for r in plan["rows"]:
            if r["status"] not in ("new_video", "new_snapshot"):
                continue
            video_id, _ = find_or_create_video(conn, imp["account_id"], r["_key"], r["_video"],
                                               created_via="csv", import_id=import_id)
            cols = ["video_id", "captured_on", *METRIC_KEYS, "source", "import_id", "created_at", "updated_at"]
            ts = now()
            conn.execute(
                f"INSERT INTO video_snapshots ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [video_id, imp["captured_on"], *(r["_metrics"][k] for k in METRIC_KEYS), "csv", import_id, ts, ts])
            touched.append(video_id)
        conn.execute(
            "UPDATE analytics_imports SET status = 'completed', raw_content = NULL, header_row = ?, mapping_json = ?, "
            "options_json = ?, imported_count = ?, new_video_count = ?, duplicate_count = ?, skipped_count = ?, "
            "error_count = ?, errors_json = ?, completed_at = ? WHERE id = ?",
            (header_row or imp["header_row"], json.dumps(plan["mapping"]), json.dumps(options), c["importable"],
             c["new_videos"], c["duplicates"], c["skipped"], c["errors"], json.dumps(errors[:MAX_STORED_ERRORS]),
             now(), import_id))
        sig = header_signature(plan["headers"])
        ts = now()
        conn.execute(
            "INSERT INTO mapping_presets (header_signature, name, mapping_json, options_json, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?) ON CONFLICT(header_signature) DO UPDATE SET mapping_json = excluded.mapping_json, "
            "options_json = excluded.options_json, name = excluded.name, updated_at = excluded.updated_at",
            (sig, (preset_name or f"{imp['brand_name']} {imp['platform']} export").strip(),
             json.dumps(plan["mapping"]), json.dumps(options), ts, ts))
        log(conn, "import", import_id, "completed", f"{c['importable']} rows")
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    after_data_change(conn, touched)
    return c


def undo_import(conn: sqlite3.Connection, import_id: int) -> dict:
    imp = get_import(conn, import_id)
    if imp["status"] != "completed":
        raise ValueError("Only completed imports can be undone.")
    snaps = conn.execute("DELETE FROM video_snapshots WHERE import_id = ?", (import_id,)).rowcount
    vids = conn.execute(
        "DELETE FROM videos WHERE import_id = ? AND idea_id IS NULL AND created_via = 'csv' "
        "AND NOT EXISTS (SELECT 1 FROM video_snapshots s WHERE s.video_id = videos.id)", (import_id,)).rowcount
    conn.execute("UPDATE analytics_imports SET status = 'undone' WHERE id = ?", (import_id,))
    log(conn, "import", import_id, "undone", f"{snaps} snapshots, {vids} videos removed")
    conn.commit()
    return {"snapshots": snaps, "videos": vids}


def discard_import(conn: sqlite3.Connection, import_id: int) -> None:
    imp = get_import(conn, import_id)
    if imp["status"] == "pending":
        conn.execute("UPDATE analytics_imports SET status = 'discarded', raw_content = NULL WHERE id = ?", (import_id,))
        conn.commit()


def sample_rows(rows, headers, n: int = 5) -> list[list[str]]:
    return [[r.get(h, "") for h in headers] for _, r, _ in rows[:n]]


def is_blank(value) -> bool:
    return is_missing(value)
