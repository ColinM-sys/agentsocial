import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("FALSE: the claim is unsupported", "FALSE"),
        ("SUPPORTED: evidence shows the claim is not false", "SUPPORTED"),
        ("  misleading: details follow", "MISLEADING"),
        ("UNVERIFIABLE: no evidence", "UNVERIFIABLE"),
        ("UNSUPPORTED: the claim is false", "?"),
        ("FALSEHOOD: the claim is supported", "?"),
        ("The verdict is FALSE", "?"),
    ],
)
def test_score_parses_only_a_leading_standalone_verdict(monkeypatch, body, expected):
    exists = os.path.exists

    def exists_for_import(path):
        if os.path.basename(path) == "stop.txt":
            return True
        if os.path.basename(path) == "claims_state.json":
            return False
        return exists(path)

    monkeypatch.setattr(os.path, "exists", exists_for_import)
    monkeypatch.setattr(sys, "argv", ["forever.py"])
    score = runpy.run_path(str(Path(__file__).with_name("forever.py")))["score"]

    def get_posts(url, params):
        if params == {"kind": "claim"}:
            posts = [{"id": 1, "body": "claim"}, {"id": 2, "body": "claim"}]
        elif params == {"reply_to": 2}:
            posts = [{"id": 3, "kind": "verdict", "body": body}, {"id": 4, "kind": "evidence"}]
        elif params == {"reply_to": 4}:
            posts = [{"kind": "confirm"}, {"kind": "challenge"}]
        else:
            pytest.fail(f"Unexpected request parameters: {params}")
        return SimpleNamespace(json=lambda: posts)

    monkeypatch.setattr(requests, "get", get_posts)
    assert score("claim", 1) == {"verdict": expected, "confirmed": 1, "challenged": 1, "thread": 2}
