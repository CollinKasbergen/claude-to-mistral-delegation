"""Tests for delegate.py and the SessionStart hook, using a fake `vibe` executable.

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
import time
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
                  "has_node_modules": os.path.isdir("node_modules"),
                  "node_modules_is_link": os.path.islink("node_modules")})
    json.dump(calls, open(calls_path, "w"))

    resumed = "--resume" in argv
    session_id = argv[argv.index("--resume") + 1] if resumed else "sess-1234567890"
    root = os.path.join(os.environ["VIBE_HOME"], "logs", "session")
    tokens_in, tokens_out = 1000, 200
    if os.environ.get("FAKE_VIBE_STORAGE") == "unified":
        sdir = os.path.join(root, "unified", session_id)
        os.makedirs(os.path.join(sdir, "journal"), exist_ok=True)
        os.makedirs(os.path.join(sdir, "generations", "0001"), exist_ok=True)
        experiments = None
        if os.environ.get("FAKE_VIBE_EXPERIMENT_PRICE"):
            experiments = {"features": {"cli_model_routing": {"value": {"model_config": {
                "alias": os.environ.get("FAKE_VIBE_MODEL"), "input_price": 2.0, "output_price": 10.0}}}}}
        json.dump({"session_id": session_id, "environment": {"working_directory": cwd}, "experiments": experiments},
                  open(os.path.join(sdir, "meta.json"), "w"))
        json.dump({"session_metadata": {"active_model": os.environ.get("FAKE_VIBE_MODEL", "mistral-medium-3.5")}},
                  open(os.path.join(sdir, "generations", "0001", "runtime-state.json"), "w"))
        journal = os.path.join(sdir, "journal", "0001.jsonl")
        prev = 0
        if os.path.exists(journal) and not resumed:
            os.remove(journal)  # a new session starts a fresh journal
        if os.path.exists(journal):
            prev = len(open(journal).read().splitlines())
        usage = {"inputTokens": tokens_in * (prev + 1), "outputTokens": tokens_out * (prev + 1), "totalTokens": 0}
        if os.environ.get("FAKE_VIBE_CACHED"):
            usage["cachedInputTokens"] = 900 * (prev + 1)
        if os.environ.get("FAKE_VIBE_CACHED_COMPLETION"):
            with open(journal, "a") as f:
                f.write(json.dumps({"type": "core_input", "payload": {"input": {"completion": {"usage": {
                    "input_tokens": 1000, "output_tokens": 200, "total_tokens": 1200,
                    "cached_input_tokens": 900}}}}}) + "\\n")
        with open(journal, "a") as f:
            f.write(json.dumps({"type": "projection_delta", "payload": {"delta": [
                {"op": "set_envelope", "state": {"session": {"id": session_id, "tokenUsage": usage}}}]}}) + "\\n")
    else:
        log_dir = os.path.join(root, "session_20260101_" + session_id[:8])
        os.makedirs(log_dir, exist_ok=True)
        meta_path = os.path.join(log_dir, "meta.json")
        stats = {"steps": 0, "session_prompt_tokens": 0, "session_completion_tokens": 0, "session_cost": 0.0}
        if os.path.exists(meta_path):
            stats = json.load(open(meta_path))["stats"]
        stats = {"steps": stats["steps"] + 4, "session_prompt_tokens": stats["session_prompt_tokens"] + tokens_in,
                 "session_completion_tokens": stats["session_completion_tokens"] + tokens_out,
                 "session_cost": stats["session_cost"] + 0.0125}
        json.dump({"session_id": session_id, "environment": {"working_directory": cwd}, "stats": stats},
                  open(meta_path, "w"))

    if os.environ.get("FAKE_VIBE_SPEND") or os.environ.get("FAKE_VIBE_EFFECTS"):
        import time as _t
        sdir = os.path.join(root, "unified", session_id)
        journal = os.path.join(sdir, "journal", "0001.jsonl")
        for step in range(1, 200):
            delta = []
            if os.environ.get("FAKE_VIBE_SPEND"):
                delta.append({"op": "set_envelope", "state": {"session": {"tokenUsage": {
                    "inputTokens": 100000 * step, "outputTokens": 0, "totalTokens": 0}}}})
            if os.environ.get("FAKE_VIBE_EFFECTS"):
                delta.append({"op": "append_entry", "entry": {"id": f"e{step}", "type": "effect"}})
            with open(journal, "a") as f:
                f.write(json.dumps({"type": "projection_delta", "payload": {"delta": delta}}) + "\\n")
            _t.sleep(0.03)
        sys.exit(0)
    behaviour = os.environ.get("FAKE_VIBE_BEHAVIOUR", "ok")
    if behaviour == "limit":
        print("I got halfway through.", file=sys.stderr)
        sys.exit(1)
    if behaviour == "error":
        print("Error: Invalid API key", file=sys.stderr)
        sys.exit(1)
    hook_results = []
    if os.environ.get("FAKE_VIBE_HOOK_CALLS"):
        import subprocess, tomllib
        hooks = tomllib.load(open(os.path.join(os.environ["VIBE_HOME"], "hooks.toml"), "rb"))["hooks"]
        for tool, tool_input in json.loads(os.environ["FAKE_VIBE_HOOK_CALLS"]):
            for hook in hooks:
                event = {"session_id": session_id, "transcript_path": "", "cwd": cwd,
                         "hook_event_name": "pre_tool", "tool_name": tool, "tool_call_id": "t1",
                         "tool_input": tool_input}
                out = subprocess.run(hook["command"], shell=True, input=json.dumps(event),
                                     capture_output=True, text=True)
                hook_results.append(json.loads(out.stdout) if out.stdout.strip() else None)
        calls[-1]["hook_results"] = hook_results
        json.dump(calls, open(calls_path, "w"))
    agent = argv[argv.index("--agent") + 1]
    if os.environ.get("FAKE_VIBE_NO_WRITE"):
        agent = "plan"
    if agent != "plan":
        if "failed (exit code" in prompt:
            open("fixed.txt", "w").write("fixed\\n")
        else:
            open("test_app.py", "w").write("def test_app():\\n    assert True\\n")
            if os.environ.get("FAKE_VIBE_TOUCH_APP"):
                open("app.py", "a").write("# changed by vibe\\n")
    entry = {"sessionId": session_id, "createdAt": 0, "updatedAt": 0, "generationStatus": "completed"}
    turn = [
        dict(entry, id=f"u{len(calls)}", type="message", role="user", source="turn_start",
             content=[{"type": "text", "text": prompt}]),
        dict(entry, id=f"r{len(calls)}", type="effect", title="Read file",
             detail={"kind": "file_read", "toolName": "read_file", "input": {"filePath": "app.py"}},
             state={"status": "completed", "display": {}}),
        dict(entry, id=f"b{len(calls)}", type="effect", title="Run command",
             detail={"kind": "shell", "toolName": "bash", "input": {"command": "npx vitest run"}},
             state={"status": "skipped", "reason": "denied", "display": {}}),
        dict(entry, id=f"a{len(calls)}", type="message", role="assistant",
             content=[{"type": "text", "text": "Done: added test_app.py"},
                      {"type": "text", "text": "Done: added test_app.py"}]),
    ]
    if os.environ.get("FAKE_VIBE_UNFINISHED") and "You stopped before finishing" not in prompt:
        turn.append(dict(entry, id=f"t{len(calls)}", type="effect", title="Read file",
                         detail={"toolName": "read_file", "input": {"path": "app.py"}}, state={"status": "completed"}))
    if os.environ.get("FAKE_VIBE_REWRITE_NOTICES"):
        for i in range(3):
            turn.append(dict(entry, id=f"n{len(calls)}{i}", type="notice", level="warning", detail={},
                             message="Rewrote tool_input for 'bash'"))
    if os.environ.get("FAKE_VIBE_CANCEL"):
        turn[-1] = dict(turn[-1], content=[{"type": "text", "text": "<user_cancellation>User cancelled the operation.</user_cancellation>"}])
    # Like the real CLI, a resumed session prints the whole history, earlier turns included.
    history = [dict(entry, id="old-u", type="message", role="user", source="turn_start",
                    content=[{"type": "text", "text": "earlier"}]),
               dict(entry, id="old-b", type="effect", title="Run command",
                    detail={"kind": "shell", "toolName": "bash", "input": {"command": "rm -rf /tmp/x"}},
                    state={"status": "skipped", "reason": "denied", "display": {}})] if resumed else []
    print(json.dumps(history + turn, indent=2))
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
                        PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}", MISTRAL_DELEGATE_WATCH_INTERVAL="0.05")
        for var in ("MISTRAL_DELEGATE_POLICY", "MISTRAL_DELEGATE_MODEL", "MISTRAL_DELEGATE_MAX_PRICE",
                    "MISTRAL_DELEGATE_MAX_TURNS", "MISTRAL_DELEGATE_WORKTREES", "FAKE_VIBE_STORAGE",
                    "FAKE_VIBE_TOUCH_APP", "FAKE_VIBE_HOOK_CALLS", "FAKE_VIBE_NO_WRITE", "FAKE_VIBE_CANCEL",
                    "FAKE_VIBE_MODEL", "FAKE_VIBE_EXPERIMENT_PRICE", "FAKE_VIBE_SPEND", "FAKE_VIBE_EFFECTS",
                    "FAKE_VIBE_UNFINISHED", "FAKE_VIBE_CACHED", "FAKE_VIBE_CACHED_COMPLETION", "FAKE_VIBE_REWRITE_NOTICES"):
            self.env.pop(var, None)

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True,
                              capture_output=True, text=True).stdout

    def run_delegate(self, *args, behaviour="ok", workdir=None, stdin=None, **extra_env):
        env = dict(self.env, FAKE_VIBE_BEHAVIOUR=behaviour, **extra_env)
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
        self.assertIn("denied_commands (read mode runs no commands):\n  - bash: npx vitest run", out.stdout)

    def test_usage_reports_cost_steps_and_tokens(self):
        out = self.run_delegate("--max-price", "0.5", "Task")
        self.assertIn("usage: 2,000 effective tokens (input 1,000 fresh + 0 cached, output 200)", out.stdout)
        self.assertIn("tool calls: 2, model steps: 4 (max_turns 15)", out.stdout)

    def test_cost_estimated_from_unified_harness_journal(self):
        out = self.run_delegate("Task", FAKE_VIBE_STORAGE="unified")
        # 1000 input tokens at $1.5/M + 200 output tokens at $7.5/M
        self.assertIn("usage: 2,000 effective tokens (input 1,000 fresh + 0 cached, output 200); "
                      "~$0.0030 at mistral-medium-3.5 list prices", out.stdout)

    def test_unified_follow_up_reports_this_runs_tokens(self):
        self.run_delegate("Task", FAKE_VIBE_STORAGE="unified")
        out = self.run_delegate("--resume", "sess-1234567890", "More", FAKE_VIBE_STORAGE="unified")
        self.assertIn("~$0.0030 at mistral-medium-3.5 list prices (session total ~$0.0060)", out.stdout)

    def test_result_text_is_not_duplicated(self):
        out = self.run_delegate("Task")
        self.assertEqual(out.stdout.count("Done: added test_app.py"), 1)
        self.assertEqual(out.stdout.count("--- result from Mistral Vibe ---"), 1)

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
        self.assertIn("## Read these files first (paths relative to the project root)\n\n- app.py", prompt)
        self.assertIn(f"The project root is `{os.path.realpath(self.repo)}`", prompt)
        self.assertIn("read_file and grep tools rather than shell commands", prompt)

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
        self.assertIn("usage: 2,000 effective tokens", out.stdout)
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
        self.assertIn("dependencies (hard-linked copies from your checkout): node_modules", out.stdout)
        self.assertFalse(seen["node_modules_is_link"])
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
        check = "test ! -f test_app.py || test -f fixed.txt || (echo 'missing fixed.txt'; exit 3)"
        out = self.run_delegate("--mode", "write", "--verify", check, "Task")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("verification: passed (after 1 fix attempt)", out.stdout)
        calls = self.calls()
        self.assertEqual(len(calls), 2)
        fix = calls[1]
        self.assertEqual(fix["argv"][fix["argv"].index("--resume") + 1], "sess-1234567890")
        self.assertIn("failed (exit code 3)", fix["prompt"])
        self.assertIn("missing fixed.txt", fix["prompt"])
        self.assertEqual(fix["cwd"], calls[0]["cwd"])
        self.assertNotIn("--max-price", fix["argv"])
        self.assertIn("usage: 4,000 effective tokens", out.stdout)  # both calls

    def test_check_still_failing_is_reported(self):
        check = "test ! -f test_app.py || (echo boom; exit 1)"
        out = self.run_delegate("--mode", "write", "--fix-attempts", "0", "--verify", check, "Task")
        self.assertEqual(out.returncode, 1)
        self.assertIn("verification: failed (first try)", out.stdout)
        self.assertIn(f"FAIL: {check} (exit 1)", out.stdout)
        self.assertIn("failing_check_output", out.stdout)
        self.assertIn("boom", out.stdout.split("failing_check_output")[1])

    def test_guard_refuses_with_an_error_and_is_removed_after_the_run(self):
        calls = [["bash", {"command": "node -e 1"}], ["read_file", {"path": "/app.py"}],
                 ["bash", {"command": "cat app.py | grep print"}]]
        out = self.run_delegate("--mode", "write", "Task", FAKE_VIBE_HOOK_CALLS=json.dumps(calls))
        self.assertEqual(out.returncode, 0, out.stdout)
        results = self.last()["hook_results"]
        self.assertEqual(results[0]["decision"], "deny")
        self.assertIn("`node -e 1` isn't allowed", results[0]["reason"])
        self.assertTrue(results[1]["hook_specific_output"]["tool_input"]["path"].endswith("/app.py"))
        self.assertIsNone(results[2])
        self.assertIn("guard: checked 3 tool calls, refused 1", out.stdout)
        self.assertIn("corrected 1 path", out.stdout)
        self.assertIn("refused_by_guard", out.stdout)
        self.assertIn("bash: npx vitest run", out.stdout.split("denied_commands")[1])  # Vibe-side denial still shown
        # Outside a run the hook does nothing.
        hooks = (self.tmpdir / "vibe-home" / "hooks.toml").read_text()
        self.assertIn("mistral-delegate-guard", hooks)
        self.assertEqual(list((self.home / "guards").glob("*.json")), [])

    def test_checks_limited_to_other_paths_are_skipped(self):
        (self.repo / ".mistral-delegate.toml").write_text(textwrap.dedent("""\
            verify = [
              "true",
              { cmd = "test -f frontend_check_ran || touch frontend_check_ran", paths = ["frontend/"] },
              { cmd = "touch backend_check_ran", paths = ["backend/**"] },
            ]
        """))
        out = self.run_delegate("--mode", "write", "--scope", "frontend/src/**", "Task")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("checks_skipped (limited to paths this run doesn't touch): touch backend_check_ran", out.stdout)
        wt = Path(self.value(out, "worktree_path"))
        self.assertFalse((wt / "backend_check_ran").exists())
        self.assertIn("pass: test -f frontend_check_ran", out.stdout)

    def test_flaky_baseline_check_is_rerun(self):
        check = "test -f .flaky_once || { touch .flaky_once; exit 1; }"
        out = self.run_delegate("--mode", "write", "--verify", check, "Task")
        self.assertIn("flaky_checks (failed, then passed on a rerun before Mistral started)", out.stdout)
        self.assertNotIn("baseline_warning", out.stdout)
        self.assertIn("verification: passed", out.stdout)

    def test_sed_is_allowed_in_the_profile_only_with_the_guard(self):
        self.run_delegate("--mode", "write", "Task")
        self.assertIn('"sed"]', self.profile(self.last()["argv"]))
        (self.tmpdir / "vibe-home" / "hooks.toml").write_text("[[hooks]\nbroken")  # guard can't install
        self.run_delegate("--mode", "write", "Task")
        self.assertNotIn('"sed"', self.profile(self.last()["argv"]))

    def test_model_is_named_and_non_mistral_default_is_flagged(self):
        out = self.run_delegate("Task", FAKE_VIBE_STORAGE="unified", FAKE_VIBE_MODEL="glm-5-3")
        self.assertIn("model: glm-5-3 (Vibe's default; not pinned)", out.stdout)
        self.assertIn("model_note: Vibe's server-side default routed this run to 'glm-5-3'", out.stdout)
        self.assertIn('add model_prices = { "glm-5-3" = [input, output, cached] }', out.stdout)

    def test_price_from_session_experiments(self):
        out = self.run_delegate("Task", FAKE_VIBE_STORAGE="unified", FAKE_VIBE_MODEL="glm-5-3",
                                FAKE_VIBE_EXPERIMENT_PRICE="1")
        # 1000 in at $2/M + 200 out at $10/M
        self.assertIn("~$0.0040 at glm-5-3 list prices", out.stdout)

    def test_model_prices_setting_prices_new_and_old_runs(self):
        self.run_delegate("Task", FAKE_VIBE_STORAGE="unified", FAKE_VIBE_MODEL="glm-5-3")
        self.assertIn("1 run(s) without a price", self.run_delegate("--stats").stdout)
        (self.repo / ".mistral-delegate.toml").write_text('model_prices = { "glm-5-3" = [1.0, 4.0] }\n')
        out = self.run_delegate("Task", FAKE_VIBE_STORAGE="unified", FAKE_VIBE_MODEL="glm-5-3")
        self.assertIn("~$0.0018 at glm-5-3 list prices", out.stdout)  # 1000 * 1 + 200 * 4 per million
        stats = self.run_delegate("--stats").stdout
        self.assertNotIn("without a price", stats)  # the earlier run is priced from its tokens now
        self.assertIn("$0.0", stats)

    def test_command_spellings_are_expanded(self):
        (self.repo / "package.json").write_text('{"scripts": {"test": "vitest run", "build": "rm -rf dist && vite build"}}')
        out = self.run_delegate("--mode", "write", "--allow-command", "npm test", "Task")
        profile = self.profile(self.last()["argv"])
        for spelling in ('"npm run test"', '"npx vitest"', '"pnpm test"'):
            self.assertIn(spelling, profile)
        self.assertNotIn('"npx rm"', profile)
        self.assertIn("vibe_may_run: npm test (also accepted:", out.stdout)

    def test_missing_folders_for_literal_scope_paths_are_created(self):
        out = self.run_delegate("--mode", "write", "--scope", "frontend/src/new/thing.test.ts", "Task")
        wt = Path(self.value(out, "worktree_path"))
        self.assertTrue((wt / "frontend/src/new").is_dir())
        self.assertFalse((wt / "frontend/src/new/thing.test.ts").exists())

    def test_status_shows_start_time_and_duration(self):
        self.run_delegate("Task")
        status = self.run_delegate("--status").stdout
        self.assertIn("started", status.splitlines()[0])
        self.assertIn("took", status.splitlines()[0])
        self.assertIn("today ", status)

    def test_price_cap_is_enforced_by_the_wrapper(self):
        out = self.run_delegate("--mode", "write", "--max-price", "0.5", "Task",
                                FAKE_VIBE_STORAGE="unified", FAKE_VIBE_MODEL="glm-5-3", FAKE_VIBE_SPEND="1")
        self.assertIn("status: budget_exceeded", out.stdout)
        self.assertIn("over this call's max_price of $0.50", out.stdout)
        self.assertIn("priced at mistral-medium-3.5 rates", out.stdout)
        spent = float(out.stdout.split("stopped Mistral at ~$")[1].split(",")[0])
        self.assertLess(spent, 1.0)  # stopped soon after crossing $0.50, not after $30

    def test_tool_call_cap_is_enforced_by_the_wrapper(self):
        out = self.run_delegate("--mode", "write", "--max-tool-calls", "5", "Task",
                                FAKE_VIBE_STORAGE="unified", FAKE_VIBE_EFFECTS="1")
        self.assertIn("status: tool_call_limit", out.stdout)
        self.assertIn("over the cap of 5", out.stdout)

    def test_resume_baseline_comes_from_before_mistrals_changes(self):
        check = "test ! -f test_app.py"
        first = self.run_delegate("--mode", "write", "--worktree-name", "mistral-bl", "--fix-attempts", "0",
                                  "--verify", check, "Task")
        self.assertIn("verification: failed", first.stdout)
        second = self.run_delegate("--mode", "write", "--worktree-name", "mistral-bl", "--fix-attempts", "0",
                                   "--resume", "sess-1234567890", "--verify", check, "--verify", "test ! -f test_app.py -o -f x",
                                   "Fix it")
        # Mistral's own failure stays Mistral's: not "already failing before Mistral".
        self.assertIn("verification: failed", second.stdout)
        self.assertNotIn("passed_except_preexisting", second.stdout)
        self.assertNotIn("baseline_warning", second.stdout)
        self.assertIn("baseline: from before Mistral's earlier changes in this worktree", second.stdout)
        self.assertIn("continues: ", second.stdout)

    def test_unfinished_run_is_asked_to_finish_once(self):
        out = self.run_delegate("--mode", "write", "Task", FAKE_VIBE_UNFINISHED="1")
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("You stopped before finishing", self.calls()[1]["prompt"])
        self.assertIn("continued: Mistral's work looked unfinished", out.stdout)
        self.assertNotIn("final_message_warning", out.stdout)

    def test_adopting_a_resume_settles_the_earlier_run_too(self):
        first = self.run_delegate("--mode", "write", "--worktree-name", "mistral-pair", "Task")
        second = self.run_delegate("--mode", "write", "--worktree-name", "mistral-pair",
                                   "--resume", "sess-1234567890", "More")
        self.assertIn(f"continues: {self.value(first, 'run_id')}", second.stdout)
        self.run_delegate("--adopt", self.value(second, "run_id"))
        status = self.run_delegate("--status").stdout
        self.assertNotIn("pending", status.split("started:")[0].split("\n", 1)[1])

    def test_runners_are_allowed_in_the_profile_with_the_guard(self):
        self.run_delegate("--mode", "write", "--allow-command", "uv run pytest", "Task")
        self.assertIn('"uv run"', self.profile(self.last()["argv"]))

    def test_cached_tokens_are_priced_at_the_cached_rate(self):
        for env in ({"FAKE_VIBE_CACHED": "1"}, {"FAKE_VIBE_CACHED_COMPLETION": "1"}):
            with self.subTest(env=env):
                out = self.run_delegate("Task", FAKE_VIBE_STORAGE="unified", **env)
                # 100 fresh * 1.5 + 900 cached * 0.15 + 200 out * 7.5, per million
                self.assertIn("usage: 1,190 effective tokens (input 100 fresh + 900 cached, output 200); "
                              "~$0.0018 at mistral-medium-3.5", out.stdout)

    def test_token_budget_is_enforced(self):
        out = self.run_delegate("--mode", "write", "--token-budget", "300000", "Task",
                                FAKE_VIBE_STORAGE="unified", FAKE_VIBE_SPEND="1", FAKE_VIBE_EFFECTS="1")
        self.assertIn("status: budget_exceeded", out.stdout)
        self.assertIn("over this call's budget of 300,000", out.stdout)
        calls = int(out.stdout.split("tool calls: ")[1].split(",")[0].split("\n")[0])
        self.assertGreater(calls, 0)  # counted live, although Vibe printed nothing

    def test_autofix_runs_before_a_fix_round(self):
        (self.repo / ".mistral-delegate.toml").write_text('autofix = ["touch fixed.txt"]\n')
        out = self.run_delegate("--mode", "write", "--verify", "test ! -f test_app.py || test -f fixed.txt", "Task")
        self.assertIn("verification: passed", out.stdout)
        self.assertIn("autofix: ran touch fixed.txt", out.stdout)
        self.assertEqual(len(self.calls()), 1)

    def test_fix_round_after_a_cap_stop(self):
        out = self.run_delegate("--mode", "write", "--token-budget", "300000", "--verify", "test -f never.txt",
                                "--no-baseline", "Task", FAKE_VIBE_STORAGE="unified", FAKE_VIBE_SPEND="1")
        self.assertIn("status: budget_exceeded", out.stdout)
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("failed (exit code", self.calls()[1]["prompt"])

    def test_monthly_credit_is_tracked(self):
        (self.repo / ".mistral-delegate.toml").write_text('monthly_credit = 10\ncurrency = "€"\n')
        out = self.run_delegate("Task", FAKE_VIBE_STORAGE="unified")
        self.assertIn("credit: ~€0.00 of €10.00 used since", out.stdout)
        second = self.run_delegate("Task", FAKE_VIBE_STORAGE="unified").stdout
        self.assertIn("credit: ~€0.01 of €10.00", second)  # both runs: 2 x 0.0030, shown in cents
        self.assertIn("credit: ~€", self.run_delegate("--stats").stdout)

    def test_follow_up_is_listed_under_the_original_task(self):
        self.run_delegate("--mode", "write", "--worktree-name", "mistral-lbl", "Add the teams endpoint")
        self.run_delegate("--mode", "write", "--worktree-name", "mistral-lbl", "--resume", "sess-1234567890",
                          "You were stopped before finishing, continue")
        status = self.run_delegate("--status").stdout
        self.assertIn("Add the teams endpoint [follow-up]", status)
        self.assertNotIn("You were stopped", status)

    def test_savings_are_recorded_and_shown(self):
        out = self.run_delegate("--mode", "write", "--kind", "tests", "Task")
        self.run_delegate("--adopt", self.value(out, "run_id"))
        stats = self.run_delegate("--stats").stdout
        self.assertRegex(stats, r"tests .* x\d")

    def test_autofix_entries_can_be_limited_to_paths(self):
        (self.repo / ".mistral-delegate.toml").write_text(textwrap.dedent("""\
            autofix = [
              { cmd = "touch frontend_fixed", paths = ["frontend/"] },
              { cmd = "touch backend_fixed", paths = ["backend/"] },
            ]
        """))
        out = self.run_delegate("--mode", "write", "--scope", "frontend/src/**", "--fix-attempts", "0",
                                "--verify", "test ! -f test_app.py", "Task")
        self.assertIn("autofix: ran touch frontend_fixed", out.stdout)
        self.assertNotIn("backend_fixed", out.stdout)

    def test_no_paid_continuation_when_changes_pass_the_checks(self):
        out = self.run_delegate("--mode", "write", "--verify", "true", "Task", FAKE_VIBE_UNFINISHED="1")
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("wasn't asked to finish", out.stdout)
        self.assertNotIn("final_message_warning", out.stdout)

    def test_continuation_when_checks_fail(self):
        out = self.run_delegate("--mode", "write", "--fix-attempts", "0", "--verify", "test ! -f test_app.py",
                                "Task", FAKE_VIBE_UNFINISHED="1")
        self.assertEqual(len(self.calls()), 2)
        self.assertIn("You stopped before finishing", self.calls()[1]["prompt"])

    def test_full_diff_is_saved_and_outlives_the_worktree(self):
        out = self.run_delegate("--mode", "write", "--diff-lines", "0", "Task")
        self.assertIn("diff: ", out.stdout)
        self.assertIn("too long to show here. Read it from ", out.stdout)
        diff_file = Path(out.stdout.split("Read it from ")[1].split("\n")[0])
        self.assertIn("+def test_app():", diff_file.read_text())
        self.run_delegate("--adopt", self.value(out, "run_id"))
        self.assertTrue(diff_file.exists())

    def test_guard_rewrite_notices_are_collapsed(self):
        out = self.run_delegate("Task", FAKE_VIBE_REWRITE_NOTICES="1")
        self.assertNotIn("Rewrote tool_input for 'bash'", out.stdout)
        self.assertIn("3 tool-input rewrite notice(s)", out.stdout)

    def test_resume_with_narrower_scope_keeps_the_earlier_runs_files(self):
        first = self.run_delegate("--mode", "write", "--worktree-name", "mistral-narrow", "--scope", "test_app.py",
                                  "--scope", "app.py", "Task", FAKE_VIBE_TOUCH_APP="1")
        self.assertNotIn("out_of_scope_changes", first.stdout)
        second = self.run_delegate("--mode", "write", "--worktree-name", "mistral-narrow", "--scope", "test_app.py",
                                   "--resume", "sess-1234567890", "Fix the test")
        self.assertNotIn("out_of_scope_changes", second.stdout)  # app.py is in the earlier run's scope
        adopt = self.run_delegate("--adopt", self.value(second, "run_id"))
        self.assertEqual(adopt.returncode, 0, adopt.stdout)
        self.assertIn("# changed by vibe", (self.repo / "app.py").read_text())

    def test_missing_file_named_in_scope_means_unfinished(self):
        out = self.run_delegate("--mode", "write", "--scope", "tests/test_new.py", "--scope", "test_app.py",
                                "--verify", "true", "Write the tests")
        self.assertEqual(len(self.calls()), 2)  # asked once to create the missing file
        self.assertIn("tests/test_new.py", self.calls()[1]["prompt"])
        self.assertEqual(out.returncode, 1)
        self.assertIn("status: incomplete", out.stdout)
        self.assertIn("missing_files (named in --scope but never created", out.stdout)

    def test_failed_autofix_shows_why(self):
        (self.repo / ".mistral-delegate.toml").write_text(
            "autofix = [\"echo 'eslint: 2 problems (2 errors)'; exit 1\"]\n")
        out = self.run_delegate("--mode", "write", "--fix-attempts", "0", "--verify", "test ! -f test_app.py", "Task")
        self.assertIn("(exit 1: eslint: 2 problems (2 errors))", out.stdout)

    def test_weak_tests_are_flagged(self):
        out = self.run_delegate("--mode", "write", "--verify", "test ! -f nothing_here", "Task",
                                FAKE_VIBE_TOUCH_APP="1")
        self.assertIn("test_strength_warning: Mistral's tests still pass on the original code", out.stdout)
        self.assertIn("app.py", out.stdout.split("test_strength_warning")[1].split("\n")[0])

    def test_tests_that_need_the_change_pass_the_strength_check(self):
        out = self.run_delegate("--mode", "write", "--verify", "test ! -f test_app.py || grep -q 'changed by vibe' app.py",
                                "Task", FAKE_VIBE_TOUCH_APP="1")
        self.assertIn("verification: passed", out.stdout)
        self.assertIn("test_strength: Mistral's tests fail on the original code, as they should", out.stdout)

    def test_no_changes_is_its_own_status(self):
        out = self.run_delegate("--mode", "write", "Task", FAKE_VIBE_NO_WRITE="1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("status: no_changes", out.stdout)
        self.assertIn("finished without changing any file", out.stdout)

    def test_cancelled_session_is_reported(self):
        out = self.run_delegate("--mode", "write", "Task", FAKE_VIBE_CANCEL="1")
        self.assertIn("status: stopped_by_refusal", out.stdout)
        self.assertIn("Resume with --resume", out.stdout)

    def test_worktrees_dir_from_config(self):
        target = self.tmpdir / "ssd-worktrees"
        (self.repo / ".mistral-delegate.toml").write_text(f'worktrees_dir = "{target}"\n')
        out = self.run_delegate("--mode", "write", "Task")
        self.assertTrue(self.value(out, "worktree_path").startswith(str(target)))

    def test_preexisting_failure_is_not_blamed_on_mistral(self):
        out = self.run_delegate("--mode", "write", "--verify", "test -f never.txt", "--verify", "true", "Task")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("verification: passed_except_preexisting (first try)", out.stdout)
        self.assertIn("already failing before Mistral changed anything", out.stdout)
        self.assertIn("baseline_warning:", out.stdout)
        self.assertEqual(len(self.calls()), 1)  # no fix round for a failure that was already there
        self.assertIn("These checks already fail before your change", self.last()["prompt"])

    def test_no_baseline_flag(self):
        out = self.run_delegate("--mode", "write", "--no-baseline", "--fix-attempts", "0",
                                "--verify", "test -f never.txt", "Task")
        self.assertIn("verification: failed", out.stdout)
        self.assertNotIn("baseline_warning", out.stdout)

    def test_denied_commands_are_named_once_per_turn(self):
        check = "test ! -f test_app.py || test -f fixed.txt"
        out = self.run_delegate("--mode", "write", "--verify", check, "Task")
        self.assertEqual(len(self.calls()), 2)
        denied = out.stdout.split("denied_commands")[1].split("\n\n")[0]
        self.assertIn("bash: npx vitest run  (2x)", denied)  # once in each of the two turns
        self.assertNotIn("rm -rf", denied)  # earlier turns repeated in resumed output are not recounted
        self.assertIn("tool calls: 4", out.stdout)
        stats = self.run_delegate("--stats").stdout
        self.assertIn("most denied commands", stats)
        self.assertIn("bash: npx vitest run", stats)

    def test_scope_is_in_prompt_and_out_of_scope_changes_are_flagged_and_not_adopted(self):
        out = self.run_delegate("--mode", "write", "--scope", "test_*.py", "Add tests", FAKE_VIBE_TOUCH_APP="1")
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("Only create or change files matching: `test_*.py`", self.last()["prompt"])
        self.assertIn("out_of_scope_changes", out.stdout)
        self.assertIn("app.py", out.stdout.split("out_of_scope_changes")[1].split("\n")[0])
        self.assertLess(out.stdout.index("out_of_scope_changes"), out.stdout.index("usage:"))  # near the top
        run_id = self.value(out, "run_id")
        refused = self.run_delegate("--adopt", run_id)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("Nothing applied: 1 changed file(s)", refused.stdout)
        self.assertIn("  app.py", refused.stdout)
        self.assertFalse((self.repo / "test_app.py").exists())
        adopt = self.run_delegate("--adopt", run_id, "--skip-out-of-scope")
        self.assertEqual(adopt.returncode, 0, adopt.stdout)
        self.assertIn("Left out", adopt.stdout)
        self.assertTrue((self.repo / "test_app.py").exists())
        self.assertEqual((self.repo / "app.py").read_text(), "print('v1')\n")
        self.assertIn("partial", self.run_delegate("--status").stdout)
        self.assertIn("+1 partial", self.run_delegate("--stats").stdout)

    def test_include_out_of_scope(self):
        out = self.run_delegate("--mode", "write", "--scope", "test_*.py", "Add tests", FAKE_VIBE_TOUCH_APP="1")
        adopt = self.run_delegate("--adopt", self.value(out, "run_id"), "--include-out-of-scope")
        self.assertEqual(adopt.returncode, 0, adopt.stdout)
        self.assertIn("# changed by vibe", (self.repo / "app.py").read_text())

    def test_hardlinked_dependencies_share_files_but_skip_caches(self):
        pkg = self.repo / "node_modules" / "left-pad"
        (pkg / "index.js").write_text("module.exports = 1\n")
        (self.repo / "node_modules" / ".vite").mkdir()
        (self.repo / "node_modules" / ".vite" / "cache.json").write_text("{}")
        out = self.run_delegate("--mode", "write", "Task")
        wt = Path(self.value(out, "worktree_path"))
        copy = wt / "node_modules" / "left-pad" / "index.js"
        self.assertFalse((wt / "node_modules").is_symlink())
        self.assertEqual(copy.stat().st_ino, (pkg / "index.js").stat().st_ino)
        self.assertFalse((wt / "node_modules" / ".vite").exists())
        self.assertNotIn("node_modules", out.stdout.split("changes_by_vibe:")[1].split("adopt_with")[0])

    def test_symlink_and_copy_deps_modes(self):
        out = self.run_delegate("--mode", "write", "--deps-mode", "symlink", "Task")
        self.assertTrue(self.last()["node_modules_is_link"])
        self.assertIn("dependencies (symlinks from your checkout)", out.stdout)
        out = self.run_delegate("--mode", "write", "--deps-mode", "copy", "Task")
        self.assertFalse(self.last()["node_modules_is_link"])
        self.assertIn("dependencies (copies from your checkout)", out.stdout)

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
        self.assertIn("usage: 2,000 effective tokens", second.stdout)
        self.assertIn("(session total ~$0.0060)", second.stdout)

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
        self.assertNotIn("--max-price", argv)  # no money cap unless one is configured
        self.assertIn("caps the first pass at 2,500,000 effective tokens and 150 tool calls", out.stdout)
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

    def test_vibe_args_are_passed_through(self):
        (self.repo / ".mistral-delegate.toml").write_text('vibe_args = ["--legacy-harness"]\n')
        self.run_delegate("Task")
        self.assertEqual(self.last()["argv"][-1], "--legacy-harness")

    def test_deps_mode_and_baseline_from_config(self):
        (self.repo / ".mistral-delegate.toml").write_text('deps_mode = "symlink"\nbaseline = false\nscope = ["tests/**"]\n')
        out = self.run_delegate("--show-config")
        self.assertIn("deps_mode: symlink", out.stdout)
        self.assertIn("baseline: off", out.stdout)
        self.assertIn("scope: tests/**", out.stdout)

    def test_show_config(self):
        (self.repo / ".mistral-delegate.toml").write_text('policy = "conservative"\nmodel = "mistral-small"\n')
        out = self.run_delegate("--show-config")
        self.assertIn("policy: conservative", out.stdout)
        self.assertIn("model: mistral-small", out.stdout)
        self.assertIn("write_caps: token_budget=400,000 effective tokens, max_tool_calls=50, max_turns=20", out.stdout)
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

    def test_session_start_credit_and_low_savings(self):
        (self.repo / ".mistral-delegate.toml").write_text('monthly_credit = 225\ncurrency = "€"\nmin_savings = 2\n')
        self.home.mkdir(parents=True, exist_ok=True)
        with open(self.home / "ledger.jsonl", "w") as f:
            for i in range(3):
                f.write(json.dumps({"event": "start", "id": f"d{i}", "kind": "docs", "mode": "write", "pid": 0,
                                    "time": time.time()}) + "\n")
                f.write(json.dumps({"event": "end", "id": f"d{i}", "status": "ok", "cost": 1.0, "tokens": 10,
                                    "claude_overhead": 1000, "claude_equivalent": 1500, "time": time.time()}) + "\n")
                f.write(json.dumps({"event": "outcome", "id": f"d{i}", "outcome": "discarded", "time": time.time()}) + "\n")
        context = self.run_hook()
        self.assertIn("Delegation hasn't paid off for: docs (x0.0)", context)
        self.assertIn("Mistral credit: ~€3.00 of €225.00 used since", context)
        self.assertIn("plenty left", context)

    def test_session_start_without_vibe(self):
        env = dict(self.env, PATH="/usr/bin:/bin")
        context = self.run_hook(env)
        self.assertIn("not installed", context)



sys.path.insert(0, str(SCRIPT.parent))
from mdelegate import gitops, guard  # noqa: E402


class GuardPolicyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self.tmp.name)
        Path(self.root, "frontend/src/router").mkdir(parents=True)
        Path(self.root, "frontend/src/router/index.ts").write_text("export {}\n")
        Path(self.root, ".env").write_text("SECRET=1\n")
        self.policy = {"root": self.root, "mode": "write", "scope": ["frontend/src/**"],
                       "allow_commands": ["cat", "grep", "ls", "find", "tail", "git diff", "npm test", "npx vitest run"],
                       "default_commands": ["cat", "grep", "ls", "find", "tail", "git diff"], "allow_shell": False}

    def tearDown(self):
        self.tmp.cleanup()

    def shell(self, command):
        return guard.check_shell(command, self.policy, self.root)

    def test_allowed_commands(self):
        for command in ["cat frontend/src/router/index.ts | grep export", "npm test -- --run", "CI=1 npm test",
                        "grep -rn foo . 2>/dev/null", 'grep "a|b;c" frontend', "ls && git diff",
                        "npx vitest run src/x.test.ts 2>&1 | tail",
                        "sed -n 1,20p frontend/src/router/index.ts | grep x", "sed -n '/export/p' frontend/src/router/index.ts",
                        "sed -nE '/^export/,/^}/p' frontend/src/router/index.ts", "sed -E -n '1,20p;40,60p' frontend/x.ts",
                        "sed -n -e '1,5p' -e '$p' frontend/x.ts", "sed 's/export/EXPORT/g' frontend/x.ts",
                        "sed -ne '10,20p' frontend/x.ts", "sed -n '2{p;q}' frontend/x.ts", "cat " + self.root + "/frontend/src/router/index.ts"]:
            with self.subTest(command=command):
                self.assertIsNone(self.shell(command))

    def test_refused_commands_explain_why(self):
        cases = {
            "node -e 'console.log(1)'": "`node -e console.log(1)` isn't allowed",
            "sed -i s/a/b/ frontend/src/router/index.ts": "`sed` is only allowed when it just prints",
            "sed -n 'w out.txt' frontend/src/router/index.ts": "`sed` is only allowed when it just prints",
            "sed 's/a/b/w out.txt' frontend/src/router/index.ts": "`sed` is only allowed when it just prints",
            "sed -n -f script.sed frontend/src/router/index.ts": "`sed` is only allowed when it just prints",
            "sed -n '1e rm -rf x' frontend/src/router/index.ts": "`sed` is only allowed when it just prints",
            "sed -ie 's/a/b/' frontend/src/router/index.ts": "`sed` is only allowed when it just prints",
            "sed -n '1r /etc/passwd' frontend/src/router/index.ts": "`sed` is only allowed when it just prints",
            "ls; rm -rf frontend": "`rm -rf frontend` isn't allowed",
            "ls\nrm -rf frontend": "`rm -rf frontend` isn't allowed",
            "echo $(whoami)": "Command substitution",
            "cat `ls`": "Command substitution",
            "ls > out.txt": "Redirection",
            "(cd frontend && ls)": "Subshells",
            "cat /etc/passwd": "outside the project",
            "cat ../../secret": "outside the project",
            "find . -name '*.ts' -delete": "-exec/-delete",
            "env rm x": "`env rm x` isn't allowed",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                reason = self.shell(command)
                self.assertIsNotNone(reason)
                self.assertIn(expected, reason)
        self.assertIn("`npm test`", self.shell("node -e 1"))  # tells Mistral what it may run

    def test_runner_options_are_tolerated(self):
        policy = dict(self.policy, allow_commands=self.policy["allow_commands"] + ["cd", "uv run pytest"])
        for command in ["cd frontend && uv run --directory . --no-sync pytest -q", "npm --prefix frontend test",
                        "npx --yes vitest run x.test.ts", "uv run --with-editable . pytest"]:
            with self.subTest(command=command):
                self.assertIsNone(guard.check_shell(command, policy, self.root))
        self.assertIn("isn't allowed", guard.check_shell("uv run --no-sync python -c 1", policy, self.root))
        self.assertIn("outside the project", guard.check_shell("uv run --directory /etc pytest", policy, self.root))

    def test_unquoted_path_with_spaces_is_rejoined(self):
        spaced = os.path.join(self.root, "2TB SSD")
        Path(spaced, "src").mkdir(parents=True)
        Path(spaced, "src/a.ts").write_text("x\n")
        policy = dict(self.policy, root=spaced)
        action, _r, new = guard.check_tool("bash", {"command": f"cat {spaced}/src/a.ts | grep x"}, policy, spaced)
        self.assertEqual(action, "rewrite")
        self.assertIn(shlex.quote(os.path.join(spaced, "src/a.ts")), new["command"])

    def test_package_script_spellings_match(self):
        policy = dict(self.policy, allow_commands=["cd", "npm --prefix frontend test"])
        for command in [f"cd {self.root} && npm --prefix frontend run test", "npm --prefix frontend test -- --run",
                        "npm run test", "npm t"]:
            with self.subTest(command=command):
                self.assertIsNone(guard.check_shell(command, policy, self.root))
        reason = guard.check_shell("npm --prefix frontend run lint", policy, self.root)
        self.assertIn("`npm --prefix frontend run …` isn't allowed", reason)

    def test_noop_edits_are_refused_with_an_explanation(self):
        action, reason, _ = guard.check_tool("search_replace", {"file_path": "frontend/x.ts", "old_string": "a",
                                                                "new_string": "a"}, self.policy, self.root)
        self.assertEqual(action, "deny")
        self.assertIn("changes nothing", reason)
        action, _r, _n = guard.check_tool("search_replace", {"file_path": "frontend/src/x.ts", "content": [
            {"old_str": "a", "new_str": "b"}]}, self.policy, self.root)
        self.assertEqual(action, "allow")

    def test_allow_shell_allows_everything(self):
        self.assertIsNone(guard.check_shell("node -e 1", dict(self.policy, allow_shell=True), self.root))

    def test_leading_slash_path_is_corrected(self):
        action, _reason, new = guard.check_tool("read_file", {"path": "/frontend/src/router/index.ts"},
                                                self.policy, self.root)
        self.assertEqual(action, "rewrite")
        self.assertEqual(new["path"], os.path.join(self.root, "frontend/src/router/index.ts"))

    def test_paths_with_a_wrong_base_are_corrected(self):
        wrong = os.path.join(os.path.dirname(self.root), "frontend/src/router/index.ts")
        action, _r, new = guard.check_tool("read_file", {"path": wrong}, self.policy, self.root)
        self.assertEqual(action, "rewrite")
        self.assertEqual(new["path"], os.path.join(self.root, "frontend/src/router/index.ts"))
        rel = ".mistral-worktrees/repo-0d0e3437/frontend/src/router/index.ts"
        action, _r, new = guard.check_tool("read_file", {"path": rel}, self.policy, self.root)
        self.assertEqual(action, "rewrite")
        self.assertEqual(new["path"], os.path.join(self.root, "frontend/src/router/index.ts"))
        action, _r, new = guard.check_tool("bash", {"command": f'cat "{wrong}" | grep export'}, self.policy, self.root)
        self.assertEqual(action, "rewrite")
        self.assertIn(os.path.join(self.root, "frontend/src/router/index.ts"), new["command"])
        # A single-name file elsewhere is not mapped into the project.
        self.assertEqual(guard.check_tool("read_file", {"path": "/etc/hosts"}, self.policy, self.root)[0], "deny")

    def test_paths_outside_the_project_are_refused(self):
        action, reason, _ = guard.check_tool("read_file", {"path": "/etc/hosts"}, self.policy, self.root)
        self.assertEqual(action, "deny")
        self.assertIn("outside the project", reason)

    def test_writes_must_stay_in_scope(self):
        ok = guard.check_tool("write_file", {"path": "frontend/src/new.ts", "content": "x"}, self.policy, self.root)
        self.assertEqual(ok[0], "allow")
        action, reason, _ = guard.check_tool("search_replace", {"file_path": "package.json", "old_string": "a",
                                                                "new_string": "b"}, self.policy, self.root)
        self.assertEqual(action, "deny")
        self.assertIn("outside this task's scope", reason)
        self.assertEqual(guard.check_tool("write_file", {"path": "frontend/x.ts", "content": "x"},
                                          dict(self.policy, mode="read"), self.root)[0], "deny")

    def test_secrets_and_network_are_refused(self):
        self.assertEqual(guard.check_tool("read_file", {"path": ".env"}, self.policy, self.root)[0], "deny")
        self.assertEqual(guard.check_tool("web_fetch", {"url": "https://x"}, self.policy, self.root)[0], "deny")

    def test_policy_lookup_and_expiry(self):
        home = Path(self.root, "home")
        os.environ["MISTRAL_DELEGATE_HOME"] = str(home)
        try:
            guard.write_policy(self.root, dict(self.policy, run_id="r1", expires=time.time() + 60), home)
            self.assertEqual(guard.find_policy(os.path.join(self.root, "frontend"))["run_id"], "r1")
            guard.remove_policy(self.root, "other-run", home)
            self.assertIsNotNone(guard.find_policy(self.root))
            guard.remove_policy(self.root, "r1", home)
            self.assertIsNone(guard.find_policy(self.root))
            guard.write_policy(self.root, dict(self.policy, run_id="r2", expires=time.time() - 1), home)
            self.assertIsNone(guard.find_policy(self.root))
        finally:
            del os.environ["MISTRAL_DELEGATE_HOME"]


class GuardInstallTest(unittest.TestCase):
    def test_install_keeps_existing_hooks_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            vibe_home, home = Path(tmp, "vibe"), Path(tmp, "md")
            vibe_home.mkdir()
            hooks = vibe_home / "hooks.toml"
            hooks.write_text('[[hooks]]\nname = "mine"\ntype = "post_agent"\ncommand = "true"\n')
            self.assertIsNone(guard.install(vibe_home, home, "python3"))
            first = hooks.read_text()
            self.assertIn('name = "mine"', first)
            self.assertIn('name = "mistral-delegate-guard"', first)
            self.assertTrue((home / "vibe_guard.py").exists())
            self.assertIsNone(guard.install(vibe_home, home, "python3"))
            self.assertEqual(hooks.read_text(), first)
            import tomllib
            self.assertEqual(len(tomllib.loads(first)["hooks"]), 2)

    def test_broken_hooks_file_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            vibe_home = Path(tmp)
            (vibe_home / "hooks.toml").write_text("[[hooks]\nbroken")
            warning = guard.install(vibe_home, Path(tmp, "md"), "python3")
            self.assertIn("doesn't parse", warning)
            self.assertEqual((vibe_home / "hooks.toml").read_text(), "[[hooks]\nbroken")


class WorktreeLocationTest(unittest.TestCase):
    def test_configured_and_automatic_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            top = os.path.join(tmp, "Projects", "Wishdom")
            os.environ["MISTRAL_DELEGATE_HOME"] = os.path.join(tmp, "md")
            try:
                self.assertTrue(str(gitops.worktree_root(top, "/Volumes/SSD/.wt")).startswith("/Volumes/SSD/.wt/Wishdom-"))
                self.assertTrue(str(gitops.worktree_root(top)).startswith(os.path.join(tmp, "md", "worktrees")))
                original = gitops._device
                gitops._device = lambda p: 1 if str(p).startswith(os.path.join(tmp, "md")) else 2
                try:
                    self.assertTrue(str(gitops.worktree_root(top)).startswith(
                        os.path.join(tmp, "Projects", ".mistral-worktrees")))
                finally:
                    gitops._device = original
            finally:
                del os.environ["MISTRAL_DELEGATE_HOME"]


class UnfinishedRunTest(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import delegate
        self.delegate = delegate

    def test_detects_runs_that_stop_early(self):
        msg = lambda text: {"type": "message", "role": "assistant", "content": [{"type": "text", "text": text}]}
        tool = {"type": "effect", "title": "read_file", "state": {"status": "completed"}}
        f = self.delegate.unfinished_reason
        self.assertIn("tool call", f([msg("Looking"), tool], "Looking"))
        self.assertIn("mid-sentence", f([msg("AppLayout triggers advisors.load()…")], "AppLayout triggers advisors.load()…"))
        self.assertEqual(f([msg("Added tests in src/a.test.ts.")], "Added tests in src/a.test.ts."), "")
        self.assertEqual(f([msg("- src/a.test.ts: new tests")], "- src/a.test.ts: new tests"), "")
        summary = "Added tests for the shop list: paused shops, renamed shops, and the empty state. Files: a.test.ts."
        self.assertEqual(f([msg(summary), tool], summary), "")  # a trailing read after a real summary is fine


class CostStatsTest(unittest.TestCase):
    def test_runs_without_cost_data_are_left_out_of_averages(self):
        from mdelegate import ledger
        runs = {
            "a": {"id": "a", "kind": "tests", "status": "ok", "started": time.time(), "cost": 0.2, "tokens": 1000},
            "b": {"id": "b", "kind": "tests", "status": "ok", "started": time.time(), "cost": None},
            "c": {"id": "c", "kind": "tests", "status": "ok", "started": time.time(), "cost": 0.0, "tokens": 0},
        }
        self.assertIn("$0.200", ledger.format_stats(runs))
        self.assertIn("2 run(s) without a price", ledger.format_stats(runs))
        self.assertIn("~$0.20 avg", ledger.compact_stats(runs))


class CommandFormsTest(unittest.TestCase):
    def test_expansions(self):
        from mdelegate import commands
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "frontend").mkdir()
            Path(tmp, "frontend/package.json").write_text('{"scripts": {"test:unit": "cross-env CI=1 vitest run", "lint": "eslint ."}}')
            forms = commands.expand(["npm run test:unit", "pytest -q"], tmp)
        for form in ("npm run test:unit", "pnpm test:unit", "yarn run test:unit", "npx vitest", "pnpm exec vitest",
                     "python -m pytest", "uv run pytest"):
            self.assertIn(form, forms)
        self.assertNotIn("npx eslint", forms)  # lint wasn't allowed

    def test_denied_call_labels_come_from_any_detail_shape(self):
        from mdelegate import vibe
        entry = {"type": "effect", "detail": {"kind": "shell", "display": {"title": "Run"},
                                              "input": {"argv": None, "cmd": "npm run test"}},
                 "state": {"status": "skipped", "reason": "denied"}}
        info = vibe.summarize_history([entry])
        self.assertEqual(info["denied"], ["Run: npm run test"])


class CallbackLabelTest(unittest.TestCase):
    def test_refused_approvals_name_the_command(self):
        from mdelegate import vibe
        history = [
            {"type": "effect", "detail": {"kind": "tool"}, "state": {"status": "skipped", "reason": "denied"}},
            {"type": "callback", "title": "Approve?", "detail": {"kind": "approval", "effect": {
                "kind": "shell", "toolName": "bash", "input": {"command": "uv run --no-sync pytest"}}},
             "state": {"status": "answered", "output": {"decision": {"type": "deny"}}}},
        ]
        self.assertEqual(vibe.summarize_history(history)["denied"], ["bash: uv run --no-sync pytest"])


class ManifestTest(unittest.TestCase):
    def test_json_files_parse(self):
        for path in [ROOT / ".claude-plugin/marketplace.json", PLUGIN / ".claude-plugin/plugin.json",
                     PLUGIN / "hooks/hooks.json"]:
            json.loads(path.read_text())


if __name__ == "__main__":
    unittest.main()
