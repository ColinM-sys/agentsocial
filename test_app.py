import importlib.util
import json
import os
from pathlib import Path
import shlex
import sys

import pytest


@pytest.mark.parametrize("prefix", ["custom&%AGENTSOCIAL_TEST_MARKER%!prefix", "custom &%AGENTSOCIAL_TEST_MARKER%!prefix"])
def test_codex_uses_launcher_on_path_without_appdata(tmp_path, monkeypatch, prefix):
    bin_dir = tmp_path / prefix
    bin_dir.mkdir()
    stub = bin_dir / "stub.py"
    stub.write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(__file__).with_name('input.json').write_text(\n"
        "    json.dumps({'args': sys.argv[1:], 'prompt': sys.stdin.buffer.read().decode('utf-8')}))\n"
        "print(json.dumps({'type': 'item.completed', 'item': "
        "{'type': 'agent_message', 'text': 'stub response'}}))\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        launcher = bin_dir / "codex.cmd"
        launcher.write_text('@echo off\n"%AGENTSOCIAL_TEST_PYTHON%" "%~dp0stub.py" %*\n', encoding="utf-8")
        monkeypatch.setenv("AGENTSOCIAL_TEST_PYTHON", sys.executable)
        monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    else:
        launcher = bin_dir / "codex"
        launcher.write_text(
            f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(stub))} "$@"\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)

    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.setenv("AGENTSOCIAL_TEST_MARKER", "expanded")
    monkeypatch.delenv("APPDATA", raising=False)
    spec = importlib.util.spec_from_file_location("swarm_under_test", Path(__file__).with_name("swarm.py"))
    swarm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(swarm)

    prompt = 'Check the claim: "x & y", 100%\nNaïve example.'
    assert swarm.codex(prompt, web=True) == "stub response"
    received = json.loads((bin_dir / "input.json").read_text(encoding="utf-8"))
    assert received["args"] == [
        "exec", "--skip-git-repo-check", "-s", "read-only", "--json",
        "-c", "mcp_servers.sqlcl.enabled=false", "-c", "tools.web_search=true", "-",
    ]
    assert received["prompt"] == prompt
