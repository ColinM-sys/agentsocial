import sqlite3

import pytest

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "test.db"))
    monkeypatch.setitem(server.app.config, "TESTING", True)
    server.init()
    with server.app.test_client() as client:
        yield client


@pytest.mark.parametrize("post", [
    {"author": "alice"},
    {"author": "alice", "body": None},
    {"author": "alice", "body": ""},
    {"author": "alice", "body": ["not text"]},
])
def test_post_rejects_invalid_body(client, post):
    assert client.post("/api/posts", json=post).status_code == 400
    assert client.get("/api/posts").get_json() == []


def test_post_rejects_null_json(client):
    response = client.post("/api/posts", data="null", content_type="application/json")
    assert response.status_code == 400
    assert client.get("/api/posts").get_json() == []


def test_feed_handles_existing_null_body(client):
    with sqlite3.connect(server.DB) as connection:
        connection.execute("insert into posts(author, body) values (?, ?)", ("legacy", None))

    assert client.post("/api/posts", json={"author": "alice", "body": "Hello <world>"}).status_code == 200
    posts = client.get("/api/posts").get_json()
    assert [(post["author"], post["body"]) for post in posts] == [
        ("legacy", ""),
        ("alice", "Hello <world>"),
    ]
