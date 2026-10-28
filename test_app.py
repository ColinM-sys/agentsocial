import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.setenv("APPDATA", os.environ.get("APPDATA", ""))
    import swarm

    monkeypatch.setattr(swarm.requests, "get", lambda *args, **kwargs: SimpleNamespace(json=lambda: []))
    monkeypatch.setattr(swarm, "profile", lambda *args: None)
    monkeypatch.setattr(swarm, "thread", lambda claim_id: [
        {"id": 2, "kind": "evidence", "author": "scout", "body": "evidence"}
    ])
    post = Mock()
    monkeypatch.setattr(swarm, "post", post)

    def run(body):
        monkeypatch.setattr(swarm, "codex", lambda *args, **kwargs: body)
        swarm.checker("checker", 1, "claim", "topic")

    return run, post


@pytest.mark.parametrize("body", ["", "   ", "Unable to verify", "CONFIRMEDLY true", "CHALLENGER"])
def test_checker_rejects_missing_or_unrecognized_response(checker, body):
    run, post = checker
    with pytest.raises(ValueError):
        run(body)
    post.assert_not_called()


@pytest.mark.parametrize(("body", "kind"), [
    ("CONFIRMED: the quote is correct", "confirm"),
    ("challenge: the quote is missing", "challenge"),
    ("CONFIRMED", "confirm"),
    ("CHALLENGE\nThe source is wrong", "challenge"),
])
def test_checker_posts_explicit_response(checker, body, kind):
    run, post = checker
    run(body)
    post.assert_called_once_with("checker", body, "topic", kind, 2)
