"""Running a plan: several delegated steps from one plan file, in one call.

Each step is an ordinary write run of delegate.py (guard, caps, checks and fix
rounds included) in its own worktree, started by this runner as a child process:
up to max_parallel at a time, each after the steps it depends on, starting from
their results. Steps that succeed are committed on their branch and merged into
the plan's integration worktree, where the checks run once more on the combined
result; if only the combination fails, one Mistral run fixes it there. Claude
reads one report and adopts or discards the plan as a whole (or some steps).
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import checks, config, gitops, ledger, vibe
from .checks import CheckResult, check_lines, measure_baseline, new_failures, run_checks, stop_process_group
from .gitops import DelegateError
from .plan import Plan, PlanError, Step, parse, select, step_spec

BASELINE_WARNING = re.compile(r"baseline_warning: these checks already failed in the untouched \w+ before Mistral "
                              r"changed anything: (.*?)\. Mistral was told")
POLL = max(0.05, min(1.0, float(os.environ.get("MISTRAL_DELEGATE_WATCH_INTERVAL") or 1.0)))
# Report lines from a step worth repeating in the plan's report.
STEP_NOTE_PREFIXES = ("out_of_scope_changes", "test_strength_warning", "final_message_warning", "missing_files",
                      "baseline_warning", "budget_warning", "note:", "denied_commands", "refused_tool_calls",
                      "assertion_hint", "test_strength:",
                      "project_rules: none", "test_cases_warning")
SUCCESS_VERIFICATIONS = ("passed", "passed_except_preexisting", "not_run")


def _exit_on_signal(signum, _frame) -> None:
    raise SystemExit(128 + signum)


def step_succeeded(record: dict) -> bool:
    return record.get("status") == "ok" and record.get("verification") in SUCCESS_VERIFICATIONS


def branch(plan_id: str, step_id: str) -> str:
    return f"{plan_id}-{step_id}"


class PlanRun:
    """One execution of a plan (or a re-integration of one)."""

    def __init__(self, args, script: Path, settings: dict, top: str, workdir: str, plan: Plan, steps: list[Step],
                 plan_id: str, script_cmd):
        self.args, self.script, self.settings, self.top, self.workdir = args, script, settings, top, workdir
        self.plan, self.steps, self.plan_id, self.script_cmd = plan, steps, plan_id, script_cmd
        self.rel = os.path.relpath(workdir, top)
        self.deps_mode = args.deps_mode or settings["deps_mode"]
        self.int_wt: dict | None = None
        self.step_wts: dict[str, dict] = {}
        self.results: dict[str, dict] = {}
        self.children: dict[str, tuple] = {}  # step id -> (process, log file handle, log path)
        self.started = time.monotonic()
        self.all_runs: list[dict] = []
        self.notes: list[str] = []
        scoped = all(s.scope for s in steps)
        self.scope = list(dict.fromkeys(e for s in steps for e in s.scope)) if scoped else []

    # --- children ---------------------------------------------------------------------

    def passthrough(self) -> list[str]:
        a, out = self.args, []
        for flag, attr in (("--model", "model"), ("--policy", "policy"), ("--timeout", "timeout"),
                           ("--verify-timeout", "verify_timeout"), ("--fix-attempts", "fix_attempts"),
                           ("--deps-mode", "deps_mode"), ("--max-turns", "max_turns"), ("--max-price", "max_price"),
                           ("--max-tokens", "max_tokens"), ("--max-tool-calls", "max_tool_calls"),
                           ("--token-budget", "token_budget"), ("--vibe-bin", "vibe_bin"), ("--agent", "agent")):
            value = getattr(a, attr, None)
            if value is not None:
                out += [flag, str(value)]
        for flag, attr in (("--no-link-deps", "no_link_deps"), ("--no-baseline", "no_baseline"),
                           ("--no-verify", "no_verify")):
            if getattr(a, attr, False):
                out.append(flag)
        for link in a.link:
            out += ["--link", link]
        for command in a.allow_command:
            out += ["--allow-command", command]
        return out

    def child_command(self, *, worktree: str, kind: str, spec: Path, step_tag: str, task: str, scope: list[str],
                      verify: list[str], allow: list[str], context: list[str], add_verify: list[str] = (),
                      extra: list[str] = ()) -> list[str]:
        cmd = [sys.executable, str(self.script), "--mode", "write", "--workdir", self.workdir,
               "--worktree-name", worktree, "--kind", kind, "--spec", str(spec), "--plan-step", step_tag,
               "--diff-lines", "0", *self.passthrough(), *extra]
        # A step's own checks come on top of the configured ones (lint, type check), which still apply.
        for flag, values in (("--scope", scope), ("--verify", verify), ("--add-verify", add_verify),
                             ("--allow-command", allow), ("--context", context)):
            for value in values:
                cmd += [flag, value]
        return cmd + [task]

    def spawn(self, key: str, cmd: list[str]) -> None:
        log_path = ledger.run_file(self.plan_id, f"{key}.log")
        log = log_path.open("w", encoding="utf-8")
        proc = subprocess.Popen(cmd, cwd=self.workdir, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        self.children[key] = (proc, log, log_path)

    def collect(self, key: str) -> dict | str:
        """A finished child's result, or "retry" when it couldn't start because too many runs are going."""
        proc, log, log_path = self.children.pop(key)
        log.close()
        out = log_path.read_text(encoding="utf-8", errors="replace")
        match = re.search(r"^run_id: (\S+)", out, re.M)
        if not match:
            if proc.returncode == 2 and "already running" in out:
                return "retry"
            last = [ln for ln in out.strip().splitlines() if ln.strip()][-3:]
            return {"ok": False, "status": "error", "note": " / ".join(last)[:300] or "no output"}
        run_id = match.group(1)
        record = ledger.load_runs().get(run_id, {})
        return {"ok": step_succeeded(record), "status": record.get("status") or "died",
                "verification": record.get("verification"), "run_id": run_id, "record": record,
                "report": ledger.read_report(run_id) or ""}

    def stop_children(self) -> None:
        for proc, log, _path in list(self.children.values()):
            try:
                os.killpg(proc.pid, signal.SIGTERM)  # each child stops its Vibe and records itself as interrupted
            except OSError:
                pass
        deadline = time.monotonic() + 60
        for proc, log, _path in list(self.children.values()):
            try:
                proc.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                stop_process_group(proc, grace=1)
            log.close()
        self.children.clear()

    # --- steps --------------------------------------------------------------------------

    def start_step(self, step: Step) -> dict | None:
        """Make the step's worktree and start its run. Returns a result right away if it can't start."""
        start = self.int_wt["base"] if not step.depends else branch(self.plan_id, step.depends[0])
        merge = [branch(self.plan_id, d) for d in step.depends[1:]]
        try:
            wt = gitops.prepare_worktree(self.top, branch(self.plan_id, step.id), snapshot=False,
                                         link_deps=not self.args.no_link_deps, extra_links=self.args.link,
                                         deps_mode=self.deps_mode, worktrees_dir=self.settings["worktrees_dir"],
                                         start=start, merge=merge)
        except DelegateError as e:
            return {"ok": False, "status": "blocked", "note": f"couldn't start from the steps it needs: {e}"}
        self.step_wts[step.id] = wt
        ledger.append({"event": "update", "id": self.plan_id, "step_worktrees": {
            k: {f: v[f] for f in ("name", "path", "toplevel", "base", "links")} for k, v in self.step_wts.items()}})
        spec = ledger.run_file(self.plan_id, f"{step.id}.spec.md")
        spec.write_text(step_spec(self.plan, step), encoding="utf-8")
        kind = step.kind or self.plan.kind or "other"
        task = f"{self.plan.title} [{step.id}]: {step.summary()}"
        self.spawn(step.id, self.child_command(
            worktree=wt["name"], kind=kind, spec=spec, step_tag=f"{self.plan_id}:{step.id}", task=task,
            scope=step.scope, verify=[], add_verify=step.verify, allow=step.allow, context=step.context))
        return None

    def finish_step(self, step: Step, result: dict) -> None:
        if result.get("ok") and step.id in self.step_wts:
            # Steps that depend on this one start from this commit, and the integration merges it.
            gitops.commit_all(self.step_wts[step.id], f"mistral-delegate: {self.plan_id} step {step.id}")
        self.results[step.id] = result
        ledger.append({"event": "update", "id": self.plan_id, "step_runs": {
            k: r.get("run_id") for k, r in self.results.items() if r.get("run_id")}})

    def run_steps(self) -> None:
        """Run every step that has no result yet (all of them, or what an earlier run left undone)."""
        pending = [s for s in self.steps if s.id not in self.results]
        retry_at: dict[str, float] = {}
        limit = max(1, self.settings["max_parallel"])
        while pending or self.children:
            for key in [k for k, (proc, _l, _p) in self.children.items() if proc.poll() is not None]:
                result = self.collect(key)
                step = self.plan.step(key)
                if result == "retry":
                    pending.insert(0, step)
                    retry_at[key] = time.monotonic() + max(1.0, 10 * POLL)  # other runs hold the slots
                    continue
                self.finish_step(step, result)
            for step in list(pending):
                failed = [d for d in step.depends if d in self.results and not self.results[d]["ok"]]
                if failed:
                    pending.remove(step)
                    self.results[step.id] = {"ok": False, "status": "skipped",
                                             "note": f"needs {', '.join(failed)}, which didn't succeed"}
            for step in list(pending):
                if len(self.children) >= limit:
                    break
                if not all(d in self.results and self.results[d]["ok"] for d in step.depends):
                    continue
                if retry_at.get(step.id, 0) > time.monotonic():
                    continue
                pending.remove(step)
                immediate = self.start_step(step)
                if immediate:
                    self.finish_step(step, immediate)
            time.sleep(POLL)

    # --- integration ------------------------------------------------------------------

    def integrate(self) -> dict:
        """Merge the steps that succeeded, run the checks on the result, and fix it once if only it fails."""
        merged, problems = [], {}
        for step in self.steps:
            result = self.results.get(step.id) or {}
            if not result.get("ok"):
                continue
            missing = [d for d in step.depends if d not in merged]
            if missing:
                problems[step.id] = f"not merged: needs {', '.join(missing)}, which wasn't merged"
                continue
            conflicts = gitops.merge_branch(self.int_wt["path"], branch(self.plan_id, step.id),
                                            f"mistral-delegate: merge {self.plan_id} step {step.id}")
            if conflicts:
                problems[step.id] = "not merged: conflicts with the steps merged before it in " + ", ".join(conflicts[:8])
            else:
                merged.append(step.id)
        out = {"merged": merged, "problems": problems, "results": {}, "baseline": {}, "failures": [],
               "verification": "not_run", "fix": None, "commands": []}
        if not merged:
            return out
        changed = gitops.changed_files(self.int_wt)
        if self.args.no_verify:
            commands = []
        else:
            # The plan's own checks (and --verify) add to the rest, as --verify does in a single run: every
            # check a merged step had (each must still hold together) and the configured checks that concern
            # the changed files.
            commands = list(dict.fromkeys([
                *(self.args.verify or []), *self.plan.verify,
                *[c for s in merged for c in self.plan.step(s).verify],
                *[c["cmd"] for c in self.settings["verify"] if vibe.check_applies(c["paths"], [], changed)]]))
        # One merged step is that step's own result, already checked; the combination needs its own check.
        if not commands or (len(merged) < 2 and not self.plan.verify and not self.args.verify):
            out["verification"] = "not_run" if not commands else "same_as_step"
            return out
        out["commands"] = commands
        run_dir = str(Path(self.int_wt["path"], self.rel))
        timeout = self.args.verify_timeout
        baseline = dict(self.baseline_for(commands))
        results = run_checks(commands, run_dir, timeout)
        failures = new_failures(results, baseline)
        fix_attempts = self.settings["fix_attempts"] if self.args.fix_attempts is None else self.args.fix_attempts
        if failures and fix_attempts > 0:
            out["fix"] = self.fix_integration(failures, commands, merged)
            results = run_checks(commands, run_dir, timeout)
            failures = new_failures(results, baseline)
        out.update(results=results, baseline=baseline, failures=failures)
        out["verification"] = ("failed" if failures else "passed_except_preexisting"
                               if any(code for code, _o in results.values()) else "passed")
        return out

    def use_worktree_code(self) -> None:
        """Checks on the merged result import its code, not the checkout's: a virtualenv installed in
        editable mode (uv sync, pip install -e) points into the user's checkout."""
        checks.EDITABLE_SOURCES[:] = gitops.editable_sources(self.top, self.int_wt.get("links") or [])

    def baseline_for(self, commands: list[str]) -> dict:
        """Check results on the plan's starting code: from steps that started there, else a clean copy."""
        baseline = {c: CheckResult.load(v) for c, v in
                    (gitops.load_state(self.int_wt["path"]).get("baseline") or {}).items()}
        for step in self.steps:
            wt = self.step_wts.get(step.id)
            if not step.depends and wt and os.path.isdir(wt["path"]):
                for command, value in (gitops.load_state(wt["path"]).get("baseline") or {}).items():
                    baseline.setdefault(command, CheckResult.load(value))
        missing = [c for c in commands if c not in baseline]
        if missing and self.settings["baseline"] and not self.args.no_baseline:
            try:
                with gitops.clean_copy(self.int_wt, self.deps_mode) as clean:
                    baseline.update(measure_baseline(missing, str(clean / self.rel), self.args.verify_timeout, []))
            except DelegateError:
                pass
        gitops.save_state(self.int_wt["path"], {"baseline": {c: v.stored() for c, v in baseline.items()}})
        return baseline

    def fix_caps(self) -> list[str]:
        """The integration fix gets half a write run's caps: it repairs, it doesn't build."""
        caps = config.caps(self.settings, "write")
        out = []
        for flag, key in (("--token-budget", "token_budget"), ("--max-tool-calls", "max_tool_calls")):
            if getattr(self.args, key, None) is None:
                out += [flag, str(max(1, int(caps[key]) // 2))]
        return out

    def fix_integration(self, failures: list, commands: list[str], merged: list[str]) -> dict:
        lines = [f"# Plan: {self.plan.title}",
                 "The steps of this plan were done separately and are now merged in your working directory:",
                 "\n".join(f"- {s}: {self.plan.step(s).summary()}" for s in merged)]
        if self.plan.shared:
            lines.append("## Shared context\n\n" + self.plan.shared)
        lines.append("## What's wrong\n\nEach step passed its own checks, but together they don't:\n\n"
                     + vibe.fix_prompt(failures))
        spec = ledger.run_file(self.plan_id, "integration.spec.md")
        spec.write_text("\n\n".join(lines) + "\n", encoding="utf-8")
        # The merged steps' scope: files named by steps that didn't merge aren't expected to exist.
        scope = [] if not all(self.plan.step(s).scope for s in merged) else list(dict.fromkeys(
            e for s in merged for e in self.plan.step(s).scope))
        self.spawn("integration", self.child_command(
            worktree=self.plan_id, kind="integration", spec=spec, step_tag=f"{self.plan_id}:integration",
            task=f"Make the merged steps of plan '{self.plan.title}' pass their checks together",
            scope=scope, verify=commands, allow=[], context=[], extra=self.fix_caps()))
        while self.children["integration"][0].poll() is None:
            time.sleep(POLL)
        result = self.collect("integration")
        return result if isinstance(result, dict) else {"ok": False, "status": "error", "note": "couldn't start"}

    # --- report -----------------------------------------------------------------------

    def report(self, integration: dict) -> tuple[str, dict]:
        merged = integration["merged"]
        runs = [r for r in self.results.values() if r.get("run_id")]
        if integration.get("fix") and integration["fix"].get("run_id"):
            runs.append(integration["fix"])
        all_merged = len(merged) == len(self.steps)
        verification = integration["verification"]
        if verification == "same_as_step":
            verification = (self.results[merged[0]].get("verification") or "not_run") if merged else "not_run"
        # ok means every step merged and the merged result passes its checks.
        status = ("failed" if not merged else "partial" if not all_merged
                  else "checks_failed" if verification == "failed" else "ok")
        lines = [f"plan_id: {self.plan_id}", f"status: {status}",
                 f"plan: {self.plan.title} ({len(merged)} of {len(self.steps)} steps merged)", *self.notes]
        for warning in self.settings["warnings"]:
            lines.append(f"config_warning: {warning}")
        unread = [s.id for s in self.steps if s.id in merged
                  and ((self.results.get(s.id) or {}).get("record") or {}).get("unfinished")]
        if unread:
            # Checks passing is why they were merged; nothing from Mistral says what it did or left out.
            lines.append("review_first: " + ", ".join(unread) + (" ends" if len(unread) == 1 else " end")
                         + " without a closing summary and " + ("was" if len(unread) == 1 else "were")
                         + " merged on passing checks alone. Read " + ("its" if len(unread) == 1 else "their")
                         + " diff before adopting: " + ", ".join(
                             f"--result {self.results[s]['run_id']}" for s in unread if self.results[s].get("run_id")))
        lines.append("steps:")
        for step in self.steps:
            lines.append("  " + self.step_line(step, integration))
        notes: dict[str, list[str]] = {}  # note -> the steps that reported it
        failed_before: dict[str, list[str]] = {}  # check -> the steps whose baseline it failed
        for step in self.steps:
            report_lines = (self.results.get(step.id) or {}).get("report", "").splitlines()
            for n, line in enumerate(report_lines):
                listed = BASELINE_WARNING.match(line)
                if listed:
                    # Steps have different checks, so their warnings differ in wording: list each check once.
                    for command in listed.group(1).split(", "):
                        failed_before.setdefault(command, []).append(step.id)
                elif line.startswith(STEP_NOTE_PREFIXES):
                    # Notes whose example matters stay whole; others keep their first sentence and list items.
                    whole = line.startswith(("out_of_scope", "missing_files", "assertion_hint", "test_strength"))
                    first = line[:2000] if whole else line.split(". ")[0][:300]
                    items = [ln.strip() for ln in report_lines[n + 1:n + 6] if ln.startswith("  - ")]
                    notes.setdefault(first + (" " + "; ".join(items) if items else ""), []).append(step.id)
        if failed_before:
            lines.append("baseline_warning: these checks already failed on the plan's starting code, before any "
                         "step changed it: " + "; ".join(
                             f"{c} (in {'every step' if len(st) == len(self.steps) else ', '.join(st)})"
                             for c, st in failed_before.items())
                         + ". Mistral was told not to work around them. If they pass in your checkout, the cause "
                           "is the worktree environment (see deps_mode); if they fail there too, it may be your "
                           "own uncommitted changes, which the steps start from.")
        if notes:
            # The same note from several steps (a baseline warning every step sees) is listed once.
            lines.append("step_notes (from the steps' own reports):\n" + "\n".join(
                f"  {', '.join(steps) if len(steps) < len(self.steps) else 'all steps'}: {note}"
                for note, steps in notes.items()))
        if integration["commands"]:
            fix = integration.get("fix")
            lines.append(f"verification: {verification} (the merged steps together"
                         + (f"; after an integration fix run, {fix.get('run_id', 'which failed to start')}" if fix else "")
                         + ")\n" + "\n".join(check_lines(integration["results"], integration["baseline"])))
        else:
            lines.append(f"verification: {verification}"
                         + (" (one step merged: its own checks)" if integration["verification"] == "same_as_step"
                            else " (no checks for the merged result)" if merged else ""))
        for cmd, code, out in integration["failures"][:2]:
            lines.append(f"failing_check_output ({cmd}, exit {code}, merged result):\n```\n{out}\n```")
        for step in self.steps:
            result = self.results.get(step.id) or {}
            if not result.get("ok") and result.get("report"):
                block = re.search(r"failing_check_output \((.*?)\):\n```\n(.*?)\n```", result["report"], re.S)
                if block:
                    tail = "\n".join(block.group(2).splitlines()[-15:])
                    lines.append(f"failing_check_output ({step.id}: {block.group(1)}):\n```\n{tail}\n```")
        lines += self.usage_lines(runs)
        lines.append(f"elapsed: {time.monotonic() - self.started:.0f}s")
        lines += self.changes_lines(merged)
        lines.append("step_reports: " + ", ".join(f"{s.id}: --result {self.results[s.id]['run_id']}"
                                                 for s in self.steps if (self.results.get(s.id) or {}).get("run_id")))
        lines.append("\n--- what Mistral said, per step ---")
        for step in self.steps:
            said = (self.results.get(step.id) or {}).get("report", "").partition("--- result from Mistral Vibe ---")[2]
            if said.strip():
                lines.append(f"{step.id}: " + " ".join(said.split())[:500])
        report = "\n".join(lines)
        end = {"status": status, "verification": verification, "merged": merged,
               "files_changed": len(gitops.changed_files(self.int_wt)) if merged else 0}
        self.all_runs = runs
        return report, end

    def step_line(self, step: Step, integration: dict) -> str:
        result = self.results.get(step.id) or {"status": "not_run"}
        record = result.get("record") or {}
        # Vibe's "ok" only means it finished; a step whose checks failed didn't succeed.
        shown = "checks failed" if result.get("status") == "ok" and result.get("verification") == "failed" \
            else result.get("status")
        parts = [f"{step.id}: {shown}"]
        if result.get("verification") == "not_run" and result.get("status") == "ok":
            parts.append("no checks")
        elif result.get("verification"):
            used = record.get("fix_attempts_used")
            parts.append(f"checks {result['verification']}"
                         + (f" after {used} fix round(s)" if used else " first try" if result.get("run_id") else ""))
        if record.get("test_quality_fix"):
            parts.append(f"a test-quality round for {record['test_quality_fix']} weak spot(s)")
        if record.get("unfinished"):
            parts.append("no closing summary (read its diff)")
        if record.get("files_changed") is not None:
            parts.append(f"{record['files_changed']} file(s)")
        # The step's cost over all its runs (a failed attempt and its follow-up), as in the plan's total.
        wt = self.step_wts.get(step.id)
        step_runs = [r for r in self.every_run() if wt and (r.get("worktree") or {}).get("path") == wt["path"]] \
            or ([record] if record else [])
        costs = [c for c in (ledger.run_cost(r, vibe.model_prices()) for r in step_runs) if c is not None]
        if costs:
            rounds = [r for run in step_runs for r in run.get("rounds") or []]
            parts.append(f"~{self.settings['currency']}{sum(costs):.4f}"
                         + (f" over {len(step_runs)} runs" if len(step_runs) > 1 else "")
                         + (f" ({_rounds_text(rounds, self.settings['currency'])})" if len(rounds) > 1 else ""))
        if result.get("run_id"):
            parts.append(f"run {result['run_id']}")
        line = ", ".join(parts)
        if step.id in integration["merged"]:
            return line + " -> merged"
        if step.id in integration["problems"]:
            return line + f" -> {integration['problems'][step.id]}"
        if result.get("note"):
            line += f" ({result['note']})"
        wt = self.step_wts.get(step.id)
        if record.get("session_id") and wt and os.path.isdir(wt["path"]):
            line += (f" -> not merged. To finish it: --mode write --resume {record['session_id']} "
                     f"--worktree-name {wt['name']} "
                     f"\"<what to fix>\", then --integrate {self.plan_id}")
        else:
            line += " -> not merged"
        return line

    def every_run(self) -> list[dict]:
        """Every finished Mistral run of this plan, earlier attempts and follow-ups included."""
        record = ledger.load_runs().get(self.plan_id) or {"id": self.plan_id}
        return [r for r in _plan_runs(record) if r["id"] != self.plan_id and r.get("status")]

    def usage_lines(self, _runs: list[dict]) -> list[str]:
        prices = vibe.model_prices()
        runs = self.every_run()
        costs = [ledger.run_cost(r, prices) for r in runs]
        effective = sum(r.get("effective") or 0 for r in runs if isinstance(r.get("effective"), (int, float)))
        priced = [c for c in costs if c is not None]
        c = self.settings["currency"]
        line = f"usage: {effective:,} effective tokens across {len(runs)} Mistral run(s) of this plan"
        fixed = [r for r in runs if (r.get("fix_attempts_used") or 0) > 0]
        if fixed:
            line += (f" ({len(fixed)} needed a fix round: "
                     + ", ".join(str(r.get("step") or r["id"]) for r in fixed) + ")")
        if priced:
            line += f"; ~{c}{sum(priced):.4f} at list prices"
        if len(priced) < len(costs):
            line += f" ({len(costs) - len(priced)} run(s) without a price aren't included)"
        out = [line]
        credit = self.settings.get("monthly_credit")
        if credit:
            spent, unpriced, since = ledger.month_spend(ledger.load_runs(), self.settings["credit_reset_day"], prices)
            out.append(f"credit: ~{c}{spent:.2f} of {c}{credit:.2f} used since {since} ({spent / credit:.0%})"
                       + (f"; {unpriced} run(s) without a price aren't included" if unpriced else ""))
        return out

    def changes_lines(self, merged: list[str]) -> list[str]:
        if not merged:
            return [f"worktree: nothing merged; the steps' worktrees are kept for --resume. "
                    f"discard_with: {self.script_cmd('--discard', self.plan_id, '--note', 'why')}"]
        lines = [f"worktree_path: {self.int_wt['path']} (the merged steps)"]
        stat = gitops.changes_stat(self.int_wt)
        lines.append("changes (all merged steps):\n" + (stat or "  (none)"))
        diff = gitops.changes_diff(self.int_wt)
        n = diff.count("\n")
        diff_file = ledger.run_file(self.plan_id, "changes.diff")
        try:
            diff_file.write_text(diff, encoding="utf-8")
        except OSError:
            diff_file = None
        if 0 < n <= self.args.diff_lines:
            lines.append(f"diff:\n```diff\n{diff.rstrip()}\n```")
            if diff_file:
                lines.append(f"diff_file: {diff_file}")
        elif n:
            lines.append(f"diff: {n} lines, too long to show here. Read it from "
                         + (str(diff_file) if diff_file else f"git -C {self.int_wt['path']} diff --cached "
                                                              f"{self.int_wt['base'][:12]}"))
        lines.append(f"adopt_with: {self.script_cmd('--adopt', self.plan_id)}   (add --steps a,b to take only "
                     "some steps)")
        lines.append(f"discard_with: {self.script_cmd('--discard', self.plan_id, '--note', 'why')}")
        return lines

    def record_end(self, report: str, end: dict, plan_text: str) -> None:
        runs = self.all_runs  # the steps and an integration fix
        record_runs = self.every_run()  # what the plan cost, earlier attempts and follow-ups included
        prices = vibe.model_prices()
        costs = [ledger.run_cost(r, prices) for r in record_runs]
        # Claude wrote the plan once and reads this one report: that is the overhead, shared by the steps.
        overhead = vibe.effective_tokens(len(report) // 4, 0, len(plan_text) // 4)
        ledger.save_report(self.plan_id, report)
        ledger.append({"event": "end", "id": self.plan_id, **end, "claude_overhead": overhead,
                       "cost": sum(c for c in costs if c is not None) if any(c is not None for c in costs) else None,
                       "effective": sum(r.get("effective") or 0 for r in record_runs
                                        if isinstance(r.get("effective"), (int, float)))})
        if runs:
            share = overhead // len(runs)
            for r in runs:
                ledger.append({"event": "update", "id": r["run_id"], "claude_overhead": share})


# --- entry points ---------------------------------------------------------------------------

def _setup(args) -> tuple[str, str, dict]:
    workdir = str(Path(args.workdir).resolve())
    top = gitops.toplevel(workdir)
    if not top:
        raise DelegateError("Plans need a git repository: each step runs in its own worktree.")
    gitops.work_dir(top)
    settings = config.load(top)
    if settings["errors"]:
        raise DelegateError("Fix the config first (nothing was run):\n" + "\n".join(f"  {e}" for e in settings["errors"]))
    if args.policy:
        settings["policy"] = args.policy
    vibe.EXTRA_PRICES.update(settings["model_prices"])
    vibe.WEIGHTS.update(settings["token_weights"])
    return workdir, top, settings


def main(args, script: Path, kinds: tuple, script_cmd) -> int:
    if args.integrate:
        return integrate_again(args, script, script_cmd)
    top = gitops.toplevel(str(Path(args.workdir).resolve()))
    try:
        plan_text = gitops.find_document(args.plan, top, "plan").read_text(encoding="utf-8")
    except OSError as e:
        raise DelegateError(f"Can't read the plan file: {e}") from e
    try:
        plan = parse(plan_text)
        steps = select(plan, [s.strip() for s in (args.steps or "").split(",") if s.strip()])
    except PlanError as e:
        raise DelegateError(f"The plan has a problem (nothing was run): {e}") from e
    bad_kinds = sorted({k for s in steps if (k := s.kind or plan.kind or "other") not in kinds})
    if bad_kinds:
        raise DelegateError(f"Unknown kind {', '.join(bad_kinds)} in the plan; use one of: {', '.join(kinds)}")
    workdir, top, settings = _setup(args)
    vibe_bin = shutil.which(args.vibe_bin) or (args.vibe_bin if Path(args.vibe_bin).is_file() else None)
    if not vibe_bin:
        raise DelegateError("Vibe CLI not found. Install it with `uv tool install mistral-vibe`, then run "
                            "`vibe --setup` once.")
    plan_id = f"plan-{uuid.uuid4().hex[:8]}"
    ledger.run_file(plan_id, "plan.md").write_text(plan_text, encoding="utf-8")
    run = PlanRun(args, script, settings, top, workdir, plan, steps, plan_id, script_cmd)
    ledger.append({"event": "start", "id": plan_id, "time": time.time(), "pid": os.getpid(),
                   "pid_started": ledger.process_started(os.getpid()), "mode": "plan", "kind": "plan",
                   "repo": Path(top).name, "workdir": workdir, "task": plan.title[:500], "policy": settings["policy"],
                   "model": args.model or settings["model"], "scope": run.scope, "steps": [s.id for s in steps],
                   "worktree": None})
    previous = {sig: signal.signal(sig, _exit_on_signal) for sig in (signal.SIGTERM, signal.SIGHUP)}
    try:
        try:
            run.int_wt = gitops.prepare_worktree(top, plan_id, snapshot=not args.no_snapshot,
                                                 link_deps=not args.no_link_deps, extra_links=args.link,
                                                 deps_mode=run.deps_mode, worktrees_dir=settings["worktrees_dir"])
        except DelegateError as e:
            ledger.append({"event": "end", "id": plan_id, "status": "error", "verification": "not_run",
                           "error": f"worktree: {e}"[:300]})
            raise DelegateError(f"Could not prepare the plan's worktree: {e}") from e
        ledger.append({"event": "update", "id": plan_id, "worktree": {
            k: run.int_wt[k] for k in ("name", "path", "toplevel", "base", "links")}})
        run.run_steps()
        run.use_worktree_code()
        integration = run.integrate()
        report, end = run.report(integration)
        run.record_end(report, end, plan_text)
    except BaseException as e:
        run.stop_children()
        if not isinstance(e, DelegateError) or run.int_wt:
            ledger.append({"event": "end", "id": plan_id, "verification": "not_run",
                           "status": "interrupted" if isinstance(e, (SystemExit, KeyboardInterrupt)) else "error",
                           "error": f"{e.__class__.__name__}: {e}"[:300]})
        raise
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    print(report)
    return 0 if end["status"] == "ok" and end["verification"] != "failed" else 1


def _plan_runs(plan_record: dict) -> list[dict]:
    """Every run belonging to a plan: its record, its steps and their follow-ups, its integration fixes."""
    runs = ledger.load_runs()
    paths = {(w or {}).get("path") for w in (plan_record.get("step_worktrees") or {}).values()}
    paths.add((plan_record.get("worktree") or {}).get("path"))
    paths.discard(None)
    return [r for r in runs.values() if r["id"] == plan_record["id"] or r.get("plan") == plan_record["id"]
            or (r.get("worktree") or {}).get("path") in paths]


def _refuse_while_running(plan_record: dict) -> None:
    busy = [r["id"] for r in _plan_runs(plan_record) if ledger.state(r) == "running"]
    if busy:
        raise DelegateError(f"{', '.join(busy)} of plan {plan_record['id']} is still going. Wait for it to finish "
                            "(see --status).")


def _load_plan(plan_record: dict) -> Plan:
    try:
        return parse(_plan_text(plan_record["id"]))
    except (OSError, PlanError) as e:
        raise DelegateError(f"Can't read plan {plan_record['id']}'s saved plan file: {e}") from e


def _plan_text(plan_id: str) -> str:
    for path in (ledger.runs_dir() / plan_id / "plan.md", ledger.runs_dir() / f"{plan_id}.plan.md"):
        if path.is_file():
            return path.read_text(encoding="utf-8")
    raise OSError(f"no saved plan for {plan_id}")


def _latest_on(path: str) -> dict | None:
    runs = [r for r in ledger.load_runs().values() if (r.get("worktree") or {}).get("path") == path]
    return max(runs, key=lambda r: r.get("started") or 0) if runs else None


def integrate_again(args, script: Path, script_cmd) -> int:
    """Finish a plan: run the steps that never ran or whose worktree is gone (once what they need succeeded),
    then merge every step again from its worktree (after a step was resumed) and check the result."""
    record = ledger.load_runs().get(args.integrate)
    if not record or record.get("mode") != "plan":
        raise DelegateError(f"No plan with id {args.integrate!r}. See --status.")
    if record.get("outcome"):
        raise DelegateError(f"Plan {record['id']} was already {record['outcome']}.")
    _refuse_while_running(record)
    plan = _load_plan(record)
    steps = [plan.step(s) for s in record.get("steps") or [] if s in {x.id for x in plan.steps}]
    args.workdir = record.get("workdir") or args.workdir
    workdir, top, settings = _setup(args)
    run = PlanRun(args, script, settings, top, workdir, plan, steps, record["id"], script_cmd)
    int_wt = record.get("worktree")
    if not int_wt:
        raise DelegateError(f"Plan {record['id']} has no worktree to integrate into.")
    ledger.append({"event": "update", "id": record["id"], "status": None, "pid": os.getpid(),
                   "pid_started": ledger.process_started(os.getpid())})
    try:
        if os.path.isdir(int_wt["path"]):
            gitops.remove_worktree(int_wt)
        run.int_wt = gitops.prepare_worktree(top, record["id"], snapshot=False, link_deps=not args.no_link_deps,
                                             extra_links=args.link, deps_mode=run.deps_mode,
                                             worktrees_dir=settings["worktrees_dir"], start=int_wt["base"])
        for step in steps:
            wt = (record.get("step_worktrees") or {}).get(step.id)
            latest = _latest_on(wt["path"]) if wt and os.path.isdir(wt["path"]) else None
            if not latest:
                continue  # never ran (skipped, blocked) or its worktree is gone: run_steps runs it now
            wt = dict(wt, base=gitops.load_state(wt["path"]).get("base") or wt["base"])
            run.step_wts[step.id] = wt
            result = {"ok": step_succeeded(latest), "status": latest.get("status"), "run_id": latest["id"],
                      "verification": latest.get("verification"), "record": latest,
                      "report": ledger.read_report(latest["id"]) or ""}
            if result["ok"]:
                gitops.commit_all(wt, f"mistral-delegate: {record['id']} step {step.id}")
            run.results[step.id] = result
        run.run_steps()
        latest_runs = {k: r.get("run_id") for k, r in run.results.items() if r.get("run_id")}
        if any(r.get("kind") == "revise" for r in _plan_runs(record)):
            run.notes.append("note: this plan had been revised (--revise); merging the steps again dropped the "
                             "revision. Run --revise again with the same list if it still applies.")
        if latest_runs == (record.get("step_runs") or {}):
            run.notes.append("note: nothing changed since the last integration: no step was resumed or run. Resume "
                             "the step that failed (with --mode write and the --resume command its line gives), "
                             "then --integrate again.")
        run.use_worktree_code()
        integration = run.integrate()
        report, end = run.report(integration)
        run.record_end(report, end, _plan_text(record["id"]))
    except BaseException as e:
        run.stop_children()
        ledger.append({"event": "end", "id": record["id"], "verification": "not_run",
                       "status": "interrupted" if isinstance(e, (SystemExit, KeyboardInterrupt)) else "error",
                       "error": f"{e.__class__.__name__}: {e}"[:300]})
        raise
    print(report)
    return 0 if end["status"] == "ok" and end["verification"] != "failed" else 1


def revise(args, record: dict, script: Path, script_cmd) -> int:
    """Claude's review findings for a plan, made by one Mistral run in the plan's merged worktree."""
    _refuse_while_running(record)
    merged = record.get("merged") or []
    int_wt = record.get("worktree") or {}
    if not merged or not os.path.isdir(int_wt.get("path", "")):
        raise DelegateError(f"Plan {record['id']} has no merged result to revise.")
    plan = _load_plan(record)
    args.workdir = record.get("workdir") or args.workdir
    workdir, top, settings = _setup(args)
    steps = [plan.step(s) for s in record.get("steps") or [] if s in {x.id for x in plan.steps}]
    run = PlanRun(args, script, settings, top, workdir, plan, steps, record["id"], script_cmd)
    changes = args.task or ""
    if args.spec:
        try:
            changes = gitops.find_document(args.spec, top, "spec").read_text(encoding="utf-8") + "\n" + changes
        except OSError as e:
            raise DelegateError(f"Can't read --spec file: {e}") from e
    parts = [f"# Plan: {plan.title}",
             "The steps of this plan were done separately and are merged in your working directory:",
             "\n".join(f"- {s}: {plan.step(s).summary()}" for s in merged)]
    if plan.shared:
        parts.append("## Shared context\n\n" + plan.shared)
    parts.append("## What to change\n\nThe reviewer read the merged result and wants these changes, and only "
                 "these. Make each one, keep the checks passing, and when you replace something, delete what it "
                 "replaced. End with the list of changes, one line each, saying how you made it or why you "
                 "couldn't.\n\n" + changes.strip())
    count = sum(1 for r in _plan_runs(record) if r.get("kind") == "revise") + 1
    spec = ledger.run_file(record["id"], f"revise-{count}.spec.md")
    spec.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    # Every check a merged step had, and the plan's own; the configured checks apply by themselves.
    checks = list(dict.fromkeys([*plan.verify, *[c for s in merged for c in plan.step(s).verify]]))
    scope = [] if not all(plan.step(s).scope for s in merged) else list(dict.fromkeys(
        [*[e for s in merged for e in plan.step(s).scope], *(args.scope or [])]))
    cmd = run.child_command(worktree=record["id"], kind="revise", spec=spec, step_tag=f"{record['id']}:revise",
                            task=f"Make the reviewer's changes to plan '{plan.title}'", scope=scope, verify=[],
                            add_verify=checks, allow=[], context=[],
                            extra=["--diff-lines", str(args.diff_lines),  # the revision's diff is for review
                                   "--revise-of", record["id"]])
    proc = subprocess.run(cmd, cwd=workdir)
    print(f"\nplan_id: {record['id']} (revision {count})\n"
          f"adopt_with: {script_cmd('--adopt', record['id'])}   (the whole plan: --steps would leave the "
          "revision out)")
    return proc.returncode


def _rounds_text(rounds: list[dict], currency: str) -> str:
    return ", ".join(r["label"] + (f" ~{currency}{r['cost']:.2f}" if r.get("cost") is not None else "")
                     for r in rounds)


def _step_worktrees(record: dict) -> dict[str, dict]:
    return {k: v for k, v in (record.get("step_worktrees") or {}).items() if v}


def _remove_all(record: dict) -> None:
    for wt in [*_step_worktrees(record).values(), record.get("worktree")]:
        if wt and os.path.isdir(wt.get("path", "")):
            gitops.remove_worktree(wt)


def _settle(record: dict, adopted_steps: list[str], partial: bool, note: str | None,
            whole_merge: bool = True) -> None:
    """Record outcomes: adopted steps (and their follow-ups), the rest discarded, the plan adopted (or partly).

    An integration fix is part of what's adopted only when the whole merged result is (not some steps)."""
    step_wts = _step_worktrees(record)
    for r in _plan_runs(record):
        if r.get("outcome"):
            continue
        path = (r.get("worktree") or {}).get("path")
        step = r.get("step") or next((s for s, w in step_wts.items() if w.get("path") == path), None)
        if r["id"] == record["id"]:
            outcome = "adopted_partial" if partial else "adopted"
        elif step == "integration" or path == (record.get("worktree") or {}).get("path"):
            outcome = ("adopted_partial" if partial else "adopted") if whole_merge else "discarded"
        else:
            outcome = "adopted" if step in adopted_steps else "discarded"
        ledger.append({"event": "outcome", "id": r["id"], "outcome": outcome, "note": note})


def adopt(args, record: dict, script_cmd) -> int:
    if args.paths:
        raise DelegateError("A plan is adopted by step: use --steps a,b instead of --paths.")
    _refuse_while_running(record)
    if record.get("outcome"):
        raise DelegateError(f"Plan {record['id']} was already {record['outcome']}.")
    merged = record.get("merged") or []
    if not merged:
        raise DelegateError(f"Plan {record['id']} has nothing merged to adopt. Resume its steps and run "
                            f"--integrate {record['id']}, or --discard it.")
    plan = _load_plan(record)
    int_wt = record.get("worktree") or {}
    if not os.path.isdir(int_wt.get("path", "")):
        raise DelegateError(f"The plan's worktree {int_wt.get('path')} no longer exists.")
    wanted = [s.strip() for s in (args.steps or "").split(",") if s.strip()] or list(merged)
    not_merged = [s for s in wanted if s not in merged]
    if not_merged:
        raise DelegateError(f"Not merged in this plan, so they can't be adopted: {', '.join(not_merged)}. "
                            f"Merged: {', '.join(merged)}.")
    missing = sorted({d for s in wanted for d in plan.step(s).depends if d not in wanted})
    if missing:
        raise DelegateError(f"The chosen steps need {', '.join(missing)}: add them to --steps.")
    for step in wanted:
        wt = _step_worktrees(record).get(step)
        if wt and os.path.isdir(wt["path"]) and gitops.changed_files(dict(wt, base="HEAD")):
            raise DelegateError(f"Step {step} has changes newer than the plan's merge (a resume?). Run "
                                f"--integrate {record['id']} first, then adopt.")
    temp = None
    try:
        if set(wanted) == set(merged):
            source = dict(int_wt, base=gitops.load_state(int_wt["path"]).get("base") or int_wt["base"])
        else:
            order = [x.id for x in plan.order() if x.id in wanted]
            temp = gitops.prepare_worktree(int_wt["toplevel"], f"{record['id']}-adopt", snapshot=False,
                                           link_deps=False, extra_links=[], start=int_wt["base"],
                                           merge=[branch(record["id"], s) for s in order])
            source = dict(temp, base=int_wt["base"])  # everything the chosen steps changed since the plan's start
        scoped = all(plan.step(s).scope for s in wanted)
        scope = list(dict.fromkeys(e for s in wanted for e in plan.step(s).scope)) if scoped else []
        paths, skipped = None, []
        if scope and not args.include_out_of_scope:
            changed = gitops.changed_files(source)
            skipped = [f for f in changed if not vibe.matches_scope(f, scope)]
            if skipped and not args.skip_out_of_scope:
                print(f"Nothing applied: {len(skipped)} changed file(s) are outside the scope of the plan's steps "
                      f"({', '.join(scope)}):")
                print("\n".join(f"  {f}" for f in skipped))
                print("Look at them, then run --adopt again with --include-out-of-scope to apply them too, or "
                      "--skip-out-of-scope to leave them out.")
                return 1
            if skipped:
                paths = [f for f in changed if vibe.matches_scope(f, scope)]
        try:
            files = gitops.apply_to_checkout(source, paths)
        except DelegateError as e:
            raise DelegateError(f"{e}\nNothing was applied. Your checkout may have changed the same lines.") from e
    finally:
        if temp:
            gitops.remove_worktree(temp)
    partial = set(wanted) != set(record.get("steps") or []) or bool(skipped)
    _settle(record, wanted, partial, args.note, whole_merge=set(wanted) == set(merged))
    print(f"Applied {len(files)} file(s) from plan {record['id']} (steps {', '.join(wanted)}) to "
          f"{int_wt['toplevel']}:")
    print("\n".join(f"  {f}" for f in files))
    if skipped:
        print("Left out (outside the steps' scope):\n" + "\n".join(f"  {f}" for f in skipped))
    if args.keep_worktree:
        print("Worktrees kept (the plan is settled; --discard removes them later).")
    else:
        _remove_all(record)
        print("The plan's worktrees were removed.")
    print("The changes are uncommitted in your checkout; review and commit them as usual.")
    return 0


def discard(args, record: dict) -> int:
    _refuse_while_running(record)
    _remove_all(record)
    for r in _plan_runs(record):
        if not r.get("outcome"):
            ledger.append({"event": "outcome", "id": r["id"], "outcome": "discarded", "note": args.note})
    print(f"Discarded plan {record['id']} and removed its worktrees.")
    return 0
