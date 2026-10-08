#!/usr/bin/env python3
"""Inventory Git tree entries; count strict UTF-8, non-NUL regular blobs.

Physical lines are LF bytes plus one for a nonempty, unterminated final line.
CRLF counts once; CR alone and Unicode line separators do not split lines.
No checkout traversal, Git textconv, clean filters, or regex file discovery.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "--no-optional-locks", "-C", str(repo), *args], check=True, capture_output=True).stdout


def count_lines(data: bytes) -> tuple[str, int | None]:
    if b"\0" in data:
        return "nul", None
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "non_utf8", None
    return "text", data.count(b"\n") + int(bool(data) and not data.endswith(b"\n"))


def oversized(lines: int | None, threshold: int) -> bool:
    return lines is not None and lines > threshold


def emit(message: str) -> None:
    sys.stdout.write(message + "\n")


def self_test() -> None:
    cases = [
        ("empty", b"", "text", 0),
        ("unterminated", b"one", "text", 1),
        ("lf", b"one\ntwo\n", "text", 2),
        ("crlf", b"one\r\ntwo\r\n", "text", 2),
        ("cr_only", b"one\rtwo", "text", 1),
        ("unicode_separator", "one\u2028two".encode(), "text", 1),
        ("binary_nul", b"a\0\n", "nul", None),
        ("binary_invalid_utf8", b"\xff\n", "non_utf8", None),
    ]
    for boundary in (5000, 5001, 10000, 10001):
        cases.append((f"boundary_{boundary}", b"x\n" * boundary, "text", boundary))
    for name, data, kind, lines in cases:
        actual = count_lines(data)
        if actual != (kind, lines):
            raise AssertionError((name, actual, kind, lines))
        emit(f"control {name}: status={actual[0]} lines={actual[1]}")
    for threshold in (5000, 10000):
        before = count_lines(b"x\n" * threshold)[1]
        after = count_lines(b"x\n" * threshold + b"mutation")[1]
        if oversized(before, threshold) or not oversized(after, threshold):
            raise AssertionError(f"mutation control failed at {threshold}")
        emit(f"mutation threshold={threshold}: before={before} selected=False after={after} selected=True")
    with tempfile.TemporaryDirectory(prefix="elspeth-line-controls-") as temporary:
        repo = Path(temporary)
        git(repo, "init", "--quiet")
        samples = {
            "positive.txt": b"x\n" * 5001,
            "negative.txt": b"x\n" * 5000,
            "empty.txt": b"",
            "binary.dat": b"a\0\n",
            "unterminated.txt": b"last",
        }
        for path, data in samples.items():
            (repo / path).write_bytes(data)
        (repo / "link").symlink_to("positive.txt")
        git(repo, "add", ".")
        git(
            repo,
            "-c",
            "user.name=Inventory Control",
            "-c",
            "user.email=control@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "Instrument control",
        )
        reader = BlobReader(repo)
        try:
            _, rows = tree_rows(repo, "control", "HEAD", reader)
        finally:
            reader.close()
        selected = {str(row["path"]) for row in rows if oversized(row["lines"], 5000)}
        actual = {str(row["path"]): (row["status"], row["lines"]) for row in rows}
        if selected != {"positive.txt"} or len(rows) != 6:
            raise AssertionError((selected, len(rows)))
        if actual["link"] != ("symlink", None) or actual["binary.dat"] != ("nul", None):
            raise AssertionError(actual)
        emit("git-tree control: entries=6 selected=['positive.txt'] binary_excluded=True symlink_excluded=True")


def category(path: str) -> str:
    parts = Path(path).parts
    name = Path(path).name
    if name in {"uv.lock", "package-lock.json", "poetry.lock", "yarn.lock", "pnpm-lock.yaml"}:
        return "lock"
    if name.endswith((".min.js", ".min.css")):
        return "minified"
    if "vendor" in parts or "node_modules" in parts or "third_party" in parts:
        return "vendor"
    if path == "tests/fixtures/dag_scenario_corpus/harness.py":
        return "authored-test-tooling"
    if "golden" in parts or "fixtures" in parts:
        return "fixture"
    if path in {
        "docs/architecture/state_engine/assessments/2026-08-15-0537/assessment.json",
        "docs/plans/2026-09-20-composer-async-operations/supplemental-obligation-collections-2026-10-07.json",
    }:
        return "generated-report"
    if path in {
        "docs/architecture/state_engine/proof-catalog/v3/catalog.json",
        "docs/architecture/state_engine/assessments/2026-08-15-0537/coverage-mapping.json",
    }:
        return "curated-proof-data"
    if path.startswith("docs/") or name.endswith(".md"):
        return "docs"
    if path.startswith(("src/", "elspeth-lints/src/", "gateway/src/")):
        return "authored-source"
    if path.startswith(("tests/", "gateway/tests/")) or "tests" in parts:
        return "authored-tests"
    if path.startswith(("scripts/", ".agents/", ".github/")):
        return "authored-tooling"
    if name.endswith((".csv", ".jsonl", ".ndjson", ".parquet")):
        return "data"
    if path.startswith("config/cicd/"):
        return "curated-policy-config"
    return "config-or-other"


class BlobReader:
    def __init__(self, repo: Path):
        self.process = subprocess.Popen(
            ["git", "--no-optional-locks", "-C", str(repo), "cat-file", "--batch"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        if self.process.stdin is None or self.process.stdout is None:
            raise RuntimeError("cat-file pipe not created")
        self.stdin = self.process.stdin
        self.stdout = self.process.stdout
        self.cache: dict[str, tuple[int, str, int | None]] = {}

    def measure(self, oid: str) -> tuple[int, str, int | None]:
        if oid in self.cache:
            return self.cache[oid]
        self.stdin.write(oid.encode("ascii") + b"\n")
        self.stdin.flush()
        actual_oid, kind, raw_size = self.stdout.readline().decode("ascii").strip().split(" ")
        if actual_oid != oid or kind != "blob":
            raise RuntimeError(f"unexpected cat-file response: {actual_oid} {kind}")
        size = int(raw_size)
        data = self.stdout.read(size)
        if len(data) != size or self.stdout.read(1) != b"\n":
            raise RuntimeError("incomplete cat-file blob")
        status, lines = count_lines(data)
        self.cache[oid] = (size, status, lines)
        return self.cache[oid]

    def close(self) -> None:
        self.stdin.close()
        self.stdout.close()
        if self.process.wait() != 0:
            raise RuntimeError("cat-file failed")


def tree_rows(repo: Path, label: str, ref: str, reader: BlobReader) -> tuple[str, list[dict[str, object]]]:
    commit = git(repo, "rev-parse", "--verify", f"{ref}^{{commit}}").decode().strip()
    entries = git(repo, "ls-tree", "-rz", "--full-tree", commit)
    rows = []
    for entry in entries.split(b"\0"):
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, kind, raw_oid = header.decode("ascii").split(" ")
        path = raw_path.decode("utf-8", errors="strict")
        oid = raw_oid
        size, status, lines = reader.measure(oid) if kind == "blob" else (0, "gitlink", None)
        if mode not in {"100644", "100755"}:
            status, lines = "symlink" if mode == "120000" else "gitlink", None
        rows.append(
            {
                "snapshot": label,
                "commit": commit,
                "path": path,
                "mode": mode,
                "blob": oid,
                "bytes": size,
                "status": status,
                "lines": lines,
                "category": category(path),
            }
        )
    return commit, rows


def overlay_rows(repo: Path, label: str, baseline: list[dict[str, object]]) -> tuple[list[dict[str, object]], str]:
    before_head = git(repo, "rev-parse", "HEAD").decode().strip()
    before_index = git(repo, "ls-files", "--stage", "-z")
    rows = []
    for entry in before_index.split(b"\0"):
        if not entry:
            continue
        header, raw_path = entry.split(b"\t", 1)
        mode, index_oid, stage = header.decode("ascii").split(" ")
        if stage != "0":
            raise RuntimeError("overlay has unresolved index entries")
        path = raw_path.decode("utf-8", errors="strict")
        file = repo / path
        row: dict[str, object] = {
            "snapshot": label,
            "commit": before_head,
            "path": path,
            "mode": mode,
            "blob": "",
            "bytes": 0,
            "status": "missing",
            "lines": None,
            "category": category(path),
        }
        try:
            info = file.lstat()
        except FileNotFoundError:
            rows.append(row)
            continue
        if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
            data = file.read_bytes() if stat.S_ISREG(info.st_mode) else os.fsencode(os.readlink(file))
            status, lines = count_lines(data) if stat.S_ISREG(info.st_mode) else ("symlink", None)
            # hash-object without -w is read-only and honours repository object format.
            oid = (
                subprocess.run(
                    ["git", "-C", str(repo), "hash-object", "--stdin", "--no-filters"], input=data, check=True, capture_output=True
                )
                .stdout.decode()
                .strip()
            )
            row.update(blob=oid, bytes=len(data), status=status, lines=lines)
        elif mode == "160000":
            row.update(blob=index_oid, status="gitlink")
        else:
            row.update(status="nonregular")
        rows.append(row)
    after_head = git(repo, "rev-parse", "HEAD").decode().strip()
    after_index = git(repo, "ls-files", "--stage", "-z")
    if before_head != after_head or before_index != after_index:
        raise RuntimeError("overlay HEAD/index moved; rerun measurement")
    # Hash re-read regular files to detect a moving overlay during the sweep.
    for row in rows:
        if row["status"] in {"text", "nul", "non_utf8", "symlink"}:
            file = repo / str(row["path"])
            data = os.fsencode(os.readlink(file)) if row["status"] == "symlink" else file.read_bytes()
            oid = (
                subprocess.run(
                    ["git", "-C", str(repo), "hash-object", "--stdin", "--no-filters"], input=data, check=True, capture_output=True
                )
                .stdout.decode()
                .strip()
            )
            if oid != row["blob"]:
                raise RuntimeError(f"overlay moved: {row['path']}")
    baseline_by_path = {str(row["path"]): row for row in baseline}
    changed = sum(
        row["blob"] != baseline_by_path[str(row["path"])]["blob"] for row in rows if str(row["path"]) in baseline_by_path and row["blob"]
    )
    digest = hashlib.sha256(json.dumps(rows, sort_keys=True).encode()).hexdigest()
    emit(f"overlay {label}: head={before_head} tracked={len(rows)} modified_vs_head={changed} matching_byte_reread=True digest={digest}")
    return rows, digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--snapshot", action="append", default=[], metavar="LABEL=REF")
    parser.add_argument("--overlay", type=Path)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        self_test()
    if not args.snapshot:
        return
    if args.output_dir is None:
        parser.error("--output-dir is required with --snapshot")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    all_rows: list[dict[str, object]] = []
    snapshots: list[tuple[str, str, list[dict[str, object]]]] = []
    reader = BlobReader(args.repo)
    try:
        for item in args.snapshot:
            label, ref = item.split("=", 1)
            commit, rows = tree_rows(args.repo, label, ref, reader)
            all_rows.extend(rows)
            snapshots.append((label, commit, rows))
        if args.overlay is not None:
            head = git(args.overlay, "rev-parse", "HEAD").decode().strip()
            _, base_rows = tree_rows(args.repo, "overlay-head", head, reader)
            rows, _digest = overlay_rows(args.overlay, "repair-live-overlay", base_rows)
            all_rows.extend(rows)
            snapshots.append(("repair-live-overlay", head, rows))
    finally:
        reader.close()
    fields = ["snapshot", "commit", "path", "mode", "blob", "bytes", "status", "lines", "category"]
    with (args.output_dir / "inventory.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(snapshots[0][2])
    main_rows = {str(row["path"]): row for row in snapshots[0][2]}
    overlap = []
    deltas = []
    for label, commit, rows in snapshots[1:]:
        row_map = {str(row["path"]): row for row in rows}
        for path in sorted(main_rows.keys() | row_map.keys()):
            before, after = main_rows.get(path), row_map.get(path)
            if before is not None and after is not None and (before["blob"], before["mode"]) == (after["blob"], after["mode"]):
                continue
            delta = dict(after) if after is not None else dict(before or {})
            delta.update(snapshot=label, commit=commit, change="added" if before is None else "deleted" if after is None else "modified")
            deltas.append(delta)
            overlap.append(
                {
                    "snapshot": label,
                    "commit": commit,
                    "path": path,
                    "change": "added" if before is None else "deleted" if after is None else "modified",
                    "main_lines": before["lines"] if before else None,
                    "snapshot_lines": after["lines"] if after else None,
                    "oversized_overlap": oversized(before["lines"] if before else None, 5000)
                    or oversized(after["lines"] if after else None, 5000),
                }
            )
    with (args.output_dir / "snapshot-deltas.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=[*fields, "change"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(deltas)
    with (args.output_dir / "changed-overlap.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(
            output,
            fieldnames=["snapshot", "commit", "path", "change", "main_lines", "snapshot_lines", "oversized_overlap"],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(overlap)
    summary = [f"Measured UTC: {datetime.now(UTC).isoformat()}"]
    for label, commit, rows in snapshots:
        text_rows = [row for row in rows if row["status"] == "text"]
        large = sorted((row for row in rows if oversized(row["lines"], 5000)), key=lambda row: (-int(row["lines"]), str(row["path"])))
        summary.append(
            f"snapshot={label} commit={commit} entries={len(rows)} text={len(text_rows)} excluded={len(rows) - len(text_rows)} gt5000={len(large)} gt10000={sum(oversized(row['lines'], 10000) for row in rows)}"
        )
        for row in large:
            summary.append(f"{row['lines']}\t{row['bytes']}\t{row['blob']}\t{row['category']}\t{row['path']}")
    summary.append(f"changed_rows={len(overlap)} oversized_overlap_rows={sum(bool(row['oversized_overlap']) for row in overlap)}")
    text = "\n".join(summary) + "\n"
    (args.output_dir / "raw-measurements.txt").write_text(text, encoding="utf-8")
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
