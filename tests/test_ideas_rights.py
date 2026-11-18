import pytest

from ontheroadai import ideas, rights


def test_create_and_get_idea(conn):
    idea_id = ideas.create_idea(conn, "on-the-road-media", "Tailgate hot takes", "street_interview",
                                hook="Who's the most overrated player in the league?",
                                target_platforms=["tiktok", "youtube_shorts"], hook_strength=9)
    idea = ideas.get_idea(conn, idea_id)
    assert idea["status"] == "draft"
    assert idea["target_platforms_list"] == ["tiktok", "youtube_shorts"]
    assert idea["score"] == idea["breakdown"]["total"]
    assert 0 <= idea["score"] <= 100


def test_validation(conn):
    with pytest.raises(ValueError, match="Title"):
        ideas.create_idea(conn, "on-the-road-media", "  ", "street_interview")
    with pytest.raises(ValueError, match="content type"):
        ideas.create_idea(conn, "on-the-road-media", "x", "gta_moment")
    with pytest.raises(ValueError, match="Platform"):
        ideas.create_idea(conn, "gta6-network", "x", "gta_moment", target_platforms=["facebook"])
    with pytest.raises(ValueError, match="between 0 and 10"):
        ideas.create_idea(conn, "gta6-network", "x", "gta_moment", trend_momentum=12)
    with pytest.raises(ValueError, match="Unknown business"):
        ideas.create_idea(conn, "nope", "x", "gta_moment")


def test_brand_must_belong_to_business(conn):
    gta_brand = conn.execute("SELECT id FROM brands WHERE slug='gta6-moments'").fetchone()[0]
    with pytest.raises(ValueError, match="brand"):
        ideas.create_idea(conn, "on-the-road-media", "x", "fan_reaction", brand_id=gta_brand)
    idea_id = ideas.create_idea(conn, "gta6-network", "Police chase glitch", "gta_moment", brand_id=gta_brand)
    assert ideas.get_idea(conn, idea_id)["brand_name"] == "GTA 6 Moments"


def test_update_rescores(conn):
    idea_id = ideas.create_idea(conn, "gta6-network", "Hidden beach easter egg", "gta_discovery")
    before = ideas.get_idea(conn, idea_id)["score"]
    ideas.update_idea(conn, idea_id, hook_strength=10, trend_momentum=10)
    assert ideas.get_idea(conn, idea_id)["score"] > before


def test_status_cannot_be_edited_directly(conn):
    idea_id = ideas.create_idea(conn, "gta6-network", "x", "gta_moment")
    with pytest.raises(ValueError):
        ideas.update_idea(conn, idea_id, status="published")
    with pytest.raises(ValueError):
        ideas.set_status(conn, idea_id, "published")


def test_list_sorted_by_score_and_filtered(conn):
    low = ideas.create_idea(conn, "gta6-network", "Low", "gta_moment", hook_strength=0)
    high = ideas.create_idea(conn, "gta6-network", "High", "gta_moment", hook_strength=10)
    ideas.create_idea(conn, "on-the-road-media", "Other biz", "fan_reaction")
    listed = [i["id"] for i in ideas.list_ideas(conn, "gta6-network")]
    assert listed == [high, low]
    ideas.archive_idea(conn, low)
    assert [i["id"] for i in ideas.list_ideas(conn, "gta6-network")] == [high]


def test_rights_tracking_affects_score(conn):
    idea_id = ideas.create_idea(conn, "gta6-network", "Streamer's 1-in-a-million heist", "streamer_story",
                                needs_external_footage=True)
    no_source = ideas.get_idea(conn, idea_id)["score"]
    src = rights.create_source(conn, "gta6-network", "StreamerX VOD 14 Nov", "creator_permission", "requested")
    rights.link_source(conn, idea_id, src)
    requested = ideas.get_idea(conn, idea_id)["score"]
    assert requested > no_source
    assert rights.rights_check(conn, idea_id)[0] is False

    with pytest.raises(ValueError, match="evidence"):
        rights.update_source_status(conn, src, "granted")
    rights.update_source_status(conn, src, "granted", evidence="DM screenshot saved in /rights/streamerx.png")
    assert ideas.get_idea(conn, idea_id)["score"] > requested
    assert rights.rights_check(conn, idea_id) == (True, [])

    rights.update_source_status(conn, src, "denied")
    ok, problems = rights.rights_check(conn, idea_id)
    assert not ok and "denied" in problems[0]


def test_cannot_link_source_across_businesses(conn):
    idea_id = ideas.create_idea(conn, "gta6-network", "x", "gta_moment", needs_external_footage=True)
    src = rights.create_source(conn, "on-the-road-media", "Our camera", "own_footage", "granted", evidence="ours")
    with pytest.raises(ValueError, match="different businesses"):
        rights.link_source(conn, idea_id, src)
