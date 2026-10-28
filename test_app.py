import importlib
import json
import os
import re
import runpy
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import requests

import server


ROOT = Path(__file__).resolve().parent


def test_reported_totals_link_to_matching_snapshots():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    table = readme.split("**Table 2. Totals**", 1)[1].split("**Example verdicts:**", 1)[0]

    def snapshot(name):
        link = re.search(rf"\[[^]]+\]\((results/{name}\.json)\)", table)
        assert link, f"Table 2 must link to the {name} snapshot"
        return json.loads((ROOT / link.group(1)).read_text(encoding="utf-8"))

    def value(label):
        row = re.search(rf"^\| {re.escape(label)} \| (.+?) \|$", table, re.MULTILINE)
        assert row, f"Missing Table 2 row: {label}"
        return row.group(1)

    stats = snapshot("feed_stats")
    state = snapshot("claims_state")
    kinds = stats["by_kind"]
    assert value("Agent profiles") == f'{stats["profiles"]:,}'
    assert value("Posts") == f'{stats["posts"]:,}'
    assert value("Claims") == f'{kinds["claim"]:,}'
    assert value("Evidence posts") == f'{kinds["evidence"]:,}'
    assert value("Confirmations / challenges") == f'{kinds["confirm"]:,} / {kinds["challenge"]:,}'
    assert value("Verdicts") == f'{kinds["verdict"]:,}'

    statuses = Counter(claim["status"] for claim in state.values())
    assert value("Claims tracked by the loop") == (
        f'{len(state)}: {statuses["settled"]} settled, '
        f'{statuses["checked"]} checked, {statuses["weak"]} weak'
    )
    max_scouts = max(round_["scouts"] for claim in state.values() for round_ in claim["history"])
    assert value("Max scouts on a claim") == str(max_scouts)


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


def test_feed_renders_user_content_as_text(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    server.init()
    client = server.app.test_client()
    payload = '<img src=x onerror=alert(1)>'
    profile = {
        "name": payload,
        "role": "role " + payload,
        "skills": "skills " + payload,
        "task": "task " + payload,
    }
    assert client.post("/api/profiles", json=profile).status_code == 200
    posts = [
        {"author": "author " + payload, "topic": "topic " + payload,
         "kind": "kind " + payload, "body": "body & " + payload, "reply_to": payload},
        {"author": "a", "topic": "t", "kind": "verdict", "body": "verdict body"},
        {"author": "b", "topic": "t", "kind": "challenge", "body": "challenge body"},
        {"author": "c", "topic": "t", "kind": "challenge reply extra", "body": "other body"},
    ]
    for post in posts:
        assert client.post("/api/posts", json=post).status_code == 200

    html = client.get("/").get_data(as_text=True)
    script = re.search(r"<script>(.*?)</script>", html, re.DOTALL).group(1)
    data = {
        "script": script,
        "profiles": client.get("/api/profiles").get_json(),
        "posts": client.get("/api/posts").get_json(),
        "stats": client.get("/api/stats").get_json(),
        "payload": payload,
    }
    # Run the actual page script with a small DOM, so untrusted HTML sinks and
    # the resulting text and CSS classes are checked together.
    harness = r"""
const assert = require('assert');
const vm = require('vm');
const data = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class Element {
  constructor(tag) { this.tagName = tag; this.className = ''; this.children = []; this.text = ''; }
  set textContent(value) { this.text = value == null ? '' : String(value); this.children = []; }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
  set innerHTML(value) {
    assert(!value.includes('<img'), 'untrusted markup reached innerHTML');
    this.text = ''; this.children = [];
  }
  append(...children) {
    this.children.push(...children.map(c => {
      if (typeof c !== 'string') return c;
      const text = new Element('#text'); text.textContent = c; return text;
    }));
  }
  replaceChildren(...children) { this.text = ''; this.children = []; this.append(...children); }
  get classList() { return {add: c => { this.className += (this.className ? ' ' : '') + c; }}; }
}
global.document = {createElement: tag => new Element(tag)};
global.profiles = new Element('div');
global.feed = new Element('div');
global.stats = new Element('span');
global.fetch = async url => ({json: async () => data[url.split('/').pop()]});
global.setInterval = () => {};
(async () => {
  await vm.runInThisContext(data.script + '\nload()');
  const profile = data.profiles[0];
  assert.strictEqual(profiles.children.length, 1);
  assert.strictEqual(profiles.children[0].textContent,
    `${profile.name} ${profile.role}${profile.skills}\n${profile.task}`);
  assert.strictEqual(feed.children.length, data.posts.length);
  for (const [index, post] of data.posts.slice().reverse().entries()) {
    const card = feed.children[index];
    const classes = ['card'];
    if (post.reply_to) classes.push('reply');
    if (['verdict', 'challenge'].includes(post.kind)) classes.push(post.kind);
    assert.deepStrictEqual(card.className.split(/\s+/).sort(), classes.sort());
    const meta = `${post.kind} · ${post.topic}${post.reply_to ? ' · reply to #' + post.reply_to : ''} · #${post.id}`;
    assert.strictEqual(card.textContent, `${post.author} ${meta}${post.body}`);
  }
  assert(stats.textContent.includes(data.payload));
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(["node", "-e", harness], input=json.dumps(data),
                            text=True, capture_output=True, check=False)
    assert result.returncode == 0, result.stderr


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    server.init()
    return server.app.test_client()


@pytest.mark.parametrize("reply_to", [
    "",
    "abc",
    "9223372036854775808",
    "-9223372036854775809",
    "9" * 4301,
])
def test_posts_reject_invalid_reply_to(client, reply_to):
    response = client.get("/api/posts", query_string={"reply_to": reply_to})
    assert response.status_code == 400


@pytest.mark.parametrize("reply_to", [-9223372036854775808, 0, 42, 9223372036854775807])
def test_posts_filter_valid_reply_to(client, reply_to):
    matching = client.post("/api/posts", json={
        "author": "tester", "body": "matching", "reply_to": reply_to,
    }).get_json()["id"]
    client.post("/api/posts", json={"author": "tester", "body": "other"})

    response = client.get("/api/posts", query_string={"reply_to": str(reply_to)})
    assert response.status_code == 200
    assert [post["id"] for post in response.get_json()] == [matching]


def test_profile_update_and_role_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "social.db"))
    clock = SimpleNamespace(time=lambda: 100.0)
    monkeypatch.setattr(server, "time", clock)
    server.init()
    client = server.app.test_client()

    original = {"name": "alice", "role": "scout", "skills": "search", "task": "explore"}
    other = {"name": "bob", "role": "scout", "skills": "research", "task": "investigate"}
    for profile in (original, other):
        assert client.post("/api/profiles", json=profile).status_code == 200

    before = client.get("/api/profiles").get_json()
    created = next(profile["created"] for profile in before if profile["name"] == "alice")
    assert created == 100.0

    clock.time = lambda: 200.0
    updated = {"name": "alice", "role": "reviewer", "skills": "testing", "task": "review"}
    assert client.post("/api/profiles", json=updated).status_code == 200

    profiles = client.get("/api/profiles").get_json()
    assert len(profiles) == 2
    assert {profile["name"]: profile for profile in profiles} == {
        "alice": {**updated, "created": created},
        "bob": {**other, "created": 100.0},
    }
    assert client.get("/api/profiles", query_string={"role": "scout"}).get_json() == [
        {**other, "created": 100.0}
    ]
    assert client.get("/api/profiles", query_string={"role": "reviewer"}).get_json() == [
        {**updated, "created": created}
    ]


def test_posts_persist_fields_and_defaults(client):
    before = time.time()
    explicit = client.post("/api/posts", json={
        "author": "alice", "reply_to": 42, "topic": "science",
        "kind": "evidence", "body": "A source",
    })
    default = client.post("/api/posts", json={"author": "bob", "body": "Hello"})
    after = time.time()

    assert explicit.status_code == default.status_code == 200
    assert explicit.get_json() == {"id": 1}
    assert default.get_json() == {"id": 2}

    posts = client.get("/api/posts").get_json()
    for post in posts:
        assert before <= post.pop("created") <= after
    assert posts == [
        {"id": 1, "author": "alice", "reply_to": 42, "topic": "science",
         "kind": "evidence", "body": "A source"},
        {"id": 2, "author": "bob", "reply_to": None, "topic": "",
         "kind": "post", "body": "Hello"},
    ]


def test_post_filters_isolate_threads_and_keep_id_order(client):
    def add(author, body, **fields):
        response = client.post("/api/posts", json={"author": author, "body": body, **fields})
        assert response.status_code == 200
        return response.get_json()["id"]

    claim = add("alice", "A claim", topic="science", kind="claim")
    evidence = add("bob", "First source", reply_to=claim, topic="science", kind="evidence")
    other_claim = add("alice", "A claim", topic="science", kind="claim")
    other_reply = add("bob", "Other thread", reply_to=other_claim, topic="science", kind="evidence")
    nested = add("bob", "Nested reply", reply_to=evidence, topic="science", kind="evidence")
    later = add("bob", "Second source", reply_to=claim, topic="science", kind="evidence")
    other_topic = add("bob", "Other topic", reply_to=claim, topic="health", kind="evidence")
    other_kind = add("bob", "Challenge", reply_to=claim, topic="science", kind="challenge")
    other_author = add("carol", "Other author", reply_to=claim, topic="science", kind="evidence")
    default = add("carol", "Plain post")

    ids = [claim, evidence, other_claim, other_reply, nested, later,
           other_topic, other_kind, other_author, default]
    assert ids == list(range(1, 11))

    cases = [
        ({}, ids),
        ({"topic": "science"}, [claim, evidence, other_claim, other_reply, nested, later,
                              other_kind, other_author]),
        ({"kind": "evidence"}, [evidence, other_reply, nested, later, other_topic, other_author]),
        ({"author": "bob"}, [evidence, other_reply, nested, later, other_topic, other_kind]),
        ({"reply_to": claim}, [evidence, later, other_topic, other_kind, other_author]),
        ({"reply_to": other_claim}, [other_reply]),
        ({"reply_to": evidence}, [nested]),
        ({"reply_to": 999}, []),
        ({"topic": "science", "kind": "claim"}, [claim, other_claim]),
        ({"topic": "science", "kind": "evidence", "author": "bob", "reply_to": claim},
         [evidence, later]),
    ]
    # Make an unordered SQLite scan run backwards, so omitting ORDER BY fails the test.
    with server.app.app_context():
        server.db().execute("PRAGMA reverse_unordered_selects = ON")
        for filters, expected in cases:
            response = client.get("/api/posts", query_string=filters)
            assert response.status_code == 200
            assert [post["id"] for post in response.get_json()] == expected


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


def test_codex_counts_concurrent_usage(monkeypatch):
    if "APPDATA" not in os.environ:
        monkeypatch.setenv("APPDATA", "test-appdata")
    swarm = importlib.import_module("swarm")
    monkeypatch.setattr(swarm, "TOKENS", {"in": 0, "out": 0, "calls": 0})

    # Both workers start parsing together. Without synchronization, each reads
    # the old input total before entering usage.get().
    start = threading.Barrier(2)
    second_lookup = threading.Event()
    lookup_lock = threading.Lock()
    lookups = 0

    class Usage(dict):
        def get(self, key, default=None):
            nonlocal lookups
            if key == "input_tokens":
                with lookup_lock:
                    lookups += 1
                    first = lookups == 1
                if first:
                    # With a lock in codex(), the other lookup cannot begin
                    # until this one finishes, so the wait must be bounded.
                    second_lookup.wait(1)
                else:
                    second_lookup.set()
            return super().get(key, default)

    def run(*args, **kwargs):
        start.wait(timeout=5)
        return SimpleNamespace(stdout='{"type":"turn.completed", "usage":'
                                      '{"input_tokens":3, "output_tokens":2}}')

    def loads(line):
        message = json.loads(line)
        message["usage"] = Usage(message["usage"])
        return message

    monkeypatch.setattr(swarm.subprocess, "run", run)
    monkeypatch.setattr(swarm, "json", SimpleNamespace(loads=loads))
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(swarm.codex, ("first", "second")))

    assert swarm.TOKENS == {"in": 6, "out": 4, "calls": 2}


def test_null_profile_names_are_rejected(client):
    for _ in range(2):
        response = client.post("/api/profiles", json={"name": None})
        assert response.status_code == 400

    assert client.get("/api/profiles").get_json() == []


def test_profile_upsert_updates_existing_row(client):
    assert client.post("/api/profiles", json={
        "name": "agent", "role": "scout", "skills": "reading", "task": "searching"
    }).status_code == 200
    original = client.get("/api/profiles").get_json()[0]

    assert client.post("/api/profiles", json={
        "name": "agent", "role": "checker", "skills": "testing", "task": "reviewing"
    }).status_code == 200
    assert client.get("/api/profiles").get_json() == [{
        "name": "agent", "role": "checker", "skills": "testing", "task": "reviewing",
        "created": original["created"],
    }]


def test_fresh_profile_schema_rejects_null_names(client):
    con = sqlite3.connect(server.DB)
    try:
        name = next(column for column in con.execute("pragma table_info(profiles)") if column[1] == "name")
        assert name[3] == 1
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("insert into profiles(name) values (?)", (None,))
    finally:
        con.close()


def test_legacy_profile_schema_migration_preserves_data(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "DB", str(tmp_path / "legacy.db"))
    con = sqlite3.connect(server.DB)
    con.executescript("""
        create table profiles(name text primary key, role text, skills text, task text, created real);
        insert into profiles values ('agent', 'scout', 'reading', 'searching', 1);
        insert into profiles values (null, 'first', 'one', 'task one', 2);
        insert into profiles values (null, 'second', 'two', 'task two', 3);
    """)
    con.close()

    server.init()
    server.init()

    con = sqlite3.connect(server.DB)
    try:
        name = next(column for column in con.execute("pragma table_info(profiles)") if column[1] == "name")
        assert name[3] == 1
        assert con.execute("select * from profiles").fetchall() == [
            ("agent", "scout", "reading", "searching", 1.0),
        ]
        assert con.execute("select * from profiles_null_names order by created").fetchall() == [
            (None, "first", "one", "task one", 2.0),
            (None, "second", "two", "task two", 3.0),
        ]
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("insert into profiles(name) values (?)", (None,))
    finally:
        con.close()


def prepare_loop(directory):
    shutil.copyfile(Path(__file__).with_name("forever.py"), directory / "forever.py")
    (directory / "claims50.txt").write_text("new claim\n", encoding="utf-8")
    (directory / "requests.py").write_text(
        """import os
from pathlib import Path

def get(url, params=None):
    Path(f'api-{os.getpid()}.started').touch()
    class Response:
        def json(self):
            if params is None:
                return []
            if params.get('kind') == 'claim':
                return [{'body': 'new claim', 'id': 1}]
            if params.get('reply_to') == 1:
                return [{'kind': 'verdict', 'body': 'SUPPORTED'}]
            return []
    return Response()
""",
        encoding="utf-8",
    )
    (directory / "swarm.py").write_text(
        """import os
from pathlib import Path
import time

Path(f'swarm-{os.getpid()}.started').touch()
while not Path('release').exists():
    time.sleep(0.01)
Path('stop.txt').touch()
""",
        encoding="utf-8",
    )


def start_loop(directory, launcher=None, capture_stderr=False):
    command = [sys.executable, "forever.py"] if launcher is None else [sys.executable, "-c", launcher]
    return subprocess.Popen(
        command, cwd=directory, stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE if capture_stderr else subprocess.DEVNULL,
    )


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.02)
    return predicate()


def test_concurrent_loop_cannot_start_a_round(tmp_path):
    prepare_loop(tmp_path)
    first = start_loop(tmp_path)
    second = None
    try:
        assert wait_until(lambda: list(tmp_path.glob("swarm-*.started")))
        # A contender must fail on the lock before attempting to load state.
        (tmp_path / "claims_state.json").write_text("{", encoding="utf-8")
        second = start_loop(
            tmp_path,
            "import pathlib, runpy, sys; pathlib.Path('second-started').touch(); "
            "sys.argv = ['forever.py']; runpy.run_path('forever.py', run_name='__main__')",
            capture_stderr=True,
        )
        assert wait_until(lambda: (tmp_path / "second-started").exists())
        _, error = second.communicate(timeout=10)
        assert second.returncode != 0
        assert b"lock" in error.lower()
        assert len(list(tmp_path.glob("api-*.started"))) == 1
        (tmp_path / "release").touch()
        assert first.wait(timeout=10) == 0
        saved = json.loads((tmp_path / "claims_state.json").read_text(encoding="utf-8"))
        assert saved["new claim"]["history"][0]["verdict"] == "SUPPORTED"
        assert subprocess.run(
            [sys.executable, "forever.py"], cwd=tmp_path,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        ).returncode == 0
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)


def test_failed_state_replacement_preserves_previous_history(tmp_path):
    prepare_loop(tmp_path)
    old_state = {"old claim": {"history": [], "status": "settled"}}
    state_path = tmp_path / "claims_state.json"
    original = json.dumps(old_state)
    state_path.write_text(original, encoding="utf-8")
    (tmp_path / "release").touch()
    process = start_loop(
        tmp_path,
        "import os, runpy, sys\n"
        "def fail_replace(src, dst):\n"
        "    raise OSError('simulated replacement failure')\n"
        "os.replace = fail_replace\n"
        "sys.argv = ['forever.py']\n"
        "runpy.run_path('forever.py', run_name='__main__')\n",
    )
    try:
        process.wait(timeout=10)
        assert state_path.read_text(encoding="utf-8") == original
        assert process.returncode != 0
        assert not list(tmp_path.glob(".claims_state.*.tmp"))
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


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
