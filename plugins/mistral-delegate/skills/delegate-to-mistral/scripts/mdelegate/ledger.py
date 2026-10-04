"""Append-only record of delegations: ~/.mistral-delegate/ledger.jsonl.

Each line is one event: "start" when a run begins, "end" when it finishes, and
"outcome" when Claude adopts or discards its result. Runs are rebuilt by folding
the events in order. Full reports are kept in ~/.mistral-delegate/runs/<id>.txt.
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict

from . import config


def ledger_path():
    return config.home() / "ledger.jsonl"


def runs_dir():
    return config.home() / "runs"


def append(event: dict) -> None:
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    event = {"time": time.time(), **event}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event) + "\n")


def load_runs() -> dict[str, dict]:
    runs: dict[str, dict] = {}
    try:
        lines = ledger_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return runs
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        run_id = event.get("id")
        if not run_id:
            continue
        kind = event.get("event")
        if kind == "start":
            runs[run_id] = {**event, "started": event.get("time")}
        elif run_id in runs and kind == "end":
            runs[run_id].update({k: v for k, v in event.items() if k not in ("event", "time")})
            runs[run_id]["finished"] = event.get("time")
        elif run_id in runs and kind == "outcome":
            runs[run_id]["outcome"] = event.get("outcome")
            runs[run_id]["outcome_note"] = event.get("note")
    return runs


def pid_alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def state(run: dict) -> str:
    if run.get("status"):
        return run["status"]
    return "running" if pid_alive(run.get("pid")) else "died"


def running(runs: dict[str, dict]) -> list[dict]:
    return [r for r in runs.values() if state(r) == "running"]


def save_report(run_id: str, text: str) -> None:
    d = runs_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.txt").write_text(text, encoding="utf-8")


def read_report(run_id: str) -> str | None:
    try:
        return (runs_dir() / f"{run_id}.txt").read_text(encoding="utf-8")
    except OSError:
        return None


def _age(seconds: float) -> str:
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    if seconds < 172800:
        return f"{seconds / 3600:.0f}h"
    return f"{seconds / 86400:.0f}d"


def pending_review(runs: dict[str, dict]) -> list[dict]:
    """Finished write runs in a worktree that still await adopt or discard."""
    return [r for r in runs.values()
            if r.get("worktree") and r.get("status") and not r.get("outcome")
            and r.get("files_changed") and os.path.isdir(r["worktree"].get("path", ""))]


def format_status(runs: dict[str, dict], limit: int = 15) -> str:
    if not runs:
        return "No delegations recorded yet."
    now = time.time()
    rows = sorted(runs.values(), key=lambda r: r.get("started") or 0, reverse=True)[:limit]
    pending = {r["id"] for r in pending_review(runs)}
    lines = ["id                 state          verify   cost     outcome    age   kind/mode         task"]
    for r in rows:
        st = state(r)
        cost = f"${r['cost']:.3f}" if isinstance(r.get("cost"), (int, float)) else "-"
        task = " ".join((r.get("task") or "").split())[:60]
        lines.append(
            f"{r['id']:<18} {st:<14} {r.get('verification') or '-':<8} {cost:<8} "
            f"{r.get('outcome') or ('pending' if r['id'] in pending else '-'):<10} "
            f"{_age(now - (r.get('started') or now)):<5} {r.get('kind', '?') + '/' + r.get('mode', '?'):<17} {task}")
    active = running(runs)
    if active:
        lines.append(f"\n{len(active)} running. Results: delegate.py --result <id>")
    return "\n".join(lines)


def compute_stats(runs: dict[str, dict], days: int = 90) -> dict:
    cutoff = time.time() - days * 86400
    by_kind: dict[str, dict] = defaultdict(lambda: {"runs": 0, "ok": 0, "verified": 0, "passed": 0,
                                                    "adopted": 0, "discarded": 0, "cost": 0.0})
    for r in runs.values():
        if (r.get("started") or 0) < cutoff or not r.get("status"):
            continue
        s = by_kind[r.get("kind") or "other"]
        s["runs"] += 1
        s["ok"] += r["status"] == "ok"
        if r.get("verification") in ("passed", "failed"):
            s["verified"] += 1
            s["passed"] += r["verification"] == "passed"
        if r.get("outcome") in ("adopted", "adopted_partial"):
            s["adopted"] += 1
        elif r.get("outcome") == "discarded":
            s["discarded"] += 1
        if isinstance(r.get("cost"), (int, float)):
            s["cost"] += r["cost"]
    return dict(by_kind)


def format_stats(runs: dict[str, dict], days: int = 90) -> str:
    stats = compute_stats(runs, days)
    if not stats:
        return f"No finished delegations in the last {days} days."
    lines = [f"Delegations in the last {days} days:",
             "kind          runs  ok   verify-pass  adopted/decided  avg-cost  total-cost"]
    total_runs, total_cost = 0, 0.0
    for kind, s in sorted(stats.items(), key=lambda kv: -kv[1]["runs"]):
        decided = s["adopted"] + s["discarded"]
        verify = f"{s['passed']}/{s['verified']}" if s["verified"] else "-"
        adopted = f"{s['adopted']}/{decided}" if decided else "-"
        lines.append(f"{kind:<13} {s['runs']:<5} {s['ok']:<4} {verify:<12} {adopted:<16} "
                     f"${s['cost'] / s['runs']:<8.3f} ${s['cost']:.2f}")
        total_runs += s["runs"]
        total_cost += s["cost"]
    lines.append(f"total: {total_runs} runs, ${total_cost:.2f}")
    return "\n".join(lines)


def compact_stats(runs: dict[str, dict], days: int = 90) -> str:
    stats = compute_stats(runs, days)
    parts = []
    for kind, s in sorted(stats.items(), key=lambda kv: -kv[1]["runs"]):
        decided = s["adopted"] + s["discarded"]
        bit = f"{kind}: {s['runs']} runs"
        if decided:
            bit += f", {s['adopted']}/{decided} adopted"
        if s["verified"]:
            bit += f", {s['passed']}/{s['verified']} passed checks"
        bit += f", ${s['cost'] / s['runs']:.2f} avg"
        parts.append(bit)
    return "; ".join(parts)
