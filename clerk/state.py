"""Save and load the docket, so a restart doesn't reopen or forget cases."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

from .court import Case, Court, Stage


def save(court: Court, seen_prs: set[int], path: Path) -> None:
    data = {
        "next_case": court.next_case,
        "seen_prs": sorted(seen_prs),
        "cases": [asdict(c) | {"stage": c.stage.value} for c in court.cases.values()],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load(court: Court, path: Path) -> set[int] | None:
    """Fill the court from disk. Returns the PRs already seen, or None on a first run."""
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    court.next_case = max(court.next_case, data.get("next_case", 1))
    for raw in data.get("cases", []):
        raw["stage"] = Stage(raw["stage"])
        raw["rulings"] = {int(k): v for k, v in raw.get("rulings", {}).items()}
        case = Case(**raw)
        court.cases[case.number] = case
    return set(data.get("seen_prs", []))
