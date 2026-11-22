"""Add the McCabe (ruff C901) sensitivity column to crap.csv and print markdown tables.

usage: python3 analyse.py crap.csv ruff_cc.json > tables.md
"""

import collections
import csv
import json
import re
import statistics
import sys

T = 30.0
csv_path, ruff_path = sys.argv[1], sys.argv[2]


def emit(line: str = "") -> None:
    sys.stdout.write(line + "\n")


def crap(comp, cov):
    return comp * comp * (1.0 - cov) ** 3 + comp


def short(r) -> str:
    return f"`{r['file'][len('src/elspeth/') :]}:{r['line']}` `{r['function']}`"


def area(f):
    p = f.split("/")[2:]
    return "/".join(p[:2]) if len(p) > 2 else p[0]


def band(c):
    if c <= 5:
        return "<= 5"
    if c <= 15:
        return "5-15"
    if c <= 30:
        return "15-30"
    if c <= 100:
        return "30-100"
    return "> 100"


with open(ruff_path) as fh:
    ruff_rows = json.load(fh)
mccabe = {}
for d in ruff_rows:
    p = d["filename"]
    p = p[p.rindex("src/elspeth") :]
    mccabe[(p, d["location"]["row"])] = int(re.search(r"\((\d+) > 0\)", d["message"]).group(1))

with open(csv_path) as fh:
    R = list(csv.DictReader(fh))
for r in R:
    for k in ("line", "length", "complexity", "file_commits_90d"):
        r[k] = int(r[k])
    for k in ("coverage_pct", "crap"):
        r[k] = float(r[k])
    m = mccabe.get((r["file"], r["line"]))
    r["mccabe_complexity"] = "" if m is None else m
    r["crap_mccabe"] = "" if m is None else round(crap(m, r["coverage_pct"] / 100), 1)

with open(csv_path, "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=list(R[0]))
    w.writeheader()
    w.writerows(R)

n = len(R)
bad = [r for r in R if r["crap"] > T]
bad_m = [r for r in R if r["crap_mccabe"] != "" and r["crap_mccabe"] > T]
joined_m = sum(1 for r in R if r["crap_mccabe"] != "")
cs = sorted(r["crap"] for r in R)

emit("## Headline\n")
emit("| Measure | radon (counts `and`/`or`) | McCabe (ruff C901) |")
emit("|---|---:|---:|")
emit(f"| Functions scored | {n} | {joined_m} |")
emit(f"| CRAP > 30 | {len(bad)} | {len(bad_m)} |")
emit(f"| Share over threshold | {100 * len(bad) / n:.2f}% | {100 * len(bad_m) / joined_m:.2f}% |")
zero_r = sum(r["coverage_pct"] == 0 for r in bad)
zero_m = sum(r["coverage_pct"] == 0 for r in bad_m)
emit(f"| Over threshold at 0% coverage | {zero_r} | {zero_m} |")
inh_r = sum(r["complexity"] > T for r in bad)
inh_m = sum(r["mccabe_complexity"] > T for r in bad_m)
emit(f"| Over threshold even at 100% coverage (complexity > 30) | {inh_r} | {inh_m} |")
emit(f"\nradon CRAP distribution: median {statistics.median(cs)}, p90 {cs[int(0.9 * n)]}, p99 {cs[int(0.99 * n)]}, max {cs[-1]}.\n")

bands = collections.Counter(band(c) for c in cs)
emit("| radon CRAP band | Functions |\n|---|---:|")
for b in ("<= 5", "5-15", "15-30", "30-100", "> 100"):
    emit(f"| {b} | {bands[b]} |")

A = collections.defaultdict(list)
for r in R:
    A[area(r["file"])].append(r)
emit("\n## By area (radon), ranked by total excess over 30\n")
emit("| Area | Functions | Over 30 | Share | Sum(CRAP - 30) | Over 30 (McCabe) |")
emit("|---|---:|---:|---:|---:|---:|")
for a, rs in sorted(A.items(), key=lambda kv: -sum(max(0, r["crap"] - T) for r in kv[1]))[:20]:
    b = [r for r in rs if r["crap"] > T]
    bm = sum(1 for r in rs if r["crap_mccabe"] != "" and r["crap_mccabe"] > T)
    emit(f"| `{a}` | {len(rs)} | {len(b)} | {100 * len(b) / len(rs):.1f}% | {sum(r['crap'] - T for r in b):.0f} | {bm} |")

emit("\n## Top 25 by radon CRAP\n")
emit("| CRAP | CRAP (McCabe) | CC | Cov % | Lines | File commits 90d | Function |")
emit("|---:|---:|---:|---:|---:|---:|---|")
for r in R[:25]:
    emit(
        f"| {r['crap']} | {r['crap_mccabe']} | {r['complexity']} | {r['coverage_pct']} "
        f"| {r['length']} | {r['file_commits_90d']} | {short(r)} |"
    )

emit("\n## Change hotspots: over 30 and in frequently changed files\n")
emit("Ranked by (CRAP - 30) x commits touching the file since 2026-08-24.\n")
emit("| CRAP | CC | Cov % | File commits 90d | Function |")
emit("|---:|---:|---:|---:|---|")
for r in sorted(bad, key=lambda r: -(r["crap"] - T) * r["file_commits_90d"])[:20]:
    emit(f"| {r['crap']} | {r['complexity']} | {r['coverage_pct']} | {r['file_commits_90d']} | {short(r)} |")

emit("\n## Worst coverage gaps (complexity <= 30, so tests alone would clear them)\n")
emit("| CRAP | CC | Cov % | Function |")
emit("|---:|---:|---:|---|")
for r in sorted((r for r in bad if r["complexity"] <= T), key=lambda r: -r["crap"])[:20]:
    emit(f"| {r['crap']} | {r['complexity']} | {r['coverage_pct']} | {short(r)} |")
