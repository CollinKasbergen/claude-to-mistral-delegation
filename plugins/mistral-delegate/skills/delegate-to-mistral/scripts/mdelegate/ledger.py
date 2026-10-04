"""Append-only record of delegations: ~/.mistral-delegate/ledger.jsonl.

Each line is one event: "start" when a run begins, "end" when it finishes, and
"outcome" when Claude adopts or discards its result. Runs are rebuilt by folding
the events in order. Full reports are kept in ~/.mistral-delegate/runs/<id>.txt.
"""

from __future__ import annotations

import calendar
import contextlib
import json
import os
import subprocess
import time
from collections import Counter, defaultdict

from . import config


def ledger_path():
    return config.home() / "ledger.jsonl"


def runs_dir():
    return config.home() / "runs"


def append(event: dict) -> None:
    """Append one event as a single write, so parallel runs never interleave their lines."""
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"time": time.time(), **event}) + "\n"
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        # A line cut short by a killed writer would swallow this event: start on a fresh line.
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = "\n" + line
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


@contextlib.contextmanager
def locked():
    """Hold the ledger's lock (between checking how many runs are going and recording a new one)."""
    path = config.home() / "ledger.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        try:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass
        yield


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
        if not isinstance(event, dict):
            continue
        run_id = event.get("id")
        if not run_id or not isinstance(run_id, str):
            continue
        if not isinstance(event.get("time"), (int, float)):
            event["time"] = None
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


_START_TIMES: dict = {}


def process_started(pid) -> str | None:
    """When a process started (as ps prints it), to tell a run's process from a later one with the same pid."""
    if pid not in _START_TIMES:
        try:
            out = subprocess.run(["ps", "-o", "lstart=", "-p", str(int(pid))], capture_output=True, text=True,
                                 timeout=5)
            _START_TIMES[pid] = out.stdout.strip() or None
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            _START_TIMES[pid] = None
    return _START_TIMES[pid]


def state(run: dict) -> str:
    if run.get("status"):
        return str(run["status"])
    if not pid_alive(run.get("pid")):
        return "died"
    recorded = run.get("pid_started")
    if recorded and process_started(run.get("pid")) not in (None, recorded):
        return "died"  # the pid now belongs to another process (after a reboot, or in a container)
    return "running"


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


def _when(ts: float | None, now: float) -> str:
    if not ts:
        return "?"
    t = time.localtime(ts)
    if time.strftime("%Y-%m-%d", t) == time.strftime("%Y-%m-%d", time.localtime(now)):
        return time.strftime("today %H:%M", t)
    return time.strftime("%b %d %H:%M", t)


def _took(r: dict, now: float) -> str:
    start = r.get("started") or now
    end = r.get("finished") or (now if state(r) == "running" else start)
    return _age(max(0.0, end - start))


def format_status(runs: dict[str, dict], limit: int = 15, currency: str = "$") -> str:
    if not runs:
        return "No delegations recorded yet."
    now = time.time()
    pending = {r["id"] for r in pending_review(runs)}
    rows = sorted(runs.values(), key=lambda r: r.get("started") or 0, reverse=True)[:limit]
    lines = ["id                 state              verify     cost      outcome    started       took   kind/mode         task"]
    for r in rows:
        st = state(r)
        cost = r.get("cost")
        cost = f"{currency}{cost:.3f}" if isinstance(cost, (int, float)) and (cost > 0 or r.get("tokens")) else "-"
        task = " ".join(str(r.get("task") or "").split())[:50]
        outcome = OUTCOME_LABELS.get(r.get("outcome"), r.get("outcome")) or ("pending" if r["id"] in pending else "-")
        lines.append(
            f"{r['id']:<18} {st:<18} {str(r.get('verification') or '-')[:10]:<10} {cost:<9} {str(outcome):<10} "
            f"{_when(r.get('started'), now):<13} {_took(r, now):<6} "
            f"{str(r.get('kind') or '?') + '/' + str(r.get('mode') or '?'):<17} {task}")
    active = running(runs)
    lines.append("\nstarted: local time the run began; took: how long it ran (so far, if running); "
                 "outcome 'pending': finished, waiting for --adopt or --discard.")
    if active:
        lines.append(f"{len(active)} running. Results: delegate.py --result <id>")
    return "\n".join(lines)


OUTCOME_LABELS = {"adopted_partial": "partial"}


def run_cost(r: dict, prices: dict | None = None) -> float | None:
    """A run's cost: recorded, or priced now from its tokens when the model's price is known."""
    cost = r.get("cost")
    if isinstance(cost, (int, float)) and (cost > 0 or r.get("tokens")):
        return float(cost)
    price = (prices or {}).get(r.get("model") or "")
    if price and (r.get("tokens_in") or r.get("tokens_out")):
        tokens_in, cached = r.get("tokens_in") or 0, min(r.get("cached") or 0, r.get("tokens_in") or 0)
        cached_price = price[2] if len(price) > 2 and price[2] is not None else price[0] * 0.1
        return ((tokens_in - cached) * price[0] + cached * cached_price + (r.get("tokens_out") or 0) * price[1]) / 1e6
    return None


def month_start(reset_day: int, now: float | None = None) -> time.struct_time:
    """The credit's last reset. A reset day past the end of a month falls on that month's last day."""
    t = time.localtime(now or time.time())
    year, month = t.tm_year, t.tm_mon
    if t.tm_mday < min(reset_day, calendar.monthrange(year, month)[1]):
        year, month = (year - 1, 12) if month == 1 else (year, month - 1)
    day = min(reset_day, calendar.monthrange(year, month)[1])
    return time.strptime(f"{year}-{month:02d}-{day:02d}", "%Y-%m-%d")


def month_spend(runs: dict[str, dict], reset_day: int = 1, prices: dict | None = None) -> tuple[float, int, str]:
    """(spent, runs without a price, 'Mon DD') since the credit's last reset."""
    start = month_start(reset_day)
    cutoff = time.mktime(start)
    spent, unpriced = 0.0, 0
    for r in runs.values():
        if (r.get("started") or 0) < cutoff or not r.get("status"):
            continue
        cost = run_cost(r, prices)
        if cost is None:
            unpriced += 1 if (r.get("tokens_in") or r.get("tokens")) else 0
        else:
            spent += cost
    return spent, unpriced, time.strftime("%b %d", start)


def savings(s: dict) -> float | None:
    """Claude-equivalent work of adopted runs per Claude token spent delegating (all decided runs)."""
    return s["saved"] / s["overhead"] if s["overhead"] else None


def compute_stats(runs: dict[str, dict], days: int = 90, prices: dict | None = None) -> dict:
    cutoff = time.time() - days * 86400
    by_kind: dict[str, dict] = defaultdict(lambda: {"runs": 0, "ok": 0, "verified": 0, "passed": 0,
                                                    "adopted": 0, "partial": 0, "discarded": 0, "cost": 0.0,
                                                    "costed": 0, "effective": 0, "saved": 0, "overhead": 0})
    for r in runs.values():
        if (r.get("started") or 0) < cutoff or not r.get("status"):
            continue
        s = by_kind[str(r.get("kind") or "other")]
        s["runs"] += 1
        s["ok"] += r["status"] == "ok"
        if r.get("verification") in ("passed", "passed_except_preexisting", "failed"):
            s["verified"] += 1
            s["passed"] += r["verification"] != "failed"
        if r.get("outcome") == "adopted":
            s["adopted"] += 1
        elif r.get("outcome") == "adopted_partial":
            s["partial"] += 1
        elif r.get("outcome") == "discarded":
            s["discarded"] += 1
        # Runs without cost data (cost missing, or $0 with no tokens recorded) stay out of the averages.
        cost = run_cost(r, prices)
        if cost is not None:
            s["cost"] += cost
            s["costed"] += 1
        s["effective"] += r.get("effective") if isinstance(r.get("effective"), int) else 0
        # Savings count decided runs only: adopted work saves Claude its equivalent (half for a partial
        # adopt); discarded work saves nothing, but its overhead still counts.
        if r.get("outcome") and isinstance(r.get("claude_overhead"), (int, float)):
            s["overhead"] += r["claude_overhead"]
            share = {"adopted": 1.0, "adopted_partial": 0.5}.get(r["outcome"], 0.0)
            s["saved"] += int((r.get("claude_equivalent") or 0) * share)
    return dict(by_kind)


def _adopted(s: dict) -> str:
    decided = s["adopted"] + s["partial"] + s["discarded"]
    if not decided:
        return "-"
    text = f"{s['adopted']}/{decided}"
    return text + (f" (+{s['partial']} partial)" if s["partial"] else "")


def top_denied(runs: dict[str, dict], days: int = 90, limit: int = 5) -> list[tuple[str, int]]:
    cutoff = time.time() - days * 86400
    counts: Counter = Counter()
    for r in runs.values():
        if (r.get("started") or 0) >= cutoff:
            counts.update(d for d in (r.get("denied") or []) if isinstance(d, str))
    return counts.most_common(limit)


def format_stats(runs: dict[str, dict], days: int = 90, prices: dict | None = None, currency: str = "$",
                 min_savings: float | None = None) -> str:
    stats = compute_stats(runs, days, prices)
    if not stats:
        return f"No finished delegations in the last {days} days."
    lines = [f"Delegations in the last {days} days:",
             "kind          runs  ok   verify-pass  adopted/decided       avg-eff-tokens  avg-cost   savings"]
    total_runs, total_cost = 0, 0.0
    for kind, s in sorted(stats.items(), key=lambda kv: -kv[1]["runs"]):
        verify = f"{s['passed']}/{s['verified']}" if s["verified"] else "-"
        ratio = savings(s)
        flag = "  (below min_savings)" if min_savings and ratio is not None and ratio < min_savings else ""
        avg_cost = f"{currency}{s['cost'] / s['costed']:.3f}" if s["costed"] else "?"
        lines.append(f"{kind:<13} {s['runs']:<5} {s['ok']:<4} {verify:<12} {_adopted(s):<21} "
                     f"{s['effective'] // max(s['runs'], 1):<15,} {avg_cost:<10} "
                     + (f"x{ratio:.1f}" if ratio is not None else "-") + flag)
        total_runs += s["runs"]
        total_cost += s["cost"]
    uncosted = sum(s["runs"] - s["costed"] for s in stats.values())
    lines.append(f"total: {total_runs} runs, ~{currency}{total_cost:.2f}"
                 + (f" ({uncosted} run(s) without a price are left out of costs)" if uncosted else ""))
    lines.append("savings: Claude-equivalent work of adopted runs per token Claude spent delegating "
                 "(writing specs, reading reports); x1 means delegating saved nothing.")
    denied = top_denied(runs, days)
    if denied:
        lines.append("most denied commands (add to allow_commands if Mistral needs them):")
        lines += [f"  {n}x  {cmd}" for cmd, n in denied]
    return "\n".join(lines)


def compact_stats(runs: dict[str, dict], days: int = 90, prices: dict | None = None, currency: str = "$") -> str:
    stats = compute_stats(runs, days, prices)
    parts = []
    for kind, s in sorted(stats.items(), key=lambda kv: -kv[1]["runs"]):
        bit = f"{kind}: {s['runs']} runs"
        if s["adopted"] + s["partial"] + s["discarded"]:
            bit += f", {_adopted(s)} adopted"
        if s["verified"]:
            bit += f", {s['passed']}/{s['verified']} passed checks"
        if (ratio := savings(s)) is not None:
            bit += f", savings x{ratio:.1f}"
        if s["costed"]:
            bit += f", ~{currency}{s['cost'] / s['costed']:.2f} avg"
        parts.append(bit)
    return "; ".join(parts)


def low_savings_kinds(runs: dict[str, dict], min_savings: float, days: int = 90, min_decided: int = 3) -> list[str]:
    """Kinds whose measured savings are below min_savings, once enough runs were adopted or discarded."""
    out = []
    for kind, s in compute_stats(runs, days).items():
        ratio = savings(s)
        if ratio is not None and s["adopted"] + s["partial"] + s["discarded"] >= min_decided and ratio < min_savings:
            out.append(f"{kind} (x{ratio:.1f})")
    return out
