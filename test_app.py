import json
import re
from collections import Counter
from pathlib import Path


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
