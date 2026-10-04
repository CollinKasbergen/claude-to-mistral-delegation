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
import tempfile
import time
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mdelegate import commands as cmdforms, config, gitops, guard, ledger, vibe  # noqa: E402
from mdelegate.gitops import DelegateError, git  # noqa: E402

KINDS = ("tests", "feature", "bugfix", "refactor", "migration", "boilerplate", "docs", "search", "other")
MAX_RESULT_CHARS = 12_000
CHECK_OUTPUT_LINES = 60
CHECK_OUTPUT_CHARS = 5_000
WATCH_INTERVAL = float(os.environ.get("MISTRAL_DELEGATE_WATCH_INTERVAL", 3))
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
    run.add_argument("--max-tool-calls", type=int, help="Stop Mistral after this many tool calls (enforced by the wrapper).")
    run.add_argument("--token-budget", type=int,
                     help="Stop Mistral after this many effective tokens (enforced by the wrapper).")
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
    manage.add_argument("--skip-out-of-scope", action="store_true",
                        help="With --adopt: apply only changes inside the --scope and leave the rest out.")
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


def worktree_scope(run: dict) -> list[str]:
    """The combined scope of every run on this run's worktree; [] if any of them had no scope (no limit)."""
    path = (run.get("worktree") or {}).get("path")
    runs = [r for r in ledger.load_runs().values() if path and (r.get("worktree") or {}).get("path") == path] or [run]
    if any(not r.get("scope") for r in runs):
        return []
    return list(dict.fromkeys(entry for r in runs for entry in r["scope"]))


def runs_on_worktree(run: dict) -> list[dict]:
    """This run plus the runs that share its worktree (a run and its resumes), not yet settled."""
    path = (run.get("worktree") or {}).get("path")
    if not path:
        return [run]
    return [r for r in ledger.load_runs().values()
            if r["id"] == run["id"] or ((r.get("worktree") or {}).get("path") == path and not r.get("outcome"))]


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
    # A resumed run may have a narrower scope than the run it continues; the worktree holds both.
    scope = worktree_scope(run)
    if scope and not args.include_out_of_scope:
        changed = gitops.changed_files(wt)
        if paths:
            changed = [f for f in changed if any(f == p or f.startswith(p.rstrip("/") + "/") for p in paths)]
        skipped = [f for f in changed if not vibe.matches_scope(f, scope)]
        if skipped and not args.skip_out_of_scope:
            print(f"Nothing applied: {len(skipped)} changed file(s) in this worktree are outside the scope of its "
                  f"run(s) ({', '.join(scope)}):")
            print("\n".join(f"  {f}" for f in skipped))
            print("Look at them, then run --adopt again with --include-out-of-scope to apply them too, or "
                  "--skip-out-of-scope to leave them out.")
            return 1
        if skipped:
            paths = [f for f in changed if vibe.matches_scope(f, scope)]
            if not paths:
                print("Every change is outside the scope (" + ", ".join(scope) + "); nothing applied.")
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
    for sibling in runs_on_worktree(run):
        ledger.append({"event": "outcome", "id": sibling["id"], "outcome": outcome, "paths": paths, "note": args.note})
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
    for sibling in runs_on_worktree(run):
        ledger.append({"event": "outcome", "id": sibling["id"], "outcome": "discarded", "note": args.note})
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
        self.unfinished = ""
        self.limit_note = ""
        self.effective = 0
        self.live_tool_calls = 0
        self.guard_log: Path | None = None
        self.currency = "$"
        self.session_id: str | None = args.resume

    def call_vibe(self, cmd: list[str], cwd: str, *, token_budget: int, max_tool_calls: int,
                  max_price: float | None = None, model_hint: str | None = None) -> None:
        """Run one Vibe call and stop it when it goes over this call's caps: `token_budget` effective
        tokens, `max_tool_calls` tool calls, and `max_price` if set. Vibe can't enforce these itself
        (no price for some models, and its turn limit counts prompts), so the wrapper watches the
        session's usage and the guard's log while Vibe runs."""
        watcher = vibe.SessionWatcher(cwd, time.time(), session_id=self.session_id, model_hint=model_hint)
        base = watcher.poll() if self.session_id else None  # a resumed session already has usage
        guard_base = len(guard.read_log(self.guard_log)) if self.guard_log else 0
        out_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        err_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=out_file, stderr=err_file, stdin=subprocess.DEVNULL, text=True)
        started, stopped, snap = time.monotonic(), "", None

        def measure():
            use = vibe.usage(snap, base) if snap else None
            effective = use["effective"] if use else 0
            calls = max(snap["tool_calls"] - (base or {}).get("tool_calls", 0) if snap else 0,
                        len(guard.read_log(self.guard_log)) - guard_base if self.guard_log else 0)
            cost, fallback = vibe.snapshot_cost(snap, base, fallback=True) if snap else (0.0, False)
            return use, effective, calls, cost, fallback

        while proc.poll() is None:
            time.sleep(WATCH_INTERVAL)
            snap = watcher.poll() or snap
            _use, effective, calls, cost, fallback = measure()
            if time.monotonic() - started > self.args.timeout:
                stopped = "timeout"
            elif effective > token_budget:
                stopped = "budget_exceeded"
                self.limit_note = (f"stopped Mistral at {effective:,} effective tokens, over this call's budget "
                                   f"of {token_budget:,}")
            elif max_price is not None and cost > max_price:
                stopped = "budget_exceeded"
                self.limit_note = (f"stopped Mistral at ~{self.currency}{cost:.2f}, over this call's max_price of "
                                   f"{self.currency}{max_price:.2f}"
                                   + (" (priced at mistral-medium-3.5 rates: the model's price is unknown)"
                                      if fallback else ""))
            elif calls > max_tool_calls:
                stopped = "tool_call_limit"
                self.limit_note = f"stopped Mistral after {calls} tool calls, over the cap of {max_tool_calls}"
            if stopped:
                proc.terminate()
                try:
                    proc.wait(10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                break
        snap = watcher.poll() or snap
        _use, effective, calls, cost, fallback = measure()
        self.effective += effective
        self.live_tool_calls += calls
        self.session_id = self.session_id or watcher.session_id
        out_file.seek(0)
        err_file.seek(0)
        proc = subprocess.CompletedProcess(cmd, proc.returncode, out_file.read(), err_file.read())
        out_file.close()
        err_file.close()
        if stopped == "timeout":
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
        self.unfinished = unfinished_reason(this_turn(history), info["final_text"])
        if stopped:
            self.status = stopped
        elif proc.returncode == 0:
            self.status = "ok"
        elif (proc.returncode == 1 and not proc.stdout.strip() and self.stderr
              and not re.match(r"(Error|Teleport error):", self.stderr) and "Traceback" not in self.stderr):
            # Vibe reports a hit limit by printing the last assistant text to stderr, unprefixed.
            self.status = "limit_reached"
            self.final_text = self.stderr
        else:
            self.status = "error"


CONTINUE_PROMPT = ("You stopped before finishing. Continue the task from where you left off, then end with "
                   "the summary: every file you changed and why, and anything you couldn't do.")

READ_ONLY_TOOL = re.compile(r"read|grep|glob|search|todo|list|view", re.I)

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


def unfinished_reason(turn: list, final_text: str) -> str:
    """Why the turn looks cut off (no closing summary, or a summary that stops mid-sentence), or ""."""
    if not turn:
        return ""
    entries = [e for e in turn if isinstance(e, dict) and e.get("type") in ("message", "effect")]
    # Trailing read-only calls (re-reading a file, updating a todo list) after the summary don't matter.
    while entries and entries[-1].get("type") == "effect" and READ_ONLY_TOOL.search(vibe._effect_tool(entries[-1])):
        if any(e.get("type") == "message" and e.get("role") == "assistant" and len(vibe.text_of(e)) >= 80
               for e in entries):
            entries.pop()
        else:
            break
    last = entries[-1] if entries else None
    if last and last.get("type") == "effect":
        return "Vibe's last step was a tool call, not a closing summary; the run may have stopped early."
    text = final_text.rstrip()
    if text and (text.endswith(("…", "...", ":", ",", ";", "(")) or text.endswith(("and", "the", "to"))):
        return "Mistral's final message stops mid-sentence; the run may have been cut short."
    return ""


TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|specs?)/|[._-](test|spec)s?\.[a-z]+$|(^|/)test_[^/]+\.py$|"
                       r"_test\.(py|go)$|(^|/)conftest\.py$")
TEST_COMMAND = re.compile(r"\b(test|tests|pytest|vitest|jest|mocha|ava|spec|unittest|phpunit|rspec)\b")


def test_strength(wt: dict, run_dir: str, commands: list[str], baseline: dict, deps_mode: str, timeout: int) -> str:
    """Run Mistral's new and changed tests against the original code, on a clean copy.

    Tests that still pass there don't exercise the change: a guarded assertion, the
    wrong object under test. A failure is what's expected; its output says whether it's
    a real assertion failure or just a missing import.
    """
    files = gitops.changed_files(wt)
    tests = [f for f in files if TEST_FILE.search(f)]
    code = [f for f in files if f not in tests]
    # Test commands whose baseline passed: a command that already failed tells nothing.
    test_cmds = [c for c in commands if TEST_COMMAND.search(c) and baseline.get(c, (0, ""))[0] == 0]
    if not tests or not code or not test_cmds:
        return ""
    gitops.stage_changes(wt)
    try:
        patch = gitops.git_checked(wt["path"], "diff", "--cached", "--binary", wt["base"], "--", *tests)
        with gitops.clean_copy(wt, deps_mode) as clean:
            if patch.strip():
                gitops.git_checked(clean, "apply", "--whitespace=nowarn", "-", input=patch)
            results = run_checks(test_cmds, str(clean / os.path.relpath(run_dir, wt["path"])), timeout)
    except DelegateError as e:
        return f"test_strength: not checked ({e})"
    passing = [c for c, (code_, _o) in results.items() if code_ == 0]
    if passing:
        return ("test_strength_warning: Mistral's tests still pass on the original code, without its changes to "
                + ", ".join(code[:5]) + (" …" if len(code) > 5 else "") + f" ({', '.join(passing)}). They don't "
                "test the change: look for guarded or missing assertions, or the wrong thing under test.")
    first = next(iter(results.items()))
    last = [ln.strip() for ln in first[1][1].splitlines() if ln.strip()][-1:] or ["no output"]
    return (f"test_strength: Mistral's tests fail on the original code, as they should ({first[0]}: {last[0][:200]}). "
            "If that's an import or setup error rather than an assertion, it proves less.")


def measure_baseline(commands: list[str], cwd: str, timeout: int, flaky: list[str]) -> dict:
    """Run checks before Mistral changes anything; a failing check is rerun once (flaky ones pass then)."""
    baseline = run_checks(commands, cwd, timeout)
    failing = [cmd for cmd, (code, _out) in baseline.items() if code != 0]
    if failing:
        rerun = run_checks(failing, cwd, timeout)
        flaky += [cmd for cmd, (code, _out) in rerun.items() if code == 0]
        baseline.update(rerun)
    return baseline


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
    vibe.EXTRA_PRICES.update(settings["model_prices"])
    vibe.WEIGHTS.update(settings["token_weights"])
    if args.policy:
        settings["policy"] = args.policy
    caps = config.caps(settings, args.mode)
    for key in ("max_turns", "max_price", "max_tokens", "max_tool_calls", "token_budget"):
        if getattr(args, key) is not None:
            caps[key] = getattr(args, key)
    model = args.model or settings["model"]
    write = args.mode == "write"
    verify = [] if (args.no_verify or not write) else (
        [{"cmd": c, "paths": []} for c in args.verify] if args.verify else settings["verify"])
    allow_commands = list(dict.fromkeys(settings["allow_commands"] + args.allow_command)) if write else []
    # `npm test` also covers `npm run test`, `npx vitest` (when that's the test script), etc.
    settings["allow_commands_as_given"] = allow_commands
    allow_commands = cmdforms.expand(allow_commands, top or workdir) if allow_commands else []
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
        # New files named literally in the scope get their folders up front, so writing them can't fail on that.
        for entry in scope:
            if not any(c in entry for c in "*?[") and not entry.endswith("/"):
                target = Path(wt["path"], entry)
                if not target.exists() and Path(wt["path"]) in target.parents:
                    target.parent.mkdir(parents=True, exist_ok=True)

    continues = None
    if wt and wt["reused"]:
        same = [r for r in ledger.load_runs().values()
                if (r.get("worktree") or {}).get("path") == wt["path"]]
        continues = max(same, key=lambda r: r.get("started") or 0)["id"] if same else None
    settings["continues"] = continues
    # A follow-up is listed under the task it continues, not under its resume message.
    original = (ledger.load_runs().get(continues) or {}).get("task") if continues else None
    label = f"{original.split(' [follow-up')[0]} [follow-up]" if original else task
    ledger.append({"event": "start", "id": run_id, "pid": os.getpid(), "mode": args.mode, "kind": kind,
                   "repo": Path(top or workdir).name, "workdir": workdir, "task": label[:500],
                   "policy": settings["policy"], "model": model, "scope": scope, "continues": continues,
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
                       allow_commands, scope, fix_attempts, baseline_on, kind, guard_log, guard_warning, guard_root)
    finally:
        guard.remove_policy(run_dir, run_id, config.home())


def execute(args, settings, run_id, run_dir, wt, top, workdir, task, spec, caps, model, write, verify,
            allow_commands, scope, fix_attempts, baseline_on, kind, guard_log, guard_warning, guard_root) -> int:
    started_wall, started = time.time(), time.monotonic()
    # Only the checks that concern this run: ones limited to paths outside the scope are skipped.
    planned = [c for c in verify if vibe.check_applies(c["paths"], scope, [])]
    commands = [c["cmd"] for c in planned]
    # Run the checks once before Mistral changes anything, so failures that were
    # already there (or come from the worktree environment) aren't blamed on it.
    # A failing check is rerun once, so a flaky one isn't reported as broken.
    baseline, flaky, baseline_source = {}, [], ""
    if commands and baseline_on:
        stored = {c: tuple(v) for c, v in (gitops.load_state(wt["path"]).get("baseline") or {}).items()} if wt else {}
        if wt and wt["reused"]:
            # This worktree already holds Mistral's earlier changes. Use the baseline from before them,
            # and run any check missing from it on a clean copy of the original snapshot.
            baseline = {c: stored[c] for c in commands if c in stored}
            missing = [c for c in commands if c not in stored]
            if missing:
                rel = os.path.relpath(run_dir, wt["path"])
                try:
                    with gitops.clean_copy(wt, settings["deps_mode"]) as clean:
                        baseline.update(measure_baseline(missing, str(clean / rel), args.verify_timeout, flaky))
                except DelegateError as e:
                    baseline_source = f"could not build a clean copy for the baseline ({e}); those checks have none"
            baseline_source = baseline_source or "from before Mistral's earlier changes in this worktree"
        else:
            baseline = measure_baseline(commands, run_dir, args.verify_timeout, flaky)
        if wt:
            gitops.save_state(wt["path"], {"baseline": {**stored, **{c: list(v) for c, v in baseline.items()}}})
    preexisting = [cmd for cmd, (code, _out) in baseline.items() if code != 0]

    prompt = vibe.build_prompt(task, mode=args.mode, spec=spec, context=args.context, verify=commands,
                               allow_commands=settings.get("allow_commands_as_given") or allow_commands, allow_shell=args.allow_shell, scope=scope,
                               preexisting_failures=preexisting, root=guard_root,
                               cwd=os.path.realpath(run_dir))
    # `sed` is safe once the guard vets each call (print-only scripts, no -i); Vibe matches allowlist
    # entries as prefixes, so `sed -nE` or `sed -E -n` need the bare `sed`. Without the guard, keep Vibe's default.
    # Likewise the runners of allowed commands (`uv run`, `npm`, ...): Vibe only matches prefixes, so
    # `uv run --no-sync pytest` would otherwise need approval; the guard vets what the runner runs.
    runners = [r for r in dict.fromkeys(guard.runner_of(c) for c in allow_commands) if r]
    vibe_allow = allow_commands + (["sed", *runners] if write and not guard_warning else [])
    agent = args.agent or vibe.write_agent_profile(args.mode, model, vibe_allow)
    trust = args.trust or wt is not None

    run = Run(args)
    stats_before = vibe.read_session_stats(args.resume, run_dir, model_hint=model) if args.resume else None

    run.guard_log = None if guard_warning else guard_log
    run.currency = settings["currency"]

    def vibe_call(text: str, share: float) -> None:
        """One Vibe call with `share` of the caps, enforced by the wrapper. If a money cap is set, Vibe gets
        it too; its --max-price counts the whole session, so a resumed session gets what it already spent on top."""
        vibe_caps = dict(caps)
        if caps.get("max_price") is not None:
            session_cost = vibe.stats_cost(vibe.read_session_stats(run.session_id, run_dir, model_hint=model)) \
                if run.session_id else 0.0
            vibe_caps["max_price"] = (session_cost or 0.0) + caps["max_price"] * share
        run.call_vibe(vibe.build_command(args.vibe_bin, text, mode=args.mode, agent=agent, caps=vibe_caps,
                                         allow_shell=args.allow_shell, trust=trust, resume=run.session_id,
                                         extra=settings["vibe_args"]), run_dir,
                      token_budget=int(caps["token_budget"] * share),
                      max_tool_calls=max(1, int(caps["max_tool_calls"] * share)),
                      max_price=caps["max_price"] * share if caps.get("max_price") is not None else None,
                      model_hint=model)

    vibe_call(prompt, 1.0)
    continued, summary_note = 0, ""

    verification, results, failures, attempts = "not_run", {}, [], 0
    # Checks limited to paths that Mistral ended up changing outside the scope join in now.
    changed_so_far = (gitops.changed_files(wt) if wt else [])
    commands += [c["cmd"] for c in verify if c not in planned and vibe.check_applies(c["paths"], [], changed_so_far)]
    skipped_checks = [c["cmd"] for c in verify if c["cmd"] not in commands]
    autofixes = [c["cmd"] for c in settings["autofix"] if vibe.check_applies(c["paths"], scope, changed_so_far)]
    autofixed: list[str] = []

    def check_round() -> None:
        nonlocal results, failures
        results = run_checks(commands, run_dir, args.verify_timeout)
        failures = new_failures(results, baseline)
        if failures and autofixes:
            # Formatting-type failures are fixed by a command, not by another Mistral round.
            for command in autofixes:
                code, out = run_checks([command], run_dir, args.verify_timeout)[command]
                tail_lines = [ln.strip() for ln in out.splitlines() if ln.strip()][-2:]
                autofixed.append(command + ("" if code == 0 else
                                            f" (exit {code}: {' / '.join(tail_lines)[:240] or 'no output'})"))
            results = run_checks(commands, run_dir, args.verify_timeout)
            failures = new_failures(results, baseline)

    checkable = run.status in ("ok", "limit_reached", "budget_exceeded", "tool_call_limit")
    if commands and checkable:
        check_round()
    def missing_new_files() -> list[str]:
        """Files named literally in --scope that still don't exist: the task asked for them."""
        base = wt["path"] if wt else (top or workdir)
        return [e for e in scope if not any(c in e for c in "*?[") and not e.endswith("/")
                and not Path(base, e).exists()]

    missing = missing_new_files() if write else []
    if run.status == "ok" and (run.unfinished or missing):
        changed_now = gitops.changed_files(wt) if wt else [ln[3:] for ln in git(workdir, "status", "--porcelain").splitlines()]
        # Asking Mistral to finish costs a round; it's only worth it when the work looks unfinished:
        # files the task named are missing, checks fail, nothing changed, or there are no checks to tell.
        follow_up = None
        if missing:
            follow_up = (f"These files from your task don't exist yet: {', '.join(missing)}. Create them as the "
                         "task describes, then end with the summary of every file you changed.")
        elif commands and not failures and changed_now:
            summary_note = ("Mistral ended without a closing summary, but its changes pass the checks, so it "
                            "wasn't asked to finish (that would cost a round). Read the diff instead.")
        else:
            follow_up = CONTINUE_PROMPT
        if follow_up and run.session_id and settings["continue_attempts"] > 0:
            continued = 1
            vibe_call(follow_up, 0.5)
            if commands:
                check_round()
            missing = missing_new_files()
    if commands and checkable:
        # A run stopped at a cap still gets its fix round (by default): its first pass is spent,
        # and the failures are often small.
        while failures and attempts < fix_attempts and run.session_id and (
                run.status == "ok" or (settings["fix_after_cap"] and run.status in ("budget_exceeded", "tool_call_limit"))):
            attempts += 1
            vibe_call(vibe.fix_prompt(failures, scope), 0.5)
            check_round()
        if failures:
            verification = "failed"
        elif any(code != 0 for code, _out in results.values()):
            verification = "passed_except_preexisting"
        else:
            verification = "passed"
    strength_line = ""
    if (settings["test_strength"] and wt and verification in ("passed", "passed_except_preexisting")
            and run.status in ("ok", "budget_exceeded", "tool_call_limit")):
        strength_line = test_strength(wt, run_dir, commands, baseline, settings["deps_mode"], args.verify_timeout)
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
    elif write and run.status == "ok" and missing:
        run.status = "incomplete"
    guard_events = guard.read_log(guard_log)

    lines = [f"run_id: {run_id}", f"status: {run.status}"]
    if run.status == "no_changes":
        lines.append("note: Mistral finished without changing any file. Read its result below to see why.")
    if run.status == "incomplete":
        lines.append("missing_files (named in --scope but never created; passing checks don't cover them): "
                     + ", ".join(missing))
    if run.status in ("budget_exceeded", "tool_call_limit"):
        lines.append(f"note: the wrapper {run.limit_note}. The work so far is in the worktree: review it, "
                     "--resume with a higher --max-price/--max-tool-calls, or discard it.")
    if settings.get("continues"):
        lines.append(f"continues: {settings['continues']} (same worktree). This run's id and the earlier one "
                     "both refer to everything in the worktree; --adopt or --discard either settles both.")
    if continued:
        lines.append("continued: Mistral's work looked unfinished (missing files, failing checks, or no closing "
                     "summary), so it was asked once to finish.")
    if run.status == "stopped_by_refusal":
        lines.append("note: Vibe ended the session after a refused tool call (it treats a refused approval as "
                     "the user cancelling). " + ("The guard hook was not active: " + guard_warning if guard_warning
                     else "The guard hook should prevent this; check guard below.")
                     + " Resume with --resume to let Mistral continue.")
    if commands:
        detail = f"after {attempts} fix attempt{'s' if attempts != 1 else ''}" if attempts else "first try"
        if verification == "not_run":
            lines.append("verification: not run (Vibe did not finish)")
        else:
            lines.append(f"verification: {verification} ({detail})\n" + "\n".join(check_lines(results, baseline)))
    if skipped_checks:
        lines.append("checks_skipped (limited to paths this run doesn't touch): " + ", ".join(skipped_checks))
    if flaky:
        lines.append("flaky_checks (failed, then passed on a rerun before Mistral started): " + ", ".join(flaky))
    if strength_line:
        lines.append(strength_line)
    if summary_note:
        lines.append("note: " + summary_note)
    elif run.unfinished:
        lines.append("final_message_warning: " + run.unfinished)
    if baseline_source:
        lines.append("baseline: " + baseline_source)
    if preexisting:
        lines.append("baseline_warning: these checks already failed in the untouched "
                     + ("worktree" if wt else "checkout") + " before Mistral changed anything: "
                     + ", ".join(preexisting) + ". Mistral was told not to work around them. If they pass "
                     "in your checkout, the cause is the worktree environment (see deps_mode); if they fail there "
                     "too, it may be your own uncommitted changes, which the worktree starts from.")
    ran_model = (use or {}).get("model") or model
    lines.append(f"mode: {args.mode}, kind: {kind}, policy: {settings['policy']}, model: "
                 + (f"{ran_model}" if ran_model else "unknown")
                 + ("" if model else " (Vibe's default; not pinned)"))
    if ran_model and not model and not vibe.is_mistral_model(ran_model):
        lines.append(f"model_note: Vibe's server-side default routed this run to {ran_model!r}, not a Mistral model. "
                     "To use Mistral, set model = \"mistral-medium-3.5\" in .mistral-delegate.toml.")
    lines.append(usage_line(use, settings["currency"]))
    lines.append(f"budget: {run.effective:,} effective tokens used across this run's Vibe calls; the wrapper caps the "
                 f"first pass at {caps['token_budget']:,} effective tokens and {caps['max_tool_calls']} tool calls"
                 + (f" and {settings['currency']}{caps['max_price']:.2f}" if caps.get("max_price") else "")
                 + ", and a continuation or fix round at half of that")
    month_line = credit_line(settings, use)
    if month_line:
        lines.append(month_line)
    tool_calls = max(run.tool_calls, run.live_tool_calls, len(guard_events))
    steps = use.get("steps") if use else None
    lines.append(f"elapsed: {elapsed:.0f}s, tool calls: {tool_calls}"
                 + (f", model steps: {steps} (max_turns {caps['max_turns']})" if steps is not None else ""))
    if autofixed:
        lines.append("autofix: ran " + ", ".join(autofixed) + " before deciding on a fix round")
    lines.append(guard_line(guard_events, tool_calls, guard_warning))
    model_warning = vibe.unknown_model_warning(model) if not args.agent else None
    if model_warning:
        lines.append(f"model_warning: {model_warning}")
    if allow_commands:
        given = settings["allow_commands_as_given"]
        extra = [c for c in allow_commands if c not in given]
        lines.append("vibe_may_run: " + ", ".join(given)
                     + (f" (also accepted: {', '.join(extra[:8])}{', …' if len(extra) > 8 else ''})" if extra else ""))
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
        # A resumed run's scope adds to the scopes of the runs it continues.
        combined = worktree_scope({"worktree": {"path": wt["path"]}, "scope": scope})
        out_of_scope = [f for f in files if combined and not vibe.matches_scope(f, combined)]
        if out_of_scope:
            lines.insert(2, "out_of_scope_changes (outside the scope of this worktree's runs; --adopt stops until "
                            "you choose --include-out-of-scope or --skip-out-of-scope): " + ", ".join(out_of_scope))
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
    notices = [n for n in run.notices if "Rewrote tool_input" not in n]
    rewrites = len(run.notices) - len(notices)
    if rewrites:
        notices.append(f"{rewrites} tool-input rewrite notice(s) from the guard's path corrections (see guard: above)")
    if notices:
        lines.append("vibe_notices:\n" + "\n".join(f"  - {n}" for n in notices[:10]))
    if run.stderr and run.status != "ok" and run.stderr.strip() != run.final_text.strip():
        lines.append("stderr:\n" + truncate(run.stderr, 2000))
    lines.append("\n--- result from Mistral Vibe ---\n" + (truncate(run.final_text) if run.final_text else "(no final message)"))

    report = "\n".join(lines)
    # What delegating cost Claude (writing the task and spec, reading this report) against what doing it
    # itself would have taken (Mistral's work, scaled by claude_relative_effort). Recorded for --stats.
    overhead = vibe.effective_tokens(len(report) // 4, 0, (len(task) + len(spec or "")) // 4)
    equivalent = int((use["effective"] if use else run.effective) * settings["claude_relative_effort"])
    ledger.save_report(run_id, report)
    ledger.append({"event": "end", "id": run_id, "status": run.status, "verification": verification,
                   "fix_attempts_used": attempts, "cost": use["cost"] if use else None,
                   "model": ran_model, "tokens_in": use["tokens_in"] if use else None,
                   "tokens_out": use["tokens_out"] if use else None, "cached": use["cached"] if use else None,
                   "effective": use["effective"] if use else run.effective,
                   "claude_overhead": overhead, "claude_equivalent": equivalent,
                   "cost_estimated": use["estimated"] if use else None,
                   "steps": steps, "tool_calls": tool_calls, "tokens": use["tokens"] if use else None,
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


def usage_line(use: dict | None, currency: str) -> str:
    if not use:
        return ("usage: unknown (no token data in Vibe's session storage; if this keeps happening, set "
                "vibe_args = [\"--legacy-harness\"] in the config)")
    fresh = use["tokens_in"] - use["cached"]
    line = (f"usage: {use['effective']:,} effective tokens (input {fresh:,} fresh + {use['cached']:,} cached, "
            f"output {use['tokens_out']:,})")
    if use["cost"] is None:
        line += (f"; cost unknown: no price for model {use.get('model')!r} (add model_prices = "
                 f"{{ \"{use.get('model')}\" = [input, output, cached] }} per million tokens to price it, "
                 "earlier runs included)")
    else:
        line += f"; ~{currency}{use['cost']:.4f} at {use.get('model')} list prices"
        if use["session_total"] is not None:
            line += f" (session total ~{currency}{use['session_total']:.4f})"
    return line


def credit_line(settings: dict, use: dict | None) -> str | None:
    """Month-to-date use of the subscription's Vibe credit, this run included (it is recorded after)."""
    credit = settings.get("monthly_credit")
    if not credit:
        return None
    spent, unpriced, since = ledger.month_spend(ledger.load_runs(), settings["credit_reset_day"], vibe.model_prices())
    if use and use["cost"] is not None:
        spent += use["cost"]
    elif use:
        unpriced += 1
    c = settings["currency"]
    return (f"credit: ~{c}{spent:.2f} of {c}{credit:.2f} used since {since} ({spent / credit:.0%})"
            + (f"; {unpriced} run(s) without a price aren't included" if unpriced else ""))


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
    if files:
        diff = gitops.changes_diff(wt)
        n = diff.count("\n")
        # The full diff is kept with the run's report, so it can still be read after --adopt removes the worktree.
        diff_file = ledger.runs_dir() / f"{run_id}.diff"
        try:
            diff_file.parent.mkdir(parents=True, exist_ok=True)
            diff_file.write_text(diff, encoding="utf-8")
        except OSError:
            diff_file = None
        if 0 < n <= diff_lines:
            lines.append(f"diff:\n```diff\n{diff.rstrip()}\n```")
        else:
            lines.append(f"diff: {n} lines, too long to show here. Read it from "
                         + (f"{diff_file}" if diff_file else
                            f"git -C {shlex.quote(wt['path'])} diff --cached {wt['base'][:12]}"))
        if diff_file and 0 < n <= diff_lines:
            lines.append(f"diff_file: {diff_file}")
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
            settings = config.load(gitops.toplevel(str(Path(args.workdir).resolve())) or args.workdir)
            vibe.EXTRA_PRICES.update(settings["model_prices"])
            print(ledger.format_stats(ledger.load_runs(), prices=vibe.model_prices(), currency=settings["currency"],
                                      min_savings=settings["min_savings"]))
            line = credit_line(settings, None)
            if line:
                print(line)
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
