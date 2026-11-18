"""SQLite connection, schema and seed data."""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL
);

-- A brand is one content identity (e.g. "GTA 6 Moments"); it owns one
-- account per platform. The GTA network is designed to grow to ~20 brands.
CREATE TABLE IF NOT EXISTS brands (
    id          INTEGER PRIMARY KEY,
    business_id INTEGER NOT NULL REFERENCES businesses(id) ON DELETE CASCADE,
    slug        TEXT NOT NULL,
    name        TEXT NOT NULL,
    positioning TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'planned'
                CHECK (status IN ('planned', 'active', 'paused')),
    created_at  TEXT NOT NULL,
    UNIQUE (business_id, slug)
);

CREATE TABLE IF NOT EXISTS accounts (
    id          INTEGER PRIMARY KEY,
    brand_id    INTEGER NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
    platform    TEXT NOT NULL
                CHECK (platform IN ('instagram','tiktok','youtube_shorts','facebook','x')),
    handle      TEXT,
    status      TEXT NOT NULL DEFAULT 'not_connected'
                CHECK (status IN ('not_connected', 'active', 'paused')),
    created_at  TEXT NOT NULL,
    UNIQUE (brand_id, platform)
);

CREATE TABLE IF NOT EXISTS ideas (
    id                     INTEGER PRIMARY KEY,
    business_id            INTEGER NOT NULL REFERENCES businesses(id),
    brand_id               INTEGER REFERENCES brands(id) ON DELETE SET NULL,
    title                  TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description            TEXT NOT NULL DEFAULT '',
    content_type           TEXT NOT NULL,
    hook                   TEXT NOT NULL DEFAULT '',
    target_platforms       TEXT NOT NULL DEFAULT '',
    needs_external_footage INTEGER NOT NULL DEFAULT 0 CHECK (needs_external_footage IN (0, 1)),
    trend_momentum         INTEGER NOT NULL DEFAULT 5 CHECK (trend_momentum BETWEEN 0 AND 10),
    audience_fit           INTEGER NOT NULL DEFAULT 5 CHECK (audience_fit BETWEEN 0 AND 10),
    hook_strength          INTEGER NOT NULL DEFAULT 5 CHECK (hook_strength BETWEEN 0 AND 10),
    conversion_potential   INTEGER NOT NULL DEFAULT 5 CHECK (conversion_potential BETWEEN 0 AND 10),
    timeliness             INTEGER NOT NULL DEFAULT 5 CHECK (timeliness BETWEEN 0 AND 10),
    production_ease        INTEGER NOT NULL DEFAULT 5 CHECK (production_ease BETWEEN 0 AND 10),
    score                  INTEGER CHECK (score BETWEEN 0 AND 100),
    score_breakdown        TEXT,
    status                 TEXT NOT NULL DEFAULT 'draft'
                           CHECK (status IN ('draft','in_production','ready_for_review',
                                             'approved','rejected','published','archived')),
    published_platform     TEXT,
    published_url          TEXT,
    published_by           TEXT,
    published_at           TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ideas_business_status ON ideas(business_id, status);

CREATE TABLE IF NOT EXISTS agent_tasks (
    id           INTEGER PRIMARY KEY,
    idea_id      INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
    agent_key    TEXT NOT NULL,
    task_type    TEXT NOT NULL,
    phase        TEXT NOT NULL CHECK (phase IN ('pre_publish', 'post_publish')),
    sequence     INTEGER NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','manual_required','in_progress',
                                   'done','blocked','failed','skipped')),
    output       TEXT NOT NULL DEFAULT '',
    completed_by TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    completed_at TEXT,
    UNIQUE (idea_id, phase, task_type)
);
CREATE INDEX IF NOT EXISTS idx_tasks_idea ON agent_tasks(idea_id);
CREATE INDEX IF NOT EXISTS idx_tasks_agent_status ON agent_tasks(agent_key, status);

-- Footage rights / permission tracking.
CREATE TABLE IF NOT EXISTS source_permissions (
    id            INTEGER PRIMARY KEY,
    business_id   INTEGER NOT NULL REFERENCES businesses(id),
    source_name   TEXT NOT NULL CHECK (length(trim(source_name)) > 0),
    source_type   TEXT NOT NULL DEFAULT 'unknown'
                  CHECK (source_type IN ('own_footage','creator_permission','licensed',
                                         'publisher_press_kit','public_domain','unknown')),
    source_url    TEXT NOT NULL DEFAULT '',
    rights_holder TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'unknown'
                  CHECK (status IN ('unknown','requested','granted','denied','expired')),
    evidence      TEXT NOT NULL DEFAULT '',
    expires_on    TEXT,
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    -- A grant must always say where the proof is.
    CHECK (status <> 'granted' OR length(trim(evidence)) > 0)
);

CREATE TABLE IF NOT EXISTS idea_sources (
    idea_id   INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
    source_id INTEGER NOT NULL REFERENCES source_permissions(id) ON DELETE CASCADE,
    PRIMARY KEY (idea_id, source_id)
);

CREATE TABLE IF NOT EXISTS approvals (
    id         INTEGER PRIMARY KEY,
    idea_id    INTEGER NOT NULL REFERENCES ideas(id) ON DELETE CASCADE,
    decision   TEXT NOT NULL CHECK (decision IN ('approved','rejected','changes_requested')),
    reviewer   TEXT NOT NULL CHECK (length(trim(reviewer)) > 0),
    notes      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analytics (
    id                INTEGER PRIMARY KEY,
    account_id        INTEGER NOT NULL REFERENCES accounts(id),
    idea_id           INTEGER REFERENCES ideas(id) ON DELETE SET NULL,
    post_url          TEXT NOT NULL DEFAULT '',
    recorded_on       TEXT NOT NULL,
    views             INTEGER NOT NULL DEFAULT 0 CHECK (views >= 0),
    likes             INTEGER NOT NULL DEFAULT 0 CHECK (likes >= 0),
    comments          INTEGER NOT NULL DEFAULT 0 CHECK (comments >= 0),
    shares            INTEGER NOT NULL DEFAULT 0 CHECK (shares >= 0),
    saves             INTEGER NOT NULL DEFAULT 0 CHECK (saves >= 0),
    follows_gained    INTEGER NOT NULL DEFAULT 0 CHECK (follows_gained >= 0),
    avg_watch_seconds REAL CHECK (avg_watch_seconds IS NULL OR avg_watch_seconds >= 0),
    source            TEXT NOT NULL DEFAULT 'manual_entry',
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id         INTEGER PRIMARY KEY,
    entity     TEXT NOT NULL,
    entity_id  INTEGER,
    action     TEXT NOT NULL,
    detail     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

-- Defence in depth: even a direct SQL update cannot mark content published
-- unless the most recent human decision on it is 'approved'.
CREATE TRIGGER IF NOT EXISTS trg_publish_requires_approval
BEFORE UPDATE OF status ON ideas
WHEN NEW.status = 'published'
 AND COALESCE((SELECT decision FROM approvals WHERE idea_id = NEW.id
               ORDER BY id DESC LIMIT 1), '') <> 'approved'
BEGIN
    SELECT RAISE(ABORT, 'Human approval is required before content can be marked published');
END;

CREATE TRIGGER IF NOT EXISTS trg_no_insert_as_published
BEFORE INSERT ON ideas
WHEN NEW.status IN ('approved', 'published')
BEGIN
    SELECT RAISE(ABORT, 'New ideas cannot start approved or published');
END;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def default_db_path() -> Path:
    return Path(os.environ.get("ONTHEROADAI_DB", PROJECT_ROOT / "data" / "ontheroadai.db"))


def connect(db_path: str | os.PathLike | None = None) -> sqlite3.Connection:
    path = str(db_path) if db_path is not None else str(default_db_path())
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection, seed: bool = True) -> None:
    conn.executescript(SCHEMA)
    if seed:
        from .seed import seed_reference_data

        seed_reference_data(conn)
    conn.commit()


def log(conn: sqlite3.Connection, entity: str, entity_id: int | None, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO activity_log (entity, entity_id, action, detail, created_at) VALUES (?,?,?,?,?)",
        (entity, entity_id, action, detail, now()),
    )


def get_business(conn: sqlite3.Connection, slug: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM businesses WHERE slug = ?", (slug,)).fetchone()
    if row is None:
        raise ValueError(f"Unknown business: {slug!r}")
    return row
