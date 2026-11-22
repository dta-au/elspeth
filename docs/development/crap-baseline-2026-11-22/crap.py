"""Join radon cyclomatic complexity with coverage.py per-function regions; score CRAP.

CRAP(m) = comp(m)^2 * (1 - cov(m))^3 + comp(m)   (Savoia/Evans, crap4j)
cov(m)  = coverage.py percent_covered for the function region (statements + branches)

Both tools report nested functions as separate regions and exclude them from the
enclosing function (radon subtracts closure complexity; coverage excludes nested
lines), so the join is region-for-region.
"""

import csv
import json
import sys
from collections import defaultdict

THRESHOLD = 30.0


def crap(comp: int, cov: float) -> float:
    return comp * comp * (1.0 - cov) ** 3 + comp


# Formula controls: known values from the crap4j paper.
assert crap(10, 0.0) == 110.0
assert crap(10, 1.0) == 10.0
assert crap(5, 0.0) == 30.0
assert abs(crap(30, 1.0) - 30.0) < 1e-9


def load_json(path: str):
    with open(path) as fh:
        return json.load(fh)


radon = load_json(sys.argv[1])
cov_files = load_json(sys.argv[2])["files"]
churn = load_json(sys.argv[3])
out_csv = sys.argv[4]


def walk(block, parent_qual):
    """Yield (qualname, lineno, endline, complexity) for function/method/closure blocks."""
    if block["type"] == "class":
        return
    if block["type"] == "method":
        qual = f"{block['classname']}.{block['name']}"
    else:
        qual = block["name"]
    if parent_qual:
        qual = f"{parent_qual}.{qual}"
    yield qual, block["lineno"], block["endline"], block["complexity"]
    for clo in block.get("closures", []):
        yield from walk(clo, qual)


rows = []
unmatched_radon = []
matched_cov_keys = set()
excluded = 0
for path, blocks in radon.items():
    cf = cov_files.get(path)
    fns = cf["functions"] if cf else {}
    # index coverage regions by bare name suffix and start line
    by_name = defaultdict(list)
    for key, region in fns.items():
        if key == "":
            continue
        by_name[key].append((region["start_line"], key, region))
        by_name[key.rsplit(".", 1)[-1]].append((region["start_line"], key, region))
    for qual, lineno, endline, comp in (e for b in blocks for e in walk(b, "")):
        cands = by_name.get(qual) or by_name.get(qual.rsplit(".", 1)[-1], [])
        # coverage start_line may sit on a decorator above the def; accept the
        # nearest region starting at or above lineno within the function span
        best = None
        for start, key, region in cands:
            if (path, key) in matched_cov_keys:
                continue
            if start <= lineno and (best is None or start > best[0]):
                best = (start, key, region)
        if best is None:
            unmatched_radon.append((path, qual, lineno))
            continue
        start, key, region = best
        matched_cov_keys.add((path, key))
        s = region["summary"]
        total = s["num_statements"] + s["num_branches"]
        if total == 0:
            excluded += 1  # pragma: no cover / abstractmethod / excluded region
            continue
        c = s["percent_covered"] / 100.0
        rows.append(
            {
                "file": path,
                "function": qual,
                "line": lineno,
                "length": endline - lineno + 1,
                "complexity": comp,
                "coverage_pct": round(s["percent_covered"], 1),
                "crap": round(crap(comp, c), 1),
                "file_commits_90d": churn.get(path, 0),
            }
        )

unmatched_cov = [(p, k) for p, f in cov_files.items() for k in f["functions"] if k and (p, k) not in matched_cov_keys]

rows.sort(key=lambda r: -r["crap"])
with open(out_csv, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(rows[0]))
    w.writeheader()
    w.writerows(rows)

summary = {
    "scored_functions": len(rows),
    "excluded_regions": excluded,
    "unmatched_radon": len(unmatched_radon),
    "unmatched_cov": len(unmatched_cov),
    "unmatched_radon_sample": unmatched_radon[:10],
    "unmatched_cov_sample": unmatched_cov[:10],
}
with open(out_csv + ".join.json", "w") as fh:
    json.dump(summary, fh, indent=1)
sys.stdout.write(json.dumps({k: v for k, v in summary.items() if not k.endswith("sample")}) + "\n")
