"""Bucket the ``widescan.py`` hit list into <=5,000 LOC lanes, one file never split.

Run from the repository root after ``widescan.py hits.json``::

    python bucket.py > buckets.md

Writes ``buckets.json`` (the plan's manifest shape) and prints the per-wave
markdown tables. Waves: 0 = contracts/, 1 = plugins/core/engine/mcp/telemetry/
tui/testing, 2 = web/composer + web/sessions + composer_mcp, 3 = other web/*.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

CAP = 5000
SRC = Path("src/elspeth")


def area_of(file: str) -> str:
    parts = Path(file).relative_to(SRC).parts
    return "/".join(parts[:2]) if len(parts) > 2 else parts[0]


def wave_of(area: str) -> int:
    if area.startswith("contracts"):
        return 0
    if area.startswith(("web/composer", "web/sessions", "composer_mcp")):
        return 2
    if area.startswith("web"):
        return 3
    return 1


def main() -> None:
    with Path("hits.json").open() as fh:
        hits = json.load(fh)
    by_file = collections.Counter(h[0] for h in hits)
    loc: dict[str, int] = {}
    for file in by_file:
        with Path(file).open() as fh:
            loc[file] = sum(1 for _ in fh)

    groups: dict[tuple[int, str], list[str]] = collections.defaultdict(list)
    for file in sorted(by_file, key=lambda f: (wave_of(area_of(f)), area_of(f), -loc[f])):
        groups[(wave_of(area_of(file)), area_of(file))].append(file)

    buckets: list[tuple[int, str, list[str]]] = []
    for (wave, area), files in sorted(groups.items()):
        current: list[str] = []
        current_loc = 0
        for file in files:
            if current and current_loc + loc[file] > CAP:
                buckets.append((wave, area, current))
                current, current_loc = [], 0
            current.append(file)
            current_loc += loc[file]
        if current:
            buckets.append((wave, area, current))

    manifest = [
        {
            "bucket": f"B{index:02}",
            "wave": wave,
            "area": area,
            "loc": sum(loc[f] for f in files),
            "sites": sum(by_file[f] for f in files),
            "files": [{"path": f, "loc": loc[f], "sites": by_file[f]} for f in files],
        }
        for index, (wave, area, files) in enumerate(buckets, 1)
    ]
    with Path("buckets.json").open("w") as fh:
        json.dump(manifest, fh, indent=1)

    for wave in range(4):
        rows = [b for b in manifest if b["wave"] == wave]
        sys.stdout.write(
            f"\n## WAVE {wave}: {len(rows)} buckets, {sum(b['sites'] for b in rows)} sites, {sum(b['loc'] for b in rows)} LOC\n"
        )
        for b in rows:
            cells = "<br>".join(f"`{Path(x['path']).relative_to(SRC)}` ({x['loc']} LOC; x{x['sites']})" for x in b["files"])
            sys.stdout.write(f"| {b['bucket']} | {b['loc']} | {b['sites']} | {cells} |\n")


if __name__ == "__main__":
    main()
