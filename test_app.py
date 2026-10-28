import server


def test_stats_empty_and_populated(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    server.init()
    client = server.app.test_client()

    def stats():
        response = client.get("/api/stats")
        assert response.status_code == 200
        return response.get_json()

    assert stats() == {"profiles": 0, "posts": 0, "by_kind": {}}

    for profile in ({"name": "alice"}, {"name": "bob"}):
        assert client.post("/api/profiles", json=profile).status_code == 200
    assert stats() == {"profiles": 2, "posts": 0, "by_kind": {}}

    assert client.post("/api/profiles", json={"name": "alice", "role": "reviewer"}).status_code == 200
    assert stats() == {"profiles": 2, "posts": 0, "by_kind": {}}

    claim = client.post("/api/posts", json={"author": "alice", "kind": "claim", "body": "A claim"})
    assert claim.status_code == 200
    claim_id = claim.get_json()["id"]
    for post in (
        {"author": "bob", "kind": "claim", "body": "Another claim"},
        {"author": "alice", "kind": "evidence", "body": "Evidence"},
        {"author": "bob", "kind": "evidence", "body": "Evidence reply", "reply_to": claim_id},
        {"author": "bob", "kind": "challenge", "body": "Challenge reply", "reply_to": claim_id},
    ):
        assert client.post("/api/posts", json=post).status_code == 200

    assert stats() == {"profiles": 2, "posts": 5, "by_kind": {"claim": 2, "evidence": 2, "challenge": 1}}
