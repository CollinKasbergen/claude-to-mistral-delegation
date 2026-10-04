#!/usr/bin/env python3
"""Hand a task to Mistral's Vibe CLI and print a compact report for Claude Code.

Running a task:
  delegate.py --mode read  "Find every place that parses config.toml"
  delegate.py --mode write --kind tests --verify "npm test" "Add unit tests for src/slugify.ts"
  delegate.py --mode write --kind feature --spec plan.md --context src/api/users.ts \\
      --allow-command "npm test" --verify "npm test" --verify "npx tsc --noEmit" -
  delegate.py --mode write --worktree-name mistral-ab12cd34 --resume <session-id> "Also cover empty strings"

Managing runs:
  delegate.py --status             running and recent delegations
  delegate.py --result ID          the full report of a run
  delegate.py --adopt ID           apply a write run's changes to your checkout and remove its worktree
  delegate.py --adopt ID --paths src/a.ts src/b.ts    apply only some files
  delegate.py --discard ID --note "why"               drop a run's worktree
  delegate.py --stats              track record per kind of task
  delegate.py --show-config        effective policy, caps, model and commands

Write mode works in its own git worktree that starts from your current code,
uncommitted and untracked files included, with dependency folders such as
node_modules symlinked in. With --verify, the wrapper runs your checks after
Vibe finishes and, if one fails, sends the output back to the same Vibe session
for a fix (--fix-attempts, default 1). With --allow-command, Vibe may run those
commands itself while it works. Settings can live in ~/.mistral-delegate/config.toml
or <repo>/.mistral-delegate.toml; see --show-config.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mdelegate import config, gitops, ledger, vibe  # noqa: E402
from mdelegate.gitops import DelegateError, git  # noqa: E402

KINDS = ("tests", "feature", "bugfix", "refactor", "migration", "boilerplate", "docs", "search", "other")
MAX_RESULT_CHARS = 12_000
CHECK_OUTPUT_LINES = 60
CHECK_OUTPUT_CHARS = 5_000
SCRIPT = Path(__file__).resolve()


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("task", nargs="?", help="Task for Vibe. Use '-' to read it from stdin.")

    run = p.add_argument_group("running a task")
    run.add_argument("--mode", choices=["read", "write"], default="read",
                     help="read: read-only tools, runs in place. write: edits, in an isolated git worktree.")
    run.add_argument("--kind", choices=KINDS, help="Kind of task, for the track record (default: search/other).")
    run.add_argument("--workdir", default=os.getcwd(), help="Project directory (default: cwd).")
    run.add_argument("--spec", metavar="FILE", help="Include this spec/plan file in the prompt.")
    run.add_argument("--context", action="append", default=[], metavar="PATH",
                     help="A file Vibe should read before starting. Repeatable.")
    run.add_argument("--verify", action="append", default=[], metavar="CMD",
                     help="Write mode: a check to run after Vibe finishes (e.g. 'npm test'). Repeatable. "
                          "Replaces the configured checks.")
    run.add_argument("--no-verify", action="store_true", help="Skip the configured checks.")
    run.add_argument("--fix-attempts", type=int, help="Times to send a failing check back to Vibe (default 1).")
    run.add_argument("--verify-timeout", type=int, default=600, help="Seconds per check (default 600).")
    run.add_argument("--allow-command", action="append", default=[], metavar="CMD",
                     help="Write mode: a command prefix Vibe may run itself (e.g. 'npm test'). Repeatable, "
                          "added to the configured ones.")
    run.add_argument("--allow-shell", action="store_true",
                     help="Pass --auto-approve so Vibe may run any shell command. Off by default.")
    run.add_argument("--model", help="Vibe model alias for this run (must exist in your Vibe config).")
    run.add_argument("--policy", choices=config.POLICIES, help="Override the delegation policy for this run's caps.")
    run.add_argument("--max-turns", type=int)
    run.add_argument("--max-price", type=float, help="Dollar cap for Vibe's first pass.")
    run.add_argument("--max-tokens", type=int)
    run.add_argument("--worktree-name", help="Worktree/branch name (default: the run id). An existing "
                                             "worktree with this name is reused, e.g. for follow-ups.")
    run.add_argument("--in-place", action="store_true", help="Write mode: edit the checkout directly.")
    run.add_argument("--no-snapshot", action="store_true",
                     help="Write mode: start from HEAD, without your uncommitted changes.")
    run.add_argument("--no-link-deps", action="store_true",
                     help="Write mode: don't symlink node_modules, .venv etc. into the worktree.")
    run.add_argument("--link", action="append", default=[], metavar="PATH",
                     help="Write mode: also symlink this path (relative to the repo root, e.g. .env). Repeatable.")
    run.add_argument("--trust", action="store_true",
                     help="Load the project's .vibe/ config and AGENTS.md (always on in worktrees).")
    run.add_argument("--resume", metavar="SESSION_ID", help="Continue an earlier Vibe session.")
    run.add_argument("--agent", help="Use this Vibe agent profile instead of the generated one.")
    run.add_argument("--timeout", type=int, default=int(os.environ.get("MISTRAL_DELEGATE_TIMEOUT", 900)),
                     help="Seconds before a Vibe call is killed (default 900).")
    run.add_argument("--diff-lines", type=int, default=300,
                     help="Include the full diff in the report when it is at most this many lines (0: never).")
    run.add_argument("--vibe-bin", default=os.environ.get("VIBE_BIN", "vibe"))

    manage = p.add_argument_group("managing runs")
    manage.add_argument("--status", action="store_true", help="List running and recent delegations.")
    manage.add_argument("--stats", action="store_true", help="Show the track record per kind of task.")
    manage.add_argument("--result", metavar="ID", help="Print the full report of a run.")
    manage.add_argument("--adopt", metavar="ID", help="Apply a write run's changes to your checkout.")
    manage.add_argument("--discard", metavar="ID", help="Discard a write run and remove its worktree.")
    manage.add_argument("--paths", nargs="+", metavar="PATH", help="With --adopt: only apply these paths.")
    manage.add_argument("--note", help="With --discard/--adopt: why, for the track record.")
    manage.add_argument("--keep-worktree", action="store_true", help="With --adopt: keep the worktree.")
    manage.add_argument("--show-config", action="store_true", help="Print the effective settings.")
    return p.parse_args(argv)


def script_cmd(*argv: str) -> str:
    return " ".join(shlex.quote(a) for a in ("python3", str(SCRIPT), *argv))


def truncate(text: str, limit: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... truncated {len(text) - limit} chars ...]"


def tail(text: str) -> str:
    lines = text.rstrip().splitlines()[-CHECK_OUTPUT_LINES:]
    out = "\n".join(lines)
    return out[-CHECK_OUTPUT_CHARS:]


# --- managing runs -------------------------------------------------------------

def find_run(run_id: str) -> dict:
    run = ledger.load_runs().get(run_id)
    if not run:
        raise DelegateError(f"No run with id {run_id!r}. See --status.")
    return run


def cmd_adopt(args: argparse.Namespace) -> int:
    run = find_run(args.adopt)
    if ledger.state(run) == "running":
        raise DelegateError("That run is still going. Wait for it to finish.")
    wt = run.get("worktree")
    if not wt:
        if run.get("mode") != "write":
            raise DelegateError("Read-only runs have nothing to adopt.")
        ledger.append({"event": "outcome", "id": run["id"], "outcome": "adopted", "note": args.note})
        print(f"Marked {run['id']} as adopted (it edited your checkout directly).")
        return 0
    if not os.path.isdir(wt["path"]):
        raise DelegateError(f"The worktree {wt['path']} no longer exists.")
    try:
        files = gitops.apply_to_checkout(wt, args.paths)
    except DelegateError as e:
        raise DelegateError(f"{e}\nNothing was applied; the worktree is still at {wt['path']}. "
                            "Your checkout may have changed the same lines: apply by hand or use --paths.") from e
    if not files:
        print("Nothing to apply" + (" for those paths." if args.paths else ": the run made no changes."))
        return 0
    outcome = "adopted_partial" if args.paths else "adopted"
    ledger.append({"event": "outcome", "id": run["id"], "outcome": outcome, "paths": args.paths, "note": args.note})
    print(f"Applied {len(files)} file(s) from {run['id']} to {wt['toplevel']}:")
    print("\n".join(f"  {f}" for f in files))
    if args.keep_worktree:
        print(f"Worktree kept at {wt['path']}.")
    else:
        gitops.remove_worktree(wt)
        print("Worktree removed.")
    print("The changes are uncommitted in your checkout; review and commit them as usual.")
    return 0


def cmd_discard(args: argparse.Namespace) -> int:
    run = find_run(args.discard)
    if ledger.state(run) == "running":
        raise DelegateError("That run is still going. Wait for it to finish.")
    wt = run.get("worktree")
    if wt and os.path.isdir(wt["path"]):
        gitops.remove_worktree(wt)
    ledger.append({"event": "outcome", "id": run["id"], "outcome": "discarded", "note": args.note})
    print(f"Discarded {run['id']}" + (" and removed its worktree." if wt else "."))
    return 0


# --- running a task ------------------------------------------------------------

class Run:
    """State of one delegation while it executes."""

    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.status = "error"
        self.tool_calls = 0
        self.problems: list[str] = []
        self.notices: list[str] = []
        self.final_text = ""
        self.stderr = ""
        self.session_id: str | None = args.resume

    def call_vibe(self, cmd: list[str], cwd: str) -> None:
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                  timeout=self.args.timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            self.status = "timeout"
            self.stderr = f"Vibe did not finish within {self.args.timeout}s and was stopped."
            return
        info = vibe.summarize_history(vibe.parse_output(proc.stdout))
        self.stderr = proc.stderr.strip()
        self.session_id = info["session_id"] or self.session_id
        self.tool_calls += info["tool_calls"]
        self.problems += info["problems"]
        self.notices += info["notices"]
        if info["final_text"]:
            self.final_text = info["final_text"]
        if proc.returncode == 0:
            self.status = "ok"
        elif (proc.returncode == 1 and not proc.stdout.strip() and self.stderr
              and not re.match(r"(Error|Teleport error):", self.stderr) and "Traceback" not in self.stderr):
            # Vibe reports a hit limit by printing the last assistant text to stderr, unprefixed.
            self.status = "limit_reached"
            self.final_text = self.final_text or self.stderr
        else:
            self.status = "error"


def run_checks(commands: list[str], cwd: str, timeout: int) -> tuple[list[str], tuple | None]:
    """Run checks in order until one fails. Returns (lines for the report, failure or None)."""
    lines = []
    for command in commands:
        try:
            proc = subprocess.run(command, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            code, output = proc.returncode, proc.stdout
        except subprocess.TimeoutExpired as e:
            code = 124
            output = (e.output or "") if isinstance(e.output, str) else (e.output or b"").decode(errors="replace")
            output += f"\n[timed out after {timeout}s]"
        lines.append(f"  {'pass' if code == 0 else 'FAIL'}: {command}" + ("" if code == 0 else f" (exit {code})"))
        if code != 0:
            return lines, (command, code, tail(output))
    return lines, None


def run_task(args: argparse.Namespace) -> int:
    workdir = str(Path(args.workdir).resolve())
    if args.task == "-":
        args.task = sys.stdin.read()
    spec = None
    if args.spec:
        try:
            spec = Path(args.spec).read_text(encoding="utf-8")
        except OSError as e:
            raise DelegateError(f"Can't read --spec file: {e}") from e
    task = (args.task or "").strip() or ("Implement the spec below." if spec else "")
    if not task:
        raise DelegateError("No task given. Pass it as an argument, '-' for stdin, or use --spec.")
    if args.mode == "read" and (args.verify or args.in_place or args.allow_command):
        raise DelegateError("--verify, --allow-command and --in-place only apply to --mode write.")

    top = gitops.toplevel(workdir)
    settings = config.load(top or workdir)
    if args.policy:
        settings["policy"] = args.policy
    caps = config.caps(settings, args.mode)
    for key in ("max_turns", "max_price", "max_tokens"):
        if getattr(args, key) is not None:
            caps[key] = getattr(args, key)
    model = args.model or settings["model"]
    write = args.mode == "write"
    verify = [] if (args.no_verify or not write) else (args.verify or settings["verify"])
    allow_commands = list(dict.fromkeys(settings["allow_commands"] + args.allow_command)) if write else []
    fix_attempts = settings["fix_attempts"] if args.fix_attempts is None else max(0, args.fix_attempts)
    kind = args.kind or ("search" if args.mode == "read" else "other")

    if shutil.which(args.vibe_bin) is None and not Path(args.vibe_bin).is_file():
        print("status: error\n\nVibe CLI not found. Install it with `uv tool install mistral-vibe` "
              "(or `pip install mistral-vibe`), then run `vibe --setup` once to store your API key.")
        return 2

    active = ledger.running(ledger.load_runs())
    if len(active) >= settings["max_parallel"]:
        raise DelegateError(f"{len(active)} delegations are already running (max_parallel = "
                            f"{settings['max_parallel']}): {', '.join(r['id'] for r in active)}. "
                            "Wait for one to finish (see --status) or raise max_parallel in the config.")

    prefix = "read" if not write else ("inplace" if args.in_place else "mistral")
    run_id = f"{prefix}-{uuid.uuid4().hex[:8]}"

    run_dir, wt = workdir, None
    if write and not args.in_place:
        if not top:
            print("status: error\n\nWrite mode needs a git repository for worktree isolation. "
                  "Pass --in-place to let Vibe edit the directory directly.")
            return 2
        try:
            wt = gitops.prepare_worktree(top, args.worktree_name or run_id, snapshot=not args.no_snapshot,
                                         link_deps=not args.no_link_deps, extra_links=args.link)
        except DelegateError as e:
            print(f"status: error\n\nCould not prepare the worktree: {e}")
            return 2
        run_dir = str(Path(wt["path"]) / Path(workdir).relative_to(top))

    prompt = vibe.build_prompt(task, mode=args.mode, spec=spec, context=args.context, verify=verify,
                               allow_commands=allow_commands, allow_shell=args.allow_shell)
    agent = args.agent or vibe.write_agent_profile(args.mode, model, allow_commands)
    trust = args.trust or wt is not None

    ledger.append({"event": "start", "id": run_id, "pid": os.getpid(), "mode": args.mode, "kind": kind,
                   "repo": Path(top or workdir).name, "workdir": workdir, "task": task[:500],
                   "policy": settings["policy"], "model": model,
                   "worktree": {k: wt[k] for k in ("name", "path", "toplevel", "base", "links")} if wt else None})

    run = Run(args)
    stats_before = vibe.read_session_stats(args.resume, run_dir) if args.resume else None
    started_wall, started = time.time(), time.monotonic()
    run.call_vibe(vibe.build_command(args.vibe_bin, prompt, mode=args.mode, agent=agent, caps=caps,
                                     allow_shell=args.allow_shell, trust=trust, resume=args.resume), run_dir)

    verification, check_lines, failure, attempts = "not_run", [], None, 0
    if verify and run.status in ("ok", "limit_reached"):
        check_lines, failure = run_checks(verify, run_dir, args.verify_timeout)
        while failure and attempts < fix_attempts and run.status == "ok" and run.session_id:
            attempts += 1
            spent = vibe.stats_cost(vibe.read_session_stats(run.session_id, run_dir))
            fix_caps = dict(caps, max_price=spent + caps["max_price"] * 0.5)
            run.call_vibe(vibe.build_command(args.vibe_bin, vibe.fix_prompt(*failure), mode=args.mode, agent=agent,
                                             caps=fix_caps, allow_shell=args.allow_shell, trust=trust,
                                             resume=run.session_id), run_dir)
            check_lines, failure = run_checks(verify, run_dir, args.verify_timeout)
        verification = "failed" if failure else "passed"
    elapsed = time.monotonic() - started

    stats_after = vibe.read_session_stats(run.session_id, run_dir,
                                          since=None if run.session_id else started_wall)
    run.session_id = run.session_id or (stats_after or {}).get("session_id")
    use = vibe.usage(stats_after, stats_before)

    lines = [f"run_id: {run_id}", f"status: {run.status}"]
    if verify:
        detail = f"after {attempts} fix attempt{'s' if attempts != 1 else ''}" if attempts else "first try"
        if verification == "not_run":
            lines.append("verification: not run (Vibe did not finish)")
        else:
            lines.append(f"verification: {verification} ({detail})\n" + "\n".join(check_lines))
    lines.append(f"mode: {args.mode}, kind: {kind}, policy: {settings['policy']}" + (f", model: {model}" if model else ""))
    if use:
        line = f"usage: cost ${use['cost']:.4f} (first-pass cap ${caps['max_price']:.2f}), {use['steps']} steps, {use['tokens']:,} tokens"
        if use["session_total"] is not None:
            line += f" (this run; session total ${use['session_total']:.4f})"
        lines.append(line)
    else:
        lines.append(f"usage: cost unknown (Vibe session log not found), first-pass cap ${caps['max_price']:.2f}")
    lines.append(f"elapsed: {elapsed:.0f}s, tool_calls: {run.tool_calls}, max_turns: {caps['max_turns']}")
    model_warning = vibe.unknown_model_warning(model) if not args.agent else None
    if model_warning:
        lines.append(f"model_warning: {model_warning}")
    if allow_commands:
        lines.append("vibe_may_run: " + ", ".join(allow_commands))
    if run.session_id:
        lines.append(f"session_id: {run.session_id}  (follow up with: --resume {run.session_id}"
                     + (f" --worktree-name {wt['name']})" if wt else ")"))

    files: list[str] = []
    worktree_removed = False
    if wt:
        files = gitops.changed_files(wt)
        if run.status in ("error", "timeout") and not wt["reused"] and not files:
            gitops.remove_worktree(wt)
            worktree_removed = True
            lines.append(f"worktree: removed {wt['name']} (Vibe failed before changing anything)")
        else:
            lines += worktree_section(wt, run_id, files, args.diff_lines)
    elif write:
        status_out = git(workdir, "status", "--porcelain").rstrip()
        files = [line[3:] for line in status_out.splitlines()]
        lines.append("changed_files (in your checkout):\n" + (status_out or "  (none)"))

    if failure:
        lines.append(f"failing_check_output ({failure[0]}):\n```\n{failure[2]}\n```")
    if run.problems:
        lines.append("tool_calls_not_completed:\n" + "\n".join(f"  - {p}" for p in run.problems[:20]))
    if run.notices:
        lines.append("vibe_notices:\n" + "\n".join(f"  - {n}" for n in run.notices[:10]))
    if run.stderr and run.status != "ok" and run.stderr != run.final_text:
        lines.append("stderr:\n" + truncate(run.stderr, 2000))
    lines.append("\n--- result from Mistral Vibe ---\n" + (truncate(run.final_text) if run.final_text else "(no final message)"))

    report = "\n".join(lines)
    ledger.save_report(run_id, report)
    ledger.append({"event": "end", "id": run_id, "status": run.status, "verification": verification,
                   "fix_attempts_used": attempts, "cost": use["cost"] if use else None,
                   "steps": use["steps"] if use else None, "tokens": use["tokens"] if use else None,
                   "files_changed": len(files), "session_id": run.session_id,
                   "worktree_removed": worktree_removed})
    print(report)
    return 0 if run.status == "ok" and verification != "failed" else 1


def worktree_section(wt: dict, run_id: str, files: list[str], diff_lines: int) -> list[str]:
    lines = [f"worktree_name: {wt['name']}" + ("  (reused)" if wt["reused"] else ""),
             f"worktree_path: {wt['path']}"]
    snap = wt.get("snapshot")
    if snap:
        lines.append(f"worktree_base: snapshot of your uncommitted work ({snap['modified']} modified, "
                     f"{snap['untracked']} untracked files) at {wt['base'][:12]}")
    else:
        lines.append(f"worktree_base: your HEAD at {wt['base'][:12]}")
    if wt.get("links"):
        lines.append("linked_from_checkout (symlinks, shared with your checkout): " + ", ".join(wt["links"]))
    stat = gitops.changes_stat(wt)
    lines.append("changes_by_vibe:\n" + (stat or "  (none)"))
    if files:
        diff = gitops.changes_diff(wt)
        n = diff.count("\n")
        if 0 < n <= diff_lines:
            lines.append(f"diff:\n```diff\n{diff.rstrip()}\n```")
        else:
            lines.append(f"diff: {n} lines, not shown. Review with: git -C {shlex.quote(wt['path'])} diff --cached {wt['base'][:12]}")
        lines.append(f"adopt_with: {script_cmd('--adopt', run_id)}   (add --paths ... to take only some files)")
    lines.append(f"discard_with: {script_cmd('--discard', run_id, '--note', 'why')}")
    return lines


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        if args.status:
            print(ledger.format_status(ledger.load_runs()))
            return 0
        if args.stats:
            print(ledger.format_stats(ledger.load_runs()))
            return 0
        if args.result:
            report = ledger.read_report(args.result)
            if report is None:
                run = find_run(args.result)
                print(f"{run['id']} is {ledger.state(run)}; no report yet.")
                return 0 if ledger.state(run) == "running" else 1
            print(report)
            return 0
        if args.show_config:
            top = gitops.toplevel(str(Path(args.workdir).resolve()))
            print(config.describe(config.load(top or args.workdir)))
            return 0
        if args.adopt:
            return cmd_adopt(args)
        if args.discard:
            return cmd_discard(args)
        return run_task(args)
    except DelegateError as e:
        print(f"status: error\n\n{e}")
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
