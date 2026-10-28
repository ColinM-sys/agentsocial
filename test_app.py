import json
from pathlib import Path
import shutil
import subprocess
import sys
import time


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
