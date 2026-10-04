"""Running the project's checks (tests, type checks, lint) for a delegation.

Each check runs in its own process group, so a timeout also stops what it started.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
from collections import Counter
from pathlib import Path

from . import gitops

CHECK_OUTPUT_LINES = 60
CHECK_OUTPUT_CHARS = 5_000
MAX_ERROR_LINES = 2_000
ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
# Lines that report a problem in the output of type checkers, linters and test runners.
ERROR_LINE = re.compile(r"\berror\b|\bERROR\b|\bFAIL(ED|URE)?\b|\bfail(ed|ure)?\b|[\u2717\u2718\u00d7]|Error:|Exception",
                        re.I)
# Checks run without colour: tools print the same lines whether or not a terminal is attached.
NO_COLOR_ENV = {"NO_COLOR": "1", "FORCE_COLOR": "0", "CLICOLOR": "0"}


class CheckResult(tuple):
    """(exit code, end of output) that also carries the check's error lines, normalised.

    Comparing those with the baseline's tells a check that still fails the same way from one that
    fails with new errors on top of old ones (a type check that already failed, plus Mistral's error).
    """

    def __new__(cls, code: int, out: str, errors: list[str] | None = None):
        result = super().__new__(cls, (code, out))
        result.errors = errors
        return result

    def stored(self) -> list:
        return [self[0], self[1], self.errors]

    @classmethod
    def load(cls, value) -> "CheckResult":
        value = list(value)
        return cls(value[0], value[1], value[2] if len(value) > 2 else None)


def error_lines(output: str) -> list[str]:
    """The output's error lines, with numbers (line, column, counts, timings) blanked so moves don't matter."""
    out = []
    for line in output.splitlines():
        if ERROR_LINE.search(line):
            out.append(re.sub(r"\s+", " ", re.sub(r"\d+", "N", line)).strip())
            if len(out) >= MAX_ERROR_LINES:
                break
    return out


def new_error_lines(result, base) -> list[str]:
    """Error lines in result that weren't in the baseline (each line counted as often as it occurs)."""
    errors, before = getattr(result, "errors", None), getattr(base, "errors", None)
    if errors is None or before is None:
        return []
    remaining = Counter(errors) - Counter(before)
    added = []
    for line in errors:
        if remaining[line] > 0:
            added.append(line)
            remaining[line] -= 1
    return added


def tail(text: str) -> str:
    lines = text.rstrip().splitlines()[-CHECK_OUTPUT_LINES:]
    out = "\n".join(lines)
    return out[-CHECK_OUTPUT_CHARS:]


def stop_process_group(proc: subprocess.Popen, grace: float = 10) -> None:
    """Stop a process started with start_new_session=True and every process in its group."""
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, None)):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass
        try:
            proc.wait(wait)
            if sig == signal.SIGTERM:
                # The leader is gone; make sure nothing it started lingers.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    pass
            return
        except subprocess.TimeoutExpired:
            continue


def measure_baseline(commands: list[str], cwd: str, timeout: int, flaky: list[str]) -> dict:
    """Run checks before Mistral changes anything; a failing check is rerun once (flaky ones pass then)."""
    baseline = run_checks(commands, cwd, timeout)
    failing = [cmd for cmd, (code, _out) in baseline.items() if code != 0]
    if failing:
        rerun = run_checks(failing, cwd, timeout)
        flaky += [cmd for cmd, (code, _out) in rerun.items() if code == 0]
        baseline.update(rerun)
    return baseline


# Folders (relative to the repo) that the linked virtualenv installs in editable mode from the user's
# checkout: checks put the copy in the folder they run in first on PYTHONPATH.
EDITABLE_SOURCES: list[str] = []


def python_path_env(root: str | None) -> dict | None:
    if not EDITABLE_SOURCES or not root:
        return None
    paths = [str(Path(root, rel)) for rel in EDITABLE_SOURCES]
    return dict(os.environ, PYTHONPATH=os.pathsep.join(paths + [p for p in [os.environ.get("PYTHONPATH")] if p]))


def run_checks(commands: list[str], cwd: str, timeout: int) -> dict[str, tuple[int, str]]:
    """Run every check. Returns {command: (exit code, end of output)}."""
    results = {}
    root = gitops.toplevel(cwd) or cwd
    env = dict(python_path_env(root) if EDITABLE_SOURCES else os.environ, **NO_COLOR_ENV)
    # The baseline may run in another folder (a clean copy): paths are compared relative to the repo.
    roots = sorted({root.rstrip("/") + "/", os.path.realpath(root).rstrip("/") + "/"}, key=len, reverse=True)
    for command in commands:
        # Its own process group, so a timeout also stops what the check started (test workers, servers).
        proc = subprocess.Popen(command, shell=True, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, start_new_session=True, env=env)
        timed_out = False
        try:
            out, _ = proc.communicate(timeout=timeout)
            code = proc.returncode
        except subprocess.TimeoutExpired:
            stop_process_group(proc, grace=2)
            out, _ = proc.communicate()
            code, timed_out = 124, True
        except BaseException:
            stop_process_group(proc, grace=2)
            raise
        output = ANSI.sub("", (out or b"").decode("utf-8", errors="replace"))
        if timed_out:
            output += f"\n[timed out after {timeout}s]"
        relative = output
        for prefix in roots:
            relative = relative.replace(prefix, "")
        results[command] = CheckResult(code, tail(output), error_lines(relative) if code else [])
    return results


def failed_before(cmd: str, baseline: dict) -> bool:
    return cmd in baseline and baseline[cmd][0] != 0


def new_failures(results: dict, baseline: dict) -> list[tuple[str, int, str]]:
    """Failing checks that passed (or weren't run) before Mistral changed anything, and checks that
    already failed but now report errors they didn't before (shown first in their output)."""
    out = []
    for cmd, result in results.items():
        code, text = result
        if code == 0:
            continue
        if not failed_before(cmd, baseline):
            out.append((cmd, code, text))
        elif (added := new_error_lines(result, baseline[cmd])):
            shown = "\n".join(added[:40]) + (f"\n[... {len(added) - 40} more]" if len(added) > 40 else "")
            out.append((cmd, code, "This check already failed before the change, but these errors are new:\n"
                        f"{shown}\n\nEnd of its output:\n{text}"))
    return out


def check_lines(results: dict, baseline: dict) -> list[str]:
    lines = []
    for cmd, result in results.items():
        code = result[0]
        if code == 0:
            note = " (was failing before Mistral)" if failed_before(cmd, baseline) else ""
            lines.append(f"  pass: {cmd}{note}")
        elif failed_before(cmd, baseline) and (added := new_error_lines(result, baseline[cmd])):
            lines.append(f"  FAIL: {cmd} (exit {code}; it failed before Mistral too, but with {len(added)} new "
                         "error line(s) now)")
        elif failed_before(cmd, baseline):
            lines.append(f"  FAIL: {cmd} (exit {code}; already failing before Mistral changed anything)")
        else:
            lines.append(f"  FAIL: {cmd} (exit {code})")
    return lines
