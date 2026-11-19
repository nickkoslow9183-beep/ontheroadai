"""Background processing jobs for the Video Studio.

Jobs live in SQLite, so they survive restarts. One worker thread runs them one at a time
(transcription and rendering are CPU-heavy). The dashboard stays responsive because FFmpeg and
Whisper run in the background. If the app is closed mid-job, the job is put back in the queue
when the app starts again (up to `max_attempts`).

Status words shown to you: queued, processing, needs review, failed, completed.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import traceback
from datetime import datetime, timedelta, timezone

from ..db import connect, log, now

LOG = logging.getLogger(__name__)
STALE_SECONDS = 180


def enqueue(conn: sqlite3.Connection, kind: str, recording_id: int | None = None, clip_id: int | None = None,
            draft_id: int | None = None, params: dict | None = None, message: str = "Waiting to start") -> int:
    dup = conn.execute(
        "SELECT id FROM studio_jobs WHERE kind = ? AND status IN ('queued', 'processing') AND "
        "COALESCE(recording_id, 0) = COALESCE(?, 0) AND COALESCE(clip_id, 0) = COALESCE(?, 0) AND "
        "COALESCE(draft_id, 0) = COALESCE(?, 0)", (kind, recording_id, clip_id, draft_id)).fetchone()
    if dup:
        return dup[0]
    cur = conn.execute(
        "INSERT INTO studio_jobs (kind, recording_id, clip_id, draft_id, params_json, message, created_at) "
        "VALUES (?,?,?,?,?,?,?)", (kind, recording_id, clip_id, draft_id, json.dumps(params or {}), message, now()))
    conn.commit()
    return cur.lastrowid


def claim(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM studio_jobs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
    if row is None:
        return None
    ts = now()
    cur = conn.execute("UPDATE studio_jobs SET status = 'processing', attempts = attempts + 1, started_at = ?, "
                       "heartbeat_at = ?, progress = 0, message = 'Starting', error = '' "
                       "WHERE id = ? AND status = 'queued'", (ts, ts, row["id"]))
    conn.commit()
    if cur.rowcount != 1:
        return None
    return dict(conn.execute("SELECT * FROM studio_jobs WHERE id = ?", (row["id"],)).fetchone())


def update(conn: sqlite3.Connection, job_id: int, progress: float | None = None, message: str | None = None) -> None:
    sets, params = ["heartbeat_at = ?"], [now()]
    if progress is not None:
        sets.append("progress = ?")
        params.append(round(max(0.0, min(1.0, progress)), 4))
    if message is not None:
        sets.append("message = ?")
        params.append(message[:300])
    conn.execute(f"UPDATE studio_jobs SET {', '.join(sets)} WHERE id = ?", [*params, job_id])
    conn.commit()


def finish(conn: sqlite3.Connection, job_id: int, status: str, message: str = "", error: str = "") -> None:
    conn.execute("UPDATE studio_jobs SET status = ?, progress = CASE WHEN ? IN ('completed', 'needs_review') THEN 1 "
                 "ELSE progress END, message = ?, error = ?, finished_at = ? WHERE id = ?",
                 (status, status, message[:300], error[:2000], now(), job_id))
    conn.commit()


def recover(conn: sqlite3.Connection, now_dt: datetime | None = None, all_processing: bool = False) -> int:
    """Re-queue jobs that were interrupted (app closed / crashed). Too many attempts -> failed."""
    now_dt = now_dt or datetime.now(timezone.utc)
    n = 0
    for j in conn.execute("SELECT * FROM studio_jobs WHERE status = 'processing'").fetchall():
        hb = datetime.fromisoformat(j["heartbeat_at"]) if j["heartbeat_at"] else now_dt - timedelta(days=1)
        if not all_processing and now_dt - hb < timedelta(seconds=STALE_SECONDS):
            continue
        if j["attempts"] < j["max_attempts"]:
            conn.execute("UPDATE studio_jobs SET status = 'queued', message = 'Restarting after an interruption' "
                         "WHERE id = ?", (j["id"],))
        else:
            conn.execute("UPDATE studio_jobs SET status = 'failed', error = 'Interrupted too many times', "
                         "finished_at = ? WHERE id = ?", (now(), j["id"]))
            if j["draft_id"]:
                conn.execute("UPDATE studio_drafts SET status = 'failed' WHERE id = ?", (j["draft_id"],))
        n += 1
    conn.commit()
    return n


def run_one(conn: sqlite3.Connection) -> dict | None:
    """Claim and run a single job. Returns the finished job row, or None if the queue is empty."""
    from . import pipeline

    job = claim(conn)
    if job is None:
        return None

    def progress(frac, msg=None):
        update(conn, job["id"], frac, msg)

    try:
        status, message = pipeline.HANDLERS[job["kind"]](conn, job, progress)
        finish(conn, job["id"], status, message)
    except Exception as exc:  # recorded on the job; the worker keeps going
        conn.rollback()
        msg = str(exc) or type(exc).__name__
        LOG.warning("Studio job %s failed: %s", job["id"], msg)
        finish(conn, job["id"], "failed", "Failed: " + msg[:250], traceback.format_exc()[-1500:])
        pipeline.on_failure(conn, job, msg)
    log(conn, "studio_job", job["id"], f"{job['kind']} finished")
    conn.commit()
    return dict(conn.execute("SELECT * FROM studio_jobs WHERE id = ?", (job["id"],)).fetchone())


def run_until_idle(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    done = []
    for _ in range(limit):
        j = run_one(conn)
        if j is None:
            break
        done.append(j)
    return done


class Worker:
    """One background thread processing jobs while the app is running."""

    def __init__(self, db_path: str, poll_seconds: float = 2.0):
        self.db_path, self.poll = db_path, poll_seconds
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        conn = connect(self.db_path)
        try:
            recover(conn, all_processing=True)  # this process just started: nothing can be running
        finally:
            conn.close()
        self.thread = threading.Thread(target=self._loop, name="studio-worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            conn = connect(self.db_path)
            try:
                job = run_one(conn)
            except Exception:
                LOG.exception("Studio worker error")
                job = None
            finally:
                conn.close()
            if job is None:
                self._stop.wait(self.poll)
            else:
                time.sleep(0.1)
