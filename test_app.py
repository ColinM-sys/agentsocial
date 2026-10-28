import re
from pathlib import Path


def test_run_instructions_use_separate_terminals():
    readme = (Path(__file__).parent / "README.md").read_text(encoding="utf-8")
    instructions = readme.split("## 6 Run it", 1)[1].split("\n## ", 1)[0]
    blocks = list(re.finditer(r"```powershell\s*\n(.*?)```", instructions, re.DOTALL))

    server = next(block for block in blocks if "python server.py" in block.group(1))
    swarm = next(block for block in blocks if "python swarm.py" in block.group(1))
    assert server.end() < swarm.start()
    assert "python forever.py" in swarm.group(1)
    between = instructions[server.end():swarm.start()]
    assert re.search(r"keep the server running.*second terminal", between, re.IGNORECASE | re.DOTALL)
    assert re.search(r"\beither\b", between, re.IGNORECASE)
