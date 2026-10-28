import importlib
import json
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
    server.init()
    with server.app.test_client() as client:
        yield client


def add_post(client, kind, body, reply_to=None):
    response = client.post("/api/posts", json={"author": body, "kind": kind, "body": body, "reply_to": reply_to})
    assert response.status_code == 200
    return response.get_json()["id"]


def connect_requests(monkeypatch, client):
    calls = []

    def get(url, params=None):
        assert url.endswith("/posts")
        calls.append(params)
        response = client.get("/api/posts", query_string=params)
        assert response.status_code == 200
        return SimpleNamespace(json=response.get_json)

    monkeypatch.setattr(requests, "get", get)
    return calls


def run_forever(directory):
    source = Path(__file__).with_name("forever.py").read_text(encoding="utf-8")
    namespace = {"__file__": str(directory / "forever.py"), "__name__": "__main__"}
    exec(compile(source, "forever.py", "exec"), namespace)
    return namespace


def test_posts_filter_new_ids_and_multiple_reply_parents(client):
    first = add_post(client, "claim", "first")
    second = add_post(client, "claim", "second")
    child_second = add_post(client, "evidence", "child of second", second)
    child_first = add_post(client, "evidence", "child of first", first)
    other = add_post(client, "verdict", "verdict", first)

    def ids(params):
        response = client.get("/api/posts", query_string=params)
        assert response.status_code == 200
        return [post["id"] for post in response.get_json()]

    assert ids({"reply_to": first}) == [child_first, other]
    assert ids({"reply_to": [first, second]}) == [child_second, child_first, other]
    assert ids({"reply_to": [first, second], "kind": "evidence", "since_id": child_second}) == [child_first]
    assert ids({"kind": "claim", "since_id": first}) == [second]
    assert ids({"reply_to": [999, 1000]}) == []


def test_init_indexes_replies_in_existing_database(tmp_path, monkeypatch):
    db_path = tmp_path / "existing.db"
    monkeypatch.setattr(server, "DB", str(db_path))
    with sqlite3.connect(db_path) as db:
        db.execute("create table posts(id integer primary key, author text, reply_to integer, topic text, "
                   "kind text, body text, created real)")
        db.execute("insert into posts(id, reply_to) values(1, 42)")

    server.init()
    server.init()
    with sqlite3.connect(db_path) as db:
        assert db.execute("select id from posts where reply_to=?", (42,)).fetchall() == [(1,)]
        plan = db.execute("explain query plan select * from posts where reply_to=?", (42,)).fetchall()
        assert any("USING INDEX" in row[-1].upper() for row in plan)


def test_round_scores_newest_threads_with_bulk_requests(client, tmp_path, monkeypatch):
    add_post(client, "claim", "A")
    old_last_id = add_post(client, "claim", "C")
    calls = connect_requests(monkeypatch, client)
    (tmp_path / "claims50.txt").write_text("A\nB\nC\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["forever.py", "--batch", "3"])
    created = {}

    def run_swarm(*args, **kwargs):
        older_a = add_post(client, "claim", "A")
        add_post(client, "verdict", "FALSE", older_a)
        created["a"] = add_post(client, "claim", "A")
        created["b"] = add_post(client, "claim", "B")
        evidence_a = add_post(client, "evidence", "a evidence", created["a"])
        evidence_b = add_post(client, "evidence", "b evidence", created["b"])
        evidence_a2 = add_post(client, "evidence", "a evidence 2", created["a"])
        note = add_post(client, "post", "note", created["a"])
        add_post(client, "verdict", "SUPPORTED", created["a"])
        add_post(client, "verdict", "FALSE", created["a"])
        add_post(client, "verdict", "MISLEADING", created["b"])
        add_post(client, "confirm", "a confirmed", evidence_a)
        add_post(client, "challenge", "b challenged", evidence_b)
        add_post(client, "challenge", "a challenged", evidence_a2)
        add_post(client, "confirm", "not evidence", note)
        (tmp_path / "stop.txt").touch()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", run_swarm)
    run_forever(tmp_path)

    state = json.loads((tmp_path / "claims_state.json").read_text(encoding="utf-8"))
    assert set(state) == {"A", "B"}
    assert {k: state["A"]["history"][0][k] for k in ("thread", "verdict", "confirmed", "challenged")} == {
        "thread": created["a"], "verdict": "SUPPORTED", "confirmed": 1, "challenged": 1}
    assert {k: state["B"]["history"][0][k] for k in ("thread", "verdict", "confirmed", "challenged")} == {
        "thread": created["b"], "verdict": "MISLEADING", "confirmed": 0, "challenged": 1}
    assert len(calls) == 4  # initial watermark, new claims, direct replies, evidence replies
    assert calls[1] == {"kind": "claim", "since_id": old_last_id}


def test_scoring_skips_empty_bulk_lookups(client, tmp_path, monkeypatch):
    (tmp_path / "claims50.txt").write_text("claim\n", encoding="utf-8")
    (tmp_path / "stop.txt").touch()
    monkeypatch.setattr(sys, "argv", ["forever.py"])
    calls = connect_requests(monkeypatch, client)
    score = run_forever(tmp_path)["score"]

    assert score(["claim"], 0) == {}
    assert calls == [{"kind": "claim", "since_id": 0}]
    calls.clear()
    claim = add_post(client, "claim", "claim")
    add_post(client, "verdict", "FALSE", claim)
    assert score(["claim"], 0) == {
        "claim": {"thread": claim, "verdict": "FALSE", "confirmed": 0, "challenged": 0}}
    assert calls == [{"kind": "claim", "since_id": 0}, {"reply_to": [claim]}]


def test_judge_bulk_fetches_replies_in_original_thread_order(client, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    swarm = importlib.import_module("swarm")
    claim = add_post(client, "claim", "claim")
    first = add_post(client, "evidence", "first evidence", claim)
    second = add_post(client, "evidence", "second evidence", claim)
    add_post(client, "challenge", "second reply", second)
    add_post(client, "confirm", "first reply", first)
    calls = connect_requests(monkeypatch, client)
    prompts, verdicts = [], []
    monkeypatch.setattr(swarm, "profile", lambda *args: None)
    monkeypatch.setattr(swarm, "codex", lambda prompt: prompts.append(prompt) or "SUPPORTED")
    monkeypatch.setattr(swarm, "post", lambda *args: verdicts.append(args))

    swarm.judge("judge", claim, "claim", "topic")

    assert len(calls) == 2
    assert calls[0] == {"reply_to": claim}
    assert calls[1] == {"reply_to": [first, second]}
    assert prompts[0].index("first reply") < prompts[0].index("second reply")
    assert verdicts == [("judge", "SUPPORTED", "topic", "verdict", claim)]


def test_judge_does_not_fetch_all_posts_for_empty_thread(client, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    swarm = importlib.import_module("swarm")
    claim = add_post(client, "claim", "claim")
    add_post(client, "post", "unrelated")
    calls = connect_requests(monkeypatch, client)
    prompts = []
    monkeypatch.setattr(swarm, "profile", lambda *args: None)
    monkeypatch.setattr(swarm, "codex", lambda prompt: prompts.append(prompt) or "UNVERIFIABLE")
    monkeypatch.setattr(swarm, "post", lambda *args: None)

    swarm.judge("judge", claim, "claim", "topic")

    assert calls == [{"reply_to": claim}]
    assert "unrelated" not in prompts[0]
