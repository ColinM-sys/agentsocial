import json
import runpy
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


def test_incomplete_fourth_round_stays_weak(tmp_path, monkeypatch):
    claim = "A claim"
    (tmp_path / "claims50.txt").write_text(claim + "\n", encoding="utf-8")
    (tmp_path / "claims_state.json").write_text(json.dumps({
        claim: {"status": "weak", "history": [{"verdict": "?"}] * 3}
    }), encoding="utf-8")
    shutil.copyfile(Path(__file__).with_name("forever.py"), tmp_path / "forever.py")

    def get(url, params=None):
        posts = [{"id": 1, "body": claim}] if params == {"kind": "claim"} else []
        return SimpleNamespace(json=lambda: posts)

    def run(*args, **kwargs):
        (tmp_path / "stop.txt").touch()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(requests, "get", get)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(sys, "argv", ["forever.py", "--batch", "1"])
    runpy.run_path(str(tmp_path / "forever.py"), run_name="__main__")

    state = json.loads((tmp_path / "claims_state.json").read_text(encoding="utf-8"))
    assert state[claim]["history"][-1]["verdict"] == "?"
    assert state[claim]["status"] == "weak"


@pytest.mark.parametrize("stage", ["scout", "checker", "judge"])
@pytest.mark.parametrize("exhausted", [False, True])
def test_swarm_retries_timeouts_without_aborting_other_claims(monkeypatch, tmp_path, capsys, stage, exhausted):
    claims_file = tmp_path / "claims.txt"
    claims_file.write_text("timed-out claim\nhealthy claim\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["swarm.py", "--claims", str(claims_file), "--scouts", "1", "--parallel", "1"])

    posts, profiles = [], []
    attempts = Counter()

    def response(value):
        return SimpleNamespace(json=lambda: value)

    def fake_post(url, json):
        if url.endswith("/profiles"):
            profiles.append(json)
            return response(json)
        post = {"id": len(posts) + 1, **json}
        posts.append(post)
        return response(post)

    def fake_get(url, params=None):
        if url.endswith("/profiles"):
            return response([p for p in profiles if p["role"] == params["role"]])
        if url.endswith("/posts"):
            return response([p for p in posts if p["reply_to"] == params["reply_to"]])
        assert url.endswith("/stats")
        return response({"posts": len(posts)})

    def fake_run(args, **kwargs):
        prompt = args[-1]
        role = ("scout" if prompt.startswith("Find the 2 strongest") else
                "checker" if prompt.startswith("Another agent") else "judge")
        claim = "timed-out claim" if "timed-out claim" in prompt else "healthy claim"
        attempts[claim, role] += 1
        assert kwargs["timeout"] == 900
        if claim == "timed-out claim" and role == stage and attempts[claim, role] <= (3 if exhausted else 1):
            raise subprocess.TimeoutExpired(args, kwargs["timeout"])
        body = {"scout": "Evidence", "checker": "CONFIRMED", "judge": "SUPPORTED"}[role]
        return SimpleNamespace(stdout=json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": body}}))

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(subprocess, "run", fake_run)
    runpy.run_path(str(Path(__file__).with_name("swarm.py")), run_name="__main__")

    summary = json.loads(capsys.readouterr().out)
    assert attempts["timed-out claim", stage] == (3 if exhausted else 2)
    assert all(attempts["healthy claim", role] == 1 for role in ("scout", "checker", "judge"))
    assert any(p["topic"] == "claim-2" and p["kind"] == "verdict" for p in posts)
    assert any(p["topic"] == "claim-1" and p["kind"] == "verdict" for p in posts) is not exhausted
    assert summary["failed_claims"] == (["timed-out claim"] if exhausted else [])
