import json
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
import requests

import server


CLAIM = "A claim to investigate"


def run_rounds(tmp_path, monkeypatch, rounds, initial_state=None):
    script = tmp_path / "forever.py"
    script.write_bytes(Path(__file__).with_name("forever.py").read_bytes())
    (tmp_path / "claims50.txt").write_text(CLAIM + "\n", encoding="utf-8")
    state_path = tmp_path / "claims_state.json"
    if initial_state is not None:
        state_path.write_text(json.dumps(initial_state), encoding="utf-8")

    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    server.init()
    client = server.app.test_client()
    monkeypatch.setattr(sys, "argv", [str(script), "--batch", "1"])

    def get(url, params=None):
        response = client.get(urlsplit(url).path, query_string=params)
        assert response.status_code == 200
        return SimpleNamespace(json=response.get_json)

    snapshots = []

    def run(*args, **kwargs):
        snapshots.append(json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {})
        returncode, verdict = rounds[len(snapshots) - 1]
        if verdict is not False:
            cid = client.post("/api/posts", json={"author": "lead", "kind": "claim", "body": CLAIM}).get_json()["id"]
            client.post("/api/posts", json={"author": "scout", "kind": "evidence", "body": "partial evidence",
                                           "reply_to": cid})
            if verdict is not None:
                client.post("/api/posts", json={"author": "judge", "kind": "verdict", "body": verdict,
                                               "reply_to": cid})
        if len(snapshots) == len(rounds):
            (tmp_path / "stop.txt").touch()
        return SimpleNamespace(returncode=returncode, stdout="", stderr="")

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr("subprocess.run", run)
    runpy.run_path(str(script), run_name="__main__")
    return snapshots, json.loads(state_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("incomplete", [(1, None), (1, "SUPPORTED"), (0, None), (0, "No conclusion"), (0, False)])
def test_incomplete_rounds_retry_without_counting_toward_settlement(tmp_path, monkeypatch, incomplete):
    snapshots, state = run_rounds(tmp_path, monkeypatch, [incomplete] * 4 + [(0, "SUPPORTED")])

    for snapshot in snapshots[1:]:
        assert snapshot[CLAIM] == {"history": [], "status": "weak"}
    assert state[CLAIM]["status"] == "checked"
    assert [entry["verdict"] for entry in state[CLAIM]["history"]] == ["SUPPORTED"]


@pytest.mark.parametrize("incomplete", [(1, "SUPPORTED"), (0, False)])
def test_incomplete_round_requeues_checked_claim(tmp_path, monkeypatch, incomplete):
    history = [{"verdict": "SUPPORTED"}]
    _, state = run_rounds(tmp_path, monkeypatch, [incomplete],
                          {CLAIM: {"history": history, "status": "checked"}})

    assert state[CLAIM] == {"history": history, "status": "weak"}


def test_old_missing_verdicts_do_not_count_as_completed_rounds(tmp_path, monkeypatch):
    history = [{"verdict": "?"}] * 3
    _, state = run_rounds(tmp_path, monkeypatch, [(0, "SUPPORTED")],
                          {CLAIM: {"history": history, "status": "weak"}})

    assert state[CLAIM]["status"] == "checked"
    assert [entry["verdict"] for entry in state[CLAIM]["history"]] == ["?", "?", "?", "SUPPORTED"]


@pytest.mark.parametrize("verdicts", [["SUPPORTED", "SUPPORTED"], ["SUPPORTED", "MISLEADING", "FALSE", "SUPPORTED"],
                                     ["UNVERIFIABLE"] * 4])
def test_completed_rounds_can_settle(tmp_path, monkeypatch, verdicts):
    _, state = run_rounds(tmp_path, monkeypatch, [(0, verdict) for verdict in verdicts])

    assert state[CLAIM]["status"] == "settled"
