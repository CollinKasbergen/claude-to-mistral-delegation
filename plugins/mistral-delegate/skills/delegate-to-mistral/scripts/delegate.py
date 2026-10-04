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
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mdelegate import config, gitops, guard, ledger, vibe  # noqa: E402
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
    run.add_argument("--scope", action="append", default=[], metavar="GLOB",
                     help="Write mode: files Mistral may create or change, relative to the repo root "
                          "(e.g. 'src/**/*.test.ts'). Repeatable. Changes outside are flagged, and --adopt skips them.")
    run.add_argument("--no-baseline", action="store_true",
                     help="Don't run the checks on the untouched worktree before Mistral starts.")
    run.add_argument("--deps-mode", choices=config.DEPS_MODES,
                     help="How dependency folders get into the worktree (default: hardlink).")
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
    manage.add_argument("--include-out-of-scope", action="store_true",
                        help="With --adopt: also apply changes outside the run's --scope.")
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
    paths = args.paths
    skipped: list[str] = []
    scope = run.get("scope") or []
    if scope and not args.include_out_of_scope:
        changed = gitops.changed_files(wt)
        if paths:
            changed = [f for f in changed if any(f == p or f.startswith(p.rstrip("/") + "/") for p in paths)]
        skipped = [f for f in changed if not vibe.matches_scope(f, scope)]
        if skipped:
            paths = [f for f in changed if vibe.matches_scope(f, scope)]
            if not paths:
                print("Every change is outside the run's scope (" + ", ".join(scope) + "); nothing applied. "
                      "Use --include-out-of-scope to apply them anyway.")
                return 1
    try:
        files = gitops.apply_to_checkout(wt, paths)
    except DelegateError as e:
        raise DelegateError(f"{e}\nNothing was applied; the worktree is still at {wt['path']}. "
                            "Your checkout may have changed the same lines: apply by hand or use --paths.") from e
    if not files:
        print("Nothing to apply" + (" for those paths." if args.paths else ": the run made no changes."))
        return 0
    outcome = "adopted_partial" if (args.paths or skipped) else "adopted"
    ledger.append({"event": "outcome", "id": run["id"], "outcome": outcome, "paths": paths, "note": args.note})
    print(f"Applied {len(files)} file(s) from {run['id']} to {wt['toplevel']}:")
    print("\n".join(f"  {f}" for f in files))
    if skipped:
        print("Left out (outside the run's scope; --include-out-of-scope to apply):")
        print("\n".join(f"  {f}" for f in skipped))
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
        self.turns = 0
        self.denied: list[str] = []
        self.problems: list[str] = []
        self.notices: list[str] = []
        self.final_text = ""
        self.stderr = ""
        self.cancelled = False
        self.session_id: str | None = args.resume

    def call_vibe(self, cmd: list[str], cwd: str) -> None:
        try:
            proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                                  timeout=self.args.timeout, stdin=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            self.status = "timeout"
            self.stderr = f"Vibe did not finish within {self.args.timeout}s and was stopped."
            return
        history = vibe.parse_output(proc.stdout)
        # A refused approval in programmatic mode cancels the session.
        self.cancelled = self.cancelled or bool(CANCELLED.search(proc.stdout) or CANCELLED.search(proc.stderr))
        info = vibe.summarize_history(this_turn(history))
        self.stderr = proc.stderr.strip()
        self.session_id = info["session_id"] or vibe.summarize_history(history)["session_id"] or self.session_id
        self.tool_calls += info["tool_calls"]
        self.turns += info["assistant_messages"]
        self.denied += info["denied"]
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
            self.final_text = self.stderr
        else:
            self.status = "error"


CANCELLED = re.compile(r"<user_cancellation>|User cancelled the operation")


def this_turn(history: list) -> list:
    """The entries from the latest prompt on.

    A resumed session's JSON output repeats the whole session, so counting all of
    it again would inflate tool calls and denied commands.
    """
    for i in range(len(history) - 1, -1, -1):
        entry = history[i]
        if (isinstance(entry, dict) and entry.get("type") == "message" and entry.get("role") == "user"
                and entry.get("source") in (None, "turn_start")):
            return history[i:]
    return history


def run_checks(commands: list[str], cwd: str, timeout: int) -> dict[str, tuple[int, str]]:
    """Run every check. Returns {command: (exit code, end of output)}."""
    results = {}
    for command in commands:
        try:
            proc = subprocess.run(command, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  text=True, timeout=timeout, stdin=subprocess.DEVNULL)
            code, output = proc.returncode, proc.stdout
        except subprocess.TimeoutExpired as e:
            code = 124
            output = (e.output or "") if isinstance(e.output, str) else (e.output or b"").decode(errors="replace")
            output += f"\n[timed out after {timeout}s]"
        results[command] = (code, tail(output))
    return results


def new_failures(results: dict, baseline: dict) -> list[tuple[str, int, str]]:
    """Failing checks that passed (or weren't run) before Mistral changed anything."""
    return [(cmd, code, out) for cmd, (code, out) in results.items()
            if code != 0 and not (cmd in baseline and baseline[cmd][0] != 0)]


def check_lines(results: dict, baseline: dict) -> list[str]:
    lines = []
    for cmd, (code, _out) in results.items():
        if code == 0:
            note = " (was failing before Mistral)" if cmd in baseline and baseline[cmd][0] != 0 else ""
            lines.append(f"  pass: {cmd}{note}")
        elif cmd in baseline and baseline[cmd][0] != 0:
            lines.append(f"  FAIL: {cmd} (exit {code}; already failing before Mistral changed anything)")
        else:
            lines.append(f"  FAIL: {cmd} (exit {code})")
    return lines


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
    if args.mode == "read" and (args.verify or args.in_place or args.allow_command or args.scope):
        raise DelegateError("--verify, --allow-command, --scope and --in-place only apply to --mode write.")

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
    scope = list(dict.fromkeys(args.scope or settings["scope"])) if write else []
    fix_attempts = settings["fix_attempts"] if args.fix_attempts is None else max(0, args.fix_attempts)
    baseline_on = settings["baseline"] and not args.no_baseline
    deps_mode = args.deps_mode or settings["deps_mode"]
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
                                         link_deps=not args.no_link_deps, extra_links=args.link,
                                         deps_mode=deps_mode, worktrees_dir=settings["worktrees_dir"])
        except DelegateError as e:
            print(f"status: error\n\nCould not prepare the worktree: {e}")
            return 2
        run_dir = str(Path(wt["path"]) / Path(workdir).relative_to(top))

    ledger.append({"event": "start", "id": run_id, "pid": os.getpid(), "mode": args.mode, "kind": kind,
                   "repo": Path(top or workdir).name, "workdir": workdir, "task": task[:500],
                   "policy": settings["policy"], "model": model, "scope": scope,
                   "worktree": {k: wt[k] for k in ("name", "path", "toplevel", "base", "links")} if wt else None})

    # The guard hook refuses disallowed tool calls with an error Mistral can work around,
    # instead of letting Vibe's approval prompt cancel the session.
    guard_root = os.path.realpath(wt["path"] if wt else (top or workdir))
    guard_log = ledger.runs_dir() / f"{run_id}.guard.jsonl"
    guard_log.parent.mkdir(parents=True, exist_ok=True)
    guard_warning = guard.install(vibe.vibe_home(), config.home())
    if not guard_warning:
        default_cmds = vibe.DEFAULT_BASH_ALLOWLIST if write else []
        guard.write_policy(run_dir, {
            "run_id": run_id, "root": guard_root, "mode": args.mode, "scope": scope,
            "allow_commands": default_cmds + allow_commands, "default_commands": default_cmds,
            "allow_shell": args.allow_shell, "log": str(guard_log),
            "expires": time.time() + args.timeout * (fix_attempts + 1) + args.verify_timeout * 3 + 600,
        }, config.home())
    try:
        return execute(args, settings, run_id, run_dir, wt, top, workdir, task, spec, caps, model, write, verify,
                       allow_commands, scope, fix_attempts, baseline_on, kind, guard_log, guard_warning)
    finally:
        guard.remove_policy(run_dir, run_id, config.home())


def execute(args, settings, run_id, run_dir, wt, top, workdir, task, spec, caps, model, write, verify,
            allow_commands, scope, fix_attempts, baseline_on, kind, guard_log, guard_warning) -> int:
    started_wall, started = time.time(), time.monotonic()
    # Run the checks once before Mistral changes anything, so failures that were
    # already there (or come from the worktree environment) aren't blamed on it.
    baseline = run_checks(verify, run_dir, args.verify_timeout) if verify and baseline_on else {}
    preexisting = [cmd for cmd, (code, _out) in baseline.items() if code != 0]

    prompt = vibe.build_prompt(task, mode=args.mode, spec=spec, context=args.context, verify=verify,
                               allow_commands=allow_commands, allow_shell=args.allow_shell, scope=scope,
                               preexisting_failures=preexisting)
    agent = args.agent or vibe.write_agent_profile(args.mode, model, allow_commands)
    trust = args.trust or wt is not None

    run = Run(args)
    stats_before = vibe.read_session_stats(args.resume, run_dir, model_hint=model) if args.resume else None
    run.call_vibe(vibe.build_command(args.vibe_bin, prompt, mode=args.mode, agent=agent, caps=caps,
                                     allow_shell=args.allow_shell, trust=trust, resume=args.resume,
                                     extra=settings["vibe_args"]), run_dir)

    verification, results, failures, attempts = "not_run", {}, [], 0
    if verify and run.status in ("ok", "limit_reached"):
        results = run_checks(verify, run_dir, args.verify_timeout)
        failures = new_failures(results, baseline)
        while failures and attempts < fix_attempts and run.status == "ok" and run.session_id:
            attempts += 1
            spent = vibe.stats_cost(vibe.read_session_stats(run.session_id, run_dir, model_hint=model)) or 0.0
            fix_caps = dict(caps, max_price=spent + caps["max_price"] * 0.5)
            run.call_vibe(vibe.build_command(args.vibe_bin, vibe.fix_prompt(failures, scope), mode=args.mode,
                                             agent=agent, caps=fix_caps, allow_shell=args.allow_shell,
                                             trust=trust, resume=run.session_id,
                                             extra=settings["vibe_args"]), run_dir)
            results = run_checks(verify, run_dir, args.verify_timeout)
            failures = new_failures(results, baseline)
        if failures:
            verification = "failed"
        elif any(code != 0 for code, _out in results.values()):
            verification = "passed_except_preexisting"
        else:
            verification = "passed"
    elapsed = time.monotonic() - started

    stats_after = vibe.read_session_stats(run.session_id, run_dir, since=None if run.session_id else started_wall,
                                          model_hint=model)
    run.session_id = run.session_id or (stats_after or {}).get("session_id")
    use = vibe.usage(stats_after, stats_before)

    files_now = (gitops.changed_files(wt) if wt else
                 [line[3:] for line in git(workdir, "status", "--porcelain").splitlines()] if write else [])
    if run.cancelled and run.status in ("ok", "error"):
        run.status = "stopped_by_refusal"
    elif write and run.status == "ok" and not files_now:
        run.status = "no_changes"
    guard_events = guard.read_log(guard_log)

    lines = [f"run_id: {run_id}", f"status: {run.status}"]
    if run.status == "no_changes":
        lines.append("note: Mistral finished without changing any file. Read its result below to see why.")
    if run.status == "stopped_by_refusal":
        lines.append("note: Vibe ended the session after a refused tool call (it treats a refused approval as "
                     "the user cancelling). " + ("The guard hook was not active: " + guard_warning if guard_warning
                     else "The guard hook should prevent this; check guard below.")
                     + " Resume with --resume to let Mistral continue.")
    if verify:
        detail = f"after {attempts} fix attempt{'s' if attempts != 1 else ''}" if attempts else "first try"
        if verification == "not_run":
            lines.append("verification: not run (Vibe did not finish)")
        else:
            lines.append(f"verification: {verification} ({detail})\n" + "\n".join(check_lines(results, baseline)))
    if preexisting:
        lines.append("baseline_warning: these checks already failed in the untouched "
                     + ("worktree" if wt else "checkout") + " before Mistral changed anything: "
                     + ", ".join(preexisting) + ". Mistral was told not to work around them. If they pass "
                     "in your checkout, the cause is the worktree environment (see deps_mode).")
    lines.append(f"mode: {args.mode}, kind: {kind}, policy: {settings['policy']}" + (f", model: {model}" if model else ""))
    lines.append(usage_line(use, caps["max_price"]))
    turns = use["steps"] if use and use.get("steps") is not None else run.turns
    lines.append(f"elapsed: {elapsed:.0f}s, turns: {turns} (max_turns {caps['max_turns']}), "
                 f"tool calls: {run.tool_calls} (several per turn; not capped by max_turns)")
    lines.append(guard_line(guard_events, run.tool_calls, guard_warning))
    model_warning = vibe.unknown_model_warning(model) if not args.agent else None
    if model_warning:
        lines.append(f"model_warning: {model_warning}")
    if allow_commands:
        lines.append("vibe_may_run: " + ", ".join(allow_commands))
    if scope:
        lines.append("scope: " + ", ".join(scope))
    if run.session_id:
        lines.append(f"session_id: {run.session_id}  (follow up with: --resume {run.session_id}"
                     + (f" --worktree-name {wt['name']})" if wt else ")"))

    files: list[str] = []
    out_of_scope: list[str] = []
    worktree_removed = False
    if wt:
        files = gitops.changed_files(wt)
        out_of_scope = [f for f in files if scope and not vibe.matches_scope(f, scope)]
        if run.status in ("error", "timeout") and not wt["reused"] and not files:
            gitops.remove_worktree(wt)
            worktree_removed = True
            lines.append(f"worktree: removed {wt['name']} (Vibe failed before changing anything)")
        else:
            lines += worktree_section(wt, run_id, files, out_of_scope, args.diff_lines)
    elif write:
        status_out = git(workdir, "status", "--porcelain").rstrip()
        files = [line[3:] for line in status_out.splitlines()]
        out_of_scope = [f for f in files if scope and not vibe.matches_scope(f, scope)]
        lines.append("changed_files (in your checkout):\n" + (status_out or "  (none)"))
        if out_of_scope:
            lines.append("out_of_scope_changes (in your checkout, outside --scope; revert them if unwanted): "
                         + ", ".join(out_of_scope))

    for cmd, code, out in failures[:3]:
        lines.append(f"failing_check_output ({cmd}, exit {code}):\n```\n{out}\n```")
    guard_denied = [f"{e.get('tool')}: {e.get('target')} -> {e.get('reason')}" for e in guard_events
                    if e.get("action") == "deny"]
    if guard_denied:
        lines.append("refused_by_guard (Mistral got these refusals as errors and could continue):\n"
                     + "\n".join(f"  - {d}" for d in guard_denied[:15]))
    guard_targets = {str(e.get("target")) for e in guard_events if e.get("action") == "deny"}
    vibe_denied = [d for d in run.denied if d.split(": ", 1)[-1] not in guard_targets]
    if vibe_denied:
        counts = Counter(vibe_denied)
        why = ("refused because they aren't in allow_commands; add them if Mistral needs them" if write
               else "read mode runs no commands")
        lines.append(f"denied_commands ({why}):\n"
                     + "\n".join(f"  - {cmd}" + (f"  ({n}x)" if n > 1 else "") for cmd, n in counts.most_common(15)))
    if run.problems:
        lines.append("tool_calls_failed:\n" + "\n".join(f"  - {p}" for p in run.problems[:20]))
    if run.notices:
        lines.append("vibe_notices:\n" + "\n".join(f"  - {n}" for n in run.notices[:10]))
    if run.stderr and run.status != "ok" and run.stderr.strip() != run.final_text.strip():
        lines.append("stderr:\n" + truncate(run.stderr, 2000))
    lines.append("\n--- result from Mistral Vibe ---\n" + (truncate(run.final_text) if run.final_text else "(no final message)"))

    report = "\n".join(lines)
    ledger.save_report(run_id, report)
    ledger.append({"event": "end", "id": run_id, "status": run.status, "verification": verification,
                   "fix_attempts_used": attempts, "cost": use["cost"] if use else None,
                   "cost_estimated": use["estimated"] if use else None,
                   "steps": turns, "tokens": use["tokens"] if use else None,
                   "files_changed": len(files), "out_of_scope": out_of_scope,
                   "denied": sorted(set(run.denied) | {f"{e.get('tool')}: {e.get('target')}" for e in guard_events
                                                       if e.get("action") == "deny"}),
                   "baseline_failures": preexisting,
                   "session_id": run.session_id, "worktree_removed": worktree_removed})
    print(report)
    return 0 if run.status == "ok" and verification != "failed" else 1


def guard_line(events: list[dict], tool_calls: int, warning: str | None) -> str:
    if warning:
        return f"guard: not installed ({warning}); a refused tool call can end the session"
    if not events:
        return ("guard: no tool calls reached it" + (" (is your Vibe version older than hooks support?)"
                                                     if tool_calls else ""))
    denied = sum(e.get("action") == "deny" for e in events)
    rewritten = sum(e.get("action") == "rewrite" for e in events)
    return (f"guard: checked {len(events)} tool calls, refused {denied} (returned to Mistral as errors), "
            f"corrected {rewritten} path(s)")


def usage_line(use: dict | None, max_price: float) -> str:
    if not use:
        return (f"usage: cost unknown (no token data in Vibe's session storage; if this keeps happening, set "
                f"vibe_args = [\"--legacy-harness\"] in the config for exact costs), first-pass cap ${max_price:.2f}")
    if use["cost"] is None:
        cost = f"cost unknown (no price for model {use.get('model')!r} in Vibe's config)"
    elif use["estimated"]:
        cost = f"cost ~${use['cost']:.4f} (estimated from tokens at {use.get('model')} list prices)"
    else:
        cost = f"cost ${use['cost']:.4f}"
    line = f"usage: {cost}, first-pass cap ${max_price:.2f}, {use['tokens']:,} tokens"
    if use["session_total"] is not None:
        line += f" (this run; session total ${use['session_total']:.4f})"
    return line


def worktree_section(wt: dict, run_id: str, files: list[str], out_of_scope: list[str], diff_lines: int) -> list[str]:
    lines = [f"worktree_name: {wt['name']}" + ("  (reused)" if wt["reused"] else ""),
             f"worktree_path: {wt['path']}"]
    snap = wt.get("snapshot")
    if snap:
        lines.append(f"worktree_base: snapshot of your uncommitted work ({snap['modified']} modified, "
                     f"{snap['untracked']} untracked files) at {wt['base'][:12]}")
    else:
        lines.append(f"worktree_base: your HEAD at {wt['base'][:12]}")
    if wt.get("links"):
        how = {"hardlink": "hard-linked copies", "copy": "copies", "symlink": "symlinks"}.get(wt.get("deps_mode"), "linked")
        lines.append(f"dependencies ({how} from your checkout): " + ", ".join(wt["links"]))
    for note in wt.get("notes") or []:
        lines.append(f"dependency_note: {note}")
    stat = gitops.changes_stat(wt)
    lines.append("changes_by_vibe:\n" + (stat or "  (none)"))
    if out_of_scope:
        lines.append("out_of_scope_changes (outside --scope; --adopt leaves these out unless you add "
                     "--include-out-of-scope): " + ", ".join(out_of_scope))
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
