import json
import os
import runpy
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

import server


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    monkeypatch.setitem(server.app.config, "TESTING", True)
    server.init()
    return server.app.test_client()


def add_post(client, body, kind="post", reply_to=None):
    response = client.post("/api/posts", json={"author": "agent", "body": body,
                                                "kind": kind, "reply_to": reply_to})
    assert response.status_code == 200
    return response.get_json()["id"]


def test_thread_returns_all_descendants_in_id_order_and_isolates_other_threads(client):
    claim = add_post(client, "claim", "claim")
    other = add_post(client, "other claim", "claim")
    evidence = add_post(client, "evidence", "evidence", claim)
    check = add_post(client, "check", "confirm", evidence)
    add_post(client, "other evidence", "evidence", other)
    verdict = add_post(client, "verdict", "verdict", claim)
    followup = add_post(client, "follow-up", "post", check)

    response = client.get("/api/posts", query_string={"thread": claim})
    assert response.status_code == 200
    assert [p["id"] for p in response.get_json()] == [evidence, check, verdict, followup]

    direct = client.get("/api/posts", query_string={"reply_to": claim}).get_json()
    assert [p["id"] for p in direct] == [evidence, verdict]


def test_thread_with_cycle_terminates_and_excludes_root(client):
    claim = add_post(client, "claim", "claim")
    evidence = add_post(client, "evidence", "evidence", claim)
    check = add_post(client, "check", "confirm", evidence)
    with sqlite3.connect(server.DB) as con:
        con.execute("update posts set reply_to=? where id=?", (check, claim))

    # Interrupt a broken recursive query instead of letting a cycle run forever.
    with server.app.app_context():
        server.db().set_progress_handler(lambda: 1, 100000)
        response = client.get("/api/posts", query_string={"thread": claim})
    assert response.status_code == 200
    assert [p["id"] for p in response.get_json()] == [evidence, check]


def test_since_id_combines_with_kind_and_excludes_boundary(client):
    add_post(client, "old claim", "claim")
    boundary = add_post(client, "boundary", "claim")
    first = add_post(client, "new claim", "claim")
    add_post(client, "new evidence", "evidence", first)
    second = add_post(client, "another claim", "claim")

    response = client.get("/api/posts", query_string={"kind": "claim", "since_id": boundary})
    assert response.status_code == 200
    assert [p["id"] for p in response.get_json()] == [first, second]


def test_init_indexes_reply_to_on_existing_database(tmp_path, monkeypatch):
    path = str(tmp_path / "existing.db")
    with sqlite3.connect(path) as con:
        con.execute("create table posts(id integer primary key, author text, reply_to integer, topic text, "
                    "kind text, body text, created real)")
        con.execute("insert into posts(id, body) values(1, 'existing post')")
    monkeypatch.setattr(server, "DB", path)

    server.init()
    server.init()

    with sqlite3.connect(path) as con:
        indexes = con.execute("pragma index_list(posts)").fetchall()
        assert any([row[2] for row in con.execute(f"pragma index_info('{index[1]}')")] == ["reply_to"]
                   for index in indexes)
        plan = con.execute("explain query plan select * from posts where reply_to=?", (1,)).fetchall()
        assert any("USING INDEX" in row[3] and "reply_to" in row[3] for row in plan)
        assert con.execute("select body from posts where id=1").fetchone() == ("existing post",)


def test_round_fetches_new_claims_once_and_each_thread_once(client, tmp_path, monkeypatch):
    shutil.copyfile(Path(server.__file__).with_name("forever.py"), tmp_path / "forever.py")
    (tmp_path / "claims50.txt").write_text("claim one\nclaim two\nmissing claim\n", encoding="utf-8")
    boundary = add_post(client, "old claim", "claim")
    ids = {}

    def run_swarm(*args, **kwargs):
        old_duplicate = add_post(client, "claim one", "claim")
        add_post(client, "FALSE", "verdict", old_duplicate)
        ids["two"] = add_post(client, "claim two", "claim")
        ids["one"] = add_post(client, "claim one", "claim")
        evidence = add_post(client, "evidence", "evidence", ids["one"])
        note = add_post(client, "note", "post", ids["one"])
        add_post(client, "SUPPORTED", "verdict", ids["one"])
        confirmation = add_post(client, "confirm", "confirm", evidence)
        add_post(client, "challenge", "challenge", evidence)
        add_post(client, "nested confirmation", "confirm", confirmation)
        add_post(client, "challenge to note", "challenge", note)
        add_post(client, "FALSE", "verdict", evidence)
        other_evidence = add_post(client, "evidence two", "evidence", ids["two"])
        add_post(client, "confirm two", "confirm", other_evidence)
        add_post(client, "FALSE", "verdict", ids["two"])
        (tmp_path / "stop.txt").touch()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    calls = []

    def get(url, params=None):
        assert url.endswith("/api/posts")
        calls.append(params)
        response = client.get("/api/posts", query_string=params)
        assert response.status_code == 200
        return SimpleNamespace(json=response.get_json)

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(subprocess, "run", run_swarm)
    monkeypatch.setattr(sys, "argv", ["forever.py", "--batch", "3"])
    runpy.run_path(str(tmp_path / "forever.py"), run_name="__main__")

    assert calls == [None, {"kind": "claim", "since_id": boundary},
                     {"thread": ids["one"]}, {"thread": ids["two"]}]
    state = json.loads((tmp_path / "claims_state.json").read_text(encoding="utf-8"))
    assert set(state) == {"claim one", "claim two"}
    assert state["claim one"]["history"][0] == {
        "round": 0, "scouts": 4, "thread": ids["one"], "verdict": "SUPPORTED",
        "confirmed": 1, "challenged": 1,
    }
    assert state["claim two"]["history"][0] == {
        "round": 0, "scouts": 4, "thread": ids["two"], "verdict": "FALSE",
        "confirmed": 1, "challenged": 0,
    }


def test_judge_fetches_complete_thread_once(monkeypatch):
    monkeypatch.setenv("APPDATA", os.environ.get("APPDATA", ""))
    import swarm

    posts = [
        {"id": 2, "reply_to": 1, "kind": "evidence", "author": "a", "body": "first"},
        {"id": 3, "reply_to": 2, "kind": "confirm", "author": "c", "body": "confirmed"},
        {"id": 4, "reply_to": 1, "kind": "evidence", "author": "b", "body": "second"},
        {"id": 5, "reply_to": 4, "kind": "challenge", "author": "d", "body": "challenged"},
        {"id": 6, "reply_to": 3, "kind": "post", "author": "e", "body": "deeper"},
    ]
    calls, prompts, verdicts = [], [], []

    class Response:
        def json(self):
            return posts

    def get(url, params):
        calls.append((url, params))
        return Response()

    monkeypatch.setattr(swarm.requests, "get", get)
    monkeypatch.setattr(swarm, "profile", lambda *args: None)
    monkeypatch.setattr(swarm, "codex", lambda prompt: prompts.append(prompt) or "SUPPORTED")
    monkeypatch.setattr(swarm, "post", lambda *args: verdicts.append(args))

    swarm.judge("judge", 1, "claim", "topic")

    assert calls == [(f"{swarm.API}/posts", {"thread": 1})]
    prompt = prompts[0]
    positions = [prompt.index(body) for body in ("first", "second", "confirmed", "challenged", "deeper")]
    assert positions == sorted(positions)
    assert verdicts == [("judge", "SUPPORTED", "topic", "verdict", 1)]
