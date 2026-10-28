import json
import runpy
import subprocess
import sys
from types import SimpleNamespace
from urllib.parse import urlsplit

import requests

import server


def test_claim_topics_keep_batches_and_replies_distinct(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    server.init()
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("AGENTSOCIAL_API", "http://test/api")

    def get(url, params=None):
        response = server.app.test_client().get(urlsplit(url).path, query_string=params)
        return SimpleNamespace(json=response.get_json)

    def post(url, json):
        response = server.app.test_client().post(urlsplit(url).path, json=json)
        return SimpleNamespace(json=response.get_json)

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(requests, "post", post)
    codex_output = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "CONFIRMED"}})
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=codex_output))

    batches = [["First batch, first claim", "First batch, second claim"],
               ["Second batch, first claim", "Second batch, second claim"]]
    for index, batch in enumerate(batches):
        claims_file = tmp_path / f"claims-{index}.txt"
        claims_file.write_text("\n".join(batch), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["swarm.py", "--claims", str(claims_file), "--scouts", "1",
                                          "--parallel", "1"])
        runpy.run_path("swarm.py", run_name="__main__")

    client = server.app.test_client()
    claims = client.get("/api/posts", query_string={"kind": "claim"}).get_json()
    assert [claim["body"] for claim in claims] == [claim for batch in batches for claim in batch]
    assert len({claim["topic"] for claim in claims}) == len(claims)

    result_ids = []
    for claim in claims:
        posts = client.get("/api/posts", query_string={"topic": claim["topic"]}).get_json()
        assert [post["kind"] for post in posts] == ["claim", "evidence", "confirm", "verdict"]
        assert posts[0]["id"] == claim["id"]
        assert [post["reply_to"] for post in posts] == [None, claim["id"], posts[1]["id"], claim["id"]]
        result_ids.append({post["id"] for post in posts})
    assert all(a.isdisjoint(b) for i, a in enumerate(result_ids) for b in result_ids[i + 1:])
