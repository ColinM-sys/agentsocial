import os
from types import SimpleNamespace
from unittest.mock import Mock


def test_checker_discovers_only_scouts_for_the_complete_claim_number(monkeypatch):
    monkeypatch.setenv("APPDATA", os.environ.get("APPDATA", ""))
    import swarm

    scouts = [
        {"name": "scout-1-a", "task": "finding evidence supporting claim #1"},
        {"name": "scout-10", "task": "finding evidence supporting claim #10"},
        {"name": "scout-11", "task": "finding evidence against claim #11"},
        {"name": "scout-1-b", "task": "finding evidence against claim #1, in progress"},
    ]
    monkeypatch.setattr(swarm.requests, "get", lambda *args, **kwargs: SimpleNamespace(json=lambda: scouts))
    monkeypatch.setattr(swarm, "thread", lambda claim_id: [])
    checker_profile = Mock()
    monkeypatch.setattr(swarm, "profile", checker_profile)

    swarm.checker("checker-1", 1, "a claim", "claim-1")

    checker_profile.assert_called_once_with(
        "checker-1", "checker", "source verification",
        "checking evidence from scout-1-a, scout-1-b on #1",
    )
