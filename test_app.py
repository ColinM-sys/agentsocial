from pathlib import Path

import pytest


@pytest.mark.parametrize(("filename", "count"), [("claims10.txt", 10), ("claims50.txt", 50)])
def test_included_claim_lists(filename, count):
    lines = (Path(__file__).resolve().parent / filename).read_text(encoding="utf-8").splitlines()
    claims = [line.strip() for line in lines if line.strip() and not line.lstrip().startswith("#")]
    assert len(claims) == count
    assert len(set(claims)) == count
