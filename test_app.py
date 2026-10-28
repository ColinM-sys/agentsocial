import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from types import SimpleNamespace

import requests


def test_readme_describes_four_attempt_status_and_fallback(tmp_path, monkeypatch):
    root = Path(__file__).parent
    script = tmp_path / "forever.py"
    script.write_text((root / "forever.py").read_text(encoding="utf-8"), encoding="utf-8")
    claim = "A claim that remains unverifiable"
    (tmp_path / "claims50.txt").write_text(claim + "\n", encoding="utf-8")
    state_path = tmp_path / "claims_state.json"
    stop_path = str(tmp_path / "stop.txt")

    # Bound the loop even if its stop-file behavior changes.
    real_exists = os.path.exists
    stop_checks = 0

    def bounded_exists(path):
        nonlocal stop_checks
        if os.fspath(path) == stop_path:
            stop_checks += 1
            return stop_checks > 5
        return real_exists(path)

    def fake_get(url, params=None):
        if params is None:
            posts = []
        elif params == {"kind": "claim"}:
            posts = [{"id": 1, "body": claim}]
        elif params == {"reply_to": 1}:
            posts = [{"kind": "verdict", "body": "UNVERIFIABLE"}]
        else:
            raise AssertionError(f"Unexpected request parameters: {params}")
        return SimpleNamespace(json=lambda: posts)

    batches = []
    statuses = []

    def fake_run(command, **kwargs):
        batches.append((tmp_path / "batch.txt").read_text(encoding="utf-8").splitlines())
        state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
        statuses.append(state.get(claim, {}).get("status"))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(os.path, "exists", bounded_exists)
    monkeypatch.setattr(requests, "get", fake_get)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", [str(script), "--batch", "1"])
    runpy.run_path(str(script), run_name="__main__")

    assert batches == [[claim]] * 5
    assert statuses == [None, "weak", "weak", "weak", "settled"]
    assert len(json.loads(state_path.read_text(encoding="utf-8"))[claim]["history"]) == 5

    readme = " ".join((root / "README.md").read_text(encoding="utf-8").split())
    assert "After four scored attempts, a claim is marked settled." in readme
    assert "When no weak or fresh claims remain, the first batch is rerun, including settled claims." in readme
