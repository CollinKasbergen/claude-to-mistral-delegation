#!/usr/bin/env python3
"""Hand a task to Mistral's Vibe CLI and print a compact report for Claude Code.

Running a task:
  delegate.py --mode read  "Find every place that parses config.toml"
  delegate.py --mode write --kind tests --verify "npm test" "Add unit tests for src/slugify.ts"
  delegate.py --mode write --kind feature --spec plan.md --context src/api/users.ts \\
      --allow-command "npm test" --verify "npm test" --verify "npx tsc --noEmit" -
  delegate.py --mode write --worktree-name mistral-ab12cd34 --resume <session-id> "Also cover empty strings"

Several steps at once (one plan file, one report):
  delegate.py --plan plan.md           run every step, merge them, check the result together
  delegate.py --integrate PLAN_ID      merge again after resuming a step
  delegate.py --adopt PLAN_ID [--steps a,b]

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
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from mdelegate import commands as cmdforms, config, gitops, guard, ledger, vibe  # noqa: E402
from mdelegate.checks import (EDITABLE_SOURCES, CheckResult, check_lines, measure_baseline, new_failures,  # noqa: E402
                              python_path_env, run_checks, stop_process_group)
from mdelegate.gitops import DelegateError  # noqa: E402

KINDS = ("tests", "feature", "bugfix", "refactor", "migration", "boilerplate", "docs", "search", "other",
         "integration")
MAX_RESULT_CHARS = 12_000
def _env_number(name: str, default, cast=int):
    try:
        return cast(os.environ.get(name) or default)
    except ValueError:
        return default


WATCH_INTERVAL = _env_number("MISTRAL_DELEGATE_WATCH_INTERVAL", 3.0, float)
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
    run.add_argument("--timeout", type=int, default=_env_number("MISTRAL_DELEGATE_TIMEOUT", 900),
                     help="Seconds before a Vibe call is killed (default 900).")
    run.add_argument("--diff-lines", type=int, default=300,
                     help="Include the full diff in the report when it is at most this many lines (0: never).")
    run.add_argument("--vibe-bin", default=os.environ.get("VIBE_BIN", "vibe"))
    run.add_argument("--via-worker", action="store_true",
                     help="Set by the mistral-worker subagent, so --stats can tell its runs apart.")
    run.add_argument("--plan-step", metavar="PLAN:STEP", help=argparse.SUPPRESS)  # set by the plan runner

    plans = p.add_argument_group("plans (several steps in one call)")
    plans.add_argument("--plan", metavar="FILE",
                       help="Run every step of a plan file: steps in parallel worktrees (after the steps they "
                            "depend on), each checked, then merged and checked together. One report.")
    plans.add_argument("--steps", metavar="ID,ID",
                       help="With --plan: run only these steps. With --adopt PLAN: apply only these steps.")
    plans.add_argument("--integrate", metavar="PLAN_ID",
                       help="Merge a plan's steps again (after you resumed one) and rerun the combined checks.")

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


# --- managing runs -------------------------------------------------------------

def find_run(run_id: str) -> dict:
    run = ledger.load_runs().get(run_id)
    if not run:
        raise DelegateError(f"No run with id {run_id!r}. See --status.")
    return run


def worktree_scope(run: dict) -> list[str]:
    """The combined scope of every run on this run's worktree; [] if any of them had no scope (no limit)."""
    runs = runs_on_worktree(run) if (run.get("worktree") or {}).get("path") else [run]
    if run.get("id") is None:  # the report of a run not recorded under an id here: add its own scope
        runs = runs + [run]
    if any(not r.get("scope") for r in runs):
        return []
    return list(dict.fromkeys(entry for r in runs for entry in r["scope"]))


def runs_on_worktree(run: dict) -> list[dict]:
    """This run plus the runs that share its worktree (a run and its resumes), not yet settled."""
    path = (run.get("worktree") or {}).get("path")
    if not path:
        return [run]
    return [r for r in ledger.load_runs().values()
            if r["id"] == run.get("id") or ((r.get("worktree") or {}).get("path") == path and not r.get("outcome"))]


def refuse_while_running(run: dict) -> None:
    """--adopt and --discard wait for every run on the worktree: a resume may still be working in it."""
    busy = [r["id"] for r in runs_on_worktree(run) if ledger.state(r) == "running"]
    if busy:
        raise DelegateError(("That run is still going" if busy == [run["id"]] else
                             f"{', '.join(busy)} is still working in this run's worktree")
                            + ". Wait for it to finish (see --status).")


def plan_of(run: dict) -> str | None:
    """The plan a run belongs to: a step's run, or a follow-up of one in the step's worktree."""
    return next((r.get("plan") for r in runs_on_worktree(run) if r.get("plan")), None)


def cmd_adopt(args: argparse.Namespace) -> int:
    run = find_run(args.adopt)
    if run.get("mode") == "plan":
        from mdelegate import plans
        return plans.adopt(args, run, script_cmd)
    if (plan := plan_of(run)):
        raise DelegateError(f"{run['id']} is part of plan {plan}. Adopt the plan: --adopt {plan} (add --steps to "
                            f"take only some steps; after resuming a step, run --integrate {plan} first).")
    refuse_while_running(run)
    wt = run.get("worktree")
    if not wt:
        if run.get("mode") != "write":
            raise DelegateError("Read-only runs have nothing to adopt.")
        ledger.append({"event": "outcome", "id": run["id"], "outcome": "adopted", "note": args.note})
        print(f"Marked {run['id']} as adopted (it edited your checkout directly).")
        return 0
    if not os.path.isdir(wt["path"]):
        raise DelegateError(f"The worktree {wt['path']} no longer exists.")
    # An earlier --adopt --keep-worktree moved the base past what it applied.
    wt = dict(wt, base=gitops.load_state(wt["path"]).get("base") or wt["base"])
    paths = args.paths
    skipped: list[str] = []
    # A resumed run may have a narrower scope than the run it continues; the worktree holds both.
    scope = worktree_scope(run)
    if scope and not args.include_out_of_scope:
        # --paths are git pathspecs (`.`, `src/*`): check exactly the files they select.
        changed = gitops.changed_files(wt, paths)
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
    partial = bool(args.paths or skipped)
    if args.keep_worktree or partial:
        # What was applied becomes the worktree's new base, so a later adopt (after a resume) applies only
        # what came after it.
        gitops.commit_applied(wt, files)
        print(f"Worktree kept at {wt['path']}"
              + (f" with the changes you didn't apply; --discard {run['id']} removes it." if partial else "."))
    else:
        gitops.remove_worktree(wt)
        print("Worktree removed.")
    print("The changes are uncommitted in your checkout; review and commit them as usual.")
    return 0


def cmd_discard(args: argparse.Namespace) -> int:
    run = find_run(args.discard)
    if run.get("mode") == "plan":
        from mdelegate import plans
        return plans.discard(args, run)
    if (plan := plan_of(run)):
        raise DelegateError(f"{run['id']} is part of plan {plan}. Discard the plan: --discard {plan}.")
    refuse_while_running(run)
    wt = run.get("worktree")
    if wt and os.path.isdir(wt["path"]):
        gitops.remove_worktree(wt)
    if run.get("outcome") == "adopted_partial":
        # The rest of a partly adopted run: the track record keeps it as partly adopted.
        print(f"Removed the worktree of {run['id']} (its adopted part stays in your checkout).")
        return 0
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
        self.limit_flag = ""
        self.effective = 0
        self.live_tool_calls = 0
        self.guard_log: Path | None = None
        self.currency = "$"
        self.session_id: str | None = args.resume
        self.run_id = ""
        self.env: dict | None = None
        self.spent = 0.0  # estimated cost of the Vibe calls so far, for a run that is interrupted
        self.unmetered = False  # a Vibe call whose session (and so its token use) was never found
        self.last_snap: dict | None = None
        self.guard_policy: dict | None = None

    def call_vibe(self, cmd: list[str], cwd: str, *, token_budget: int, max_tool_calls: int,
                  max_price: float | None = None, model_hint: str | None = None) -> None:
        """Run one Vibe call and stop it when it goes over this call's caps: `token_budget` effective
        tokens, `max_tool_calls` tool calls, and `max_price` if set. Vibe can't enforce these itself
        (no price for some models, and its turn limit counts prompts), so the wrapper watches the
        session's usage and the guard's log while Vibe runs."""
        watcher = vibe.SessionWatcher(cwd, time.time(), session_id=self.session_id, model_hint=model_hint,
                                      marker=self.run_id)
        base = watcher.poll() if self.session_id else None  # a resumed session already has usage
        guard_base = len(guard.read_log(self.guard_log)) if self.guard_log else 0
        out_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        err_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        # Vibe gets its own process group, so stopping it also stops the tests and servers it started.
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=out_file, stderr=err_file, stdin=subprocess.DEVNULL, text=True,
                                encoding="utf-8", errors="replace", env=self.env, start_new_session=True)
        started, stopped, snap = time.monotonic(), "", None

        def measure():
            use = vibe.usage(snap, base) if snap else None
            effective = use["effective"] if use else 0
            calls = max(snap["tool_calls"] - (base or {}).get("tool_calls", 0) if snap else 0,
                        len(guard.read_log(self.guard_log)) - guard_base if self.guard_log else 0)
            cost, fallback = vibe.snapshot_cost(snap, base, fallback=True) if snap else (0.0, False)
            return use, effective, calls, cost, fallback

        try:
            while proc.poll() is None:
                time.sleep(WATCH_INTERVAL)
                snap = watcher.poll() or snap
                _use, effective, calls, cost, fallback = measure()
                if time.monotonic() - started > self.args.timeout:
                    stopped = "timeout"
                elif effective > token_budget:
                    stopped = "budget_exceeded"
                    self.limit_note = (f"stopped Mistral at {effective:,} effective tokens, over this call's "
                                       f"token_budget of {token_budget:,}")
                    self.limit_flag = "--token-budget"
                elif max_price is not None and cost > max_price:
                    stopped = "budget_exceeded"
                    self.limit_note = (f"stopped Mistral at ~{self.currency}{cost:.2f}, over this call's max_price "
                                       f"of {self.currency}{max_price:.2f}"
                                       + (" (priced at mistral-medium-3.5 rates: the model's price is unknown)"
                                          if fallback else ""))
                    self.limit_flag = "--max-price"
                elif calls > max_tool_calls:
                    stopped = "tool_call_limit"
                    self.limit_note = f"stopped Mistral after {calls} tool calls, over the cap of {max_tool_calls}"
                    self.limit_flag = "--max-tool-calls"
                if stopped:
                    break
        finally:
            # Stopped at a cap, or the wrapper itself is being stopped: end Vibe and everything it started.
            stop_process_group(proc)
            snap = watcher.poll() or snap
            _use, effective, calls, cost, fallback = measure()
            self.effective += effective
            self.live_tool_calls += calls
            self.spent += cost or 0.0
            if snap is None:
                self.unmetered = True
            elif not self.session_id:
                self.last_snap = snap
        self.session_id = self.session_id or watcher.found_id
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
        # A refused approval in programmatic mode cancels the session. Only this turn counts: a resumed
        # session prints its whole history, earlier cancellations included.
        turn_text = json.dumps(this_turn(history)) if history else proc.stdout
        self.cancelled = self.cancelled or bool(CANCELLED.search(turn_text) or CANCELLED.search(proc.stderr))
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

SHELL_TOOLS = {"bash", "shell", "sh", "run command", "run_command", "terminal", "exec"}

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
    if not text:
        return "Mistral ended without a final message, so there's no summary of what it did; read the diff."
    if text.endswith(("…", "...", ":", ",", ";", "(")) or re.search(r"\b(and|the|to)$", text):
        return "Mistral's final message stops mid-sentence; the run may have been cut short."
    return ""


TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|specs?)/|[._-](test|spec)s?\.[a-z]+$|(^|/)test_[^/]+\.py$|"
                       r"_test\.(py|go)$|(^|/)conftest\.py$")
# Changed files that count as code under test: source files, not docs, configs, lockfiles or fixtures.
CODE_FILE = re.compile(r"\.(py|pyi|ts|tsx|js|jsx|mjs|cjs|vue|svelte|go|rs|java|kt|kts|rb|php|cs|swift|c|cc|cpp|h|hpp|"
                       r"m|mm|scala|ex|exs|clj|dart|lua|sql)$")
NOT_CODE = re.compile(r"(^|/)(testdata|fixtures?|__snapshots__|__mocks__|migrations)/|\.config\.[a-z]+$|"
                      r"(^|/)(setup|conftest|manage)\.py$")
# Test runners, recognised after runner options are dropped and script spellings normalised.
TEST_RUNNERS = {"pytest", "vitest", "jest", "mocha", "ava", "rspec", "phpunit", "tox", "nox", "karma", "jasmine",
                "playwright", "cypress"}
# Runners that accept test files as arguments, so only Mistral's changed tests need to run.
FILE_ARG_RUNNERS = {"pytest", "vitest", "jest"}


def _test_runner(command: str, extra: list[str]) -> str | None:
    """The test runner a check command invokes (None for lint, type checks, `test -f` and the like)."""
    if any(command == e or command.startswith(e + " ") for e in extra):
        return "configured"
    for segment in re.split(r"&&|\|\||;|\|", command):
        words = guard.normalize_command(segment.split())
        if not words:
            continue
        if words[0] in ("npx", "bunx") or words[:2] in (["pnpm", "exec"], ["npm", "exec"]) or \
                words[:2] in (["uv", "run"], ["poetry", "run"], ["pdm", "run"], ["hatch", "run"]):
            words = words[2:] if words[0] in ("pnpm", "npm", "uv", "poetry", "pdm", "hatch") else words[1:]
        if words[:2] in (["python", "-m"], ["python3", "-m"]):
            words = words[2:]
        if not words:
            continue
        if words[0] in TEST_RUNNERS or words[0] == "unittest":
            return words[0]
        if words[:2] in (["go", "test"], ["cargo", "test"], ["dotnet", "test"], ["mix", "test"]):
            return " ".join(words[:2])
        if len(words) >= 3 and words[0] in ("npm", "pnpm", "yarn", "bun") and words[1] == "run" and \
                (words[2] == "test" or words[2].startswith("test:")):
            return f"{words[0]} run {words[2]}"
    return None


def test_strength(wt: dict, run_dir: str, checks: list[dict], baseline: dict, settings: dict, timeout: int) -> str:
    """Run Mistral's new and changed tests against the original code, on a clean copy.

    Tests that still pass there don't exercise the change: a guarded assertion, the
    wrong object under test. Only test commands whose baseline passed and that concern
    the changed tests are used; pytest, vitest and jest run just the changed test files.
    """
    files = gitops.changed_files(wt)
    tests = [f for f in files if TEST_FILE.search(f)]
    code = [f for f in files if f not in tests and CODE_FILE.search(f) and not NOT_CODE.search(f)]
    if not tests or not code:
        return ""
    selected = []
    for check in checks:
        runner = _test_runner(check["cmd"], settings["test_commands"])
        measured = check["cmd"] in baseline and baseline[check["cmd"]][0] == 0
        if runner and measured and vibe.check_applies(check["paths"], [], tests):
            selected.append((check["cmd"], runner))
    if not selected:
        return ""
    rel_dir = os.path.relpath(run_dir, wt["path"])
    commands = []
    for command, runner in selected:
        if runner in FILE_ARG_RUNNERS and not re.search(r"(^|\s)--(\s|$)|[;&|<>]", command):
            args = [os.path.relpath(os.path.join(wt["path"], t), run_dir) for t in tests]
            commands.append((command, command + " " + " ".join(shlex.quote(a) for a in args)))
        else:
            commands.append((command, command))
    gitops.stage_changes(wt)
    try:
        patch = gitops.git_checked(wt["path"], "diff", *gitops.DIFF_OPTS, "--cached", "--binary", wt["base"],
                                   "--", *tests)
        with gitops.clean_copy(wt, settings["deps_mode"]) as clean:
            if patch.strip():
                gitops.git_checked(clean, "apply", "--whitespace=nowarn", "-", input=patch)
            cwd = clean / rel_dir
            if not cwd.is_dir():
                return f"test_strength: not checked ({rel_dir} doesn't exist in the original code)"
            results = run_checks([run for _orig, run in commands], str(cwd), timeout)
    except (DelegateError, OSError) as e:
        return f"test_strength: not checked ({e})"
    timed_out = [run for run, (code_, _o) in results.items() if code_ == 124]
    if timed_out:
        return f"test_strength: not checked ({', '.join(timed_out)} timed out on the original code)"
    failing = [(run, out) for run, (code_, out) in results.items() if code_ != 0]
    if not failing:
        # Every relevant suite passed without Mistral's code changes: its tests didn't notice them.
        return ("test_strength_warning: Mistral's tests still pass on the original code, without its changes to "
                + ", ".join(code[:5]) + (" …" if len(code) > 5 else "")
                + f" ({', '.join(results)}). They don't test the change: look for guarded or missing assertions, "
                "or the wrong thing under test.")
    shown = []
    for run, out in failing[:2]:
        last = [ln.strip() for ln in out.splitlines() if ln.strip()][-1:] or ["no output"]
        shown.append(f"{run}: {last[0][:160]}")
    return ("test_strength: Mistral's tests fail on the original code, as they should (" + "; ".join(shown)
            + "). If that's an import or setup error rather than an assertion, it proves less.")


def run_task(args: argparse.Namespace) -> int:
    workdir = str(Path(args.workdir).resolve())
    if args.task == "-":
        args.task = sys.stdin.read()
    spec = None
    if args.spec:
        try:
            spec = gitops.find_document(args.spec, gitops.toplevel(workdir), "spec").read_text(encoding="utf-8")
        except OSError as e:
            raise DelegateError(f"Can't read --spec file: {e}") from e
    task = (args.task or "").strip() or ("Implement the spec below." if spec else "")
    if not task:
        raise DelegateError("No task given. Pass it as an argument, '-' for stdin, or use --spec.")
    if args.mode == "read" and (args.verify or args.in_place or args.allow_command or args.scope):
        raise DelegateError("--verify, --allow-command, --scope and --in-place only apply to --mode write.")

    if not Path(workdir).is_dir():
        raise DelegateError(f"--workdir {workdir} doesn't exist.")
    top = gitops.toplevel(workdir)
    if top:
        gitops.work_dir(top)  # keeps the plans and specs folder out of git from the first run on
    settings = config.load(top or workdir)
    if settings["errors"]:
        # Running without the project's settings would also run without its checks and caps.
        raise DelegateError("Fix the config first (nothing was run):\n" + "\n".join(f"  {e}" for e in settings["errors"]))
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

    vibe_path = shutil.which(args.vibe_bin) or (args.vibe_bin if Path(args.vibe_bin).is_file() else None)
    if vibe_path is None:
        print("status: error\n\nVibe CLI not found. Install it with `uv tool install mistral-vibe` "
              "(or `pip install mistral-vibe`), then run `vibe --setup` once to store your API key.")
        return 2
    if not os.access(vibe_path, os.X_OK):
        raise DelegateError(f"{vibe_path} isn't executable (chmod +x it, or point VIBE_BIN at the vibe CLI).")
    if write and not args.in_place and not top:
        print("status: error\n\nWrite mode needs a git repository for worktree isolation. "
              "Pass --in-place to let Vibe edit the directory directly.")
        return 2

    prefix = "read" if not write else ("inplace" if args.in_place else "mistral")
    run_id = f"{prefix}-{uuid.uuid4().hex[:8]}"
    plan_id, _, plan_step = (args.plan_step or "").partition(":")
    start = {"event": "start", "id": run_id, "time": time.time(), "pid": os.getpid(),
             "pid_started": ledger.process_started(os.getpid()), "mode": args.mode, "kind": kind,
             "repo": Path(top or workdir).name, "workdir": workdir, "task": task[:500], "policy": settings["policy"],
             "model": model, "scope": scope, "continues": None, "worktree": None,
             **({"plan": plan_id, "step": plan_step} if args.plan_step else {}),
             **({"via": "worker"} if args.via_worker else {})}
    # Counting the running delegations and recording this one happen under one lock, so two runs
    # started at the same moment can't both slip under max_parallel.
    with ledger.locked():
        # A plan's own record doesn't take a slot: its steps do.
        active = [r for r in ledger.running(ledger.load_runs()) if r.get("mode") != "plan"]
        if len(active) >= settings["max_parallel"]:
            raise DelegateError(f"{len(active)} delegations are already running (max_parallel = "
                                f"{settings['max_parallel']}): {', '.join(r['id'] for r in active)}. "
                                "Wait for one to finish (see --status) or raise max_parallel in the config.")
        ledger.append(start)

    run_dir, wt = workdir, None
    if write and not args.in_place:
        try:
            wt = gitops.prepare_worktree(top, args.worktree_name or run_id, snapshot=not args.no_snapshot,
                                         link_deps=not args.no_link_deps, extra_links=args.link,
                                         deps_mode=deps_mode, worktrees_dir=settings["worktrees_dir"])
        except DelegateError as e:
            ledger.append({"event": "end", "id": run_id, "status": "error", "verification": "not_run",
                           "error": f"worktree: {e}"[:300]})
            print(f"status: error\n\nCould not prepare the worktree: {e}")
            return 2
        if plan_step and plan_step != "integration":
            wt["reused"] = False  # the plan runner made this step's worktree for this run
        if not Path(wt["path"], Path(workdir).relative_to(top)).is_dir():
            if not wt["reused"]:
                gitops.remove_worktree(wt)
            ledger.append({"event": "end", "id": run_id, "status": "error", "verification": "not_run",
                           "error": "workdir missing in worktree"})
            print(f"status: error\n\n{Path(workdir).relative_to(top)} isn't in the worktree: it is untracked or "
                  "ignored in your checkout. Run from a tracked folder, or drop --no-snapshot.")
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
    parent = (ledger.load_runs().get(continues) or {}) if continues else {}
    original = parent.get("task")
    label = f"{original.split(' [follow-up')[0]} [follow-up]" if original else task
    # A follow-up of a plan step belongs to the plan too: it's adopted with the plan, after --integrate.
    if parent.get("plan") and not args.plan_step:
        start.update(plan=parent["plan"], step=parent.get("step"))
    settings["plan"] = start.get("plan")
    # The full start record, now that the worktree and the run it continues are known.
    ledger.append(dict(start, task=label[:500], continues=continues,
                       worktree={k: wt[k] for k in ("name", "path", "toplevel", "base", "links")} if wt else None))

    # The guard hook refuses disallowed tool calls with an error Mistral can work around,
    # instead of letting Vibe's approval prompt cancel the session.
    guard_root = os.path.realpath(wt["path"] if wt else (top or workdir))
    guard_log = ledger.run_file(run_id, "guard.jsonl")
    # What Mistral was asked, kept with the run: a spec written to a scratch folder may not survive.
    ledger.run_file(run_id, "spec.md").write_text(f"# Task\n\n{task}\n" + (f"\n# Spec\n\n{spec}\n" if spec else ""),
                                               encoding="utf-8")
    guard_warning = guard.install(vibe.vibe_home(), config.home())
    run = Run(args)
    run.run_id = run_id
    # Vibe passes its environment on to hooks: the guard finds this run's policy by this id.
    EDITABLE_SOURCES[:] = gitops.editable_sources(top, wt.get("links") or []) if wt else []
    run.env = dict(python_path_env(wt["path"] if wt else None) or os.environ, **{guard.RUN_ENV: run_id})
    if not guard_warning:
        default_cmds = vibe.DEFAULT_BASH_ALLOWLIST if write else []
        run.guard_policy = {
            "run_id": run_id, "root": guard_root, "mode": args.mode, "scope": scope,
            "allow_commands": default_cmds + allow_commands, "default_commands": default_cmds,
            "allow_shell": args.allow_shell, "log": str(guard_log),
            # Dependency folders and --link paths come from the checkout (links, hard links): never written to.
            "protected": list((wt or {}).get("links") or []),
        }
    # A stopped wrapper (a timeout in the calling tool, a closed terminal) still ends Vibe and records the run.
    previous = {sig: signal.signal(sig, _exit_on_signal) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        return execute(args, settings, run_id, run_dir, wt, top, workdir, task, spec, caps, model, write, verify,
                       allow_commands, scope, fix_attempts, baseline_on, kind, guard_log, guard_warning, guard_root,
                       run)
    except BaseException as e:
        interrupted = isinstance(e, (SystemExit, KeyboardInterrupt))
        ledger.append({"event": "end", "id": run_id, "status": "interrupted" if interrupted else "error",
                       "verification": "not_run", "cost": run.spent or None, "cost_estimated": True,
                       "effective": run.effective, "session_id": run.session_id,
                       "error": (f"{e.__class__.__name__}: {e}")[:300]})
        if interrupted or isinstance(e, DelegateError):
            raise
        print(f"status: error\n\nrun_id: {run_id}\nUnexpected error: {e.__class__.__name__}: {e}\n"
              + "".join(traceback.format_exception(type(e), e, e.__traceback__)[-3:]).rstrip())
        return 2
    finally:
        guard.remove_policy(run_dir, run_id, config.home())
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _exit_on_signal(signum, _frame) -> None:
    raise SystemExit(128 + signum)


def execute(args, settings, run_id, run_dir, wt, top, workdir, task, spec, caps, model, write, verify,
            allow_commands, scope, fix_attempts, baseline_on, kind, guard_log, guard_warning, guard_root,
            run: "Run") -> int:
    started = time.monotonic()
    # Only the checks that concern this run: ones limited to paths outside the scope are skipped.
    planned = [c for c in verify if vibe.check_applies(c["paths"], scope, [])]
    commands = [c["cmd"] for c in planned]
    # Run the checks once before Mistral changes anything, so failures that were
    # already there (or come from the worktree environment) aren't blamed on it.
    # A failing check is rerun once, so a flaky one isn't reported as broken.
    baseline, flaky, baseline_source = {}, [], ""
    if commands and baseline_on:
        stored = {c: CheckResult.load(v) for c, v in (gitops.load_state(wt["path"]).get("baseline") or {}).items()} \
            if wt else {}
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
            gitops.save_state(wt["path"], {"baseline": {c: v.stored() for c, v in {**stored, **baseline}.items()}})
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

    stats_before = vibe.read_session_stats(args.resume, run_dir, model_hint=model) if args.resume else None

    run.guard_log = None if guard_warning else guard_log
    run.currency = settings["currency"]

    def vibe_call(text: str, share: float) -> None:
        """One Vibe call with `share` of the caps, enforced by the wrapper. If a money cap is set, Vibe gets
        it too; its --max-price counts the whole session, so a resumed session gets what it already spent on top."""
        if run.guard_policy:
            # Active for this call only: a Vibe that outlives the wrapper finds no live policy and is refused.
            guard.write_policy(run_dir, dict(run.guard_policy, expires=time.time() + args.timeout + 300),
                               config.home())
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

    # In place, only what changes from now on is Mistral's: the checkout may hold the user's own uncommitted work.
    inplace_root = (top or workdir) if write and not wt else None
    inplace_before = gitops.checkout_state(inplace_root) if inplace_root else {}

    def inplace_changes() -> list[str]:
        return gitops.changed_since(inplace_root, inplace_before) if inplace_root else []

    vibe_call(prompt if args.resume else prompt + "\n\n" + vibe.run_marker(run_id), 1.0)
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
        changed_now = gitops.changed_files(wt) if wt else inplace_changes()
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
        strength_line = test_strength(wt, run_dir, [c for c in verify if c["cmd"] in commands], baseline,
                                      settings, args.verify_timeout)
    elapsed = time.monotonic() - started

    # Without a session id (Vibe was stopped before printing one), only the snapshot the watcher
    # identified counts; the newest session in the directory may be a parallel run's.
    stats_after = (vibe.read_session_stats(run.session_id, run_dir, model_hint=model) if run.session_id
                   else run.last_snap)
    run.session_id = run.session_id or (stats_after or {}).get("session_id")
    use = vibe.usage(stats_after, stats_before)

    files_now = gitops.changed_files(wt) if wt else inplace_changes()
    if run.cancelled and run.status in ("ok", "error"):
        run.status = "stopped_by_refusal"
    elif write and run.status == "ok" and not files_now:
        run.status = "no_changes"
    elif write and run.status == "ok" and missing:
        run.status = "incomplete"
    guard_events = guard.read_log(guard_log)

    lines = [f"run_id: {run_id}", f"status: {run.status}"]
    for warning in settings["warnings"]:
        lines.append(f"config_warning: {warning}")
    if run.unmetered and (run.tool_calls or run.live_tool_calls or run.final_text):  # Vibe did work unwatched
        lines.append("budget_warning: the wrapper couldn't find this run's Vibe session, so its token and price caps "
                     "weren't enforced for at least one call (tool calls were still capped by the guard, and turns "
                     "by Vibe). Check session_logging in Vibe's config.toml.")
    if run.status == "no_changes":
        lines.append("note: Mistral finished without changing any file. Read its result below to see why.")
    if run.status == "incomplete":
        lines.append("missing_files (named in --scope but never created; passing checks don't cover them): "
                     + ", ".join(missing))
    if run.status in ("budget_exceeded", "tool_call_limit"):
        lines.append(f"note: the wrapper {run.limit_note}. "
                     + ("The work so far is in the worktree: review it, --resume with a higher " if wt else
                        "Review what it found, or --resume with a higher ")
                     + (run.limit_flag or "--token-budget") + (", or discard it." if wt else "."))
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
            lines += worktree_section(wt, run_id, files, out_of_scope, args.diff_lines, plan=settings.get("plan") or "")
    elif write:
        files = files_now
        out_of_scope = [f for f in files if scope and not vibe.matches_scope(f, scope)]
        lines.append("changed_files (in your checkout, by this run; your earlier uncommitted changes aren't "
                     "listed):\n" + ("\n".join(f"  {f}" for f in files) or "  (none)"))
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
    # Shell commands are refused for not being allowed; other tools (an edit, a write) for other reasons.
    denied_shell = [d for d in vibe_denied if d.split(":", 1)[0].strip().lower() in SHELL_TOOLS]
    denied_other = [d for d in vibe_denied if d not in denied_shell]
    if denied_shell:
        counts = Counter(denied_shell)
        why = ("refused because they aren't in allow_commands; add them if Mistral needs them" if write
               else "read mode runs no commands")
        lines.append(f"denied_commands ({why}):\n"
                     + "\n".join(f"  - {cmd}" + (f"  ({n}x)" if n > 1 else "") for cmd, n in counts.most_common(15)))
    if denied_other:
        counts = Counter(denied_other)
        lines.append("refused_tool_calls (Vibe's own permissions refused these; they aren't shell commands):\n"
                     + "\n".join(f"  - {call}" + (f"  ({n}x)" if n > 1 else "") for call, n in counts.most_common(15)))
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
    # A plan step's report is read by the plan runner, not Claude: the plan records Claude's overhead.
    overhead = 0 if args.plan_step else vibe.effective_tokens(len(report) // 4, 0, (len(task) + len(spec or "")) // 4)
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


def worktree_section(wt: dict, run_id: str, files: list[str], out_of_scope: list[str], diff_lines: int,
                     plan: str = "") -> list[str]:
    lines = [f"worktree_name: {wt['name']}" + ("  (reused)" if wt["reused"] else ""),
             f"worktree_path: {wt['path']}"]
    snap = wt.get("snapshot")
    if snap:
        lines.append(f"worktree_base: snapshot of your uncommitted work ({snap['modified']} modified, "
                     f"{snap['untracked']} untracked files) at {wt['base'][:12]}")
    else:
        lines.append(f"worktree_base: your HEAD at {wt['base'][:12]}")
    if wt.get("links"):
        labels = {"clone": "copy-on-write clones", "hardlink": "hard-linked copies", "copy": "copies",
                  "symlink": "symlinks"}
        methods = wt.get("deps_methods") or {}
        groups: dict[str, list[str]] = {}
        for link in wt["links"]:
            how = methods.get(link) or wt.get("deps_mode")
            groups.setdefault(labels.get(how, "linked"), []).append(link)
        for label, links in groups.items():
            lines.append(f"dependencies ({label} from your checkout): " + ", ".join(links))
    for note in wt.get("notes") or []:
        lines.append(f"dependency_note: {note}")
    if EDITABLE_SOURCES:
        lines.append("python_path: your virtualenv installs " + ", ".join(EDITABLE_SOURCES) + " in editable mode "
                     "from your checkout; checks and Mistral's commands put the worktree's copy first on PYTHONPATH, "
                     "so they test Mistral's code")
    stat = gitops.changes_stat(wt)
    lines.append("changes_by_vibe:\n" + (stat or "  (none)"))
    if files:
        diff = gitops.changes_diff(wt)
        n = diff.count("\n")
        # The full diff is kept with the run's report, so it can still be read after --adopt removes the worktree.
        diff_file = ledger.run_file(run_id, "changes.diff")
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
        if plan:
            lines.append(f"part_of_plan: {plan} (adopt or discard the plan, not this step; after a follow-up, "
                         f"run --integrate {plan} first)")
            return lines
        lines.append(f"adopt_with: {script_cmd('--adopt', run_id)}   (add --paths ... to take only some files)")
    if plan:
        lines.append(f"part_of_plan: {plan} (adopt or discard the plan, not this step)")
        return lines
    lines.append(f"discard_with: {script_cmd('--discard', run_id, '--note', 'why')}")
    return lines


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    try:
        if args.status:
            top = gitops.toplevel(str(Path(args.workdir).resolve()))
            currency = config.load(top or args.workdir)["currency"]
            print(ledger.format_status(ledger.load_runs(), currency=currency))
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
        if args.plan or args.integrate:
            from mdelegate import plans
            return plans.main(args, SCRIPT, KINDS, script_cmd)
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
