import json
import os
import subprocess
from types import SimpleNamespace

import pytest

os.environ.setdefault("APPDATA", "")
import swarm


def agent_output(text):
    return json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}})


@pytest.fixture
def claim_run(monkeypatch):
    posts = []
    stages = []
    responses = {
        "scout": "Evidence with a source",
        "checker": "CONFIRMED: the quote matches",
        "judge": "SUPPORTED: the evidence supports the claim",
    }
    failure = {}

    def run(args, **kwargs):
        prompt = args[-1]
        stage = ("scout" if prompt.startswith("Find the 2") else
                 "checker" if prompt.startswith("Another agent") else "judge")
        stages.append(stage)
        if failure.get("stage") == stage:
            if failure["mode"] == "timeout":
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            if failure["mode"] == "nonzero":
                return SimpleNamespace(returncode=1, stdout=agent_output(responses[stage]))
            if failure["mode"] == "missing":
                return SimpleNamespace(returncode=0, stdout='log line\n{"type": "turn.completed"}')
            if failure["mode"] == "malformed":
                return SimpleNamespace(returncode=0, stdout='null\n[]\n{"type":"item.completed"}\n'
                                       '{"type":"item.completed","item":{"type":"agent_message","text":null}}\n'
                                       '{"type":"turn.completed","usage":null}\n'
                                       '{"type":"turn.completed","usage":{"input_tokens":"invalid"}}')
            return SimpleNamespace(returncode=0, stdout=agent_output("  "))
        return SimpleNamespace(returncode=0, stdout=agent_output(responses[stage]))

    def post(author, body, topic, kind="post", reply_to=None):
        item = dict(id=len(posts) + 1, author=author, body=body, topic=topic, kind=kind, reply_to=reply_to)
        posts.append(item)
        return item["id"]

    monkeypatch.setattr(swarm.subprocess, "run", run)
    monkeypatch.setattr(swarm, "post", post)
    monkeypatch.setattr(swarm, "profile", lambda *args: None)
    monkeypatch.setattr(swarm, "thread", lambda pid: [p for p in posts if p["reply_to"] == pid])
    monkeypatch.setattr(swarm.requests, "get", lambda *args, **kwargs: SimpleNamespace(json=lambda: []))
    return posts, stages, responses, failure


@pytest.mark.parametrize("stage", ["scout", "checker", "judge"])
@pytest.mark.parametrize("mode", ["nonzero", "empty", "missing", "malformed", "timeout"])
def test_agent_failure_marks_claim_failed_without_posting_invalid_results(claim_run, stage, mode):
    posts, stages, _, failure = claim_run
    failure.update(stage=stage, mode=mode)

    swarm.run_claim(1, "A claim", 1)

    assert stages == ["scout", "checker", "judge"][:["scout", "checker", "judge"].index(stage) + 1]
    assert posts[-1]["kind"] == "failure"
    assert posts[-1]["reply_to"] == posts[0]["id"]
    assert all(p["body"].strip() for p in posts)
    assert not any(p["kind"] == "verdict" for p in posts)
    if stage == "checker":
        assert not any(p["kind"] in ("confirm", "challenge") for p in posts)


@pytest.mark.parametrize("response", ["I cannot verify this", "CONFIRMEDLY correct", "CHALLENGER"])
def test_checker_requires_explicit_result(claim_run, response):
    posts, stages, responses, _ = claim_run
    responses["checker"] = response

    swarm.run_claim(1, "A claim", 1)

    assert stages == ["scout", "checker"]
    assert posts[-1]["kind"] == "failure"
    assert not any(p["kind"] in ("confirm", "challenge", "verdict") for p in posts)


@pytest.mark.parametrize(("response", "kind"), [
    ("CONFIRMED: the quote matches", "confirm"),
    ("challenge: the quote is missing", "challenge"),
])
def test_checker_posts_explicit_results(claim_run, response, kind):
    posts, stages, responses, _ = claim_run
    responses["checker"] = response

    swarm.run_claim(1, "A claim", 1)

    assert stages == ["scout", "checker", "judge"]
    assert [p["kind"] for p in posts] == ["claim", "evidence", kind, "verdict"]
    assert posts[2]["reply_to"] == posts[1]["id"]


def test_failed_claim_does_not_stop_a_later_claim(claim_run):
    posts, _, _, failure = claim_run
    failure.update(stage="scout", mode="timeout")
    swarm.run_claim(1, "First claim", 1)

    failure.clear()
    swarm.run_claim(2, "Second claim", 1)

    assert [p["kind"] for p in posts] == ["claim", "failure", "claim", "evidence", "confirm", "verdict"]
    assert posts[-1]["reply_to"] == posts[2]["id"]
