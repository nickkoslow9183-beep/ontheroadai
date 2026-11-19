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

-- ===== Milestone 2: performance analytics ==================================
-- One row per uploaded CSV. raw_content is kept only while the import is
-- pending (mapping/preview) and cleared once it is completed or discarded.
CREATE TABLE IF NOT EXISTS analytics_imports (
    id             INTEGER PRIMARY KEY,
    account_id     INTEGER NOT NULL REFERENCES accounts(id),
    filename       TEXT NOT NULL,
    file_sha256    TEXT NOT NULL,
    captured_on    TEXT NOT NULL,
    raw_content    TEXT,
    header_row     INTEGER NOT NULL DEFAULT 1,
    mapping_json   TEXT,
    options_json   TEXT NOT NULL DEFAULT '{}',
    status         TEXT NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'completed', 'undone', 'discarded')),
    row_count      INTEGER NOT NULL DEFAULT 0,
    imported_count INTEGER NOT NULL DEFAULT 0,
    new_video_count INTEGER NOT NULL DEFAULT 0,
    duplicate_count INTEGER NOT NULL DEFAULT 0,
    skipped_count  INTEGER NOT NULL DEFAULT 0,
    error_count    INTEGER NOT NULL DEFAULT 0,
    errors_json    TEXT NOT NULL DEFAULT '[]',
    created_at     TEXT NOT NULL,
    completed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_imports_sha ON analytics_imports(file_sha256, status);

-- A published video on one account. video_key is how re-imports find it:
-- 'id:<platform post id>', 'url:<normalised link>', 'title:<title>|<date>' or 'manual:<random>'.
CREATE TABLE IF NOT EXISTS videos (
    id               INTEGER PRIMARY KEY,
    account_id       INTEGER NOT NULL REFERENCES accounts(id),
    idea_id          INTEGER REFERENCES ideas(id) ON DELETE SET NULL,
    video_key        TEXT NOT NULL,
    external_id      TEXT,
    post_url         TEXT NOT NULL DEFAULT '',
    title            TEXT NOT NULL DEFAULT '',
    topic            TEXT NOT NULL DEFAULT '',
    format           TEXT NOT NULL DEFAULT '',
    published_at     TEXT,
    duration_seconds REAL CHECK (duration_seconds IS NULL OR duration_seconds > 0),
    created_via      TEXT NOT NULL DEFAULT 'manual'
                     CHECK (created_via IN ('manual', 'csv', 'publish', 'legacy')),
    import_id        INTEGER REFERENCES analytics_imports(id) ON DELETE SET NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (account_id, video_key)
);
CREATE INDEX IF NOT EXISTS idx_videos_idea ON videos(idea_id);

-- Point-in-time performance. NULL always means "not available"; it is never
-- replaced by a guess. One snapshot per video per capture date.
CREATE TABLE IF NOT EXISTS video_snapshots (
    id                  INTEGER PRIMARY KEY,
    video_id            INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    captured_on         TEXT NOT NULL,
    views               INTEGER CHECK (views IS NULL OR views >= 0),
    reach               INTEGER CHECK (reach IS NULL OR reach >= 0),
    likes               INTEGER CHECK (likes IS NULL OR likes >= 0),
    comments            INTEGER CHECK (comments IS NULL OR comments >= 0),
    shares              INTEGER CHECK (shares IS NULL OR shares >= 0),
    saves               INTEGER CHECK (saves IS NULL OR saves >= 0),
    follows_gained      INTEGER CHECK (follows_gained IS NULL OR follows_gained >= 0),
    avg_watch_seconds   REAL CHECK (avg_watch_seconds IS NULL OR avg_watch_seconds >= 0),
    completion_rate     REAL CHECK (completion_rate IS NULL OR completion_rate BETWEEN 0 AND 100),
    skip_rate           REAL CHECK (skip_rate IS NULL OR skip_rate BETWEEN 0 AND 100),
    source              TEXT NOT NULL CHECK (source IN ('csv', 'manual', 'legacy')),
    import_id           INTEGER REFERENCES analytics_imports(id) ON DELETE CASCADE,
    legacy_analytics_id INTEGER UNIQUE,
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE (video_id, captured_on)
);
CREATE INDEX IF NOT EXISTS idx_snapshots_import ON video_snapshots(import_id);

-- Remembered column mappings, keyed by the exact set of CSV headers.
CREATE TABLE IF NOT EXISTS mapping_presets (
    id               INTEGER PRIMARY KEY,
    header_signature TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    mapping_json     TEXT NOT NULL,
    options_json     TEXT NOT NULL DEFAULT '{}',
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- ===== Milestone 3: Trend Scout =========================================
CREATE TABLE IF NOT EXISTS research_sources (
    id                   INTEGER PRIMARY KEY,
    key                  TEXT NOT NULL UNIQUE,
    name                 TEXT NOT NULL,
    business             TEXT NOT NULL CHECK (business IN ('on-the-road-media', 'gta6-network', 'both')),
    kind                 TEXT NOT NULL CHECK (kind IN ('rss', 'youtube_channel', 'manual')),
    url                  TEXT NOT NULL DEFAULT '',
    category             TEXT NOT NULL DEFAULT '',
    reliability          TEXT NOT NULL CHECK (reliability IN ('official', 'major_publisher', 'publisher',
                                                              'creator', 'community', 'user')),
    access_note          TEXT NOT NULL DEFAULT '',
    enabled              INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    requires_credentials INTEGER NOT NULL DEFAULT 0 CHECK (requires_credentials IN (0, 1)),
    min_interval_minutes INTEGER NOT NULL DEFAULT 60 CHECK (min_interval_minutes >= 15),
    is_builtin           INTEGER NOT NULL DEFAULT 0,
    etag                 TEXT,
    last_modified        TEXT,
    last_attempt_at      TEXT,
    last_success_at      TEXT,
    last_status          TEXT NOT NULL DEFAULT 'never',
    last_error           TEXT NOT NULL DEFAULT '',
    last_item_count      INTEGER NOT NULL DEFAULT 0,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS research_runs (
    id                INTEGER PRIMARY KEY,
    trigger           TEXT NOT NULL CHECK (trigger IN ('manual', 'scheduled', 'cli', 'test')),
    status            TEXT NOT NULL DEFAULT 'running'
                      CHECK (status IN ('running', 'success', 'partial', 'failed')),
    started_at        TEXT NOT NULL,
    finished_at       TEXT,
    sources_attempted INTEGER NOT NULL DEFAULT 0,
    sources_ok        INTEGER NOT NULL DEFAULT 0,
    sources_failed    INTEGER NOT NULL DEFAULT 0,
    sources_skipped   INTEGER NOT NULL DEFAULT 0,
    items_new         INTEGER NOT NULL DEFAULT 0,
    items_duplicate   INTEGER NOT NULL DEFAULT 0,
    stories_new       INTEGER NOT NULL DEFAULT 0,
    stories_updated   INTEGER NOT NULL DEFAULT 0,
    report_json       TEXT NOT NULL DEFAULT '{}',
    error             TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS research_run_sources (
    run_id      INTEGER NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    source_id   INTEGER NOT NULL REFERENCES research_sources(id) ON DELETE CASCADE,
    status      TEXT NOT NULL,
    http_status INTEGER,
    item_count  INTEGER NOT NULL DEFAULT 0,
    new_items   INTEGER NOT NULL DEFAULT 0,
    error       TEXT NOT NULL DEFAULT '',
    duration_ms INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, source_id)
);

-- One opportunity = one story: related items from one or more sources.
CREATE TABLE IF NOT EXISTS research_stories (
    id                   INTEGER PRIMARY KEY,
    business             TEXT NOT NULL CHECK (business IN ('on-the-road-media', 'gta6-network')),
    category             TEXT NOT NULL DEFAULT '',
    topic_type           TEXT NOT NULL DEFAULT 'general',
    title                TEXT NOT NULL,
    summary              TEXT NOT NULL DEFAULT '',
    keywords_json        TEXT NOT NULL DEFAULT '[]',
    entities_json        TEXT NOT NULL DEFAULT '[]',
    first_seen_at        TEXT NOT NULL,
    last_seen_at         TEXT NOT NULL,
    latest_published_at  TEXT,
    item_count           INTEGER NOT NULL DEFAULT 0,
    source_count         INTEGER NOT NULL DEFAULT 0,
    verification         TEXT NOT NULL DEFAULT 'unverified',
    verification_note    TEXT NOT NULL DEFAULT '',
    status               TEXT NOT NULL DEFAULT 'new' CHECK (status IN ('new', 'updated', 'seen')),
    last_run_id          INTEGER REFERENCES research_runs(id) ON DELETE SET NULL,
    score                INTEGER,
    score_json           TEXT NOT NULL DEFAULT '{}',
    rec_json             TEXT NOT NULL DEFAULT '{}',
    saved                INTEGER NOT NULL DEFAULT 0,
    dismissed            INTEGER NOT NULL DEFAULT 0,
    idea_id              INTEGER REFERENCES ideas(id) ON DELETE SET NULL,
    claims_acknowledged_by TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_stories_business ON research_stories(business, last_seen_at);

CREATE TABLE IF NOT EXISTS research_items (
    id                INTEGER PRIMARY KEY,
    source_id         INTEGER NOT NULL REFERENCES research_sources(id) ON DELETE CASCADE,
    story_id          INTEGER REFERENCES research_stories(id) ON DELETE SET NULL,
    canonical_url     TEXT NOT NULL UNIQUE,
    url               TEXT NOT NULL,
    guid              TEXT NOT NULL DEFAULT '',
    title             TEXT NOT NULL,
    summary           TEXT NOT NULL DEFAULT '',
    author            TEXT NOT NULL DEFAULT '',
    published_at      TEXT,
    fetched_at        TEXT NOT NULL,
    first_run_id      INTEGER REFERENCES research_runs(id) ON DELETE SET NULL,
    title_fingerprint TEXT NOT NULL DEFAULT '',
    relevant          INTEGER NOT NULL DEFAULT 1,
    extra_json        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_items_story ON research_items(story_id);
CREATE INDEX IF NOT EXISTS idx_items_source ON research_items(source_id, published_at);

CREATE TABLE IF NOT EXISTS creators (
    id                  INTEGER PRIMARY KEY,
    name                TEXT NOT NULL CHECK (length(trim(name)) > 0),
    business            TEXT NOT NULL CHECK (business IN ('on-the-road-media', 'gta6-network')),
    category            TEXT NOT NULL CHECK (category IN ('sports_creator', 'sports_media', 'gta_streamer',
                                                           'gta_creator', 'gaming_news')),
    platform            TEXT NOT NULL DEFAULT '',
    profile_url         TEXT NOT NULL DEFAULT '',
    source_id           INTEGER REFERENCES research_sources(id) ON DELETE SET NULL,
    rights_source_id    INTEGER REFERENCES source_permissions(id) ON DELETE SET NULL,
    notes               TEXT NOT NULL DEFAULT '',
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ===== Milestone 4: Video Studio ============================================
-- Media files live on disk under data/media (git-ignored); only paths and metadata are stored here.
CREATE TABLE IF NOT EXISTS studio_uploads (
    token         TEXT PRIMARY KEY,
    filename      TEXT NOT NULL,
    size_expected INTEGER NOT NULL CHECK (size_expected > 0),
    received      INTEGER NOT NULL DEFAULT 0,
    temp_path     TEXT NOT NULL,
    meta_json     TEXT NOT NULL DEFAULT '{}',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studio_recordings (
    id                INTEGER PRIMARY KEY,
    business          TEXT NOT NULL CHECK (business IN ('on-the-road-media', 'gta6-network')),
    kind              TEXT NOT NULL CHECK (kind IN ('podcast', 'sports_story', 'gameplay', 'streamer_footage', 'other')),
    title             TEXT NOT NULL CHECK (length(trim(title)) > 0),
    original_filename TEXT NOT NULL,
    stored_path       TEXT NOT NULL,
    sha256            TEXT NOT NULL UNIQUE,
    size_bytes        INTEGER NOT NULL,
    duration          REAL,
    width             INTEGER,
    height            INTEGER,
    fps               REAL,
    video_codec       TEXT,
    audio_codec       TEXT,
    has_audio         INTEGER NOT NULL DEFAULT 0,
    container         TEXT,
    ownership         TEXT NOT NULL DEFAULT 'own' CHECK (ownership IN ('own', 'third_party')),
    creator           TEXT NOT NULL DEFAULT '',
    source_url        TEXT NOT NULL DEFAULT '',
    rights_source_id  INTEGER REFERENCES source_permissions(id) ON DELETE SET NULL,
    status            TEXT NOT NULL DEFAULT 'uploaded'
                      CHECK (status IN ('uploaded', 'processing', 'ready', 'failed', 'deleted')),
    notes             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studio_transcripts (
    id            INTEGER PRIMARY KEY,
    recording_id  INTEGER NOT NULL UNIQUE REFERENCES studio_recordings(id) ON DELETE CASCADE,
    source        TEXT NOT NULL CHECK (source IN ('whisper', 'import_srt', 'import_vtt', 'import_json')),
    model         TEXT NOT NULL DEFAULT '',
    language      TEXT NOT NULL DEFAULT '',
    segments_json TEXT NOT NULL,
    word_timings  INTEGER NOT NULL DEFAULT 0,
    edited        INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS studio_clips (
    id                 INTEGER PRIMARY KEY,
    recording_id       INTEGER NOT NULL REFERENCES studio_recordings(id) ON DELETE CASCADE,
    start              REAL NOT NULL CHECK (start >= 0),
    end                REAL NOT NULL,
    score              INTEGER,
    score_json         TEXT NOT NULL DEFAULT '{}',
    hook               TEXT NOT NULL DEFAULT '',
    title              TEXT NOT NULL DEFAULT '',
    category           TEXT NOT NULL DEFAULT 'general',
    excerpt            TEXT NOT NULL DEFAULT '',
    context_flags_json TEXT NOT NULL DEFAULT '[]',
    status             TEXT NOT NULL DEFAULT 'candidate'
                       CHECK (status IN ('candidate', 'selected', 'rejected', 'rendered')),
    preset             TEXT NOT NULL DEFAULT 'podcast_clean',
    framing_json       TEXT NOT NULL DEFAULT '{}',
    caption_json       TEXT NOT NULL DEFAULT '{}',
    overlays_json      TEXT NOT NULL DEFAULT '[]',
    tighten_pauses     INTEGER NOT NULL DEFAULT 0,
    origin             TEXT NOT NULL DEFAULT 'auto' CHECK (origin IN ('auto', 'manual')),
    trend_story_id     INTEGER REFERENCES research_stories(id) ON DELETE SET NULL,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL,
    CHECK (end > start)
);
CREATE INDEX IF NOT EXISTS idx_clips_recording ON studio_clips(recording_id, score);

CREATE TABLE IF NOT EXISTS studio_drafts (
    id            INTEGER PRIMARY KEY,
    clip_id       INTEGER NOT NULL REFERENCES studio_clips(id) ON DELETE CASCADE,
    recording_id  INTEGER NOT NULL REFERENCES studio_recordings(id) ON DELETE CASCADE,
    version       INTEGER NOT NULL DEFAULT 1,
    file_path     TEXT,
    size_bytes    INTEGER,
    duration      REAL,
    width         INTEGER,
    height        INTEGER,
    settings_json TEXT NOT NULL DEFAULT '{}',
    status        TEXT NOT NULL DEFAULT 'queued'
                  CHECK (status IN ('queued', 'rendering', 'qc', 'needs_review', 'approved', 'rejected', 'failed')),
    qc_json       TEXT NOT NULL DEFAULT '{}',
    qc_status     TEXT CHECK (qc_status IS NULL OR qc_status IN ('pass', 'warn', 'fail')),
    approved_by   TEXT,
    approved_at   TEXT,
    review_notes  TEXT NOT NULL DEFAULT '',
    idea_id       INTEGER REFERENCES ideas(id) ON DELETE SET NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    -- a draft can only be approved by a named person after QC found no failures
    CHECK (status <> 'approved' OR (approved_by IS NOT NULL AND length(trim(approved_by)) > 0
                                    AND qc_status IN ('pass', 'warn')))
);

CREATE TABLE IF NOT EXISTS studio_jobs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('inspect', 'transcribe', 'find_clips', 'render', 'qc')),
    recording_id INTEGER REFERENCES studio_recordings(id) ON DELETE CASCADE,
    clip_id      INTEGER REFERENCES studio_clips(id) ON DELETE CASCADE,
    draft_id     INTEGER REFERENCES studio_drafts(id) ON DELETE CASCADE,
    status       TEXT NOT NULL DEFAULT 'queued'
                 CHECK (status IN ('queued', 'processing', 'completed', 'failed', 'needs_review', 'cancelled')),
    progress     REAL NOT NULL DEFAULT 0,
    message      TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 2,
    params_json  TEXT NOT NULL DEFAULT '{}',
    heartbeat_at TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON studio_jobs(status, id);
"""

SCHEMA_VERSION = 4


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
    """Create or upgrade the database. Safe to run on a Milestone 1 database."""
    conn.executescript(SCHEMA)
    if seed:
        from .seed import seed_reference_data

        seed_reference_data(conn)
    migrate_legacy_analytics(conn)
    from .trends.sources import seed_sources

    seed_sources(conn)
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def migrate_legacy_analytics(conn: sqlite3.Connection) -> int:
    """Copy Milestone 1 `analytics` rows into videos + snapshots (idempotent).

    The old table is kept untouched; each copied row is remembered by
    video_snapshots.legacy_analytics_id so it is never copied twice.
    """
    rows = conn.execute(
        "SELECT * FROM analytics WHERE id NOT IN "
        "(SELECT legacy_analytics_id FROM video_snapshots WHERE legacy_analytics_id IS NOT NULL)"
    ).fetchall()
    from .analytics.store import video_key

    for r in rows:
        ts = now()
        key = video_key(None, r["post_url"]) or f"legacy:{r['id']}"
        cur = conn.execute(
            "INSERT OR IGNORE INTO videos (account_id, idea_id, video_key, post_url, created_via, created_at, "
            "updated_at) VALUES (?,?,?,?,?,?,?)",
            (r["account_id"], r["idea_id"], key, r["post_url"], "legacy", ts, ts),
        )
        video_id = cur.lastrowid if cur.rowcount else conn.execute(
            "SELECT id FROM videos WHERE account_id = ? AND video_key = ?", (r["account_id"], key)).fetchone()[0]
        # INSERT OR IGNORE: two old rows for the same link on the same date keep the first
        conn.execute(
            "INSERT OR IGNORE INTO video_snapshots (video_id, captured_on, views, likes, comments, shares, saves, "
            "follows_gained, avg_watch_seconds, source, legacy_analytics_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,'legacy',?,?,?)",
            (video_id, r["recorded_on"], r["views"], r["likes"], r["comments"], r["shares"], r["saves"],
             r["follows_gained"], r["avg_watch_seconds"], r["id"], ts, ts),
        )
    return len(rows)


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
