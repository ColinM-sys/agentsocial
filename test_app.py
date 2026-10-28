import importlib
import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace


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
