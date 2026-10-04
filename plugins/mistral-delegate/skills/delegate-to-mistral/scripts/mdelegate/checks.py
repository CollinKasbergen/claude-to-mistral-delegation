"""Running the project's checks (tests, type checks, lint) for a delegation.

Each check runs in its own process group, so a timeout also stops what it started.
"""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from . import gitops

CHECK_OUTPUT_LINES = 60
CHECK_OUTPUT_CHARS = 5_000


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
    env = python_path_env(gitops.toplevel(cwd)) if EDITABLE_SOURCES else None
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
        output = (out or b"").decode("utf-8", errors="replace")
        if timed_out:
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
