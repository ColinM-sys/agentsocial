import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import time
from types import SimpleNamespace

import requests


def test_scheduler_processes_checked_claims_and_stops_when_no_work_remains(tmp_path, monkeypatch):
    script = tmp_path / "forever.py"
    script.write_text(Path(__file__).with_name("forever.py").read_text(encoding="utf-8"), encoding="utf-8")
    claims = ["settled", "exhausted weak", "exhausted checked", "checked first", "checked later",
              "eligible weak", "fresh"]
    (tmp_path / "claims50.txt").write_text("\n".join(claims), encoding="utf-8")
    history = [{"verdict": "SUPPORTED"}]
    state = {
        "settled": {"status": "settled", "history": history * 2},
        "exhausted weak": {"status": "weak", "history": history * 4},
        "exhausted checked": {"status": "checked", "history": history * 4},
        "checked first": {"status": "checked", "history": history},
        "checked later": {"status": "checked", "history": history},
        "eligible weak": {"status": "weak", "history": history},
    }
    (tmp_path / "claims_state.json").write_text(json.dumps(state), encoding="utf-8")

    posts = []
    batches = []

    def get_posts(_url, params=None):
        if params and "reply_to" in params:
            result = [{"kind": "verdict", "body": "SUPPORTED"}]
        else:
            result = list(posts)
        return SimpleNamespace(json=lambda: result)

    def run_swarm(*_args, **_kwargs):
        batch = (tmp_path / "batch.txt").read_text(encoding="utf-8").splitlines()
        batches.extend(batch)
        assert len(batches) <= 5, "scheduler kept running after eligible claims were processed"
        posts.extend({"id": len(posts) + 1, "kind": "claim", "body": claim} for claim in batch)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    real_exists = os.path.exists
    stop_checks = 0

    def exists(path):
        nonlocal stop_checks
        if str(path) == str(tmp_path / "stop.txt"):
            stop_checks += 1
            assert stop_checks <= 10, "scheduler spun without waiting or stopping"
        return real_exists(path)

    monkeypatch.setattr(sys, "argv", [str(script), "--batch", "1"])
    monkeypatch.setattr(requests, "get", get_posts)
    monkeypatch.setattr(subprocess, "run", run_swarm)
    monkeypatch.setattr(os.path, "exists", exists)
    monkeypatch.setattr(time, "sleep", lambda *_: (tmp_path / "stop.txt").touch())
    runpy.run_path(str(script), run_name="__main__")

    assert batches == ["eligible weak", "fresh", "checked first", "checked later", "fresh"]
    final_state = json.loads((tmp_path / "claims_state.json").read_text(encoding="utf-8"))
    assert all(final_state[claim]["status"] == "settled"
               for claim in ("eligible weak", "fresh", "checked first", "checked later"))
    assert len(final_state["settled"]["history"]) == 2
    assert len(final_state["exhausted weak"]["history"]) == 4
    assert len(final_state["exhausted checked"]["history"]) == 4
