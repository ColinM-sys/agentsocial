import json
import runpy
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


@pytest.mark.parametrize(
    ("verdicts", "expected"),
    [
        ([None] * 4, "unresolved"),
        (["UNVERIFIABLE"] * 4, "unresolved"),
        (["SUPPORTED", "FALSE", "SUPPORTED", "FALSE"], "unresolved"),
        (["SUPPORTED", "FALSE", "SUPPORTED", "SUPPORTED"], "settled"),
        (["SUPPORTED", "SUPPORTED"], "settled"),
        (["UNVERIFIABLE"] * 2, "weak"),
    ],
)
def test_round_status_after_verdicts(tmp_path, monkeypatch, verdicts, expected):
    script = tmp_path / "forever.py"
    script.write_text(Path(__file__).with_name("forever.py").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "claims50.txt").write_text("A claim\n", encoding="utf-8")
    attempts = 0

    def run_swarm(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == len(verdicts):
            (tmp_path / "stop.txt").touch()
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def get_posts(url, params=None):
        if params is None:
            posts = []
        elif params == {"kind": "claim"}:
            posts = [{"body": "A claim", "id": 1}]
        else:
            assert params == {"reply_to": 1}
            verdict = verdicts[attempts - 1]
            posts = [] if verdict is None else [{"kind": "verdict", "body": verdict}]
        return SimpleNamespace(json=lambda: posts)

    monkeypatch.setattr(subprocess, "run", run_swarm)
    monkeypatch.setattr(requests, "get", get_posts)
    monkeypatch.setattr(sys, "argv", [str(script)])
    runpy.run_path(str(script), run_name="__main__")

    state = json.loads((tmp_path / "claims_state.json").read_text(encoding="utf-8"))["A claim"]
    assert len(state["history"]) == len(verdicts)
    assert state["status"] == expected
    if expected == "unresolved":
        assert "'unresolved': 1" in (tmp_path / "rounds.log").read_text(encoding="utf-8")
