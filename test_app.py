import time

import pytest

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    server.init()
    return server.app.test_client()


def test_posts_persist_fields_and_defaults(client):
    before = time.time()
    explicit = client.post("/api/posts", json={
        "author": "alice", "reply_to": 42, "topic": "science",
        "kind": "evidence", "body": "A source",
    })
    default = client.post("/api/posts", json={"author": "bob", "body": "Hello"})
    after = time.time()

    assert explicit.status_code == default.status_code == 200
    assert explicit.get_json() == {"id": 1}
    assert default.get_json() == {"id": 2}

    posts = client.get("/api/posts").get_json()
    for post in posts:
        assert before <= post.pop("created") <= after
    assert posts == [
        {"id": 1, "author": "alice", "reply_to": 42, "topic": "science",
         "kind": "evidence", "body": "A source"},
        {"id": 2, "author": "bob", "reply_to": None, "topic": "",
         "kind": "post", "body": "Hello"},
    ]


def test_post_filters_isolate_threads_and_keep_id_order(client):
    def add(author, body, **fields):
        response = client.post("/api/posts", json={"author": author, "body": body, **fields})
        assert response.status_code == 200
        return response.get_json()["id"]

    claim = add("alice", "A claim", topic="science", kind="claim")
    evidence = add("bob", "First source", reply_to=claim, topic="science", kind="evidence")
    other_claim = add("alice", "A claim", topic="science", kind="claim")
    other_reply = add("bob", "Other thread", reply_to=other_claim, topic="science", kind="evidence")
    nested = add("bob", "Nested reply", reply_to=evidence, topic="science", kind="evidence")
    later = add("bob", "Second source", reply_to=claim, topic="science", kind="evidence")
    other_topic = add("bob", "Other topic", reply_to=claim, topic="health", kind="evidence")
    other_kind = add("bob", "Challenge", reply_to=claim, topic="science", kind="challenge")
    other_author = add("carol", "Other author", reply_to=claim, topic="science", kind="evidence")
    default = add("carol", "Plain post")

    ids = [claim, evidence, other_claim, other_reply, nested, later,
           other_topic, other_kind, other_author, default]
    assert ids == list(range(1, 11))

    cases = [
        ({}, ids),
        ({"topic": "science"}, [claim, evidence, other_claim, other_reply, nested, later,
                              other_kind, other_author]),
        ({"kind": "evidence"}, [evidence, other_reply, nested, later, other_topic, other_author]),
        ({"author": "bob"}, [evidence, other_reply, nested, later, other_topic, other_kind]),
        ({"reply_to": claim}, [evidence, later, other_topic, other_kind, other_author]),
        ({"reply_to": other_claim}, [other_reply]),
        ({"reply_to": evidence}, [nested]),
        ({"reply_to": 999}, []),
        ({"topic": "science", "kind": "claim"}, [claim, other_claim]),
        ({"topic": "science", "kind": "evidence", "author": "bob", "reply_to": claim},
         [evidence, later]),
    ]
    # Make an unordered SQLite scan run backwards, so omitting ORDER BY fails the test.
    with server.app.app_context():
        server.db().execute("PRAGMA reverse_unordered_selects = ON")
        for filters, expected in cases:
            response = client.get("/api/posts", query_string=filters)
            assert response.status_code == 200
            assert [post["id"] for post in response.get_json()] == expected
