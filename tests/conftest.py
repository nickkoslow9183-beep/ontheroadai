import pytest

from ontheroadai.db import connect, init_db


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def app(tmp_path):
    from ontheroadai.web import create_app

    app = create_app(str(tmp_path / "web.db"))
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def make_ready_idea(conn, needs_footage=False, business="on-the-road-media", content_type="street_interview"):
    """Create an idea and push it through production, completing manual steps by hand."""
    from ontheroadai.agents.orchestrator import Orchestrator
    from ontheroadai.ideas import create_idea

    idea_id = create_idea(conn, business, "Fans react to the comeback", content_type,
                          hook="I asked 50 fans one question after the comeback",
                          target_platforms=["tiktok", "instagram"], needs_external_footage=needs_footage)
    orch = Orchestrator(conn)
    orch.plan(idea_id)
    for _ in range(10):
        manual = [t for t in orch.tasks(idea_id) if t["status"] == "manual_required"]
        if not manual:
            break
        orch.complete_manual_task(manual[0]["id"], "Tester")
    return idea_id


@pytest.fixture(autouse=True)
def _no_real_waiting(monkeypatch):
    """Fixture feeds are served instantly; don't wait between requests in tests."""
    from ontheroadai.trends.fetcher import Fetcher

    monkeypatch.setattr(Fetcher, "DEFAULT_HOST_INTERVAL", 0.0)
