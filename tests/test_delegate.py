"""Tests for delegate.py and the SessionStart hook, using a fake `vibe` executable.

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
PLUGIN = ROOT / "plugins/mistral-delegate"
SCRIPT = PLUGIN / "skills/delegate-to-mistral/scripts/delegate.py"
HOOK = PLUGIN / "hooks/session_start.py"

# Mimics the parts of `vibe --prompt ... --output json` the wrapper relies on, including
# the session log that holds the run's cost. In accept-edits/generated-agent runs it
# writes test_app.py; when asked to fix a failing check it also writes fixed.txt.
FAKE_VIBE = textwrap.dedent('''\
    #!/usr/bin/env python3
    import json, os, sys
    argv = sys.argv[1:]
    cwd = os.getcwd()
    prompt = argv[argv.index("--prompt") + 1]
    calls_path = os.environ["FAKE_VIBE_CALLS"]
    calls = json.load(open(calls_path)) if os.path.exists(calls_path) else []
    calls.append({"argv": argv, "cwd": cwd, "prompt": prompt,
                  "sees_draft": os.path.exists("draft.py"),
                  "sees_edit": open("app.py").read() if os.path.exists("app.py") else None,
                  "has_node_modules": os.path.isdir("node_modules")})
    json.dump(calls, open(calls_path, "w"))

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
    agent = argv[argv.index("--agent") + 1]
    if agent != "plan":
        if "failed (exit code" in prompt:
            open("fixed.txt", "w").write("fixed\\n")
        else:
            open("test_app.py", "w").write("def test_app():\\n    assert True\\n")
    entry = {"sessionId": session_id, "createdAt": 0, "updatedAt": 0, "generationStatus": "completed"}
    history = [
        dict(entry, id="1", type="message", role="user", content=[{"type": "text", "text": "task"}]),
        dict(entry, id="2", type="effect", title="read_file", state={"status": "completed", "display": {}}),
        dict(entry, id="3", type="effect", title="bash: pytest", state={"status": "skipped", "reason": "denied", "display": {}}),
        dict(entry, id="4", type="message", role="assistant", content=[{"type": "text", "text": "Done: added test_app.py"}]),
    ]
    print(json.dumps(history, indent=2))
''')


class DelegateTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        tmp = self.tmpdir = Path(self.tmp.name)
        self.repo = tmp / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("print('v1')\n")
        (self.repo / ".gitignore").write_text("node_modules/\n")
        self.git("init", "-q")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
        (self.repo / "node_modules" / "left-pad").mkdir(parents=True)
        bindir = tmp / "bin"
        bindir.mkdir()
        self.vibe = bindir / "vibe"
        self.vibe.write_text(FAKE_VIBE)
        self.vibe.chmod(self.vibe.stat().st_mode | stat.S_IEXEC)
        self.calls_file = tmp / "calls.json"
        self.home = tmp / "md-home"
        self.env = dict(os.environ, VIBE_BIN=str(self.vibe), FAKE_VIBE_CALLS=str(self.calls_file),
                        VIBE_HOME=str(tmp / "vibe-home"), MISTRAL_DELEGATE_HOME=str(self.home),
                        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}")
        for var in ("MISTRAL_DELEGATE_POLICY", "MISTRAL_DELEGATE_MODEL", "MISTRAL_DELEGATE_MAX_PRICE",
                    "MISTRAL_DELEGATE_MAX_TURNS", "MISTRAL_DELEGATE_WORKTREES"):
            self.env.pop(var, None)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def run_delegate(self, *args, behaviour="ok", workdir=None, stdin=None):
        env = dict(self.env, FAKE_VIBE_BEHAVIOUR=behaviour)
        return subprocess.run([sys.executable, str(SCRIPT), "--workdir", str(workdir or self.repo), *args],
                              capture_output=True, text=True, env=env, input=stdin)

    def calls(self):
        return json.loads(self.calls_file.read_text()) if self.calls_file.exists() else []

    def last(self):
        return self.calls()[-1]

    def value(self, out, key):
        for line in out.stdout.splitlines():
            if line.startswith(key + ": "):
                return line[len(key) + 2:]
        self.fail(f"{key} not in report:\n{out.stdout}")

    def profile(self, argv):
        name = argv[argv.index("--agent") + 1]
        return (self.tmpdir / "vibe-home" / "agents" / f"{name}.toml").read_text()


class ReadModeTest(DelegateTestBase):
    def test_read_mode_is_read_only_and_capped(self):
        out = self.run_delegate("--mode", "read", "Find the config parser")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        argv = self.last()["argv"]
        self.assertEqual(argv[argv.index("--agent") + 1], "plan")
        self.assertNotIn("--auto-approve", argv)
        self.assertNotIn("--trust", argv)
        enabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--enabled-tools"]
        self.assertEqual(sorted(enabled), ["grep", "read_file", "todo"])
        self.assertEqual(Path(self.last()["cwd"]).resolve(), self.repo.resolve())
        self.assertIn("status: ok", out.stdout)
        self.assertIn("kind: search", out.stdout)
        self.assertIn("session_id: sess-1234567890", out.stdout)
        self.assertIn("Done: added test_app.py", out.stdout)
        self.assertIn("bash: pytest: skipped (denied)", out.stdout)

    def test_usage_reports_cost_steps_and_tokens(self):
        out = self.run_delegate("--max-price", "0.5", "Task")
        self.assertIn("usage: cost $0.0125 (first-pass cap $0.50), 4 steps, 1,200 tokens", out.stdout)

    def test_model_generates_read_only_profile(self):
        self.run_delegate("--model", "mistral-small", "Task")
        argv = self.last()["argv"]
        self.assertTrue(argv[argv.index("--agent") + 1].startswith("claude-delegate-"))
        profile = self.profile(argv)
        self.assertIn('active_model = "mistral-small"', profile)
        self.assertIn('permission = "never"', profile)
        self.assertNotIn("[tools.bash]", profile)

    def test_unknown_model_is_flagged(self):
        out = self.run_delegate("--model", "mistral-small", "Task")
        self.assertIn("model_warning: model 'mistral-small' is not in Vibe's configured models", out.stdout)
        config = self.tmpdir / "vibe-home" / "config.toml"
        config.write_text('[[models]]\nname = "mistral-small-latest"\nprovider = "mistral"\nalias = "mistral-small"\n')
        out = self.run_delegate("--model", "mistral-small", "Task")
        self.assertNotIn("model_warning", out.stdout)

    def test_spec_and_context_go_into_prompt(self):
        spec = self.tmpdir / "spec.md"
        spec.write_text("Goal: explain the parser.\n")
        self.run_delegate("--spec", str(spec), "--context", "app.py", "")
        prompt = self.last()["prompt"]
        self.assertIn("Implement the spec below.", prompt)
        self.assertIn("## Spec\n\nGoal: explain the parser.", prompt)
        self.assertIn("## Read these files first\n\n- app.py", prompt)

    def test_task_from_stdin(self):
        self.run_delegate("-", stdin="Long task from stdin")
        self.assertIn("Long task from stdin", self.last()["prompt"])

    def test_write_only_flags_rejected_in_read_mode(self):
        out = self.run_delegate("--verify", "true", "Task")
        self.assertEqual(out.returncode, 2)
        self.assertIn("only apply to --mode write", out.stdout)

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

    def test_missing_vibe(self):
        env = dict(self.env, VIBE_BIN="definitely-not-vibe")
        out = subprocess.run([sys.executable, str(SCRIPT), "Task"], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 2)
        self.assertIn("uv tool install mistral-vibe", out.stdout)


class WriteModeTest(DelegateTestBase):
    def test_worktree_sees_uncommitted_work_and_links_deps(self):
        (self.repo / "app.py").write_text("print('v2 uncommitted')\n")
        (self.repo / "draft.py").write_text("x = 1\n")
        out = self.run_delegate("--mode", "write", "--kind", "tests", "Add tests")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        seen = self.last()
        self.assertTrue(seen["sees_draft"])
        self.assertEqual(seen["sees_edit"], "print('v2 uncommitted')\n")
        self.assertTrue(seen["has_node_modules"])
        self.assertIn("--trust", seen["argv"])
        self.assertIn("1 modified, 1 untracked", out.stdout)
        self.assertIn("node_modules", out.stdout.split("linked_from_checkout")[1].split("\n")[0])
        changes = out.stdout.split("changes_by_vibe:")[1].split("diff:")[0]
        self.assertIn("test_app.py", changes)
        self.assertNotIn("draft.py", changes)
        self.assertNotIn("node_modules", changes)
        self.assertIn("+def test_app():", out.stdout)  # small diff is included
        self.assertFalse((self.repo / "test_app.py").exists())

    def test_generated_profile_allows_listed_commands(self):
        self.run_delegate("--mode", "write", "--allow-command", "npm test", "--verify", "true", "Task")
        argv = self.last()["argv"]
        profile = self.profile(argv)
        self.assertIn('permission = "always"', profile)
        self.assertIn('"npm test"', profile)
        self.assertIn('"git diff"', profile)  # Vibe's defaults are kept
        prompt = self.last()["prompt"]
        self.assertIn("You may run these shell commands yourself", prompt)
        self.assertIn("`npm test`", prompt)
        self.assertIn("these checks will be run, and they must pass: `true`", prompt)
        self.assertIn("Don't install, upgrade or remove packages", prompt)
        self.assertNotIn("--auto-approve", argv)

    def test_verify_passes_first_try(self):
        out = self.run_delegate("--mode", "write", "--verify", "test -f test_app.py", "Task")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("verification: passed (first try)", out.stdout)
        self.assertIn("pass: test -f test_app.py", out.stdout)
        self.assertEqual(len(self.calls()), 1)

    def test_failed_check_goes_back_to_vibe_for_a_fix(self):
        out = self.run_delegate("--mode", "write", "--verify", "test -f fixed.txt || (echo 'missing fixed.txt'; exit 3)", "Task")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("verification: passed (after 1 fix attempt)", out.stdout)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        fix = calls[1]
        self.assertEqual(fix["argv"][fix["argv"].index("--resume") + 1], "sess-1234567890")
        self.assertIn("failed (exit code 3)", fix["prompt"])
        self.assertIn("missing fixed.txt", fix["prompt"])
        self.assertEqual(fix["cwd"], calls[0]["cwd"])
        # The fix round gets half the first-pass cap on top of what was spent.
        self.assertEqual(float(fix["argv"][fix["argv"].index("--max-price") + 1]), 0.0125 + 0.5)
        self.assertIn("usage: cost $0.0250", out.stdout)

    def test_check_still_failing_is_reported(self):
        out = self.run_delegate("--mode", "write", "--fix-attempts", "0", "--verify", "echo boom; exit 1", "Task")
        self.assertEqual(out.returncode, 1)
        self.assertIn("verification: failed (first try)", out.stdout)
        self.assertIn("FAIL: echo boom; exit 1 (exit 1)", out.stdout)
        self.assertIn("failing_check_output", out.stdout)
        self.assertIn("boom", out.stdout.split("failing_check_output")[1])

    def test_adopt_applies_changes_and_removes_worktree(self):
        (self.repo / "app.py").write_text("print('v2 uncommitted')\n")
        out = self.run_delegate("--mode", "write", "--kind", "tests", "Task")
        run_id = self.value(out, "run_id")
        path = self.value(out, "worktree_path")
        adopt = subprocess.run(self.value(out, "adopt_with").split("   ")[0], shell=True,
                               capture_output=True, text=True, env=self.env)
        self.assertEqual(adopt.returncode, 0, adopt.stdout + adopt.stderr)
        self.assertIn("test_app.py", adopt.stdout)
        self.assertTrue((self.repo / "test_app.py").exists())
        self.assertEqual((self.repo / "app.py").read_text(), "print('v2 uncommitted')\n")
        self.assertFalse(Path(path).exists())
        self.assertNotIn(run_id, self.git("branch"))
        stats = self.run_delegate("--stats")
        self.assertIn("tests", stats.stdout)
        self.assertIn("1/1", stats.stdout)

    def test_adopt_only_some_paths(self):
        out = self.run_delegate("--mode", "write", "--verify", "true", "--fix-attempts", "0", "Task")
        run_id = self.value(out, "run_id")
        wt = Path(self.value(out, "worktree_path"))
        (wt / "other.py").write_text("y = 2\n")
        res = self.run_delegate("--adopt", run_id, "--paths", "other.py")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertTrue((self.repo / "other.py").exists())
        self.assertFalse((self.repo / "test_app.py").exists())

    def test_discard_removes_worktree_and_records_note(self):
        out = self.run_delegate("--mode", "write", "--kind", "feature", "Task")
        run_id = self.value(out, "run_id")
        path = self.value(out, "worktree_path")
        res = self.run_delegate("--discard", run_id, "--note", "wrong approach")
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertFalse(Path(path).exists())
        status = self.run_delegate("--status").stdout
        self.assertIn(run_id, status)
        self.assertIn("discarded", status)

    def test_status_and_result(self):
        out = self.run_delegate("--mode", "write", "--kind", "docs", "Write docs")
        run_id = self.value(out, "run_id")
        status = self.run_delegate("--status").stdout
        self.assertIn(run_id, status)
        self.assertIn("pending", status)
        self.assertIn("docs/write", status)
        result = self.run_delegate("--result", run_id).stdout
        self.assertIn(f"run_id: {run_id}", result)

    def test_no_snapshot_starts_from_head(self):
        (self.repo / "draft.py").write_text("x = 1\n")
        out = self.run_delegate("--mode", "write", "--no-snapshot", "--no-link-deps", "Task")
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertFalse(self.last()["sees_draft"])
        self.assertFalse(self.last()["has_node_modules"])
        self.assertIn("worktree_base: your HEAD", out.stdout)

    def test_follow_up_reuses_worktree_and_reports_run_cost(self):
        first = self.run_delegate("--mode", "write", "--worktree-name", "mistral-fu", "Task")
        path = self.value(first, "worktree_path")
        second = self.run_delegate("--mode", "write", "--worktree-name", "mistral-fu",
                                   "--resume", "sess-1234567890", "Follow up")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertEqual(self.value(second, "worktree_path"), path)
        self.assertIn("worktree_name: mistral-fu  (reused)", second.stdout)
        self.assertIn("usage: cost $0.0125", second.stdout)
        self.assertIn("session total $0.0250", second.stdout)

    def test_subdirectory_workdir_maps_into_worktree(self):
        sub = self.repo / "pkg"
        sub.mkdir()
        (sub / "mod.py").write_text("")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "pkg")
        out = self.run_delegate("--mode", "write", "Task", workdir=sub)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertEqual(Path(self.last()["cwd"]).name, "pkg")
        self.assertIn("pkg/test_app.py", out.stdout)

    def test_allow_shell_and_in_place(self):
        out = self.run_delegate("--mode", "write", "--in-place", "--allow-shell", "Fix it")
        argv = self.last()["argv"]
        self.assertIn("--auto-approve", argv)
        self.assertNotIn("--trust", argv)
        self.assertTrue((self.repo / "test_app.py").exists())
        self.assertIn("?? test_app.py", out.stdout)

    def test_failed_run_removes_untouched_worktree(self):
        out = self.run_delegate("--mode", "write", "--worktree-name", "mistral-err", "Task", behaviour="error")
        self.assertEqual(out.returncode, 1)
        self.assertIn("worktree: removed mistral-err", out.stdout)
        self.assertNotIn("mistral-err", self.git("branch"))

    def test_write_mode_outside_git_requires_in_place(self):
        plain = self.tmpdir / "plain"
        plain.mkdir()
        out = self.run_delegate("--mode", "write", "Task", workdir=plain)
        self.assertEqual(out.returncode, 2)
        self.assertIn("--in-place", out.stdout)

    def test_existing_branch_without_worktree_is_refused(self):
        self.git("branch", "mistral-taken")
        out = self.run_delegate("--mode", "write", "--worktree-name", "mistral-taken", "Task")
        self.assertEqual(out.returncode, 2)
        self.assertIn("already exists", out.stdout)


class ConfigTest(DelegateTestBase):
    def test_project_config_sets_policy_checks_and_commands(self):
        (self.repo / ".mistral-delegate.toml").write_text(textwrap.dedent('''\
            policy = "aggressive"
            verify = ["test -f test_app.py"]
            allow_commands = ["make test"]
        '''))
        out = self.run_delegate("--mode", "write", "Task")
        self.assertEqual(out.returncode, 0, out.stdout)
        argv = self.last()["argv"]
        self.assertEqual(argv[argv.index("--max-turns") + 1], "50")
        self.assertEqual(float(argv[argv.index("--max-price") + 1]), 2.5)
        self.assertIn("verification: passed", out.stdout)
        self.assertIn('"make test"', self.profile(argv))
        self.assertIn("policy: aggressive", out.stdout)

    def test_flags_override_config(self):
        (self.repo / ".mistral-delegate.toml").write_text('verify = ["exit 1"]\n[write]\nmax_price = 3.0\n')
        out = self.run_delegate("--mode", "write", "--no-verify", "--max-price", "0.4", "Task")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertNotIn("verification", out.stdout)
        argv = self.last()["argv"]
        self.assertEqual(float(argv[argv.index("--max-price") + 1]), 0.4)

    def test_show_config(self):
        (self.repo / ".mistral-delegate.toml").write_text('policy = "conservative"\nmodel = "mistral-small"\n')
        out = self.run_delegate("--show-config")
        self.assertIn("policy: conservative", out.stdout)
        self.assertIn("model: mistral-small", out.stdout)
        self.assertIn("write_caps: max_turns=20 max_price=$0.50", out.stdout)
        self.assertIn("source of policy: project", out.stdout)

    def test_max_parallel_is_enforced(self):
        self.home.mkdir(parents=True)
        (self.home / "config.toml").write_text("max_parallel = 1\n")
        (self.home / "ledger.jsonl").write_text(json.dumps(
            {"event": "start", "id": "mistral-busy", "pid": os.getpid(), "mode": "write", "time": 0}) + "\n")
        out = self.run_delegate("--mode", "write", "Task")
        self.assertEqual(out.returncode, 2)
        self.assertIn("already running", out.stdout)
        self.assertEqual(self.calls(), [])


class HookTest(DelegateTestBase):
    def run_hook(self, env=None):
        out = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"cwd": str(self.repo)}),
                             capture_output=True, text=True, env=env or self.env)
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]

    def test_session_start_context(self):
        (self.repo / ".mistral-delegate.toml").write_text('policy = "aggressive"\nverify = ["npm test"]\n')
        out = self.run_delegate("--mode", "write", "--kind", "tests", "Task")
        run_id = self.value(out, "run_id")
        context = self.run_hook()
        self.assertIn("Delegation policy: aggressive", context)
        self.assertIn(str(SCRIPT), context)
        self.assertIn("Configured checks: npm test", context)
        self.assertIn("tests: 1 runs", context)
        self.assertIn(f"Awaiting --adopt or --discard: {run_id}", context)

    def test_session_start_without_vibe(self):
        env = dict(self.env, PATH="/usr/bin:/bin")
        context = self.run_hook(env)
        self.assertIn("not installed", context)


class ManifestTest(unittest.TestCase):
    def test_json_files_parse(self):
        for path in [ROOT / ".claude-plugin/marketplace.json", PLUGIN / ".claude-plugin/plugin.json",
                     PLUGIN / "hooks/hooks.json"]:
            json.loads(path.read_text())


if __name__ == "__main__":
    unittest.main()
