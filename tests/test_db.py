import sqlite3

import pytest

from ontheroadai.db import init_db

EXPECTED_TABLES = {"businesses", "brands", "accounts", "ideas", "agent_tasks", "source_permissions",
                   "idea_sources", "approvals", "analytics", "activity_log"}


def test_all_tables_exist(conn):
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert EXPECTED_TABLES <= names


def test_seed_businesses_brands_accounts(conn):
    slugs = {r[0] for r in conn.execute("SELECT slug FROM businesses")}
    assert slugs == {"on-the-road-media", "gta6-network"}
    gta_brands = {r[0] for r in conn.execute(
        "SELECT br.name FROM brands br JOIN businesses b ON b.id = br.business_id WHERE b.slug='gta6-network'")}
    assert gta_brands == {"GTA 6 Moments", "GTA 6 Discoveries", "GTA Streamer Stories"}
    # OTRM: 1 brand x 5 platforms, GTA: 3 brands x 4 platforms
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 5 + 12
    # nothing is connected to a real platform
    assert conn.execute("SELECT COUNT(*) FROM accounts WHERE status <> 'not_connected'").fetchone()[0] == 0


def test_init_is_idempotent(conn):
    init_db(conn)
    init_db(conn)
    assert conn.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 17


def test_db_trigger_blocks_publish_without_approval(conn):
    from ontheroadai.ideas import create_idea

    idea_id = create_idea(conn, "on-the-road-media", "Raw SQL bypass attempt", "fan_reaction")
    with pytest.raises(sqlite3.IntegrityError, match="Human approval"):
        conn.execute("UPDATE ideas SET status='published' WHERE id=?", (idea_id,))


def test_db_rejects_granted_without_evidence(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO source_permissions (business_id, source_name, status, created_at, updated_at) "
                     "VALUES (1, 'x', 'granted', 'now', 'now')")
