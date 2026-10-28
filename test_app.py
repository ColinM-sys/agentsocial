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
