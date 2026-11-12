"""Audit harness: drive ONE multi-turn composer session against a live local server.

Not product code. Serial by design: provider calls captured by launch_web.py are
attributed to a turn by wall-clock window, so run one driver per server instance.

usage: drive_session.py --base http://127.0.0.1:8451 --capture <capture dir> \
           --creds <credentials.json> --scenario <scenario.json> --out <dir>

scenario.json:
{
  "name": "edit-orders",
  "attachments": [{"path": "fixtures/feedback.csv", "mime": "text/csv"}],   # optional, uploaded before turn 1
  "turns": [
     "first user message",
     {"content": "second message", "reviews": "accept" | "leave", "proposals": "accept" | "reject" | "leave",
      "attachments": [...]}                                              # per-turn overrides
  ],
  "reviews": "accept",        # default per-turn policy for pending interpretation reviews
  "proposals": "accept"       # default per-turn policy for pending proposals
}
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

import requests

POLL_CAP_S = 700.0


class Client:
    def __init__(self, base: str) -> None:
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.log: list[dict[str, Any]] = []

    def call(self, step: str, method: str, path: str, **kw: Any) -> tuple[int, Any]:
        t0 = time.time()
        try:
            r = self.s.request(method, self.base + path, timeout=kw.pop("timeout", 60), **kw)
            try:
                body = r.json()
            except ValueError:
                body = r.text
            status = r.status_code
        except requests.RequestException as exc:
            status, body = -1, repr(exc)
        self.log.append({"step": step, "method": method, "path": path, "status": status, "elapsed_ms": int((time.time() - t0) * 1000)})
        return status, body


def slice_capture(capture: Path, t0: float, t1: float) -> list[dict[str, Any]]:
    f = capture / "llm_calls.jsonl"
    if not f.exists():
        return []
    out = []
    for line in f.read_text(encoding="utf-8").splitlines():
        rec = json.loads(line)
        if t0 <= rec["ts"] <= t1 + 2:
            out.append(rec)
    return out


def render_calls(calls: list[dict[str, Any]], prev_len_by_model: dict[str, int]) -> str:
    """Human-readable transcript: for each provider call, the NEW messages since the
    previous call to the same model, then reasoning, content and tool calls."""
    lines: list[str] = []
    for c in calls:
        model = c["model"] or "?"
        msgs = c["messages"] or []
        resp = c.get("response") or {}
        usage = resp.get("usage") or {}
        lines.append(
            f"\n---\n### provider call seq={c['seq']} model={model} kind={c['kind']} "
            f"duration={c['duration_s']}s prompt_tokens={usage.get('prompt_tokens')} "
            f"completion_tokens={usage.get('completion_tokens')} n_messages={len(msgs)} n_tools={len(c['tool_names'])}"
        )
        lines.append(f"tools offered: {', '.join(t or '?' for t in c['tool_names'])}")
        prev = prev_len_by_model.get(model, 0)
        start = prev if len(msgs) >= prev else 0
        if start == 0:
            lines.append(f"(full message list; {len(msgs)} messages. System/skill text elided to first 400 chars.)")
        for i, m in enumerate(msgs[start:], start=start):
            role = m.get("role")
            content = m.get("content")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if role == "system" and len(content) > 400:
                content = content[:400] + f" …[{len(content)} chars]"
            elif len(content) > 6000:
                content = content[:6000] + f" …[truncated; {len(content)} chars]"
            extra = ""
            if m.get("tool_calls"):
                extra = (
                    " tool_calls="
                    + json.dumps(
                        [{"name": t["function"]["name"], "arguments": t["function"]["arguments"]} for t in m["tool_calls"]],
                        ensure_ascii=False,
                    )[:6000]
                )
            if m.get("tool_call_id"):
                extra += f" (tool_call_id={m['tool_call_id']})"
            lines.append(f"\n**[{i}] {role}**{extra}\n\n{content}")
        prev_len_by_model[model] = len(msgs) + 1
        if c["kind"] != "success":
            lines.append(f"\n**PROVIDER FAILURE**: {c.get('exception')}")
            continue
        msg = ((resp.get("choices") or [{}])[0]).get("message") or {}
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        lines.append(f"\n#### MODEL REASONING ({len(reasoning)} chars)\n\n{reasoning or '(none returned)'}")
        lines.append(f"\n#### MODEL CONTENT\n\n{msg.get('content') or '(empty)'}")
        for t in msg.get("tool_calls") or []:
            lines.append(f"\n#### MODEL TOOL CALL `{t['function']['name']}`\n\n```json\n{t['function']['arguments']}\n```")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--capture", required=True)
    ap.add_argument("--creds", required=True)
    ap.add_argument("--scenario", required=True)
    ap.add_argument("--out", required=True)
    ns = ap.parse_args()

    scenario_path = Path(ns.scenario)
    scenario = json.loads(scenario_path.read_text())
    out = Path(ns.out)
    out.mkdir(parents=True, exist_ok=True)
    capture = Path(ns.capture)
    c = Client(ns.base)

    st, body = c.call("login", "POST", "/api/auth/login", json=json.loads(Path(ns.creds).read_text()))
    if st != 200:
        print(f"login failed {st} {body}", file=sys.stderr)
        return 2
    c.s.headers["Authorization"] = f"Bearer {body['access_token']}"
    st, body = c.call("create_session", "POST", "/api/sessions", json={})
    if st != 201:
        print(f"create_session failed {st} {body}", file=sys.stderr)
        return 2
    sid = body["id"]
    c.call("patch_title", "PATCH", f"/api/sessions/{sid}", json={"title": f"audit/{scenario['name']}"})
    prefs = scenario.get("preferences")
    if prefs:
        c.call("patch_preferences", "PATCH", f"/api/sessions/{sid}/composer/preferences", json=prefs)

    def upload(atts: list[dict[str, Any]]) -> list[Any]:
        res = []
        for a in atts:
            p = (scenario_path.parent / a["path"]).resolve()
            with p.open("rb") as fh:
                st_, b_ = c.call(
                    "upload_blob",
                    "POST",
                    f"/api/sessions/{sid}/blobs",
                    files={"file": (p.name, fh, a.get("mime", "application/octet-stream"))},
                )
            res.append({"file": p.name, "status": st_, "body": b_})
        return res

    summary: dict[str, Any] = {"scenario": scenario["name"], "session_id": sid, "turns": []}
    uploads = upload(scenario.get("attachments", []))
    if uploads:
        (out / "uploads.json").write_text(json.dumps(uploads, indent=2))
    prev_len: dict[str, int] = {}
    transcript = [f"# Live transcript: {scenario['name']}\n\nsession_id: {sid}\n"]

    for n, turn in enumerate(scenario["turns"], start=1):
        if isinstance(turn, str):
            turn = {"content": turn}
        tdir = out / f"turn{n:02d}"
        tdir.mkdir(exist_ok=True)
        if turn.get("attachments"):
            (tdir / "uploads.json").write_text(json.dumps(upload(turn["attachments"]), indent=2))
        t0 = time.time()
        st, head = c.call("state_head", "GET", f"/api/sessions/{sid}/state")
        state_id = head.get("id") if st == 200 and isinstance(head, dict) else None
        op = str(uuid4())
        st, admit = c.call(
            "post_message",
            "POST",
            f"/api/sessions/{sid}/messages",
            json={"content": turn["content"], "operation_id": op, "state_id": state_id},
        )
        terminal: Any = {"admission_status": st, "body": admit}
        if st == 202:
            deadline = time.time() + POLL_CAP_S
            while time.time() < deadline:
                st2, obs = c.call("operation", "GET", f"/api/sessions/{sid}/operations/{op}")
                if st2 != 200:
                    terminal = {"poll_status": st2, "body": obs}
                    break
                if obs["status"] in ("completed", "failed"):
                    terminal = obs
                    break
                time.sleep(max(0.5, obs.get("poll_after_ms", 1000) / 1000))
            else:
                c.call("cancel", "POST", f"/api/sessions/{sid}/operations/{op}/cancel")
                terminal = {"driver": "poll cap reached; cancelled"}
        t_compose_end = time.time()
        (tdir / "terminal.json").write_text(json.dumps(terminal, indent=2))

        # proposals
        proposals_policy = turn.get("proposals", scenario.get("proposals", "accept"))
        reviews_policy = turn.get("reviews", scenario.get("reviews", "accept"))
        actions: list[dict[str, Any]] = []
        for _round in range(5):
            acted = False
            st, rv = c.call("list_reviews", "GET", f"/api/sessions/{sid}/interpretations", params={"status": "pending"})
            events = rv.get("events", []) if st == 200 and isinstance(rv, dict) else []
            if _round == 0:
                (tdir / "reviews_pending.json").write_text(json.dumps(rv, indent=2))
            if reviews_policy == "accept":
                for ev in events:
                    s_, b_ = c.call(
                        "resolve_review",
                        "POST",
                        f"/api/sessions/{sid}/interpretations/{ev['id']}/resolve",
                        json={"choice": "accepted_as_drafted"},
                    )
                    actions.append(
                        {
                            "action": "resolve_review",
                            "id": ev["id"],
                            "kind": ev.get("kind"),
                            "status": s_,
                            "body": b_ if s_ >= 400 else None,
                        }
                    )
                    acted = True
            st, pr = c.call("list_proposals", "GET", f"/api/sessions/{sid}/proposals")
            if _round == 0:
                (tdir / "proposals.json").write_text(json.dumps(pr, indent=2))
            plist = pr.get("proposals", pr) if isinstance(pr, dict) else pr
            for p in plist if isinstance(plist, list) else []:
                if isinstance(p, dict) and p.get("status") == "pending" and proposals_policy in ("accept", "reject"):
                    draft_hash = (p.get("pipeline_metadata") or {}).get("draft_hash")
                    body_ = {"draft_hash": draft_hash} if proposals_policy == "accept" and draft_hash else None
                    s_, b_ = c.call(
                        f"{proposals_policy}_proposal", "POST", f"/api/sessions/{sid}/proposals/{p['id']}/{proposals_policy}", json=body_
                    )
                    actions.append(
                        {"action": f"{proposals_policy}_proposal", "id": p["id"], "status": s_, "body": b_ if s_ >= 400 else None}
                    )
                    acted = True
            if not acted:
                break
        (tdir / "post_turn_actions.json").write_text(json.dumps(actions, indent=2))

        st, state = c.call("state", "GET", f"/api/sessions/{sid}/state")
        (tdir / "state.json").write_text(json.dumps(state, indent=2))
        validation: Any = None
        if st == 200 and isinstance(state, dict) and state.get("id"):
            _, validation = c.call("validate", "POST", f"/api/sessions/{sid}/validate", params={"state_id": state["id"]}, timeout=120)
            (tdir / "validation.json").write_text(json.dumps(validation, indent=2))
        _, yaml_text = c.call("yaml", "GET", f"/api/sessions/{sid}/state/yaml")
        (tdir / "state_yaml.json").write_text(json.dumps(yaml_text, indent=2))
        _, msgs = c.call("messages", "GET", f"/api/sessions/{sid}/messages", params={"include_llm_audit": "true", "limit": 500})
        (tdir / "messages.json").write_text(json.dumps(msgs, indent=2))
        t1 = time.time()
        calls = slice_capture(capture, t0, t1)
        (tdir / "llm_calls.json").write_text(json.dumps(calls, indent=1))

        result = terminal.get("result") if isinstance(terminal, dict) else None
        reply = (result or {}).get("message", {}).get("content") if isinstance(result, dict) else None
        error = terminal.get("error") if isinstance(terminal, dict) else None
        tsum = {
            "turn": n,
            "user": turn["content"],
            "compose_seconds": round(t_compose_end - t0, 1),
            "operation_status": terminal.get("status") if isinstance(terminal, dict) else None,
            "error": error,
            "assistant_reply": reply,
            "provider_calls": [
                {
                    "seq": k["seq"],
                    "model": k["model"],
                    "kind": k["kind"],
                    "duration_s": k["duration_s"],
                    "prompt_tokens": ((k.get("response") or {}).get("usage") or {}).get("prompt_tokens"),
                    "completion_tokens": ((k.get("response") or {}).get("usage") or {}).get("completion_tokens"),
                    "tool_calls": [
                        t["function"]["name"]
                        for t in ((((k.get("response") or {}).get("choices") or [{}])[0]).get("message") or {}).get("tool_calls") or []
                    ],
                }
                for k in calls
            ],
            "post_turn_actions": actions,
            "state_id": state.get("id") if isinstance(state, dict) else None,
            "validation_is_valid": validation.get("is_valid") if isinstance(validation, dict) else None,
        }
        summary["turns"].append(tsum)
        (out / "summary.json").write_text(json.dumps(summary, indent=2))
        transcript.append(
            f"\n\n# TURN {n}\n\n## USER\n\n{turn['content']}\n\n"
            f"compose_seconds={tsum['compose_seconds']} operation_status={tsum['operation_status']} "
            f"provider_calls={len(calls)}\n"
        )
        transcript.append(render_calls(calls, prev_len))
        transcript.append(
            f"\n\n## WHAT THE USER SAW (assistant reply)\n\n{reply}\n\n## TERMINAL ERROR\n\n"
            f"{json.dumps(error, indent=2) if error else '(none)'}\n\n## POST-TURN ACTIONS (driver)\n\n"
            f"{json.dumps(actions, indent=2)}\n\n## VALIDATION\n\n{json.dumps(validation, indent=2)[:3000]}"
        )
        (out / "transcript.md").write_text("\n".join(transcript), encoding="utf-8")
        print(
            json.dumps(
                {k: tsum[k] for k in ("turn", "compose_seconds", "operation_status", "validation_is_valid")}
                | {"n_calls": len(calls), "reply": (reply or "")[:160]}
            ),
            flush=True,
        )
        time.sleep(4)  # stay under the per-user compose rate limit

    (out / "http_log.json").write_text(json.dumps(c.log, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
