"""Reference data: the two businesses, their brands and planned accounts.

Seeding is idempotent (INSERT OR IGNORE), so it is safe to run repeatedly.
Accounts are created as 'not_connected' with no handle: nothing here talks
to any social platform.
"""

from .constants import BUSINESS_PLATFORMS, GTA, OTRM
from .db import now

BUSINESSES = [
    (
        OTRM,
        "On The Road Media",
        "Sports content brand: street interviews, fan reactions, game-day coverage, "
        "podcast clips and sports storytelling.",
    ),
    (
        GTA,
        "GTA 6 Content Network",
        "Network of differentiated GTA 6 accounts (designed to scale to ~20 brands).",
    ),
]

BRANDS = [
    (OTRM, "on-the-road-media", "On The Road Media",
     "Flagship sports brand. Real fans, real reactions, on location.", "active"),
    (GTA, "gta6-moments", "GTA 6 Moments",
     "Funny, chaotic and cinematic gameplay moments.", "planned"),
    (GTA, "gta6-discoveries", "GTA 6 Discoveries",
     "Hidden details, easter eggs, map secrets and mechanics explained.", "planned"),
    (GTA, "gta-streamer-stories", "GTA Streamer Stories",
     "Story-driven recaps of streamer gameplay, with creator permission only.", "planned"),
]


def seed_reference_data(conn) -> None:
    ts = now()
    for slug, name, desc in BUSINESSES:
        conn.execute(
            "INSERT OR IGNORE INTO businesses (slug, name, description, created_at) VALUES (?,?,?,?)",
            (slug, name, desc, ts),
        )
    for biz_slug, slug, name, positioning, status in BRANDS:
        biz_id = conn.execute("SELECT id FROM businesses WHERE slug = ?", (biz_slug,)).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO brands (business_id, slug, name, positioning, status, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (biz_id, slug, name, positioning, status, ts),
        )
        brand_id = conn.execute(
            "SELECT id FROM brands WHERE business_id = ? AND slug = ?", (biz_id, slug)
        ).fetchone()[0]
        for platform in BUSINESS_PLATFORMS[biz_slug]:
            conn.execute(
                "INSERT OR IGNORE INTO accounts (brand_id, platform, created_at) VALUES (?,?,?)",
                (brand_id, platform, ts),
            )
