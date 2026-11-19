"""Automatic research while the dashboard is running.

A background thread checks once a minute whether a run is due. It only works while the
app is open on your computer. If the computer is asleep or off, or the app is closed,
nothing runs. Storing the code on GitHub does not run it.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone

from ..db import connect

log = logging.getLogger(__name__)
SETTING = "auto_research_hours"
CHOICES = [0, 1, 2, 3, 6, 12, 24]


def get_setting(conn, key: str, default: str = "") -> str:
    row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute("INSERT INTO app_settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()


def interval_hours(conn) -> int:
    try:
        return int(get_setting(conn, SETTING, "0"))
    except ValueError:
        return 0


def set_interval(conn, hours: int) -> None:
    if int(hours) not in CHOICES:
        raise ValueError("Choose one of the listed intervals.")
    set_setting(conn, SETTING, str(int(hours)))


def next_due(conn, now: datetime | None = None) -> datetime | None:
    """When the next automatic run should start, or None if automatic research is off."""
    hours = interval_hours(conn)
    if hours <= 0:
        return None
    now = now or datetime.now(timezone.utc)
    last = conn.execute("SELECT MAX(started_at) FROM research_runs WHERE trigger IN ('scheduled', 'manual', 'cli')").fetchone()[0]
    if not last:
        return now
    return datetime.fromisoformat(last) + timedelta(hours=hours)


def is_due(conn, now: datetime | None = None) -> bool:
    now = now or datetime.now(timezone.utc)
    nd = next_due(conn, now)
    return nd is not None and nd <= now


class Scheduler:
    def __init__(self, db_path: str, transport=None, check_seconds: int = 60):
        self.db_path, self.transport, self.check_seconds = db_path, transport, check_seconds
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._loop, name="trend-scout-scheduler", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop.set()

    def tick(self) -> int | None:
        """One check. Returns a run id if a run happened."""
        from ..agents.orchestrator import Orchestrator
        from .job import RunInProgress

        conn = connect(self.db_path)
        try:
            if not is_due(conn):
                return None
            try:
                return Orchestrator(conn).run_research(trigger="scheduled", transport=self.transport)
            except RunInProgress:
                return None
        finally:
            conn.close()

    def _loop(self) -> None:
        while not self._stop.wait(self.check_seconds):
            try:
                self.tick()
            except Exception:  # keep the dashboard alive; the failure is recorded on the run
                log.exception("Scheduled research run failed")
