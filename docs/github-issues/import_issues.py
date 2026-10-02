#!/usr/bin/env python3
"""Create GitHub issues from the staged files. DRY RUN unless --execute.

    python3 docs/github-issues/import_issues.py                 # show what would be created
    python3 docs/github-issues/import_issues.py --execute        # create them

Refuses to run at all if check_issues.py reports a BLOCK: the publication gate is not
optional, and a dry run that skips it would give false confidence.

Resumable. Every created issue is appended to --state as {slug, number, url}; a re-run
skips slugs already recorded, so an interrupted import is safe to repeat. Nothing here
edits or closes anything on GitHub.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import re
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent
ISSUES = ROOT / "issues"


def parse(path: pathlib.Path) -> tuple[str, list[str], str]:
    """Split a staged file into (title, labels, body)."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ValueError(f"{path.name}: no front matter")
    _, fm, body = text.split("---\n", 2)
    m = re.search(r"^title:\s*(.+?)\s*$", fm, re.M)
    if not m:
        raise ValueError(f"{path.name}: no title in front matter")
    title = m.group(1).strip().strip('"').strip("'")
    labels: list[str] = []
    lm = re.search(r"^labels:\s*\[(.*?)\]\s*$", fm, re.M | re.S)
    if lm:
        labels = [x.strip().strip('"').strip("'") for x in lm.group(1).split(",") if x.strip()]
    return title, labels, body.strip()


def gate() -> bool:
    r = subprocess.run([sys.executable, str(ROOT / "check_issues.py")], capture_output=True, text=True)
    sys.stdout.write(r.stdout[-4000:])
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default="dta-au/elspeth", help="owner/name")
    ap.add_argument("--state", default=str(ROOT / ".import-state.jsonl"))
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="stop after N (0 = all)")
    a = ap.parse_args()

    print("=== publication gate")
    if not gate():
        print("\nGATE FAILED — nothing will be imported. Resolve every BLOCK first.")
        return 1

    done: dict[str, dict] = {}
    state = pathlib.Path(a.state)
    if state.exists():
        for line in state.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                done[rec["slug"]] = rec

    files = sorted(ISSUES.glob("*.md"))
    print(f"\n=== {'EXECUTE' if a.execute else 'DRY RUN'} — {len(files)} staged, {len(done)} already imported, repo={a.repo}\n")

    created = skipped = failed = 0
    sf = state.open("a") if a.execute else None
    for f in files:
        slug = f.stem
        if slug in done:
            skipped += 1
            continue
        if a.limit and created >= a.limit:
            break
        try:
            title, labels, body = parse(f)
        except ValueError as exc:
            print(f"  !! {exc}")
            failed += 1
            continue

        cmd = ["gh", "issue", "create", "--repo", a.repo, "--title", title, "--body", body]
        for lab in labels:
            cmd += ["--label", lab]

        if not a.execute:
            print(f"  DRY  {slug}")
            print(f"       title:  {title}")
            print(f"       labels: {', '.join(labels) or '(none)'}  body: {len(body)} chars")
            created += 1
            continue

        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print(f"  !! {slug}: {(r.stderr or '').strip()[:200]}")
            failed += 1
            continue
        url = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else ""
        num = url.rsplit("/", 1)[-1] if url else ""
        print(f"  OK   #{num:<5} {slug}")
        sf.write(json.dumps({"slug": slug, "number": num, "url": url}) + "\n")
        sf.flush()
        created += 1

    if sf:
        sf.close()
    print(f"\n=== created {created} | skipped {skipped} | failed {failed}")
    if not a.execute:
        print("=== DRY RUN — nothing was created. Add --execute to import.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
