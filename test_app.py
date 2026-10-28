import re
import subprocess
import sys
from pathlib import Path


def test_readme_claim_file_setup(tmp_path):
    readme = Path(__file__).with_name("README.md").read_text(encoding="utf-8")
    instructions = readme.split("If either sample file is missing", 1)
    assert len(instructions) == 2
    command = re.search(r'```powershell\npython -c "([^\n]+)"\n```', instructions[1])
    assert command, "README should provide a command to create missing claim files"

    def create_missing_files():
        subprocess.run([sys.executable, "-c", command.group(1)], cwd=tmp_path, check=True)

    create_missing_files()
    for name in ("claims10.txt", "claims50.txt"):
        claims = (tmp_path / name).read_text(encoding="utf-8").splitlines()
        assert claims and all(claim.strip() and not claim.startswith("#") for claim in claims)

    existing = tmp_path / "claims10.txt"
    existing.write_text("Keep this claim.\n", encoding="utf-8")
    (tmp_path / "claims50.txt").unlink()
    create_missing_files()
    assert existing.read_text(encoding="utf-8") == "Keep this claim.\n"
    assert (tmp_path / "claims50.txt").read_text(encoding="utf-8").strip()
