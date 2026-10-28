from types import SimpleNamespace

import pytest

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    server.init()
    return server.app.test_client()


@pytest.mark.parametrize("reply_to", [
    "",
    "abc",
    "9223372036854775808",
    "-9223372036854775809",
    "9" * 4301,
])
def test_posts_reject_invalid_reply_to(client, reply_to):
    response = client.get("/api/posts", query_string={"reply_to": reply_to})
    assert response.status_code == 400


@pytest.mark.parametrize("reply_to", [-9223372036854775808, 0, 42, 9223372036854775807])
def test_posts_filter_valid_reply_to(client, reply_to):
    matching = client.post("/api/posts", json={
        "author": "tester", "body": "matching", "reply_to": reply_to,
    }).get_json()["id"]
    client.post("/api/posts", json={"author": "tester", "body": "other"})

    response = client.get("/api/posts", query_string={"reply_to": str(reply_to)})
    assert response.status_code == 200
    assert [post["id"] for post in response.get_json()] == [matching]


def test_profile_update_and_role_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    clock = SimpleNamespace(time=lambda: 100.0)
    monkeypatch.setattr(server, "time", clock)
    server.init()
    client = server.app.test_client()

    original = {"name": "alice", "role": "scout", "skills": "search", "task": "explore"}
    other = {"name": "bob", "role": "scout", "skills": "research", "task": "investigate"}
    for profile in (original, other):
        assert client.post("/api/profiles", json=profile).status_code == 200

    before = client.get("/api/profiles").get_json()
    created = next(profile["created"] for profile in before if profile["name"] == "alice")
    assert created == 100.0

    clock.time = lambda: 200.0
    updated = {"name": "alice", "role": "reviewer", "skills": "testing", "task": "review"}
    assert client.post("/api/profiles", json=updated).status_code == 200

    profiles = client.get("/api/profiles").get_json()
    assert len(profiles) == 2
    assert {profile["name"]: profile for profile in profiles} == {
        "alice": {**updated, "created": created},
        "bob": {**other, "created": 100.0},
    }
    assert client.get("/api/profiles", query_string={"role": "scout"}).get_json() == [
        {**other, "created": 100.0}
    ]
    assert client.get("/api/profiles", query_string={"role": "reviewer"}).get_json() == [
        {**updated, "created": created}
    ]
