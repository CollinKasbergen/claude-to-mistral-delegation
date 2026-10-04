"""Tests for delegate.py, using a fake `vibe` executable so no API key is needed.

Run with: python3 -m unittest discover -s tests
"""

import json
import os
import shlex
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "plugins/mistral-delegate/skills/delegate-to-mistral/scripts/delegate.py"

# Mimics the parts of `vibe --prompt ... --output json` the wrapper relies on,
# including the session log that holds the run's cost.
FAKE_VIBE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    argv = sys.argv[1:]
    cwd = os.getcwd()
    with open(os.environ["FAKE_VIBE_ARGS"], "w") as f:
        json.dump({"argv": argv, "cwd": cwd,
                   "sees_draft": os.path.exists("draft.py"),
                   "sees_edit": open("app.py").read() if os.path.exists("app.py") else None,
                   "has_node_modules": os.path.isdir("node_modules")}, f)

    session_id = argv[argv.index("--resume") + 1] if "--resume" in argv else "sess-1234567890"
    log_dir = os.path.join(os.environ["VIBE_HOME"], "logs", "session", "session_20260101_" + session_id[:8])
    os.makedirs(log_dir, exist_ok=True)
    meta_path = os.path.join(log_dir, "meta.json")
    stats = {"steps": 0, "session_prompt_tokens": 0, "session_completion_tokens": 0, "session_cost": 0.0}
    if os.path.exists(meta_path):
        stats = json.load(open(meta_path))["stats"]
    stats = {"steps": stats["steps"] + 4, "session_prompt_tokens": stats["session_prompt_tokens"] + 1000,
             "session_completion_tokens": stats["session_completion_tokens"] + 200,
             "session_cost": stats["session_cost"] + 0.0125}
    json.dump({"session_id": session_id, "environment": {"working_directory": cwd}, "stats": stats},
              open(meta_path, "w"))

    behaviour = os.environ.get("FAKE_VIBE_BEHAVIOUR", "ok")
    if behaviour == "limit":
        print("I got halfway through.", file=sys.stderr)
        sys.exit(1)
    if behaviour == "error":
        print("Error: Invalid API key", file=sys.stderr)
        sys.exit(1)
    if "accept-edits" in argv:
        with open("test_app.py", "w") as f:
            f.write("def test_app():\\n    assert True\\n")
    entry = {"sessionId": session_id, "createdAt": 0, "updatedAt": 0, "generationStatus": "completed"}
    history = [
        dict(entry, id="1", type="message", role="user", content=[{"type": "text", "text": "task"}]),
        dict(entry, id="2", type="effect", title="read_file", state={"status": "completed", "display": {}}),
        dict(entry, id="3", type="effect", title="bash: pytest", state={"status": "skipped", "reason": "denied", "display": {}}),
        dict(entry, id="4", type="message", role="assistant", content=[{"type": "text", "text": "Done: added test_app.py"}]),
    ]
    print(json.dumps(history, indent=2))
''')


class DelegateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.repo = tmp / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("print('v1')\n")
        (self.repo / ".gitignore").write_text("node_modules/\n")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
        (self.repo / "node_modules" / "left-pad").mkdir(parents=True)
        self.vibe = tmp / "vibe"
        self.vibe.write_text(FAKE_VIBE)
        self.vibe.chmod(self.vibe.stat().st_mode | stat.S_IEXEC)
        self.args_file = tmp / "args.json"
        self.env = dict(os.environ, VIBE_BIN=str(self.vibe), FAKE_VIBE_ARGS=str(self.args_file),
                        VIBE_HOME=str(tmp / "vibe-home"), MISTRAL_DELEGATE_WORKTREES=str(tmp / "worktrees"))

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def run_delegate(self, *args, behaviour="ok", workdir=None):
        env = dict(self.env, FAKE_VIBE_BEHAVIOUR=behaviour)
        return subprocess.run([sys.executable, str(SCRIPT), "--workdir", str(workdir or self.repo), *args],
                              capture_output=True, text=True, env=env)

    def seen(self):
        return json.loads(self.args_file.read_text())

    def report_value(self, out, key):
        for line in out.stdout.splitlines():
            if line.startswith(key + ": "):
                return line[len(key) + 2:]
        self.fail(f"{key} not in report:\n{out.stdout}")

    def test_read_mode_is_read_only_and_capped(self):
        out = self.run_delegate("--mode", "read", "Find the config parser")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        argv = self.seen()["argv"]
        self.assertIn("plan", argv)
        self.assertIn("--max-price", argv)
        self.assertNotIn("--auto-approve", argv)
        self.assertNotIn("--trust", argv)
        enabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--enabled-tools"]
        self.assertEqual(sorted(enabled), ["grep", "read_file", "todo"])
        self.assertEqual(Path(self.seen()["cwd"]).resolve(), self.repo.resolve())
        self.assertIn("status: ok", out.stdout)
        self.assertIn("session_id: sess-1234567890", out.stdout)
        self.assertIn("Done: added test_app.py", out.stdout)
        self.assertIn("bash: pytest: skipped (denied)", out.stdout)

    def test_usage_reports_cost_steps_and_tokens(self):
        out = self.run_delegate("--mode", "read", "--max-price", "0.5", "Task")
        self.assertIn("usage: cost $0.0125 of $0.50 cap, 4 steps, 1,200 tokens", out.stdout)

    def test_write_mode_sees_uncommitted_work_and_links_deps(self):
        (self.repo / "app.py").write_text("print('v2 uncommitted')\n")
        (self.repo / "draft.py").write_text("x = 1\n")
        out = self.run_delegate("--mode", "write", "--worktree-name", "mistral-test", "Add tests")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        seen = self.seen()
        self.assertTrue(seen["sees_draft"])
        self.assertEqual(seen["sees_edit"], "print('v2 uncommitted')\n")
        self.assertTrue(seen["has_node_modules"])
        self.assertIn("--trust", seen["argv"])
        self.assertIn("1 modified, 1 untracked", out.stdout)
        self.assertIn("node_modules", self.report_value(out, "linked_from_checkout (symlinks, shared with your checkout)"))

        changes = out.stdout.split("changes_by_vibe:")[1]
        self.assertIn("test_app.py", changes.split("\n")[1])
        # Only Vibe's file is in the change set: not the snapshot, not the node_modules symlink.
        self.assertNotIn("draft.py", changes.split("review_with")[0])
        self.assertNotIn("node_modules", changes.split("review_with")[0])
        # The checkout itself is untouched.
        self.assertFalse((self.repo / "test_app.py").exists())

        subprocess.run(self.report_value(out, "apply_to_checkout_with"), shell=True, check=True)
        self.assertTrue((self.repo / "test_app.py").exists())
        self.assertEqual((self.repo / "app.py").read_text(), "print('v2 uncommitted')\n")

        subprocess.run(self.report_value(out, "cleanup_with"), shell=True, check=True, capture_output=True)
        self.assertNotIn("mistral-test", self.git("branch"))

    def test_no_snapshot_starts_from_head(self):
        (self.repo / "draft.py").write_text("x = 1\n")
        out = self.run_delegate("--mode", "write", "--no-snapshot", "--no-link-deps", "Task")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertFalse(self.seen()["sees_draft"])
        self.assertFalse(self.seen()["has_node_modules"])
        self.assertIn("worktree_base: your HEAD", out.stdout)

    def test_follow_up_reuses_worktree_and_reports_run_cost(self):
        first = self.run_delegate("--mode", "write", "--worktree-name", "mistral-fu", "Task")
        path = self.report_value(first, "worktree_path")
        second = self.run_delegate("--mode", "write", "--worktree-name", "mistral-fu",
                                   "--resume", "sess-1234567890", "Follow up")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.report_value(second, "worktree_path"), path)
        self.assertIn("worktree_name: mistral-fu  (reused)", second.stdout)
        self.assertIn("usage: cost $0.0125", second.stdout)
        self.assertIn("session total $0.0250", second.stdout)

    def test_subdirectory_workdir_maps_into_worktree(self):
        sub = self.repo / "pkg"
        sub.mkdir()
        (sub / "mod.py").write_text("")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "pkg")
        out = self.run_delegate("--mode", "write", "--worktree-name", "mistral-sub", "Task", workdir=sub)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(Path(self.seen()["cwd"]).name, "pkg")
        self.assertIn("pkg/test_app.py", out.stdout)

    def test_allow_shell_and_in_place(self):
        out = self.run_delegate("--mode", "write", "--in-place", "--allow-shell", "Fix it")
        argv = self.seen()["argv"]
        self.assertIn("--auto-approve", argv)
        self.assertNotIn("--trust", argv)
        self.assertTrue((self.repo / "test_app.py").exists())
        self.assertIn("?? test_app.py", out.stdout)

    def test_limit_reached_still_reports_cost(self):
        out = self.run_delegate("Big task", behaviour="limit")
        self.assertEqual(out.returncode, 1)
        self.assertIn("status: limit_reached", out.stdout)
        self.assertIn("I got halfway through.", out.stdout)
        self.assertIn("usage: cost $0.0125", out.stdout)
        self.assertIn("session_id: sess-1234567890", out.stdout)

    def test_vibe_error(self):
        out = self.run_delegate("Task", behaviour="error")
        self.assertEqual(out.returncode, 1)
        self.assertIn("status: error", out.stdout)
        self.assertIn("Invalid API key", out.stdout)

    def test_failed_run_removes_untouched_worktree(self):
        out = self.run_delegate("--mode", "write", "--worktree-name", "mistral-err", "Task", behaviour="error")
        self.assertEqual(out.returncode, 1)
        self.assertIn("worktree: removed mistral-err", out.stdout)
        self.assertNotIn("mistral-err", self.git("branch"))
        self.assertNotIn("mistral-err", self.git("worktree", "list"))

    def test_missing_vibe(self):
        env = dict(self.env, VIBE_BIN="definitely-not-vibe")
        out = subprocess.run([sys.executable, str(SCRIPT), "Task"], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("uv tool install mistral-vibe", out.stdout)

    def test_write_mode_outside_git_requires_in_place(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        out = self.run_delegate("--mode", "write", "Task", workdir=plain)
        self.assertEqual(out.returncode, 2)
        self.assertIn("--in-place", out.stdout)

    def test_existing_branch_without_worktree_is_refused(self):
        self.git("branch", "mistral-taken")
        out = self.run_delegate("--mode", "write", "--worktree-name", "mistral-taken", "Task")
        self.assertEqual(out.returncode, 2)
        self.assertIn("already exists", out.stdout)


if __name__ == "__main__":
    unittest.main()
