#!/usr/bin/env python3
"""Run one task through Mistral's Vibe CLI in programmatic mode and print a compact report.

The report is meant to be read by Claude Code: the final answer from Vibe, what
the run cost, the session id (for follow-ups), any tool calls that did not
complete, and, in write mode, the worktree and the changes Vibe made. The full
message history stays out of Claude's context.

Usage:
  delegate.py --mode read  "Find every place that parses config.toml"
  delegate.py --mode write "Add unit tests for utils/slugify.py"
  delegate.py --mode write --worktree-name mistral-ab12cd34 --resume <session-id> "Also cover empty strings"
  echo "long prompt" | delegate.py --mode read -

Write mode creates its own git worktree from HEAD, copies your uncommitted and
untracked files into it (so Vibe sees work you haven't committed), commits that
copy as a snapshot inside the worktree, and symlinks dependency folders such as
node_modules and .venv from your checkout. Vibe's changes are then reported
relative to the snapshot, with a command to apply them to your checkout.

Vibe behaviour this script relies on (checked against mistral-vibe 2.25.x):
  * `--output json` prints a JSON list of history entries (camelCase keys).
  * In programmatic mode, any tool call that needs approval is denied, not
    prompted. `--auto-approve` lifts that.
  * Hitting --max-turns / --max-price / --max-tokens exits 1 with the last
    assistant text on stderr and nothing on stdout.
  * Each session writes $VIBE_HOME/logs/session/<prefix>_<time>_<id[:8]>/meta.json
    whose "stats" include session_cost, steps and token counts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

READ_ONLY_TOOLS = ["read_file", "grep", "todo"]

# Ignored directories with these names are symlinked from the checkout into the worktree.
DEPENDENCY_DIRS = {"node_modules", ".venv", "venv", "vendor", "bower_components"}

STATE_FILE = "mistral-delegate.json"

DEFAULTS = {
    "read": {"max_turns": 15, "max_price": 0.25},
    "write": {"max_turns": 30, "max_price": 1.00},
}

MAX_RESULT_CHARS = 12_000

GIT_IDENTITY = ["-c", "user.name=mistral-delegate", "-c", "user.email=mistral-delegate@localhost",
                "-c", "commit.gpgsign=false"]


class DelegateError(Exception):
    pass


def env_float(name: str, fallback: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return fallback


def env_int(name: str, fallback: int) -> int:
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return fallback


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("task", help="Task for Vibe. Use '-' to read it from stdin.")
    p.add_argument("--mode", choices=["read", "write"], default="read",
                   help="read: plan agent, read-only tools, runs in place. "
                        "write: accept-edits agent, runs in an isolated git worktree.")
    p.add_argument("--workdir", default=os.getcwd(), help="Project directory (default: cwd).")
    p.add_argument("--max-turns", type=int)
    p.add_argument("--max-price", type=float, help="Dollar cap for this run.")
    p.add_argument("--max-tokens", type=int)
    p.add_argument("--worktree-name",
                   help="Worktree/branch name in write mode (default: mistral-<id>). "
                        "An existing worktree with this name is reused, e.g. for follow-ups.")
    p.add_argument("--in-place", action="store_true",
                   help="Write mode only: edit the working tree directly instead of a worktree.")
    p.add_argument("--no-snapshot", action="store_true",
                   help="Write mode: start the worktree from HEAD only, without your uncommitted changes.")
    p.add_argument("--no-link-deps", action="store_true",
                   help="Write mode: don't symlink node_modules, .venv etc. into the worktree.")
    p.add_argument("--link", action="append", default=[], metavar="PATH",
                   help="Write mode: also symlink this path (relative to the repo root, e.g. .env) "
                        "into the worktree. Repeatable.")
    p.add_argument("--allow-shell", action="store_true",
                   help="Pass --auto-approve so Vibe may run shell commands. Off by default.")
    p.add_argument("--trust", action="store_true",
                   help="Load the project's .vibe/ config and AGENTS.md for this run "
                        "(always on for write-mode worktrees).")
    p.add_argument("--resume", metavar="SESSION_ID", help="Continue an earlier Vibe session.")
    p.add_argument("--agent", help="Override the Vibe agent profile.")
    p.add_argument("--timeout", type=int, default=env_int("MISTRAL_DELEGATE_TIMEOUT", 900),
                   help="Seconds before the run is killed (default 900).")
    p.add_argument("--vibe-bin", default=os.environ.get("VIBE_BIN", "vibe"))
    args = p.parse_args(argv)

    if args.task == "-":
        args.task = sys.stdin.read()
    if not args.task.strip():
        p.error("task is empty")
    if args.in_place and args.mode != "write":
        p.error("--in-place only applies to --mode write")

    d = DEFAULTS[args.mode]
    if args.max_turns is None:
        args.max_turns = env_int("MISTRAL_DELEGATE_MAX_TURNS", d["max_turns"])
    if args.max_price is None:
        args.max_price = env_float("MISTRAL_DELEGATE_MAX_PRICE", d["max_price"])
    if args.max_tokens is None and "MISTRAL_DELEGATE_MAX_TOKENS" in os.environ:
        args.max_tokens = env_int("MISTRAL_DELEGATE_MAX_TOKENS", 0) or None
    return args


def build_command(args: argparse.Namespace, in_worktree: bool) -> list[str]:
    cmd = [
        args.vibe_bin,
        "--prompt", args.task,
        "--output", "json",
        "--max-turns", str(args.max_turns),
        "--max-price", str(args.max_price),
    ]
    if args.max_tokens:
        cmd += ["--max-tokens", str(args.max_tokens)]

    if args.mode == "read":
        cmd += ["--agent", args.agent or "plan"]
        for tool in READ_ONLY_TOOLS:
            cmd += ["--enabled-tools", tool]
    else:
        cmd += ["--agent", args.agent or "accept-edits"]
        if args.allow_shell:
            cmd += ["--auto-approve"]

    if args.trust or in_worktree:
        cmd += ["--trust"]
    if args.resume:
        cmd += ["--resume", args.resume]
    return cmd


# --- git helpers -------------------------------------------------------------

def git(cwd: str | Path, *argv: str) -> str:
    """Run git and return stdout, or "" on any failure."""
    try:
        out = subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def git_checked(cwd: str | Path, *argv: str, input: bytes | None = None) -> bytes:
    """Run git and return stdout bytes, raising DelegateError on failure."""
    try:
        out = subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, input=input, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise DelegateError(f"git {' '.join(argv[:2])} failed: {e}") from e
    if out.returncode != 0:
        msg = out.stderr.decode(errors="replace").strip()
        raise DelegateError(f"git {' '.join(argv[:2])} failed: {msg}")
    return out.stdout


def find_worktree(repo: str | Path, branch: str) -> str | None:
    path = None
    for line in git(repo, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def worktree_root(toplevel: str) -> Path:
    custom = os.environ.get("MISTRAL_DELEGATE_WORKTREES")
    base = Path(custom).expanduser() if custom else Path.home() / ".mistral-delegate" / "worktrees"
    digest = hashlib.sha1(toplevel.encode()).hexdigest()[:8]
    return base / f"{Path(toplevel).name}-{digest}"


def state_path(worktree: str | Path) -> Path:
    return Path(git(worktree, "rev-parse", "--absolute-git-dir").strip()) / STATE_FILE


def load_state(worktree: str | Path) -> dict:
    try:
        return json.loads(state_path(worktree).read_text())
    except (OSError, ValueError):
        return {}


def exclude_pathspecs(links: list[str]) -> list[str]:
    return [f":(top,exclude){link}" for link in links]


# --- worktree preparation ----------------------------------------------------

def snapshot_uncommitted(toplevel: str, worktree: Path) -> dict | None:
    """Copy tracked changes and untracked files from the checkout and commit them in the worktree."""
    diff = git_checked(toplevel, "diff", "HEAD", "--binary")
    untracked = [f for f in git_checked(toplevel, "ls-files", "--others", "--exclude-standard", "-z")
                 .decode().split("\0") if f]
    if not diff.strip() and not untracked:
        return None
    if diff.strip():
        git_checked(worktree, "apply", "--binary", "--whitespace=nowarn", "-", input=diff)
    for rel in untracked:
        src, dst = Path(toplevel, rel), worktree / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            shutil.copy2(src, dst)
    git_checked(worktree, "add", "-A")
    git_checked(worktree, *GIT_IDENTITY, "commit", "-q", "--no-verify",
                "-m", "mistral-delegate: snapshot of uncommitted work")
    changed = len([line for line in git(toplevel, "diff", "HEAD", "--name-only").splitlines() if line])
    return {"modified": changed, "untracked": len(untracked)}


def link_dependencies(toplevel: str, worktree: Path, extra: list[str], auto: bool) -> list[str]:
    candidates: list[str] = []
    if auto:
        listing = git(toplevel, "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z")
        for entry in listing.split("\0"):
            entry = entry.rstrip("/")
            if entry and Path(entry).name in DEPENDENCY_DIRS:
                candidates.append(entry)
    candidates += [e.strip("/") for e in extra]

    linked = []
    for rel in dict.fromkeys(candidates):
        src, dst = Path(toplevel, rel), worktree / rel
        if not src.exists() or dst.exists() or dst.is_symlink():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(src, dst, target_is_directory=src.is_dir())
        linked.append(rel)
    return linked


def prepare_worktree(args: argparse.Namespace, toplevel: str) -> dict:
    name = args.worktree_name or f"mistral-{uuid.uuid4().hex[:8]}"
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name):
        raise DelegateError(f"Invalid worktree name: {name!r} (use letters, digits, '.', '_' and '-')")

    existing = find_worktree(toplevel, name)
    if existing:
        state = load_state(existing)
        return {
            "name": name,
            "path": existing,
            "base": state.get("base") or git(existing, "rev-parse", "HEAD").strip(),
            "snapshot": state.get("snapshot"),
            "links": state.get("links", []),
            "reused": True,
        }
    if git(toplevel, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}").strip():
        raise DelegateError(f"Branch {name!r} already exists without a worktree. Pick another --worktree-name.")

    path = worktree_root(toplevel) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    git_checked(toplevel, "worktree", "add", "-q", "-b", name, str(path), "HEAD")

    snapshot = None if args.no_snapshot else snapshot_uncommitted(toplevel, path)
    links = link_dependencies(toplevel, path, args.link, auto=not args.no_link_deps)
    state = {
        "base": git(path, "rev-parse", "HEAD").strip(),
        "snapshot": snapshot,
        "links": links,
    }
    state_path(path).write_text(json.dumps(state))
    return {"name": name, "path": str(path), "reused": False, **state}


# --- Vibe output and session stats -------------------------------------------

def text_of(entry: dict) -> str:
    return "\n\n".join(
        block.get("text", "")
        for block in entry.get("content") or []
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()


def summarize_history(history: list) -> dict:
    session_id = None
    final_text = ""
    tool_calls = 0
    problems: list[str] = []
    notices: list[str] = []

    for entry in history:
        if not isinstance(entry, dict):
            continue
        session_id = session_id or entry.get("sessionId")
        kind = entry.get("type")
        if kind == "message" and entry.get("role") == "assistant":
            text = text_of(entry)
            if text:
                final_text = text
        elif kind == "effect":
            tool_calls += 1
            state = entry.get("state") or {}
            status = state.get("status")
            if status not in ("completed", None):
                reason = state.get("reason") or (state.get("error") or {}).get("message") or ""
                problems.append(f"{entry.get('title', 'tool')}: {status}" + (f" ({reason})" if reason else ""))
            elif status == "completed" and state.get("decision") == "skip":
                problems.append(f"{entry.get('title', 'tool')}: skipped (not approved)")
        elif kind == "notice" and entry.get("level") in ("warning", "error"):
            notices.append(f"{entry.get('level')}: {entry.get('message', '')}")

    return {
        "session_id": session_id,
        "final_text": final_text,
        "tool_calls": tool_calls,
        "problems": problems,
        "notices": notices,
    }


def parse_output(stdout: str) -> list:
    stdout = stdout.strip()
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # Tolerate stray lines before the JSON payload.
        start = stdout.find("[")
        if start == -1:
            return []
        try:
            data = json.loads(stdout[start:])
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = data.get("history", [])
    return data if isinstance(data, list) else []


def session_log_dir() -> Path:
    home = os.environ.get("VIBE_HOME")
    return (Path(home).expanduser() if home else Path.home() / ".vibe") / "logs" / "session"


def read_session_stats(session_id: str | None, cwd: str, since: float | None = None) -> dict | None:
    """Find a session's stats in Vibe's session log.

    With a session id, match it exactly. Without one (e.g. when a limit stopped the
    run and nothing was printed), take the newest session written since `since`
    for the same working directory.
    """
    log_dir = session_log_dir()
    if not log_dir.is_dir():
        return None
    if session_id:
        metas = list(log_dir.glob(f"*_{session_id[:8]}/meta.json"))
    else:
        dirs = sorted((d for d in log_dir.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime)[-50:]
        metas = [d / "meta.json" for d in dirs]
    for meta_path in sorted(metas, key=lambda m: m.stat().st_mtime if m.exists() else 0, reverse=True):
        try:
            if since is not None and meta_path.stat().st_mtime < since - 1:
                continue
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        if session_id and meta.get("session_id") != session_id:
            continue
        workdir = (meta.get("environment") or {}).get("working_directory")
        if not session_id and workdir and Path(workdir).resolve() != Path(cwd).resolve():
            continue
        stats = meta.get("stats")
        if isinstance(stats, dict):
            return {"session_id": meta.get("session_id"), **stats}
    return None


def stats_cost(stats: dict) -> float:
    if "session_cost" in stats:
        return float(stats["session_cost"])
    prompt = stats.get("session_prompt_tokens", 0)
    cached = min(stats.get("session_cached_tokens", 0), prompt)
    in_price = stats.get("input_price_per_million", 0.0)
    cached_price = stats.get("cached_input_price_per_million")
    cached_price = in_price if cached_price is None else cached_price
    out = stats.get("session_completion_tokens", 0) * stats.get("output_price_per_million", 0.0)
    return ((prompt - cached) * in_price + cached * cached_price + out) / 1_000_000


def usage_line(after: dict | None, before: dict | None, max_price: float) -> str:
    if not after:
        return f"usage: cost unknown (Vibe session log not found), cap ${max_price:.2f}"
    before = before or {}
    cost = stats_cost(after) - (stats_cost(before) if before else 0.0)
    steps = after.get("steps", 0) - before.get("steps", 0)
    tokens = (after.get("session_prompt_tokens", 0) + after.get("session_completion_tokens", 0)
              - before.get("session_prompt_tokens", 0) - before.get("session_completion_tokens", 0))
    line = f"usage: cost ${cost:.4f} of ${max_price:.2f} cap, {steps} steps, {tokens:,} tokens"
    if before:
        line += f" (this run; session total ${stats_cost(after):.4f})"
    return line


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} chars ...]"


# --- report ------------------------------------------------------------------

def vibe_changes(wt: dict) -> str:
    """Stage everything Vibe did (except our symlinks) and return the diffstat against the base."""
    git(wt["path"], "add", "-A", "--", ".", *exclude_pathspecs(wt.get("links", [])))
    return git(wt["path"], "diff", "--cached", "--stat", wt["base"]).rstrip()


def remove_worktree(wt: dict, toplevel: str) -> None:
    git(toplevel, "worktree", "remove", "--force", wt["path"])
    git(toplevel, "branch", "-D", wt["name"])


def worktree_report(wt: dict, toplevel: str) -> list[str]:
    path, base = wt["path"], wt["base"]
    q = shlex.quote
    lines = [f"worktree_name: {wt['name']}" + ("  (reused)" if wt["reused"] else ""),
             f"worktree_path: {path}"]
    snap = wt.get("snapshot")
    if snap:
        lines.append(f"worktree_base: snapshot of your uncommitted work ({snap['modified']} modified, "
                     f"{snap['untracked']} untracked files) at {base[:12]}")
    else:
        lines.append(f"worktree_base: your HEAD at {base[:12]}")
    if wt.get("links"):
        lines.append("linked_from_checkout (symlinks, shared with your checkout): " + ", ".join(wt["links"]))

    stat = vibe_changes(wt)
    lines.append("changes_by_vibe:\n" + (stat or "  (none)"))
    if stat:
        lines.append(f"review_with: git -C {q(path)} diff --cached {base[:12]}")
        lines.append(f"apply_to_checkout_with: git -C {q(path)} diff --cached --binary {base[:12]} "
                     f"| git -C {q(toplevel)} apply")
    lines.append(f"cleanup_with: git -C {q(toplevel)} worktree remove --force {q(path)} "
                 f"&& git -C {q(toplevel)} branch -D {wt['name']}")
    return lines


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    workdir = str(Path(args.workdir).resolve())

    if shutil.which(args.vibe_bin) is None and not Path(args.vibe_bin).is_file():
        print("status: error\n\nVibe CLI not found. Install it with `uv tool install mistral-vibe` "
              "(or `pip install mistral-vibe`), then run `vibe --setup` once to store your API key.")
        return 2

    run_dir = workdir
    wt = None
    toplevel = ""
    if args.mode == "write" and not args.in_place:
        toplevel = git(workdir, "rev-parse", "--show-toplevel").strip()
        if not toplevel:
            print("status: error\n\nWrite mode needs a git repository for worktree isolation. "
                  "Pass --in-place to let Vibe edit the directory directly.")
            return 2
        try:
            wt = prepare_worktree(args, toplevel)
        except DelegateError as e:
            print(f"status: error\n\nCould not prepare the worktree: {e}")
            return 2
        run_dir = str(Path(wt["path"]) / Path(workdir).relative_to(toplevel))

    stats_before = read_session_stats(args.resume, run_dir) if args.resume else None
    cmd = build_command(args, in_worktree=wt is not None)
    started_wall = time.time()
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=run_dir, capture_output=True, text=True,
                              timeout=args.timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        lines = ["status: timeout", f"Vibe did not finish within {args.timeout}s and was stopped."]
        if wt:
            lines += worktree_report(wt, toplevel)
        print("\n".join(lines))
        return 1
    elapsed = time.monotonic() - started

    history = parse_output(proc.stdout)
    info = summarize_history(history)
    stderr = proc.stderr.strip()

    if proc.returncode == 0:
        status = "ok"
    elif (proc.returncode == 1 and not proc.stdout.strip() and stderr
          and not re.match(r"(Error|Teleport error):", stderr) and "Traceback" not in stderr):
        # Vibe reports a hit limit by printing the last assistant text to stderr, unprefixed.
        status = "limit_reached"
    else:
        status = "error"

    stats_after = read_session_stats(info["session_id"] or args.resume, run_dir,
                                     since=None if (info["session_id"] or args.resume) else started_wall)
    session_id = info["session_id"] or (stats_after or {}).get("session_id") or args.resume

    lines = [
        f"status: {status}",
        f"mode: {args.mode}",
        usage_line(stats_after, stats_before, args.max_price),
        f"elapsed: {elapsed:.0f}s, tool_calls: {info['tool_calls']}, max_turns: {args.max_turns}",
    ]
    if session_id:
        follow_up = f"--resume {session_id}" + (f" --worktree-name {wt['name']}" if wt else "")
        lines.append(f"session_id: {session_id}  (follow up with: {follow_up})")

    if wt and status == "error" and not wt["reused"] and not vibe_changes(wt):
        remove_worktree(wt, toplevel)
        lines.append(f"worktree: removed {wt['name']} (Vibe failed before changing anything)")
    elif wt:
        lines += worktree_report(wt, toplevel)
    elif args.mode == "write":
        lines.append("changed_files:\n" + (git(workdir, "status", "--porcelain").rstrip() or "  (none)"))

    if info["problems"]:
        lines.append("tool_calls_not_completed:\n" + "\n".join(f"  - {p}" for p in info["problems"][:20]))
    if info["notices"]:
        lines.append("vibe_notices:\n" + "\n".join(f"  - {n}" for n in info["notices"][:10]))

    result = info["final_text"]
    if not result and status != "ok":
        result = stderr
    elif stderr and status != "ok":
        lines.append("stderr:\n" + truncate(stderr, 2000))

    lines.append("\n--- result from Mistral Vibe ---\n" + (truncate(result) if result else "(no final message)"))
    print("\n".join(lines))
    return 0 if status == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
