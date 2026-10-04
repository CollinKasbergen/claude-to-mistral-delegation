#!/usr/bin/env python3
"""Run one task through Mistral's Vibe CLI in programmatic mode and print a compact report.

The report is meant to be read by Claude Code: the final answer from Vibe, the
session id (for follow-ups), any tool calls that did not complete, and, in write
mode, the worktree and the files Vibe changed. The full message history stays
out of Claude's context.

Usage:
  delegate.py --mode read  "Find every place that parses config.toml"
  delegate.py --mode write "Add unit tests for utils/slugify.py"
  delegate.py --mode write --resume <session-id> "Also cover empty strings"
  echo "long prompt" | delegate.py --mode read -

Vibe behaviour this script relies on (checked against mistral-vibe 2.25.x):
  * `--output json` prints a JSON list of history entries (camelCase keys).
  * In programmatic mode, any tool call that needs approval is denied, not
    prompted. `--auto-approve` lifts that.
  * Hitting --max-turns / --max-price / --max-tokens exits 1 with the last
    assistant text on stderr and nothing on stdout.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

READ_ONLY_TOOLS = ["read_file", "grep", "todo"]

DEFAULTS = {
    "read": {"max_turns": 15, "max_price": 0.25},
    "write": {"max_turns": 30, "max_price": 1.00},
}

MAX_RESULT_CHARS = 12_000


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
    p.add_argument("--worktree-name", help="Worktree/branch name in write mode (default: mistral-<id>).")
    p.add_argument("--in-place", action="store_true",
                   help="Write mode only: edit the working tree directly instead of a worktree.")
    p.add_argument("--allow-shell", action="store_true",
                   help="Pass --auto-approve so Vibe may run shell commands. Off by default.")
    p.add_argument("--trust", action="store_true",
                   help="Load the project's .vibe/ config and AGENTS.md for this run.")
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


def build_command(args: argparse.Namespace, worktree: str | None) -> list[str]:
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
        if worktree:
            cmd += ["--worktree", worktree]
        if args.allow_shell:
            cmd += ["--auto-approve"]

    if args.trust:
        cmd += ["--trust"]
    if args.resume:
        cmd += ["--resume", args.resume]
    return cmd


def git(workdir: str, *argv: str) -> str:
    try:
        out = subprocess.run(["git", "-C", workdir, *argv], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return out.stdout if out.returncode == 0 else ""


def find_worktree(workdir: str, branch: str) -> str | None:
    path = None
    for line in git(workdir, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


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


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} chars ...]"


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    workdir = str(Path(args.workdir).resolve())

    if shutil.which(args.vibe_bin) is None and not Path(args.vibe_bin).is_file():
        print("status: error\n\nVibe CLI not found. Install it with `uv tool install mistral-vibe` "
              "(or `pip install mistral-vibe`), then run `vibe --setup` once to store your API key.")
        return 2

    worktree = None
    if args.mode == "write" and not args.in_place:
        if not git(workdir, "rev-parse", "--is-inside-work-tree").strip():
            print("status: error\n\nWrite mode needs a git repository for worktree isolation. "
                  "Pass --in-place to let Vibe edit the directory directly.")
            return 2
        worktree = args.worktree_name or f"mistral-{uuid.uuid4().hex[:8]}"
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", worktree):
            print(f"status: error\n\nInvalid worktree name: {worktree!r}")
            return 2

    cmd = build_command(args, worktree)
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=workdir, capture_output=True, text=True,
                              timeout=args.timeout, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        print(f"status: timeout\n\nVibe did not finish within {args.timeout}s and was stopped.")
        if worktree:
            print(f"worktree branch: {worktree} (may contain partial work)")
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

    lines = [
        f"status: {status}",
        f"mode: {args.mode}",
        f"elapsed: {elapsed:.0f}s",
        f"limits: max_turns={args.max_turns} max_price=${args.max_price:.2f}"
        + (f" max_tokens={args.max_tokens}" if args.max_tokens else ""),
    ]
    if info["session_id"]:
        lines.append(f"session_id: {info['session_id']}  (pass --resume {info['session_id']} for a follow-up)")
    lines.append(f"tool_calls: {info['tool_calls']}")

    if worktree:
        wt_path = find_worktree(workdir, worktree)
        lines.append(f"worktree_branch: {worktree}")
        if wt_path:
            lines.append(f"worktree_path: {wt_path}")
            status_out = git(wt_path, "status", "--porcelain").rstrip()
            base = git(workdir, "rev-parse", "HEAD").strip()
            committed = git(wt_path, "diff", "--stat", base, "HEAD").rstrip() if base else ""
            lines.append("uncommitted_changes:\n" + (status_out or "  (none)"))
            if committed:
                lines.append("committed_changes_vs_your_HEAD:\n" + committed)
        else:
            lines.append("worktree_path: (not found; Vibe may have failed before creating it)")
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
