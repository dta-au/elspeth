"""Composer path-quality battery — live driver (spec §4).

Login only (never register). Captures runs into runs/<round>/<case>/<n>/;
never scores for measurement (report.py does) — it consults the scorer's
exclusion verdict only for the abort rules. Compose + validate only; never
/execute.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from evals.lib.battery_capture import Instrument  # noqa: E402
from evals.lib.battery_corpus import CorpusCase, load_corpus  # noqa: E402
from evals.lib.battery_score import INSTRUMENT_KINDS, path_from_disk  # noqa: E402

CLIENT_TIMEOUT_S = 620.0
PAGE = 500
SESSION_PAGE = 200  # GET /api/sessions caps limit at 200 and defaults to 50 (web/sessions.py list_sessions)
MAX_PAGES = 40
MAX_REVIEW_ROUNDS = 5
SETTLE_POLLS = 12
SETTLE_INTERVAL_S = 5.0
CANARY_N = 10
MIN_RUN_SPACING_S = 7.0  # per-user compose rate limit is 10/min (deploy/elspeth-web.env); never let fast failures amplify into 429s
DEFAULT_BASE = "https://elspeth.foundryside.dev"
PINNED_PREFERENCES = {
    "trust_mode": "auto_commit",
    "density_default": "high",
}  # the product defaults (sessions/models.py), pinned per session for comparability
# composer-progress `reason` → the terminal budget it reports (contracts/composer_progress.py convergence set).
PROGRESS_BUDGETS: dict[str, str] = {
    "convergence_wall_clock_timeout": "timeout",
    "convergence_composition_budget": "composition",
    "convergence_discovery_budget": "discovery",
}


# ── HTTP seam ──────────────────────────────────────────────────────────────


@dataclass
class HttpResponse:
    status_code: int
    body: Any
    text: str = ""


class HttpTimeout(Exception):
    pass


class HttpTransportError(Exception):
    """Connection-level failure (refused/reset/DNS) — degrades one run, never the round."""


class HttpClient(Protocol):
    def request(
        self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> HttpResponse: ...
    def set_token(self, token: str) -> None: ...


class RequestsClient:
    def __init__(self, base: str) -> None:
        import requests

        self._requests = requests
        self._base = base.rstrip("/")
        self._s = requests.Session()

    def set_token(self, token: str) -> None:
        self._s.headers["Authorization"] = f"Bearer {token}"

    def request(
        self, method: str, path: str, *, json: Any = None, params: Mapping[str, Any] | None = None, timeout: float | None = None
    ) -> HttpResponse:
        try:
            r = self._s.request(method, self._base + path, json=json, params=dict(params or {}), timeout=timeout or 60.0)
        except self._requests.Timeout as exc:
            raise HttpTimeout(str(exc)) from exc
        except self._requests.RequestException as exc:
            raise HttpTransportError(f"{type(exc).__name__}: {exc}") from exc
        try:
            body = r.json() if r.content else None
        except ValueError:
            body = None
        return HttpResponse(r.status_code, body, r.text)


class BatteryAuthError(RuntimeError):
    pass


class BatteryIdentityError(RuntimeError):
    pass


def read_env_budgets(env_file: Path) -> dict[str, Any]:
    values: dict[str, str] = {}
    for line in Path(env_file).read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        values[k.strip()] = v.strip()
    out: dict[str, Any] = {}
    for key, name, cast in (
        ("advisor_model", "ELSPETH_WEB__COMPOSER_ADVISOR_MODEL", str),
        ("composition_turns", "ELSPETH_WEB__COMPOSER_MAX_COMPOSITION_TURNS", int),
        ("discovery_turns", "ELSPETH_WEB__COMPOSER_MAX_DISCOVERY_TURNS", int),
    ):
        if name not in values:
            raise ValueError(f"{env_file}: missing {name} — binding identity would be incomplete")
        out[key] = cast(values[name])
    out["_env_file_sha256"] = hashlib.sha256(
        Path(env_file).read_bytes()
    ).hexdigest()  # recorded identity: operator-asserted budgets are at least visible as a delta
    return out


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _local_skill_hash() -> str | None:
    """SHA-256 of the checkout's pipeline_composer.md — the same function the server uses for its audit rows."""
    try:
        from elspeth.web.composer.skills import load_skill_with_hash

        return load_skill_with_hash("pipeline_composer")[1]
    except Exception:  # recorded, not binding; absence is honest
        return None


def _is_instrument(verdict: str | None) -> bool:
    return verdict in INSTRUMENT_KINDS


def should_abort(verdicts: Sequence[str | None]) -> str | None:
    """Three consecutive INSTRUMENT exclusions ⇒ abort reason; measurement kinds (surface/no_calls) never count."""
    if len(verdicts) >= 3 and all(_is_instrument(v) for v in verdicts[-3:]):
        return "3 consecutive instrument_error"
    return None


def run_dir_is_complete(run_dir: Path) -> bool:
    for name in ("messages.json", "meta.json", "reviews.json"):
        p = run_dir / name
        if not p.exists():
            return False
        try:
            json.loads(p.read_text())
        except ValueError:
            return False
    return True


# ── the driver ─────────────────────────────────────────────────────────────


class Battery:
    def __init__(
        self,
        client: HttpClient,
        *,
        base: str,
        round_name: str,
        runs_dir: Path,
        corpus_version: int,
        env_budgets: Mapping[str, Any],
        repeats: int = 5,
        resume: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.client = client
        self.base = base
        self.round = round_name
        self.runs_dir = Path(runs_dir)
        self.round_dir = self.runs_dir / round_name
        self.corpus_version = corpus_version
        self.env = dict(env_budgets)
        self.repeats = repeats
        self.resume = resume
        self._sleep = sleep
        self._clock = clock
        self._status: dict[str, Any] | None = None
        self.env_file_sha256: str | None = self.env.get("_env_file_sha256")
        self.local_skill_hash: str | None = _local_skill_hash()
        self._fired_any = False
        self._firing: dict[str, Any] = {
            "round": round_name,
            "base": base,
            "started_at": None,
            "completed": [],
            "aborted": False,
            "abort_reason": None,
            "tripwire_error": None,
            "case_flags": {},
        }

    # -- auth / identity --
    def login(self, username: str, password: str) -> None:
        r = self.client.request("POST", "/api/auth/login", json={"username": username, "password": password}, timeout=30)
        token = (r.body or {}).get("access_token") if isinstance(r.body, dict) else None
        if r.status_code != 200 or not token:
            raise BatteryAuthError(f"login failed (HTTP {r.status_code}); refusing to continue — never register from the battery")
        self.client.set_token(token)

    def system_status(self) -> dict[str, Any]:
        if self._status is None:
            r = self.client.request("GET", "/api/system/status", timeout=30)
            if r.status_code != 200 or not isinstance(r.body, dict):
                raise BatteryIdentityError(
                    f"/api/system/status returned {r.status_code}; binding identity would be incomplete — refusing to fire"
                )
            for key in ("composer_model", "composer_timeout_seconds"):
                if r.body.get(key) in (None, ""):
                    raise BatteryIdentityError(
                        f"/api/system/status carries no {key}; binding identity would be incomplete — refusing to fire"
                    )
            self._status = r.body
        return self._status

    def _prime_status(self) -> None:
        """Best-effort ``system_status()`` warm-up: swallow any failure so a status outage degrades
        ``_identity``'s binding fields to ``None`` rather than escaping into a caller that cannot
        afford to raise (``fire()``'s top-of-round call, and ``run_prompt``'s own entry so a direct
        caller — a test, ``planner_probe`` — still gets real identity data whenever the status
        endpoint is actually reachable). Idempotent: a no-op once ``self._status`` is cached."""
        if self._status is None:
            with contextlib.suppress(Exception):
                self.system_status()

    # -- one run --
    def run_prompt(self, *, label: str, prompt: str, run_dir: Path, case: str, repeat: int, capture_proposals: bool = False) -> str | None:
        self._prime_status()
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        http: list[dict[str, Any]] = []
        instrument = {
            "truncated": False,
            "read_integrity": None,
            "http_unrecovered": None,
            "auth_failed": False,
            "review_rounds_exhausted": False,
        }
        terminal: dict[str, Any] = {"budget_exhausted": None, "reason": None, "source": "none"}

        def step(name: str, method: str, path: str, **kw: Any) -> HttpResponse | None:
            t0 = self._clock()
            try:
                r = self.client.request(method, path, **kw)
            except HttpTimeout:
                http.append({"step": name, "status": None, "elapsed_ms": int((self._clock() - t0) * 1000), "detail": "client timeout"})
                return None
            except HttpTransportError as exc:
                http.append({"step": name, "status": None, "elapsed_ms": int((self._clock() - t0) * 1000), "detail": f"transport: {exc}"})
                instrument["http_unrecovered"] = instrument["http_unrecovered"] or f"{name}: transport error ({exc})"
                return None
            http.append({"step": name, "status": r.status_code, "elapsed_ms": int((self._clock() - t0) * 1000), "detail": None})
            if r.status_code == 429:
                instrument["http_unrecovered"] = instrument["http_unrecovered"] or f"429 rate limited at {name}"
            return r

        # 1. session
        r = step("create_session", "POST", "/api/sessions", json={}, timeout=30)
        if r is None or r.status_code != 201:
            instrument["http_unrecovered"] = f"POST /api/sessions {r.status_code if r else 'timeout'}"
            (run_dir / "reviews.json").write_text("[]")
            self._write_meta(
                run_dir,
                case=case,
                repeat=repeat,
                label=label,
                prompt=prompt,
                session_id=None,
                state_id=None,
                http=http,
                terminal=terminal,
                instrument=instrument,
                messages=[],
            )
            (run_dir / "messages.json").write_text("[]")
            return self._verdict(run_dir, case)
        sid = str(r.body["id"])
        # 2. title BEFORE any message — suppresses the unaudited auto-title provider call
        tr = step("patch_title", "PATCH", f"/api/sessions/{sid}", json={"title": label}, timeout=30)
        if tr is None or tr.status_code != 200:
            instrument["http_unrecovered"] = instrument["http_unrecovered"] or f"PATCH title {tr.status_code if tr else 'timeout'}"
        step("patch_preferences", "PATCH", f"/api/sessions/{sid}/composer/preferences", json=dict(PINNED_PREFERENCES), timeout=30)
        pr_ = step("get_preferences", "GET", f"/api/sessions/{sid}/composer/preferences", timeout=30)
        preferences: dict[str, Any] | None = None
        if pr_ is not None and pr_.status_code == 200 and isinstance(pr_.body, dict):
            preferences = {k: pr_.body.get(k) for k in PINNED_PREFERENCES}
        if preferences != dict(PINNED_PREFERENCES):
            instrument["http_unrecovered"] = instrument["http_unrecovered"] or f"preferences not pinned: read back {preferences!r}"
        # 3. compose
        r = step("post_message", "POST", f"/api/sessions/{sid}/messages", json={"content": prompt}, timeout=CLIENT_TIMEOUT_S)
        if r is None:
            pr = step("composer_progress", "GET", f"/api/sessions/{sid}/composer-progress", timeout=30)
            reason = pr.body.get("reason") if pr is not None and isinstance(pr.body, dict) else None
            terminal = {
                "budget_exhausted": PROGRESS_BUDGETS.get(str(reason)) if reason is not None else None,
                "reason": reason,
                # no reason means no snapshot (progress endpoint non-200/empty): the scorer's terminal_missing
                # keys on `source`, so claiming composer_progress here would hide a missing terminal.
                "source": "composer_progress" if reason is not None else "none",
            }
            self._settle(sid, step)
        elif r.status_code != 200:
            detail = r.body.get("detail") if isinstance(r.body, dict) else None
            http[-1]["detail"] = detail
            if r.status_code == 422 and isinstance(detail, dict):
                terminal = {"budget_exhausted": detail.get("budget_exhausted"), "reason": detail.get("reason"), "source": "422_detail"}
            if r.status_code in (401, 403):
                instrument["auth_failed"] = True
            if r.status_code >= 500:
                # the composer's structured terminal (turn/wall budget) is a 422, so a 5xx here is a SERVER
                # fault, never a product outcome: exclude the run as an instrument error and let it feed the
                # abort rule rather than scoring a dead substrate as 95 product findings.
                instrument["http_unrecovered"] = instrument["http_unrecovered"] or f"post_message {r.status_code}"
            self._settle(sid, step)
        # 4. reviews
        reviews: list[dict[str, Any]] = []
        exhausted = True
        for rnd in range(1, MAX_REVIEW_ROUNDS + 1):
            lr = step("list_reviews", "GET", f"/api/sessions/{sid}/interpretations", params={"status": "pending"}, timeout=30)
            if lr is None or lr.status_code != 200 or not isinstance(lr.body, dict):
                instrument["http_unrecovered"] = instrument["http_unrecovered"] or f"list_reviews {lr.status_code if lr else 'timeout'}"
                exhausted = False
                break
            events = lr.body.get("events", [])
            if not events:
                exhausted = False
                break
            for ev in events:
                reviews.append({"round": rnd, "event": ev})
                step(
                    "resolve_review",
                    "POST",
                    f"/api/sessions/{sid}/interpretations/{ev['id']}/resolve",
                    json={"choice": "accepted_as_drafted"},
                    timeout=60,
                )
        instrument["review_rounds_exhausted"] = exhausted
        (run_dir / "reviews.json").write_text(json.dumps(reviews, indent=2))
        # 5. state + validate (pinned to state_id)
        state_id: str | None = None
        sr = step("get_state", "GET", f"/api/sessions/{sid}/state", timeout=30)
        if sr is not None and sr.status_code != 200:
            # "no state yet" is 200 + a null body (composer/state.py get_current_state); only a non-200 is a
            # fault, and without this the missing state.json would score as an empty final state — a PRODUCT
            # finding for a server outage.
            instrument["http_unrecovered"] = instrument["http_unrecovered"] or f"get_state {sr.status_code}"
        if sr is not None and sr.status_code == 200 and isinstance(sr.body, dict):
            (run_dir / "state.json").write_text(json.dumps(sr.body, indent=2))
            state_id = str(sr.body.get("id"))
            vr = step("validate", "POST", f"/api/sessions/{sid}/validate", params={"state_id": state_id}, timeout=120)
            if vr is not None and vr.status_code == 200:
                (run_dir / "validate.json").write_text(json.dumps(vr.body, indent=2))
            else:
                # same reasoning: no validate.json reads as `not is_valid`, which is a product verdict the
                # capture never actually obtained.
                instrument["http_unrecovered"] = instrument["http_unrecovered"] or f"validate {vr.status_code if vr else 'timeout'}"
        # 6. paginated thread capture
        messages: list[dict[str, Any]] = []
        last_full = False
        for k in range(MAX_PAGES):
            params = {
                "include_tool_rows": "true",
                "include_llm_audit": "true",
                "include_raw_content": "true",
                "limit": PAGE,
                "offset": k * PAGE,
            }
            pr = step("get_messages", "GET", f"/api/sessions/{sid}/messages", params=params, timeout=120)
            if pr is None or pr.status_code != 200 or not isinstance(pr.body, list):
                status = pr.status_code if pr is not None else "timeout"
                if pr is not None and isinstance(pr.body, dict) and pr.body.get("error_type") == "audit_integrity_error":
                    instrument["read_integrity"] = str(pr.body.get("detail"))
                instrument["http_unrecovered"] = f"GET /messages {status} at offset {k * PAGE}"
                break
            messages.extend(pr.body)
            last_full = len(pr.body) == PAGE
            if not last_full:
                break
        else:
            last_full = True  # MAX_PAGES exhausted with full pages
        instrument["truncated"] = last_full
        (run_dir / "messages.json").write_text(json.dumps(messages, indent=2))
        # 7. proposals (tripwire/probe need them; harmless otherwise)
        if capture_proposals:
            p1 = step("get_proposals", "GET", f"/api/sessions/{sid}/proposals", timeout=30)
            p2 = step("get_proposal_events", "GET", f"/api/sessions/{sid}/proposal-events", timeout=30)
            (run_dir / "proposals.json").write_text(
                json.dumps(
                    {
                        "proposals": p1.body if p1 and p1.status_code == 200 else None,
                        "events": p2.body if p2 and p2.status_code == 200 else None,
                    },
                    indent=2,
                )
            )
        # 8. meta
        self._write_meta(
            run_dir,
            case=case,
            repeat=repeat,
            label=label,
            prompt=prompt,
            session_id=sid,
            state_id=state_id,
            http=http,
            terminal=terminal,
            instrument=instrument,
            messages=messages,
            preferences=preferences,
            reviews=reviews,
        )
        return self._verdict(run_dir, case)

    def _settle(self, sid: str, step: Callable[..., HttpResponse | None]) -> None:
        """After a non-200 compose response server writes may still be in flight; wait for the audit-row count to hold
        across two reads. Goes through ``step`` so a timeout/transport error here degrades the run, never the round."""
        prev = -1
        for _ in range(SETTLE_POLLS):
            r = step("settle", "GET", f"/api/sessions/{sid}/messages", params={"include_llm_audit": "true", "limit": PAGE}, timeout=60)
            n = len(r.body) if r is not None and isinstance(r.body, list) else -2
            if n == prev:
                return
            prev = n
            self._sleep(SETTLE_INTERVAL_S)

    def _identity(self, messages: list[dict[str, Any]], reviews: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """Never performs I/O: reads the ALREADY-CACHED status (primed by ``fire()``/``main()`` via
        ``system_status()``). Called from exception-containment paths (``_contained`` → ``_write_meta``),
        so a live request here could itself fail and escape containment — an unprimed cache degrades the
        binding fields to ``None`` instead, which the report already treats as an incomparable run."""
        st = self._status or {}
        review_hash = next(
            (
                rv["event"].get("composer_skill_hash")
                for rv in (reviews or [])
                if isinstance(rv.get("event"), dict) and rv["event"].get("composer_skill_hash")
            ),
            None,
        )
        first_call = None
        first_tool = None
        for m in messages:
            if m.get("role") != "audit":
                continue
            for env in m.get("tool_calls") or []:
                if isinstance(env, dict) and env.get("_kind") == "llm_call_audit":
                    c = env.get("call") or {}
                    first_call = first_call or c
                    if c.get("tools_spec_hash") and c.get("status") == "success" and first_tool is None:
                        first_tool = c
        ft = first_tool or {}
        return {
            "binding": {
                "substrate": self.base,
                "composer_model": st.get("composer_model"),
                "advisor_model": self.env["advisor_model"],
                "model_returned": ft.get("model_returned"),
                "composer_timeout_seconds": st.get("composer_timeout_seconds"),
                "budgets": {"composition_turns": self.env["composition_turns"], "discovery_turns": self.env["discovery_turns"]},
                "tools_spec_hash": ft.get("tools_spec_hash"),
                "temperature": ft.get("temperature"),
                "seed": ft.get("seed"),
            },
            "recorded": {
                "composer_skill_hash": review_hash,
                "composer_skill_hash_source": "review_payload" if review_hash else "null",
                "local_skill_file_sha256": self.local_skill_hash,
                "env_file_sha256": self.env_file_sha256,
                "first_call_messages_hash": (first_call or {}).get("messages_hash"),
                # no server_version: /api/system/status carries no version key, so recording one only ever
                # rendered a null. frontend_build is the real build fingerprint the endpoint does return.
                "frontend_build": st.get("frontend_build"),
            },
        }

    def _write_meta(
        self,
        run_dir: Path,
        *,
        case: str,
        repeat: int,
        label: str,
        prompt: str,
        session_id: str | None,
        state_id: str | None,
        http: list[dict[str, Any]],
        terminal: dict[str, Any],
        instrument: dict[str, Any],
        messages: list[dict[str, Any]],
        preferences: dict[str, Any] | None = None,
        reviews: list[dict[str, Any]] | None = None,
    ) -> None:
        meta = {
            "round": self.round,
            "case": case,
            "repeat": repeat,
            "corpus_version": self.corpus_version,
            "prompt_sha256": _sha(prompt),
            "session_id": session_id,
            "state_id": state_id,
            "label": label,
            "preferences": preferences,
            "http": http,
            "server_terminal": terminal,
            "instrument": Instrument(**instrument).to_dict(),
            "identity": self._identity(messages, reviews),
        }
        (run_dir / "meta.json").write_text(json.dumps(meta, indent=2))

    def _verdict(self, run_dir: Path, case: str) -> str | None:  # case kept for log lines
        return path_from_disk(run_dir).excluded  # scenario-free: the driver never loads a scenario

    # -- the firing --
    def _label(self, case: str, repeat: int) -> str:
        return f"battery/{self.round}/{case}/{repeat}"

    def _record(self, case: str, repeat: int, label: str, excluded: str | None) -> None:
        self._firing["completed"].append({"case": case, "repeat": repeat, "label": label, "session_id": None, "excluded": excluded})
        self.round_dir.mkdir(parents=True, exist_ok=True)
        (self.round_dir / "firing.json").write_text(json.dumps(self._firing, indent=2))

    def resume_skip(self, run_dir: Path) -> bool:
        """True when ``--resume`` is on and this run's capture is already complete (spec §4: a resume never
        re-fetches or overwrites a captured page). Public so the tripwire/probe wrappers honour --resume too."""
        return self.resume and run_dir_is_complete(Path(run_dir))

    def _run_or_resume(self, case: str, repeat: int, prompt: str) -> str | None:
        run_dir = self.round_dir / case / str(repeat)
        label = self._label(case, repeat)
        if self.resume_skip(run_dir):
            return self._verdict(run_dir, case)
        if self._fired_any:
            self._sleep(MIN_RUN_SPACING_S)
        self._fired_any = True
        return self.run_prompt(label=label, prompt=prompt, run_dir=run_dir, case=case, repeat=repeat)

    def _contained(self, case: str, repeat: int, prompt: str) -> str | None:
        """Run one prompt; an unexpected exception is recorded as an http instrument fault, never propagated."""
        run_dir = self.round_dir / case / str(repeat)
        try:
            return self._run_or_resume(case, repeat, prompt)
        except Exception as exc:  # containment is the point
            try:
                run_dir.mkdir(parents=True, exist_ok=True)
                for name, body in (("messages.json", "[]"), ("reviews.json", "[]")):
                    if not (run_dir / name).exists():
                        (run_dir / name).write_text(body)
                if not (run_dir / "meta.json").exists():
                    self._write_meta(
                        run_dir,
                        case=case,
                        repeat=repeat,
                        label=self._label(case, repeat),
                        prompt=prompt,
                        session_id=None,
                        state_id=None,
                        http=[],
                        terminal={"budget_exhausted": None, "reason": None, "source": "none"},
                        instrument={
                            "truncated": False,
                            "read_integrity": None,
                            "http_unrecovered": f"driver exception: {exc!r}",
                            "auth_failed": False,
                            "review_rounds_exhausted": False,
                        },
                        messages=[],
                    )
                return path_from_disk(run_dir).excluded
            except Exception:
                # a SECOND failure inside the handler (an unwritable dir, a partial capture the scorer cannot
                # even reach a CaptureError on) must not escape either: fall back to the capture verdict
                # without re-scoring. The round survives; the run is excluded and visible.
                return "capture"

    def fire(
        self,
        cases: Mapping[str, CorpusCase],
        *,
        tripwire: Callable[[Battery], None] | None,
        preflight: Callable[[], None] | None = None,
        only: set[str] | None = None,
    ) -> dict[str, Any]:
        self._firing["started_at"] = self._firing["started_at"] or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        # The classifier-drift check runs BEFORE the canary: it is a config failure, and discovering it after
        # ten canary runs (once the tripwire fires) would spend the canary to learn the round cannot be read.
        if preflight is not None:
            preflight()
        self._prime_status()  # once up front; main() already primes too (idempotent) and surfaces a hard failure separately
        selected = {n: c for n, c in cases.items() if only is None or n in only}
        streak: list[str | None] = []  # shared across canary AND corpus runs: instrument failures don't reset at the seam
        if "canary" in selected:
            for rep in range(1, CANARY_N + 1):
                verdict = self._contained("canary", rep, selected["canary"].prompt)
                self._record("canary", rep, self._label("canary", rep), verdict)
                streak.append(verdict)
                reason = should_abort(streak)
                if reason:
                    self._firing["aborted"] = True
                    self._firing["abort_reason"] = reason
                    self._record_flush()
                    return self._firing
        if tripwire is not None:
            try:
                tripwire(self)
            except Exception as exc:  # same containment a run gets: a multi-hour round never dies on one traceback
                self._firing["tripwire_error"] = repr(exc)
                self._record_flush()
        corpus_names = sorted(n for n in selected if n != "canary")
        per_case: dict[str, list[str | None]] = {n: [] for n in corpus_names}
        total = excluded_n = 0
        for rep in range(1, self.repeats + 1):
            for name in corpus_names:
                verdict = self._contained(name, rep, selected[name].prompt)
                self._record(name, rep, self._label(name, rep), verdict)
                streak.append(verdict)
                per_case[name].append(verdict)
                total += 1
                excluded_n += _is_instrument(verdict)
                if len(per_case[name]) >= 2 and _is_instrument(per_case[name][-1]) and _is_instrument(per_case[name][-2]):
                    flags = self._firing["case_flags"].setdefault(name, [])
                    if "instrument_error on two consecutive repeats" not in flags:
                        flags.append("instrument_error on two consecutive repeats")
                if total >= 10 and excluded_n / total > 0.15:
                    self._firing["case_flags"]["_round"] = ["exclusions above 15%"]
                reason = should_abort(streak)
                if reason:
                    self._firing["aborted"] = True
                    self._firing["abort_reason"] = reason
                    self._record_flush()
                    return self._firing
        self._record_flush()
        return self._firing

    def _record_flush(self) -> None:
        self.round_dir.mkdir(parents=True, exist_ok=True)
        (self.round_dir / "firing.json").write_text(json.dumps(self._firing, indent=2))

    def _safe_request(self, method: str, path: str, **kw: Any) -> HttpResponse | None:
        """The step/error seam without a run to attach to: a timeout or transport failure is None, never a raise."""
        try:
            return self.client.request(method, path, **kw)
        except (HttpTimeout, HttpTransportError):
            return None

    def cleanup(self) -> list[str]:
        """Delete this round's sessions whose capture is complete — corpus runs (``<case>/<n>``) AND the
        tripwire/probe runs (``_tripwire/<fixture>/1``, ``_probe/<fixture>/<arm>``), which carry three path
        segments after the prefix. Paginates: the route defaults to 50 and caps at 200, so an unpaginated read
        left most of a 95-run round's sessions behind."""
        deleted: list[str] = []
        prefix = f"battery/{self.round}/"
        offset = 0
        for _ in range(MAX_PAGES):
            r = self._safe_request("GET", "/api/sessions", params={"limit": SESSION_PAGE, "offset": offset}, timeout=60)
            if r is None or r.status_code != 200 or not isinstance(r.body, list):
                break  # a non-200 body is a dict: iterating it would yield strings, not sessions
            for s in r.body:
                if not isinstance(s, Mapping) or not s.get("id"):
                    continue
                title = str(s.get("title") or "")
                if not title.startswith(prefix):
                    continue
                rest = title[len(prefix) :].split("/")
                if len(rest) not in (2, 3) or any(part in ("", ".", "..") for part in rest):
                    continue  # a title is server-supplied text: never let it address a directory of its choosing
                if not run_dir_is_complete(self.round_dir.joinpath(*rest)):
                    continue
                d = self._safe_request("DELETE", f"/api/sessions/{s['id']}", timeout=30)
                if d is not None and d.status_code in (200, 204):
                    deleted.append(str(s["id"]))
            if len(r.body) < SESSION_PAGE:
                break
            offset += SESSION_PAGE
        return deleted


# ── CLI ────────────────────────────────────────────────────────────────────


def _load_credentials(state_dir: Path) -> tuple[str, str]:
    """Raises ``ValueError`` for any credentials-file config problem (bad mode, unparseable, missing key) —
    a config/usage failure (exit 64), never the bare ``SystemExit``/``KeyError`` that would otherwise land
    on the exit-1 "aborted by the instrument rules" code."""
    p = state_dir / "credentials.json"
    if p.exists():
        if p.stat().st_mode & 0o077:
            raise ValueError(f"{p}: must be mode 600")
        try:
            doc = json.loads(p.read_text())
            return str(doc["username"]), str(doc["password"])
        except (json.JSONDecodeError, KeyError) as exc:
            raise ValueError(f"{p}: {exc}") from exc
    user = os.environ.get("ELSPETH_EVAL_USER", "battery_local")  # sibling-harness names (evals/lib/common.sh)
    pw = os.environ.get("ELSPETH_EVAL_PASS") or getpass.getpass(f"password for {user}: ")
    return user, pw


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base", default=os.environ.get("ELSPETH_EVAL_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--round", required=True)
    ap.add_argument("--repeats", type=int, default=5)
    ap.add_argument("--cases", default=None, help="comma-separated case names; omit for all")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--cleanup", action="store_true", help="after firing, delete this round's sessions whose capture is complete")
    ap.add_argument("--cleanup-only", action="store_true", help="do not fire; only run the cleanup for --round")
    ap.add_argument("--probe", action="store_true", help="run the §7 paired planner probe (calibration only)")
    ap.add_argument("--no-tripwire", action="store_true")
    ap.add_argument("--env-file", default=str(REPO / "deploy/elspeth-web.env"))
    ap.add_argument("--state-dir", default=str(Path.home() / ".elspeth-battery"))
    ap.add_argument("--runs-dir", default=str(REPO / "evals/composer-battery/runs"))
    ns = ap.parse_args(argv)

    from evals.lib.battery_planner import ProbeUnpaired
    from planner_probe import run_probe, run_tripwire, tripwire_preflight  # local import: same directory

    version, cases = load_corpus()
    try:
        user, pw = _load_credentials(Path(ns.state_dir))
        env_budgets = read_env_budgets(Path(ns.env_file))
    except (OSError, ValueError) as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 64
    only = set(ns.cases.split(",")) if ns.cases else None
    if only is not None:
        # before the network: a typo'd case name used to fire nothing and exit 0 — a silently empty round
        unknown = sorted(only - set(cases))
        if unknown:
            print(f"config: unknown --cases: {', '.join(unknown)}", file=sys.stderr)
            return 64
    battery = Battery(
        RequestsClient(ns.base),
        base=ns.base,
        round_name=ns.round,
        runs_dir=Path(ns.runs_dir),
        corpus_version=version,
        env_budgets=env_budgets,
        repeats=ns.repeats,
        resume=ns.resume,
    )
    try:
        battery.login(user, pw)
        status = battery.system_status()
    except BatteryAuthError as exc:
        print(f"auth: {exc}", file=sys.stderr)
        return 70
    except BatteryIdentityError as exc:
        print(f"identity: {exc}", file=sys.stderr)
        return 64
    print(json.dumps({k: status.get(k) for k in ("composer_model", "composer_timeout_seconds", "frontend_build")}), file=sys.stderr)
    if ns.cleanup_only:
        print(f"cleanup deleted {len(battery.cleanup())} sessions", file=sys.stderr)
        return 0
    if ns.probe:
        try:
            run_probe(battery)
        except ProbeUnpaired as exc:
            print(f"probe pairing: {exc}", file=sys.stderr)
            return 64
        return 0
    try:
        doc = battery.fire(
            cases,
            tripwire=None if ns.no_tripwire else run_tripwire,
            preflight=None if ns.no_tripwire else tripwire_preflight,
            only=only,
        )
    except ProbeUnpaired as exc:
        print(f"tripwire preflight: {exc}", file=sys.stderr)
        return 64
    if ns.cleanup:
        print(f"cleanup deleted {len(battery.cleanup())} sessions", file=sys.stderr)
    print(
        json.dumps(
            {
                "aborted": doc["aborted"],
                "abort_reason": doc["abort_reason"],
                "tripwire_error": doc["tripwire_error"],
                "completed": len(doc["completed"]),
                "case_flags": doc["case_flags"],
            }
        )
    )
    return 1 if doc["aborted"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
