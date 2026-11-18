import pytest

from ontheroadai import analytics


def _account(conn, brand_slug="on-the-road-media", platform="tiktok"):
    return conn.execute(
        "SELECT a.id FROM accounts a JOIN brands b ON b.id = a.brand_id WHERE b.slug=? AND a.platform=?",
        (brand_slug, platform)).fetchone()[0]


def test_record_and_summarise(conn):
    acc = _account(conn)
    analytics.record_metrics(conn, acc, "2026-11-01", views=10000, likes=800, comments=50, shares=100,
                             saves=50, follows_gained=120, avg_watch_seconds=7.5)
    analytics.record_metrics(conn, acc, "2026-11-02", views=10000, follows_gained=80)
    [row] = analytics.summary(conn, "on-the-road-media")
    assert row["views"] == 20000 and row["follows_gained"] == 200
    assert row["follows_per_1k_views"] == 10.0
    assert row["engagement_rate"] == 5.0
    assert analytics.summary(conn, "gta6-network") == []


def test_validation(conn):
    acc = _account(conn)
    with pytest.raises(ValueError):
        analytics.record_metrics(conn, acc, views=-1)
    with pytest.raises(ValueError):
        analytics.record_metrics(conn, 99999, views=1)
    with pytest.raises(ValueError):
        analytics.record_metrics(conn, acc, "not-a-date", views=1)
    with pytest.raises(ValueError):
        analytics.record_metrics(conn, acc, impressions=5)


def test_zero_views_does_not_divide_by_zero(conn):
    analytics.record_metrics(conn, _account(conn, "gta6-moments", "x"))
    [row] = analytics.summary(conn, "gta6-network")
    assert row["engagement_rate"] is None and row["follows_per_1k_views"] is None
