"""Tests for delegate.py, using a fake `vibe` executable so no API key is needed.

Run with: python3 -m unittest discover -s tests
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "plugins/mistral-delegate/skills/delegate-to-mistral/scripts/delegate.py"

# Mimics the parts of `vibe --prompt ... --output json` the wrapper relies on.
FAKE_VIBE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, subprocess, sys
    argv = sys.argv[1:]
    with open(os.environ["FAKE_VIBE_ARGS"], "w") as f:
        json.dump(argv, f)
    behaviour = os.environ.get("FAKE_VIBE_BEHAVIOUR", "ok")
    if behaviour == "limit":
        print("I got halfway through.", file=sys.stderr)
        sys.exit(1)
    if behaviour == "error":
        print("Error: Invalid API key", file=sys.stderr)
        sys.exit(1)
    if "--worktree" in argv:
        name = argv[argv.index("--worktree") + 1]
        path = os.path.join(os.environ["FAKE_VIBE_HOME"], name)
        subprocess.run(["git", "worktree", "add", "-q", "-b", name, path], check=True)
        with open(os.path.join(path, "new_file.py"), "w") as f:
            f.write("x = 1\\n")
    entry = {"sessionId": "sess-123", "createdAt": 0, "updatedAt": 0, "generationStatus": "completed"}
    history = [
        dict(entry, id="1", type="message", role="user", content=[{"type": "text", "text": "task"}]),
        dict(entry, id="2", type="effect", title="read_file", state={"status": "completed", "display": {}}),
        dict(entry, id="3", type="effect", title="bash: pytest", state={"status": "skipped", "reason": "denied", "display": {}}),
        dict(entry, id="4", type="message", role="assistant", content=[{"type": "text", "text": "Done: added new_file.py"}]),
    ]
    print(json.dumps(history, indent=2))
''')


class DelegateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = Path(self.tmp.name)
        self.repo = tmp / "repo"
        self.repo.mkdir()
        git = lambda *a: subprocess.run(["git", "-C", str(self.repo), *a], check=True, capture_output=True)
        git("init", "-q")
        git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init")
        self.vibe = tmp / "vibe"
        self.vibe.write_text(FAKE_VIBE)
        self.vibe.chmod(self.vibe.stat().st_mode | stat.S_IEXEC)
        self.args_file = tmp / "args.json"
        self.env = dict(os.environ, VIBE_BIN=str(self.vibe), FAKE_VIBE_ARGS=str(self.args_file),
                        FAKE_VIBE_HOME=str(tmp / "worktrees"))

    def tearDown(self):
        self.tmp.cleanup()

    def run_delegate(self, *args, behaviour="ok"):
        env = dict(self.env, FAKE_VIBE_BEHAVIOUR=behaviour)
        return subprocess.run([sys.executable, str(SCRIPT), "--workdir", str(self.repo), *args],
                              capture_output=True, text=True, env=env)

    def vibe_args(self):
        return json.loads(self.args_file.read_text())

    def test_read_mode_is_read_only_and_capped(self):
        out = self.run_delegate("--mode", "read", "Find the config parser")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        argv = self.vibe_args()
        self.assertIn("plan", argv)
        self.assertIn("--max-price", argv)
        self.assertIn("--max-turns", argv)
        self.assertNotIn("--auto-approve", argv)
        self.assertNotIn("--worktree", argv)
        enabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--enabled-tools"]
        self.assertEqual(sorted(enabled), ["grep", "read_file", "todo"])
        self.assertIn("status: ok", out.stdout)
        self.assertIn("session_id: sess-123", out.stdout)
        self.assertIn("Done: added new_file.py", out.stdout)
        self.assertIn("bash: pytest: skipped (denied)", out.stdout)

    def test_write_mode_uses_worktree_and_reports_changes(self):
        out = self.run_delegate("--mode", "write", "--worktree-name", "mistral-test", "Add a file")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        argv = self.vibe_args()
        self.assertIn("accept-edits", argv)
        self.assertEqual(argv[argv.index("--worktree") + 1], "mistral-test")
        self.assertIn("worktree_branch: mistral-test", out.stdout)
        self.assertIn("worktree_path:", out.stdout)
        self.assertIn("new_file.py", out.stdout)
        self.assertEqual(subprocess.run(["git", "-C", str(self.repo), "status", "--porcelain"],
                                        capture_output=True, text=True).stdout, "")

    def test_allow_shell_and_resume_are_passed_through(self):
        self.run_delegate("--mode", "write", "--in-place", "--allow-shell", "--resume", "abc", "Fix it")
        argv = self.vibe_args()
        self.assertIn("--auto-approve", argv)
        self.assertEqual(argv[argv.index("--resume") + 1], "abc")
        self.assertNotIn("--worktree", argv)

    def test_limit_reached(self):
        out = self.run_delegate("Big task", behaviour="limit")
        self.assertEqual(out.returncode, 1)
        self.assertIn("status: limit_reached", out.stdout)
        self.assertIn("I got halfway through.", out.stdout)

    def test_vibe_error(self):
        out = self.run_delegate("Task", behaviour="error")
        self.assertEqual(out.returncode, 1)
        self.assertIn("status: error", out.stdout)
        self.assertIn("Invalid API key", out.stdout)

    def test_missing_vibe(self):
        env = dict(self.env, VIBE_BIN="definitely-not-vibe")
        out = subprocess.run([sys.executable, str(SCRIPT), "Task"], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("uv tool install mistral-vibe", out.stdout)

    def test_write_mode_outside_git_requires_in_place(self):
        plain = Path(self.tmp.name) / "plain"
        plain.mkdir()
        out = subprocess.run([sys.executable, str(SCRIPT), "--workdir", str(plain), "--mode", "write", "Task"],
                             capture_output=True, text=True, env=self.env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("--in-place", out.stdout)


if __name__ == "__main__":
    unittest.main()
