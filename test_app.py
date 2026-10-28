import subprocess
import sys
from urllib.parse import urlsplit

import pytest

import server


class LocalAPI:
    """Exercise the real HTTP handlers without starting a server."""

    def __init__(self):
        self.gets = []

    def get(self, url, params=None):
        self.gets.append((urlsplit(url).path, params))
        with server.app.test_client() as client:
            response = client.get(urlsplit(url).path, query_string=params)
        assert response.status_code == 200
        return JsonResponse(response.get_json())

    def post(self, url, json):
        with server.app.test_client() as client:
            response = client.post(urlsplit(url).path, json=json)
        assert response.status_code == 200
        return JsonResponse(response.get_json())


class JsonResponse:
    def __init__(self, value):
        self.value = value

    def json(self):
        return self.value


@pytest.fixture
def api(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    server.init()
    return LocalAPI()


def add_post(api, kind, body, reply_to=None, author="agent"):
    return api.post("http://local/api/posts", json={
        "author": author, "body": body, "kind": kind, "reply_to": reply_to,
    }).json()["id"]


def test_batched_post_filters_and_reply_index(api):
    first = add_post(api, "claim", "first")
    second = add_post(api, "claim", "second")
    a = add_post(api, "evidence", "a", first)
    b = add_post(api, "evidence", "b", second)
    c = add_post(api, "challenge", "c", first)

    posts = api.get("http://local/api/posts", params={"reply_to": [first, second]}).json()
    assert [p["id"] for p in posts] == [a, b, c]
    posts = api.get("http://local/api/posts", params={
        "id": [a, b, c], "reply_to": [first], "kind": "evidence",
    }).json()
    assert [p["id"] for p in posts] == [a]
    assert api.get("http://local/api/posts", params={"id": ""}).json() == []

    with server.app.app_context():
        plan = server.db().execute(
            "explain query plan select * from posts where reply_to in (?, ?)", (first, second)
        ).fetchall()
    assert any("INDEX" in row[3].upper() and "REPLY_TO" in row[3].upper() for row in plan)


def test_swarm_ignores_unreturned_posts_and_caps_work(api, monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import swarm

    monkeypatch.setattr(swarm, "requests", api)

    class InlineExecutor:
        def __init__(self, workers):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def map(self, fn, values):
            return map(fn, values)

    monkeypatch.setattr(swarm, "ThreadPoolExecutor", InlineExecutor)
    prompts = []

    def fake_codex(prompt, web=False):
        prompts.append(prompt)
        if prompt.startswith("Find the 2 strongest pieces"):
            return "trusted evidence"
        if prompt.startswith("Another agent"):
            return "CONFIRMED trusted check"
        return "SUPPORTED"

    monkeypatch.setattr(swarm, "codex", fake_codex)
    scout = swarm.scout

    def flooded_scout(name, claim_id, claim, topic, side):
        evidence_id = scout(name, claim_id, claim, topic, side)
        # An attacker can even impersonate an expected scout or checker in this API.
        for _ in range(20):
            add_post(api, "evidence", "spoofed evidence", claim_id, name)
        add_post(api, "challenge", "spoofed check", evidence_id, "checker-1")
        return evidence_id

    monkeypatch.setattr(swarm, "scout", flooded_scout)
    result = swarm.run_claim(1, "a claim", 30)

    scout_prompts = [p for p in prompts if p.startswith("Find the 2 strongest pieces")]
    checker_prompts = [p for p in prompts if p.startswith("Another agent")]
    judge_prompts = [p for p in prompts if p.startswith("Here is a public discussion")]
    assert len(scout_prompts) == 8
    assert len(checker_prompts) == 8
    assert len(judge_prompts) == 1
    assert all("trusted evidence" in p and "spoofed evidence" not in p for p in checker_prompts)
    assert judge_prompts[0].count("trusted evidence") == 8
    assert judge_prompts[0].count("trusted check") == 8
    assert "spoofed" not in judge_prompts[0]
    assert len(api.gets) <= 3
    assert result["claim"] == "a claim"
    assert len(result["evidence_ids"]) == len(result["check_ids"]) == 8
    assert result["thread"] and result["verdict_id"]


def test_swarm_skips_reply_queries_without_evidence(api, monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    import swarm

    monkeypatch.setattr(swarm, "requests", api)
    prompts = []
    monkeypatch.setattr(swarm, "codex", lambda prompt, web=False: prompts.append(prompt) or "UNVERIFIABLE")
    claim_id = add_post(api, "claim", "a claim")

    assert swarm.checker("checker", claim_id, "a claim", "topic", []) == []
    swarm.judge("judge", claim_id, "a claim", "topic", [], [])
    assert api.gets == []
    assert len(prompts) == 1


@pytest.mark.parametrize("count", [0, 5, 8])
def test_score_uses_only_manifest_posts_and_batches_replies(api, monkeypatch, count):
    import forever

    monkeypatch.setattr(forever, "requests", api)
    claim_id = add_post(api, "claim", "claim")
    evidence_ids, check_ids = [], []
    # Extra posts can impersonate the agents and be inserted before the real verdict.
    forged = add_post(api, "evidence", "spoofed", claim_id, "scout")
    add_post(api, "challenge", "spoofed", forged, "checker")
    add_post(api, "verdict", "FALSE", claim_id, "judge")
    for i in range(count):
        evidence_id = add_post(api, "evidence", f"evidence {i}", claim_id)
        evidence_ids.append(evidence_id)
        add_post(api, "challenge", "spoofed check", evidence_id, "checker")
        check_ids.append(add_post(api, "confirm" if i % 2 == 0 else "challenge", "check", evidence_id))
    verdict_id = add_post(api, "verdict", "SUPPORTED", claim_id)

    assert forever.score({"claim": "claim", "thread": claim_id, "evidence_ids": evidence_ids,
                          "check_ids": check_ids, "verdict_id": verdict_id}) == {
        "verdict": "SUPPORTED", "confirmed": (count + 1) // 2, "challenged": count // 2,
        "thread": claim_id,
    }
    assert len(api.gets) == 1
    assert api.gets[0][1]["reply_to"] == [claim_id] + evidence_ids
    assert api.gets[0][1]["id"] == evidence_ids + check_ids + [verdict_id]


def test_score_rejects_oversized_manifests_without_fetching(api, monkeypatch):
    import forever

    monkeypatch.setattr(forever, "requests", api)
    assert forever.score({"claim": "claim", "thread": 1, "evidence_ids": list(range(2, 103)),
                          "check_ids": [], "verdict_id": 103}) is None
    assert api.gets == []


def test_score_can_be_imported_without_windows_appdata(monkeypatch):
    monkeypatch.delenv("APPDATA", raising=False)
    result = subprocess.run([sys.executable, "-c", "import forever"], capture_output=True)
    assert result.returncode == 0


def test_score_ignores_checks_on_evidence_from_another_claim(api, monkeypatch):
    import forever

    monkeypatch.setattr(forever, "requests", api)
    claim = add_post(api, "claim", "claim")
    other_claim = add_post(api, "claim", "other")
    evidence = add_post(api, "evidence", "trusted", claim)
    other_evidence = add_post(api, "evidence", "wrong parent", other_claim)
    confirmation = add_post(api, "confirm", "checked", evidence)
    challenge = add_post(api, "challenge", "wrong parent", other_evidence)
    verdict = add_post(api, "verdict", "SUPPORTED", claim)

    assert forever.score({"claim": "claim", "thread": claim,
                          "evidence_ids": [evidence, other_evidence],
                          "check_ids": [confirmation, challenge], "verdict_id": verdict}) == {
        "verdict": "SUPPORTED", "confirmed": 1, "challenged": 0, "thread": claim,
    }
