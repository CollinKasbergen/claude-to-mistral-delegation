"""Tests for delegate.py and the SessionStart hook, using a fake `vibe` executable.

Run with: python3 -m unittest discover -s tests
"""

import json
import re
import os
import shlex
import shutil
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
    import fcntl, hashlib, re
    lock = open(calls_path + ".lock", "w")  # plan steps run in parallel
    fcntl.flock(lock, fcntl.LOCK_EX)
    calls = json.load(open(calls_path)) if os.path.exists(calls_path) else []
    calls.append({"argv": argv, "cwd": cwd, "prompt": prompt, "files": sorted(os.listdir(".")),
                  "sees_draft": os.path.exists("draft.py"),
                  "sees_edit": open("app.py").read() if os.path.exists("app.py") else None,
                  "has_node_modules": os.path.isdir("node_modules"),
                  "node_modules_is_link": os.path.islink("node_modules")})
    json.dump(calls, open(calls_path, "w"))
    fcntl.flock(lock, fcntl.LOCK_UN)

    resumed = "--resume" in argv
    session_id = argv[argv.index("--resume") + 1] if resumed else (
        "sess-" + hashlib.md5(cwd.encode()).hexdigest()[:10] if os.environ.get("FAKE_VIBE_UNIQUE_SESSION")
        else "sess-1234567890")
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

    if os.environ.get("FAKE_VIBE_CHILD"):
        import subprocess as _sp
        child = _sp.Popen(["sleep", "300"])  # a test server or watcher Vibe started
        open(os.environ["FAKE_VIBE_CHILD"], "w").write(str(child.pid))
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
            for path, content in re.findall(r"^FAKE_FILE (\\S+) (.*)$", prompt, re.M):  # files a plan step asks for
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                open(path, "w").write(content + "\\n")
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
    for result in (calls[-1].get("hook_results") or []):
        if result and result.get("decision") == "deny":  # how Vibe records a call its hook refused
            turn.insert(1, dict(entry, id=f"d{len(calls)}{len(turn)}", type="effect", title="tool",
                                detail={"kind": "tool", "toolName": "tool"},
                                state={"status": "skipped", "reason": result["reason"]}))
            turn.append(dict(entry, id=f"dn{len(calls)}{len(turn)}", type="notice", level="error", detail={},
                             message="Denied tool 'bash'"))
    if os.environ.get("FAKE_VIBE_DENIED_EDIT"):
        turn.insert(2, dict(entry, id=f"e{len(calls)}", type="effect", title="Edit",
                            detail={"kind": "file_edit", "toolName": "search_replace", "input": {"file_path": "app.py"}},
                            state={"status": "skipped", "reason": "denied", "display": {}}))
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
    if resumed and os.environ.get("FAKE_VIBE_OLD_CANCEL"):
        history.append(dict(entry, id="old-a", type="message", role="assistant", content=[
            {"type": "text", "text": "<user_cancellation>User cancelled the operation.</user_cancellation>"}]))
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
                    "FAKE_VIBE_UNFINISHED", "FAKE_VIBE_CACHED", "FAKE_VIBE_CACHED_COMPLETION", "FAKE_VIBE_REWRITE_NOTICES",
                    "FAKE_VIBE_CHILD", "FAKE_VIBE_OLD_CANCEL", "GIT_CONFIG_GLOBAL", "FAKE_VIBE_UNIQUE_SESSION",
                    "FAKE_VIBE_DENIED_EDIT"):
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
        profile = self.profile(argv)  # read-only shell commands, vetted by the guard; no edits
        self.assertIn('permission = "never"', profile)
        self.assertIn('"ls"', profile)
        self.assertIn('"sed"]', profile)
        self.assertNotIn("--auto-approve", argv)
        self.assertNotIn("--trust", argv)
        enabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--enabled-tools"]
        self.assertEqual(sorted(enabled), ["bash", "grep", "read_file", "todo"])
        self.assertEqual(Path(self.last()["cwd"]).resolve(), self.repo.resolve())
        self.assertIn("status: ok", out.stdout)
        self.assertIn("kind: search", out.stdout)
        self.assertIn("session_id: sess-1234567890", out.stdout)
        self.assertIn("Done: added test_app.py", out.stdout)
        self.assertIn("denied_commands (read mode allows only read-only commands such as ls, cat, grep, find and "
                      "sed -n):\n  - bash: npx vitest run", out.stdout)

    def test_read_mode_without_the_guard_has_no_shell(self):
        (self.tmpdir / "vibe-home").mkdir(exist_ok=True)
        (self.tmpdir / "vibe-home" / "hooks.toml").write_text("[[hooks]\nbroken")  # guard can't install
        self.run_delegate("--mode", "read", "Find the config parser")
        argv = self.last()["argv"]
        self.assertEqual(argv[argv.index("--agent") + 1], "plan")
        enabled = [argv[i + 1] for i, a in enumerate(argv) if a == "--enabled-tools"]
        self.assertEqual(sorted(enabled), ["grep", "read_file", "todo"])

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
        self.assertNotIn('"npm test"', profile)  # only read-only commands in read mode

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
        out = self.run_delegate("--mode", "read", "--verify", "true", "Task")
        self.assertEqual(out.returncode, 2)
        self.assertIn("only apply to --mode write", out.stdout)
        out = self.run_delegate("--verify", "true", "Task")  # without --mode, write flags mean a write run
        self.assertIn("mode: write", out.stdout)

    def test_a_resume_continues_in_the_mode_it_resumes(self):
        first = self.run_delegate("--mode", "write", "Task")
        self.assertIn(f"follow up with: --mode write --resume sess-1234567890 --worktree-name "
                      f"{self.value(first, 'worktree_name')}", first.stdout)
        again = self.run_delegate("--resume", "sess-1234567890", "--worktree-name", self.value(first, "worktree_name"),
                                  "More")
        self.assertIn("mode: write", again.stdout)
        self.assertIn("continues:", again.stdout)
        again = self.run_delegate("--resume", "sess-1234567890", "More")  # mode of the resumed run
        self.assertIn("mode: write", again.stdout)
        refused = self.run_delegate("--mode", "read", "--worktree-name", "x", "Task")
        self.assertIn("--worktree-name is for write runs", refused.stdout)

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
        self.assertIn("over this call's token_budget of 300,000", out.stdout)
        self.assertIn("--resume with a higher --token-budget", out.stdout)
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
        (self.repo / ".mistral-delegate.toml").write_text('test_commands = ["test ! -f nothing_here"]\n')
        out = self.run_delegate("--mode", "write", "--verify", "test ! -f nothing_here", "Task",
                                FAKE_VIBE_TOUCH_APP="1")
        self.assertIn("test_strength_warning: Mistral's tests still pass on the original code", out.stdout)
        self.assertIn("app.py", out.stdout.split("test_strength_warning")[1].split("\n")[0])

    def test_tests_that_need_the_change_pass_the_strength_check(self):
        (self.repo / ".mistral-delegate.toml").write_text('test_commands = ["test ! -f test_app.py"]\n')
        out = self.run_delegate("--mode", "write", "--verify", "test ! -f test_app.py || grep -q 'changed by vibe' app.py",
                                "Task", FAKE_VIBE_TOUCH_APP="1")
        self.assertIn("verification: passed", out.stdout)
        self.assertIn("test_strength: Mistral's tests fail on the original code, as they should", out.stdout)

    def test_strength_check_needs_both_suites_to_pass_to_warn(self):
        (self.repo / ".mistral-delegate.toml").write_text('test_commands = ["test ! -f"]\n')
        out = self.run_delegate("--mode", "write", "--verify", "test ! -f nothing_here",
                                "--verify", "test ! -f test_app.py || grep -q 'changed by vibe' app.py",
                                "Task", FAKE_VIBE_TOUCH_APP="1")
        self.assertNotIn("test_strength_warning", out.stdout)  # the unrelated suite passing proves nothing
        self.assertIn("as they should", out.stdout)

    def test_strength_check_skips_non_test_commands_and_unmeasured_baselines(self):
        out = self.run_delegate("--mode", "write", "--verify", "test ! -f nothing_here", "Task",
                                FAKE_VIBE_TOUCH_APP="1")
        self.assertNotIn("test_strength", out.stdout)  # `test -f` isn't a test runner
        (self.repo / ".mistral-delegate.toml").write_text('test_commands = ["test ! -f"]\nbaseline = false\n')
        out = self.run_delegate("--mode", "write", "--verify", "test ! -f nothing_here", "Task",
                                FAKE_VIBE_TOUCH_APP="1")
        self.assertNotIn("test_strength", out.stdout)  # no baseline: unknown whether it passed before

    def test_tests_only_changes_skip_the_strength_check(self):
        (self.repo / ".mistral-delegate.toml").write_text('test_commands = ["test ! -f"]\n')
        out = self.run_delegate("--mode", "write", "--verify", "test ! -f nothing_here", "Task")
        self.assertNotIn("test_strength", out.stdout)

    def test_a_worktree_the_user_made_is_never_reused(self):
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "worktree", "add", "-q", "-b", "feat",
                 str(self.tmpdir / "feat"))
        (self.tmpdir / "feat" / "wip.py").write_text("work in progress\n")
        for name in ("feat", self.git("branch", "--show-current").strip()):
            with self.subTest(name=name):
                out = self.run_delegate("--mode", "write", "--worktree-name", name, "Task")
                self.assertEqual(out.returncode, 2)
                self.assertIn("worktree mistral-delegate didn't create", out.stdout)
        self.assertEqual(self.calls(), [])
        self.assertTrue((self.tmpdir / "feat" / "wip.py").exists())
        self.assertNotIn("wip.py", self.git("-C", str(self.tmpdir / "feat"), "diff", "--cached", "--name-only"))

    def test_adopt_and_discard_wait_for_a_resume_in_the_same_worktree(self):
        first = self.run_delegate("--mode", "write", "Task")
        run_id = self.value(first, "run_id")
        runs = [json.loads(line) for line in (self.home / "ledger.jsonl").read_text().splitlines()]
        worktree = next(e["worktree"] for e in runs if e.get("event") == "start" and e.get("worktree"))
        with open(self.home / "ledger.jsonl", "a") as f:  # a resume of it, still running (this process)
            f.write(json.dumps({"event": "start", "id": "mistral-resume1", "pid": os.getpid(), "mode": "write",
                                "time": time.time(), "worktree": worktree}) + "\n")
        for flag in ("--adopt", "--discard"):
            with self.subTest(flag=flag):
                out = self.run_delegate(flag, run_id)
                self.assertEqual(out.returncode, 2)
                self.assertIn("mistral-resume1 is still working in this run's worktree", out.stdout)
        self.assertTrue(Path(worktree["path"]).is_dir())

    def test_user_git_settings_dont_break_runs_or_adopt(self):
        gitconfig = self.tmpdir / "gitconfig"
        gitconfig.write_text("[diff]\n\tnoprefix = true\n\texternal = false\n[color]\n\tui = always\n"
                             "[core]\n\tquotePath = true\n")
        (self.repo / "app.py").write_text("print('dirty')\n")  # snapshot needs a diff
        out = self.run_delegate("--mode", "write", "Task", GIT_CONFIG_GLOBAL=str(gitconfig))
        self.assertEqual(out.returncode, 0, out.stdout)
        adopt = self.run_delegate("--adopt", self.value(out, "run_id"), GIT_CONFIG_GLOBAL=str(gitconfig))
        self.assertEqual(adopt.returncode, 0, adopt.stdout)
        self.assertTrue((self.repo / "test_app.py").exists())

    def test_hard_linked_dependencies_skip_build_info_and_are_protected(self):
        (self.repo / "node_modules" / ".tmp").mkdir()
        (self.repo / "node_modules" / ".tmp" / "tsconfig.app.tsbuildinfo").write_text("user\n")
        (self.repo / "node_modules" / "left-pad" / "index.js").write_text("module.exports = 1\n")
        calls = [["write_file", {"path": "node_modules/left-pad/index.js", "content": "changed"}]]
        out = self.run_delegate("--mode", "write", "--deps-mode", "hardlink", "Task",
                                FAKE_VIBE_HOOK_CALLS=json.dumps(calls))
        wt = Path(self.value(out, "worktree_path"))
        self.assertFalse((wt / "node_modules" / ".tmp").exists())
        self.assertTrue((wt / "node_modules" / "left-pad" / "index.js").exists())
        self.assertEqual(self.last()["hook_results"][0]["decision"], "deny")
        self.assertIn("shared with the user's checkout", self.last()["hook_results"][0]["reason"])

    def test_stopping_vibe_at_a_cap_stops_what_it_started(self):
        (self.repo / ".mistral-delegate.toml").write_text("[write]\ntoken_budget = 300000\n")
        pid_file = self.tmpdir / "child.pid"
        out = self.run_delegate("--mode", "write", "Task", FAKE_VIBE_STORAGE="unified", FAKE_VIBE_SPEND="1",
                                FAKE_VIBE_CHILD=str(pid_file))
        self.assertIn("status: budget_exceeded", out.stdout)
        pid = int(pid_file.read_text())
        deadline = time.time() + 5
        while time.time() < deadline and _alive(pid):
            time.sleep(0.05)
        self.assertFalse(_alive(pid), "the child Vibe started is still running")

    def test_a_stopped_wrapper_stops_vibe_and_records_the_run(self):
        pid_file = self.tmpdir / "child.pid"
        env = dict(self.env, FAKE_VIBE_BEHAVIOUR="ok", FAKE_VIBE_STORAGE="unified", FAKE_VIBE_EFFECTS="1",
                   FAKE_VIBE_CHILD=str(pid_file))
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "--workdir", str(self.repo), "--mode", "write", "Task"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        deadline = time.time() + 10
        while time.time() < deadline and not pid_file.exists():
            time.sleep(0.05)
        time.sleep(0.2)
        proc.terminate()
        proc.communicate(timeout=30)
        pid = int(pid_file.read_text())
        deadline = time.time() + 5
        while time.time() < deadline and _alive(pid):
            time.sleep(0.05)
        self.assertFalse(_alive(pid))
        status = self.run_delegate("--status")
        self.assertIn("interrupted", status.stdout)
        self.assertEqual(list((self.home / "guards" / "runs").glob("*.json")), [])

    def test_an_earlier_refusal_doesnt_mark_a_resume_as_refused(self):
        first = self.run_delegate("--mode", "write", "Task")
        name = self.value(first, "worktree_name")
        out = self.run_delegate("--mode", "write", "--worktree-name", name, "--resume", "sess-1234567890",
                                "Go on", FAKE_VIBE_OLD_CANCEL="1")
        self.assertIn("status: ok", out.stdout)

    def test_a_broken_config_stops_the_run(self):
        (self.repo / ".mistral-delegate.toml").write_text('verify = ["false"]\npolicy = balanced\n')
        out = self.run_delegate("--mode", "write", "Task")
        self.assertEqual(out.returncode, 2)
        self.assertIn("Fix the config first", out.stdout)
        self.assertIn(".mistral-delegate.toml", out.stdout)
        self.assertEqual(self.calls(), [])

    def test_config_mistakes_are_reported_in_the_run(self):
        (self.repo / ".mistral-delegate.toml").write_text(
            'verfy = ["npm test"]\nmax_price = 0.5\nfix_after_cap = "false"\n'
            '[write]\nmax_price = "cheap"\nmax-price = 2\ntoken_budget = 500000\n')
        out = self.run_delegate("--mode", "write", "Task")
        self.assertEqual(out.returncode, 0, out.stdout)
        report = out.stdout
        self.assertIn("config_warning: ignored unknown setting 'verfy'", report)
        self.assertIn("config_warning: ignored max_price in", report)
        self.assertIn("caps go under [write] or [read]", report)
        self.assertIn("config_warning: ignored [write].max_price = 'cheap'", report)
        self.assertIn("config_warning: ignored unknown setting [write] 'max-price'", report)
        self.assertIn("first pass at 500,000 effective tokens", report)

    def test_new_errors_in_an_already_failing_check_count(self):
        check = ('echo "src/a.ts(1,1): error TS1: old"; '
                 'test -f test_app.py && echo "src/b.test.ts(3,4): error TS2: global.Date is new"; exit 1')
        out = self.run_delegate("--mode", "write", "--verify", check, "Task")
        self.assertIn("verification: failed (after 1 fix attempt)", out.stdout)
        self.assertIn("it failed before Mistral too, but with 1 new error line(s) now", out.stdout)
        fix_prompt = self.calls()[-1]["prompt"]
        self.assertIn("these errors are new:\nsrc/b.test.ts(N,N): error TSN: global.Date is new", fix_prompt)

    def test_an_already_failing_check_with_the_same_errors_stays_preexisting(self):
        check = 'echo "\033[31msrc/a.ts($(ls | wc -l),1): error TS1: old\033[0m"; exit 1'  # line moves, colour
        out = self.run_delegate("--mode", "write", "--verify", check, "Task")
        self.assertIn("verification: passed_except_preexisting", out.stdout)
        self.assertIn("already failing before Mistral changed anything", out.stdout)

    def test_guard_refusals_are_listed_once_with_their_command(self):
        calls = [["bash", {"command": "rm -rf src"}]]
        out = self.run_delegate("--mode", "write", "Task", FAKE_VIBE_HOOK_CALLS=json.dumps(calls))
        self.assertIn("refused_by_guard", out.stdout)
        self.assertIn("rm -rf src", out.stdout.split("refused_by_guard")[1])
        self.assertNotIn("didn't name", out.stdout)
        self.assertNotIn("refused_tool_calls", out.stdout)
        self.assertIn("1 \"Denied tool\" notice(s) for the guard's refusals", out.stdout)
        end = [e for e in map(json.loads, (self.home / "ledger.jsonl").read_text().splitlines())
               if e.get("event") == "end"][-1]
        self.assertNotIn("tool", [d.split(":")[0] for d in end["denied"]])

    def test_refused_edits_arent_listed_as_commands(self):
        out = self.run_delegate("--mode", "write", "Task", FAKE_VIBE_DENIED_EDIT="1")
        commands = out.stdout.split("denied_commands")[1].split("\n\n")[0].split("refused_tool_calls")[0]
        self.assertIn("bash: npx vitest run", commands)
        self.assertNotIn("search_replace", commands)
        self.assertIn("refused_tool_calls (Vibe's own permissions refused these; they aren't shell commands):\n"
                      "  - search_replace: app.py", out.stdout)

    def test_a_spec_by_name_and_the_run_folder(self):
        specs = self.repo / ".mistral-delegate" / "specs"
        specs.mkdir(parents=True)
        (specs / "teams.md").write_text("Add the teams endpoint.\n")
        out = self.run_delegate("--mode", "write", "--spec", "teams", "Task")
        prompt = self.last()["prompt"]
        self.assertIn("Add the teams endpoint.", prompt)
        self.assertIn("Other plans, specs or notes you come across in the project are background", prompt)
        run = self.home / "runs" / self.value(out, "run_id")
        self.assertIn("Add the teams endpoint.", (run / "spec.md").read_text())
        for name in ("report.md", "changes.diff"):
            self.assertTrue((run / name).exists(), name)
        self.assertIn("status: ok", self.run_delegate("--result", self.value(out, "run_id")).stdout)
        (self.home / "runs" / "mistral-old00001.txt").write_text("status: ok (an old report)\n")
        with open(self.home / "ledger.jsonl", "a") as f:
            f.write(json.dumps({"event": "start", "id": "mistral-old00001", "time": 1}) + "\n")
        self.assertIn("an old report", self.run_delegate("--result", "mistral-old00001").stdout)

    def test_agents_md_rules_are_in_the_prompt(self):
        (self.repo / "AGENTS.md").write_text("Tests assert exact values.\n")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "rules")
        self.run_delegate("--mode", "write", "Task")
        prompt = self.last()["prompt"]
        self.assertIn("## Project rules (from AGENTS.md; follow them in code and tests)\n\nTests assert exact values.",
                      prompt)
        self.assertIn("check your code and tests against the project rules", prompt)

    def test_autofix_formats_only_the_changed_files(self):
        (self.repo / ".mistral-delegate.toml").write_text(
            'autofix = ["echo {files:*.py} > formatted.txt; touch fixed.txt", "echo {files:*.ts} > ts.txt"]\n')
        out = self.run_delegate("--mode", "write", "--fix-attempts", "0", "--verify",
                                "test ! -f test_app.py || test -f fixed.txt", "Task")
        wt = Path(self.value(out, "worktree_path"))
        self.assertEqual((wt / "formatted.txt").read_text().strip(), "test_app.py")
        self.assertFalse((wt / "ts.txt").exists())  # no TypeScript file changed: that autofix didn't run
        self.assertIn("autofix: ran echo test_app.py > formatted.txt; touch fixed.txt", out.stdout)

    def test_formatters_only_change_mistrals_lines(self):
        upper = ('python3 -c \\"import sys; [open(f, \'w\').write(t) for f in sys.argv[1:] '
                 'for t in [open(f).read().upper()]]\\"')
        (self.repo / ".mistral-delegate.toml").write_text(
            f'autofix = ["{upper} {{files:*.py}}; touch fixed.txt"]\n')
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "config")
        out = self.run_delegate("--mode", "write", "--fix-attempts", "0", "--verify",
                                "test ! -f test_app.py || test -f fixed.txt", "Task", FAKE_VIBE_TOUCH_APP="1")
        wt = Path(self.value(out, "worktree_path"))
        self.assertEqual((wt / "app.py").read_text(), "print('v1')\n# CHANGED BY VIBE\n")  # the user's line kept
        self.assertIn("DEF TEST_APP", (wt / "test_app.py").read_text())  # a new file is all Mistral's
        self.assertIn("kept formatting changes to Mistral's lines only, in app.py", out.stdout)

    def test_verify_adds_to_the_configured_checks(self):
        (self.repo / ".mistral-delegate.toml").write_text('verify = ["test -f app.py"]\n')
        out = self.run_delegate("--mode", "write", "--verify", "true", "Task")
        self.assertIn("pass: test -f app.py", out.stdout)
        self.assertIn("pass: true", out.stdout)
        out = self.run_delegate("--mode", "write", "--no-verify", "--verify", "true", "Task")
        self.assertNotIn("test -f app.py", out.stdout.split("verification:")[1].split("\n\n")[0])

    def test_tests_that_only_fail_to_load_are_not_conclusive(self):
        (self.repo / ".mistral-delegate.toml").write_text('test_commands = ["test ! -f"]\n')
        out = self.run_delegate("--mode", "write", "--verify", 'test ! -f test_app.py || python3 -c "import newmod"',
                                "Task\nFAKE_FILE newmod.py X = 1", FAKE_VIBE_TOUCH_APP="1")
        self.assertIn("test_strength: not conclusive", out.stdout)

    def test_loose_assertions_are_flagged(self):
        out = self.run_delegate("--mode", "write", "Task\nFAKE_FILE tests/test_loose.py assert 1 in [1, 2]")
        self.assertIn("assertion_hint: 1 assertion(s) in the new tests check presence", out.stdout)
        self.assertIn("tests/test_loose.py: assert 1 in [1, 2]", out.stdout)
        self.assertIn("worktree_base: your HEAD when the worktree was made", out.stdout)
        out = self.run_delegate("--mode", "write", "Task\nFAKE_FILE tests/test_q.py assert first == (a if a < b else b)")
        self.assertIn("tests/test_q.py: assert first == (a if a < b else b)", out.stdout)

    def test_checks_mistral_may_run_and_checks_that_cant_run_yet(self):
        check = "cd . && python3 -c \"open('new_test_file.py')\""  # fails like pytest on a file that isn't there yet
        out = self.run_delegate("--mode", "write", "--verify", check, "Task")
        prompt = self.calls()[0]["prompt"]  # the first call's rules (a fix round's prompt is just the failures)
        self.assertIn("`python3 -c \"open('new_test_file.py')\"`",
                      prompt.split("You may run these shell commands yourself:")[1])
        self.assertIn("baseline_note: couldn't run before Mistral's change", out.stdout)
        self.assertIn("verification: failed", out.stdout)  # still failing afterwards: Mistral's to fix
        self.assertIn("Run them yourself before you finish", prompt)
        self.assertNotIn("already failing before Mistral", out.stdout)  # the file it tests didn't exist yet: new
        self.assertNotIn("baseline_warning", out.stdout)

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
        self.assertTrue((wt / "test_app.py").exists())  # the rest stays in the kept worktree
        self.assertIn(f"--discard {run_id} removes it", res.stdout)
        self.run_delegate("--discard", run_id)
        self.assertFalse(wt.exists())
        self.assertIn("partial", self.run_delegate("--status").stdout)  # still counted as partly adopted

    def test_adopt_paths_are_scope_checked_like_git_reads_them(self):
        out = self.run_delegate("--mode", "write", "--scope", "test_*.py", "Task", FAKE_VIBE_TOUCH_APP="1")
        res = self.run_delegate("--adopt", self.value(out, "run_id"), "--paths", ".")
        self.assertEqual(res.returncode, 1)
        self.assertIn("app.py", res.stdout)
        self.assertNotIn("# changed by vibe", (self.repo / "app.py").read_text())

    def test_a_resume_after_a_kept_adopt_can_be_adopted(self):
        first = self.run_delegate("--mode", "write", "Task")
        name = self.value(first, "worktree_name")
        adopt = self.run_delegate("--adopt", self.value(first, "run_id"), "--keep-worktree")
        self.assertEqual(adopt.returncode, 0, adopt.stdout)
        wt = Path(self.value(first, "worktree_path"))
        (wt / "later.py").write_text("z = 3\n")
        second = self.run_delegate("--mode", "write", "--worktree-name", name, "--resume", "sess-1234567890",
                                   "More", FAKE_VIBE_NO_WRITE="1")
        res = self.run_delegate("--adopt", self.value(second, "run_id"))
        self.assertEqual(res.returncode, 0, res.stdout)
        self.assertTrue((self.repo / "later.py").exists())
        self.assertNotIn("test_app.py", res.stdout)  # applied the first time

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
        (self.repo / "app.py").write_text("print('my own edit')\n")  # the user's uncommitted work
        (self.repo / "NOTES.txt").write_text("mine\n")
        out = self.run_delegate("--mode", "write", "--in-place", "--allow-shell", "--scope", "test_app.py", "Fix it")
        argv = self.last()["argv"]
        self.assertIn("--auto-approve", argv)
        self.assertNotIn("--trust", argv)
        self.assertTrue((self.repo / "test_app.py").exists())
        changed = out.stdout.split("changed_files")[1].split("\n\n")[0]
        self.assertIn("  test_app.py", changed)
        self.assertNotIn("app.py\n", changed.replace("test_app.py", ""))
        self.assertNotIn("NOTES.txt", out.stdout)
        self.assertNotIn("out_of_scope_changes", out.stdout)

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


class PlanParserTest(unittest.TestCase):
    PLAN = textwrap.dedent("""\
        # Teams page
        verify: npm test
        kind: feature

        Use the patterns in src/api/users.ts.
        Note: tests use vitest.

        ## step: api - Teams endpoint
        scope: src/api/teams.ts, tests/api/teams.test.ts
        verify: npx vitest run tests/api
        verify: npx tsc --noEmit
        allow: `npx vitest run`

        Build GET /api/teams.
        Note: keep it small.

        ## Step: ui
        depends: api

        Build the page.
        """)

    def test_parses_settings_shared_context_and_steps(self):
        from mdelegate import plan as plans
        p = plans.parse(self.PLAN)
        self.assertEqual((p.title, p.verify, p.kind), ("Teams page", ["npm test"], "feature"))
        self.assertIn("Note: tests use vitest.", p.shared)  # an unknown "key:" line is text
        api = p.step("api")
        self.assertEqual(api.title, "Teams endpoint")
        self.assertEqual(api.scope, ["src/api/teams.ts", "tests/api/teams.test.ts"])
        self.assertEqual(api.verify, ["npx vitest run tests/api", "npx tsc --noEmit"])
        self.assertEqual(api.allow, ["npx vitest run"])
        self.assertIn("Note: keep it small.", api.text)
        self.assertEqual(p.step("ui").depends, ["api"])
        spec = plans.step_spec(p, p.step("ui"))
        self.assertIn("Use the patterns in src/api/users.ts.", spec)
        self.assertIn("Already done in the code you start from\n\n- api: Teams endpoint", spec)
        self.assertNotIn("Build GET /api/teams.", spec)  # only its own step's instructions

    def test_the_example_plan_parses(self):
        from mdelegate import plan as plans
        p = plans.parse((PLUGIN.parent.parent / "examples" / "plan.md").read_text())
        self.assertEqual([s.id for s in p.order()], ["api", "store", "page", "docs"])

    def test_rejects_broken_plans(self):
        from mdelegate import plan as plans
        for text, problem in [("# T\n\nno steps", "at least one"), ("## step: a\nx", "starts with"),
                              ("# T\n## step: a\ndepends: b\nx", "isn't a step"),
                              ("# T\n## step: a\ndepends: b\nx\n## step: b\ndepends: a\ny", "cycle"),
                              ("# T\n## step: a\n\n## step: a\nx", "used twice")]:
            with self.subTest(text=text):
                with self.assertRaises(plans.PlanError) as ctx:
                    plans.parse(text)
                self.assertIn(problem, str(ctx.exception))
        p = plans.parse(self.PLAN)
        with self.assertRaises(plans.PlanError):
            plans.select(p, ["ui"])  # needs api
        self.assertEqual([s.id for s in plans.select(p, ["api"])], ["api"])


class PlanRunTest(DelegateTestBase):
    def setUp(self):
        super().setUp()
        self.env["FAKE_VIBE_UNIQUE_SESSION"] = "1"

    def plan(self, text):
        path = self.tmpdir / "plan.md"
        path.write_text(textwrap.dedent(text))
        return path

    def runs(self):
        return {e["id"]: e for e in map(json.loads, (self.home / "ledger.jsonl").read_text().splitlines())
                if e.get("event") == "start"}

    def test_steps_run_after_what_they_need_and_merge(self):
        plan = self.plan("""\
            # Two files
            Shared rule: keep it short.

            ## step: a - File A
            scope: a.txt, test_app.py
            verify: test -f a.txt
            FAKE_FILE a.txt A

            ## step: b - File B
            scope: b.txt, test_app.py
            depends: a
            FAKE_FILE b.txt B

            ## step: c
            scope: c.txt, test_app.py
            FAKE_FILE c.txt C
            """)
        out = self.run_delegate("--plan", str(plan))
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn("status: ok", out.stdout)
        self.assertIn("(3 of 3 steps merged)", out.stdout)
        calls = {Path(c["cwd"]).name.rsplit("-", 1)[1]: c for c in self.calls()}
        self.assertIn("a.txt", calls["b"]["files"])  # b starts from a's result
        self.assertNotIn("a.txt", calls["c"]["files"])
        self.assertIn("Shared rule: keep it short.", calls["c"]["prompt"])
        self.assertNotIn("FAKE_FILE a.txt", calls["c"]["prompt"])  # only its own step
        self.assertIn("pass: test -f a.txt", out.stdout)  # a's check, run again on the merged result
        plan_id = self.value(out, "plan_id")
        step_run = next(r["id"] for r in self.runs().values() if r.get("step") == "a")
        refused = self.run_delegate("--adopt", step_run)
        self.assertIn(f"part of plan {plan_id}", refused.stdout)
        adopt = self.run_delegate("--adopt", plan_id)
        self.assertEqual(adopt.returncode, 0, adopt.stdout)
        for name in ("a.txt", "b.txt", "c.txt", "test_app.py"):
            self.assertTrue((self.repo / name).exists(), name)
        self.assertEqual(list((self.home / "worktrees").glob("*/plan-*")), [])
        status = self.run_delegate("--status").stdout
        self.assertNotIn("pending", status.split("started:")[0])

    def test_conflicts_skips_and_an_integration_fix(self):
        plan = self.plan("""\
            # Conflicts
            verify: test -f fixed.txt || test $(ls step_*.txt | wc -l) -le 1

            ## step: a
            scope: *.txt, test_app.py
            FAKE_FILE step_a.txt A
            FAKE_FILE shared.txt from-a

            ## step: b
            scope: *.txt, test_app.py
            FAKE_FILE step_b.txt B

            ## step: c
            scope: *.txt, test_app.py
            FAKE_FILE shared.txt from-c

            ## step: d
            depends: c
            scope: d.txt, test_app.py
            FAKE_FILE d.txt D
            """)
        out = self.run_delegate("--plan", str(plan))
        self.assertEqual(out.returncode, 1)
        self.assertIn("status: partial", out.stdout)
        self.assertIn("c: ok", out.stdout)
        self.assertIn("not merged: conflicts with the steps merged before it in shared.txt", out.stdout)
        self.assertIn("not merged: needs c, which wasn't merged", out.stdout)
        self.assertIn("after an integration fix run", out.stdout)  # a + b together failed, then were fixed
        self.assertIn("verification: passed", out.stdout)
        fix = next(r for r in self.runs().values() if r.get("step") == "integration")
        self.assertIn("failed (exit code", next(c["prompt"] for c in self.calls()
                                                 if Path(c["cwd"]).name == self.value(out, "plan_id")))
        adopt = self.run_delegate("--adopt", self.value(out, "plan_id"), "--steps", "a")
        self.assertEqual(adopt.returncode, 0, adopt.stdout)
        self.assertTrue((self.repo / "step_a.txt").exists())
        self.assertFalse((self.repo / "step_b.txt").exists())
        self.assertFalse((self.repo / "fixed.txt").exists())  # the fix belonged to a + b together
        outcomes = {e["id"]: e["outcome"] for e in map(json.loads, (self.home / "ledger.jsonl").read_text().splitlines())
                    if e.get("event") == "outcome"}
        steps = {r.get("step"): r["id"] for r in self.runs().values() if r.get("step")}
        self.assertEqual(outcomes[steps["a"]], "adopted")
        self.assertEqual(outcomes[steps["b"]], "discarded")
        self.assertEqual(outcomes[fix["id"]], "discarded")
        fix_end = next(e for e in map(json.loads, (self.home / "ledger.jsonl").read_text().splitlines())
                       if e.get("event") == "end" and e["id"] == fix["id"])
        self.assertEqual(fix_end["claude_equivalent"], 0)  # a repair replaces no work Claude would have done
        fix_report = (self.home / "runs" / fix["id"] / "report.md").read_text()
        self.assertIn("first pass at 500,000 effective tokens and 40 tool calls", fix_report)  # half the caps
        self.assertEqual(outcomes[self.value(out, "plan_id")], "adopted_partial")

    def test_a_resumed_step_joins_after_integrate(self):
        plan = self.plan("""\
            # Resume
            ## step: a
            scope: a.txt, test_app.py
            FAKE_FILE a.txt A

            ## step: b
            scope: b.txt, test_app.py, fixed.txt
            verify: test -f fixed.txt
            FAKE_FILE b.txt B
            """)
        out = self.run_delegate("--plan", str(plan), "--fix-attempts", "0")
        self.assertIn("status: partial", out.stdout)
        plan_id = self.value(out, "plan_id")
        resume = re.search(r"--resume (\S+) --worktree-name (\S+)", out.stdout)
        follow = self.run_delegate("--mode", "write", "--resume", resume.group(1), "--worktree-name", resume.group(2),
                                   "The check `test -f fixed.txt` failed (exit code 1). Fix it.")
        self.assertIn(f"part_of_plan: {plan_id}", follow.stdout)
        early = self.run_delegate("--adopt", plan_id, "--steps", "a")
        self.assertEqual(early.returncode, 0, early.stdout)  # a alone is fine; b isn't merged yet
        self.assertTrue((self.repo / "a.txt").exists())
        self.assertFalse((self.repo / "b.txt").exists())

    def test_integrate_merges_a_finished_step(self):
        plan = self.plan("""\
            # Resume
            ## step: a
            scope: a.txt, test_app.py
            FAKE_FILE a.txt A

            ## step: b
            scope: b.txt, test_app.py, fixed.txt
            verify: test -f fixed.txt
            FAKE_FILE b.txt B
            """)
        out = self.run_delegate("--plan", str(plan), "--fix-attempts", "0")
        plan_id = self.value(out, "plan_id")
        stale = self.run_delegate("--adopt", plan_id, "--steps", "a,b")
        self.assertIn("Not merged in this plan", stale.stdout)
        resume = re.search(r"--resume (\S+) --worktree-name (\S+)", out.stdout)
        self.run_delegate("--mode", "write", "--resume", resume.group(1), "--worktree-name", resume.group(2),
                          "The check `test -f fixed.txt` failed (exit code 1). Fix it.")
        again = self.run_delegate("--integrate", plan_id)
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn("(2 of 2 steps merged)", again.stdout)
        adopt = self.run_delegate("--adopt", plan_id)
        self.assertEqual(adopt.returncode, 0, adopt.stdout)
        for name in ("a.txt", "b.txt", "fixed.txt"):
            self.assertTrue((self.repo / name).exists(), name)
        stats = self.run_delegate("--stats").stdout
        self.assertIn("Claude tokens per delegated step", stats)
        self.assertIn("in plans ~", stats)

    def test_a_stopped_plan_stops_its_steps(self):
        plan = self.plan("""\
            # Slow
            ## step: a
            FAKE_FILE a.txt A
            ## step: b
            FAKE_FILE b.txt B
            """)
        env = dict(self.env, FAKE_VIBE_BEHAVIOUR="ok", FAKE_VIBE_STORAGE="unified", FAKE_VIBE_EFFECTS="1")
        proc = subprocess.Popen([sys.executable, str(SCRIPT), "--workdir", str(self.repo), "--plan", str(plan)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        deadline = time.time() + 20
        while time.time() < deadline and len(self.calls()) < 2:
            time.sleep(0.05)
        proc.terminate()
        proc.communicate(timeout=90)
        states = {r["id"]: r for r in map(json.loads, (self.home / "ledger.jsonl").read_text().splitlines())
                  if r.get("event") == "end"}
        plan_id = next(i for i in self.runs() if i.startswith("plan-"))
        self.assertEqual(states[plan_id]["status"], "interrupted")
        self.assertEqual(sorted(s["status"] for i, s in states.items() if i != plan_id), ["interrupted"] * 2)
        out = self.run_delegate("--discard", plan_id)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertEqual(list((self.home / "worktrees").glob("*/plan-*")), [])

    def test_plans_and_specs_live_in_the_project_folder(self):
        plans = self.repo / ".mistral-delegate" / "plans"
        plans.mkdir(parents=True)
        (plans / "two-files.md").write_text("# Two\n## step: a\nscope: a.txt, test_app.py\nFAKE_FILE a.txt A\n")
        out = self.run_delegate("--plan", "two-files")
        self.assertIn("status: ok", out.stdout)
        self.assertNotIn(".mistral-delegate", self.calls()[0]["files"])  # never copied into a worktree
        self.assertEqual(self.git("status", "--porcelain"), "")  # ignored by git
        plan_id = self.value(out, "plan_id")
        self.assertTrue((self.home / "runs" / plan_id / "plan.md").is_file())
        self.assertTrue((self.home / "runs" / plan_id / "report.md").is_file())
        self.assertTrue((self.home / "runs" / plan_id / "a.spec.md").is_file())

    def test_merged_checks_import_the_worktrees_code(self):
        (self.repo / ".gitignore").write_text("node_modules/\n.venv/\n")
        (self.repo / "src" / "pkg").mkdir(parents=True)
        (self.repo / "src" / "pkg" / "__init__.py").write_text("")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "pkg")
        site = self.repo / ".venv" / "lib" / "python3.11" / "site-packages"
        site.mkdir(parents=True)
        (site / "_editable_impl_pkg.pth").write_text(str(self.repo / "src") + "\n")  # uv sync / pip install -e
        plan = self.plan("""\
            # Editable
            verify: python3 -c "import pkg.extra"

            ## step: a
            FAKE_FILE src/pkg/extra.py VALUE = 1

            ## step: b
            FAKE_FILE b.txt B
            """)
        out = self.run_delegate("--plan", str(plan), "--fix-attempts", "0", PYTHONPATH="")
        self.assertIn("status: ok", out.stdout)
        self.assertIn('pass: python3 -c "import pkg.extra"', out.stdout)

    def test_step_checks_add_to_the_configured_ones_and_failed_checks_show_in_the_status(self):
        (self.repo / ".mistral-delegate.toml").write_text('verify = ["test -f app.py"]\n')
        plan = self.plan("""\
            # Checks
            verify: test ! -f a.txt -o ! -f b.txt

            ## step: a
            verify: test -f a.txt
            FAKE_FILE a.txt A

            ## step: b
            FAKE_FILE b.txt B
            """)
        out = self.run_delegate("--plan", str(plan), "--fix-attempts", "0")
        step_a = next(c["prompt"] for c in self.calls() if Path(c["cwd"]).name.endswith("-a"))
        self.assertIn("`test -f app.py`, `test -f a.txt`", step_a)  # configured check kept, step's added
        self.assertEqual(out.returncode, 1)
        self.assertIn("status: checks_failed", out.stdout)  # every step merged, but together they fail
        self.assertIn("verification: failed", out.stdout)

    def test_integrate_runs_the_steps_a_failed_step_held_back(self):
        plan = self.plan("""\
            # Finish
            ## step: a
            scope: a.txt, test_app.py, fixed.txt
            verify: test -f fixed.txt
            FAKE_FILE a.txt A

            ## step: b
            depends: a
            FAKE_FILE b.txt B
            """)
        out = self.run_delegate("--plan", str(plan), "--fix-attempts", "0")
        self.assertIn("b: skipped", out.stdout)
        plan_id = self.value(out, "plan_id")
        resume = re.search(r"--resume (\S+) --worktree-name (\S+)", out.stdout)
        follow = self.run_delegate("--mode", "write", "--resume", resume.group(1), "--worktree-name",
                                   resume.group(2), "The check `test -f fixed.txt` failed (exit code 1). Fix it.")
        self.assertIn("caps this follow-up's first call at 500,000 effective tokens", follow.stdout)
        again = self.run_delegate("--integrate", plan_id)
        self.assertIn("(2 of 2 steps merged)", again.stdout)
        b_call = next(c for c in self.calls() if Path(c["cwd"]).name.endswith("-b"))
        self.assertIn("a.txt", b_call["files"])  # b started from a's finished result
        self.assertIn("across 3 Mistral run(s) of this plan", again.stdout)  # a, its follow-up, and b
        b_report = (self.home / "runs" / next(r["id"] for r in self.runs().values() if r.get("step") == "b")
                    / "report.md").read_text()
        self.assertIn("worktree_base: the plan's starting code with the steps it depends on", b_report)

    def test_the_usage_line_shows_fix_rounds(self):
        plan = self.plan("""\
            # Fixes
            ## step: a
            verify: test ! -f a.txt || test -f fixed.txt
            FAKE_FILE a.txt A
            ## step: b
            FAKE_FILE b.txt B
            """)
        out = self.run_delegate("--plan", str(plan))
        self.assertIn("(1 needed a fix round: a)", out.stdout)

    def test_steps_wait_for_free_slots(self):
        (self.home).mkdir(parents=True, exist_ok=True)
        (self.home / "config.toml").write_text("max_parallel = 2\n")
        with open(self.home / "ledger.jsonl", "a") as f:  # another delegation holds one slot (this process)
            f.write(json.dumps({"event": "start", "id": "mistral-other", "pid": os.getpid(), "mode": "write",
                                "time": time.time()}) + "\n")
        plan = self.plan("""\
            # Slots
            ## step: a
            FAKE_FILE a.txt A
            ## step: b
            FAKE_FILE b.txt B
            """)
        out = self.run_delegate("--plan", str(plan))
        self.assertIn("(2 of 2 steps merged)", out.stdout)

    def test_a_broken_plan_runs_nothing(self):
        out = self.run_delegate("--plan", str(self.plan("# T\n## step: a\ndepends: zz\nx\n")))
        self.assertEqual(out.returncode, 2)
        self.assertIn("The plan has a problem", out.stdout)
        self.assertEqual(self.calls(), [])
        out = self.run_delegate("--plan", str(self.plan("# T\nkind: chores\n## step: a\nx\n")))
        self.assertIn("Unknown kind chores", out.stdout)


class EditableInstallTest(DelegateTestBase):
    def test_checks_import_the_worktrees_copy_of_an_editable_package(self):
        (self.repo / ".gitignore").write_text("node_modules/\n.venv/\n")
        (self.repo / "src" / "pkg").mkdir(parents=True)
        (self.repo / "src" / "pkg" / "__init__.py").write_text("VALUE = 'user'\n")
        self.git("add", "-A")
        self.git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "pkg")
        site = self.repo / ".venv" / "lib" / "python3.11" / "site-packages"
        site.mkdir(parents=True)
        (site / "__editable__.pkg.pth").write_text(str(self.repo / "src") + "\n")
        check = "python3 -c \"import pkg, sys; sys.exit(0 if pkg.VALUE == 'user' else 3)\""
        # Without the fix, the check would import the checkout's pkg; the worktree's copy says 'mistral'.
        out = self.run_delegate("--mode", "write", "--no-baseline", "--fix-attempts", "0", "--verify",
                                "sed -i s/user/mistral/ src/pkg/__init__.py; " + check.replace("'user'", "'mistral'"),
                                "Task", PYTHONPATH="")
        self.assertIn("python_path: your virtualenv installs src in editable mode", out.stdout)
        self.assertIn("verification: passed", out.stdout)


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
        env.pop("VIBE_BIN")
        context = self.run_hook(env)
        self.assertIn("not installed", context)
        context = self.run_hook(dict(env, VIBE_BIN=str(self.vibe)))  # vibe outside PATH, named by VIBE_BIN
        self.assertNotIn("not installed", context)

    def test_session_start_mentions_a_broken_config(self):
        (self.repo / ".mistral-delegate.toml").write_text("policy = balanced\n")
        context = self.run_hook(self.env)
        self.assertIn("Config problem", context)

    def test_session_start_names_the_plans_folder(self):
        context = self.run_hook(self.env)
        self.assertIn(".mistral-delegate/plans/<name>.md", context)
        self.assertFalse((self.repo / ".mistral-delegate").exists())  # nothing is created at session start



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
            "sed -i s/a/b/ frontend/src/router/index.ts": "`-i` edits files in place",
            "sed -n 'w out.txt' frontend/src/router/index.ts": "isn't print-only",
            "sed 's/a/b/w out.txt' frontend/src/router/index.ts": "isn't print-only",
            "sed -n -f script.sed frontend/src/router/index.ts": "`-f` runs a script file",
            "sed -n '1e rm -rf x' frontend/src/router/index.ts": "isn't print-only",
            "sed -ie 's/a/b/' frontend/src/router/index.ts": "`-i` edits files in place",
            "sed -n '1r /etc/passwd' frontend/src/router/index.ts": "isn't print-only",
            "sed --in-place s/a/b/ frontend/x.ts": "`--in-place` edits files",
            "sed -x p frontend/x.ts": "isn't one of the allowed sed options",
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

    def test_escaped_parentheses_are_arguments(self):
        self.assertIsNone(self.shell(r"find frontend \( -name '*.ts' -o -name '*.vue' \) -print"))
        self.assertIsNone(self.shell("find frontend '(' -name x ')'"))
        self.assertIn("Subshells", self.shell("(cd frontend && ls)"))

    def test_options_that_run_code_or_write_are_refused(self):
        policy = dict(self.policy, allow_commands=self.policy["allow_commands"]
                      + ["uv run pytest", "sort", "uniq", "tree", "git log", "diff"])
        cases = {
            "npm test --node-options=--require=./x.js": "`--node-options` isn't allowed",
            "npm --script-shell=./x.sh test": "`--script-shell`",
            "npm --userconfig=./npmrc test": "`--userconfig`",
            "npx --package=evil vitest run": "`--package`",
            "uv run --with requests pytest": "`--with` of `uv run`",
            "uv run --python 3.9 pytest": "`--python` of `uv run`",
            "GIT_EXTERNAL_DIFF=x git diff": "Setting `GIT_EXTERNAL_DIFF`",
            "NODE_OPTIONS=--require=x npm test": "Setting `NODE_OPTIONS`",
            "git diff --output=/tmp/pwned": "`git --output` writes a file",
            "git log --output=frontend/x.ts": "`git --output` writes a file",
            "sort -o frontend/x.ts frontend/src/router/index.ts": "`sort -o` writes a file",
            "sort -uo frontend/x.ts frontend/src/router/index.ts": "`sort -o` writes a file",
            "uniq frontend/src/router/index.ts frontend/x.ts": "`uniq` with an output file",
            "grep -f/etc/passwd x": "outside the project",
            "diff frontend/src/router/index.ts --to-file=/etc/passwd": "outside the project",
            "cat $HOME/.bashrc": "Shell variables",
            'cat "${HOME}"/.ssh/id_rsa': "Shell variables",
            "cat .env": "may contain secrets",
            "grep SECRET frontend/../.env": "may contain secrets",
        }
        for command, expected in cases.items():
            with self.subTest(command=command):
                self.assertIn(expected, guard.check_shell(command, policy, self.root) or "allowed")
        self.assertIsNone(guard.check_shell("uv run --with pytest-cov pytest -q",
                                            dict(policy, allow_commands=["uv run --with pytest-cov pytest"]), self.root))
        for command in ["CI=1 npm test", "grep -n 'end$' frontend/src/router/index.ts", "sed -n '$p' frontend/x.ts",
                        "npm test -- --run", "sort frontend/src/router/index.ts", "git log --oneline -3"]:
            with self.subTest(command=command):
                self.assertIsNone(guard.check_shell(command, policy, self.root))

    def test_writes_through_links_out_of_the_project_are_refused(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside, True)
        os.symlink(outside, os.path.join(self.root, "node_modules"))
        policy = dict(self.policy, scope=[])
        action, reason, _ = guard.check_tool("write_file", {"path": "node_modules/x.js", "content": "x"},
                                             policy, self.root)
        self.assertEqual(action, "deny")
        self.assertIn("outside the project", reason)
        self.assertFalse(os.path.exists(os.path.join(outside, "x.js")))

    def test_shared_dependency_folders_are_protected(self):
        Path(self.root, "frontend/node_modules/pkg").mkdir(parents=True)
        policy = dict(self.policy, scope=[], protected=["frontend/node_modules"])
        action, reason, _ = guard.check_tool("write_file", {"path": "frontend/node_modules/pkg/index.js",
                                                            "content": "x"}, policy, self.root)
        self.assertEqual(action, "deny")
        self.assertIn("shared with the user's checkout", reason)
        action, _r, _n = guard.check_tool("read_file", {"path": "frontend/node_modules/pkg/index.js"},
                                          policy, self.root)
        self.assertNotEqual(action, "deny")

    def test_policies_are_found_by_run_id(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, True)
        os.environ["MISTRAL_DELEGATE_HOME"] = str(home)
        self.addCleanup(os.environ.pop, "MISTRAL_DELEGATE_HOME", None)
        a = dict(self.policy, run_id="read-a", mode="read", expires=time.time() + 60)
        b = dict(self.policy, run_id="read-b", mode="write", expires=time.time() + 60)
        guard.write_policy(self.root, a)
        guard.write_policy(self.root, b)  # the same directory: the fallback file now holds b
        self.assertEqual(guard.find_policy(self.root, "read-a")["mode"], "read")
        self.assertEqual(guard.find_policy(self.root)["run_id"], "read-b")
        guard.remove_policy(self.root, "read-b")
        self.assertEqual(guard.find_policy(self.root, "read-a")["mode"], "read")  # b ending doesn't unguard a
        self.assertTrue(guard.find_policy(self.root, "read-b")["expired"])  # a leftover Vibe of b is refused

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
        same = "<<<<<<< SEARCH\nfoo()\n=======\nfoo()\n>>>>>>> REPLACE"
        action, _r, _n = guard.check_tool("search_replace", {"file_path": "frontend/x.ts", "content": same},
                                          self.policy, self.root)
        self.assertEqual(action, "deny")
        changed = same.replace("=======\nfoo()", "=======\nbar()")
        action, _r, _n = guard.check_tool("search_replace", {"file_path": "frontend/src/x.ts", "content": changed},
                                          self.policy, self.root)
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
    @unittest.skipIf(sys.version_info < (3, 11), "reads the result with tomllib")
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
        for text in ("Added a test for the export command", "Added the `export` subcommand", "Set it to auto"):
            self.assertEqual(f([msg(text)], text), "", text)
        self.assertIn("mid-sentence", f([msg("Then I updated the")], "Then I updated the"))
        self.assertIn("without a final message", f([msg("")], ""))
        sys.path.insert(0, str(SCRIPT.parent))
        import delegate
        for output in ("FAILED tests/t.py::test_a - AttributeError: x has no attribute y\n9 failed, 2 passed in 0.3s",
                       "Tests  9 failed | 2 passed (11)"):
            self.assertTrue(delegate.REAL_FAILURE.search(output), output)
        self.assertFalse(delegate.REAL_FAILURE.search("ModuleNotFoundError: No module named x\n1 error in 0.1s"))
        answer = "Open loans are counted in Store.count_open_loans; tests/test_service.py covers the limit."
        refused = {"type": "effect", "title": "Denied tool 'bash'", "detail": {"input": {"command": "ls"}},
                   "state": {"status": "skipped", "reason": "denied"}}
        self.assertEqual(f([msg(answer), refused], answer), "")  # refused calls after the answer
        from mdelegate import vibe
        self.assertEqual(vibe._label(refused), "bash: ls")
        sys.path.insert(0, str(SCRIPT.parent))
        import delegate
        self.assertTrue(delegate.is_shell_label("tool: Denied tool 'bash'"))
        self.assertFalse(delegate.is_shell_label("search_replace: app.py"))
        self.assertEqual(vibe._label({"detail": {"kind": "tool", "input": {"command": "find ."}}}), "bash: find .")
        summary = "Added tests for the shop list: paused shops, renamed shops, and the empty state. Files: a.test.ts."
        self.assertEqual(f([msg(summary), tool], summary), "")  # a trailing read after a real summary is fine


def _alive(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/status") as f:
            return "\nState:\tZ" not in f.read()
    except OSError:
        pass
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class GitOpsTest(unittest.TestCase):
    def test_a_rename_out_of_scope_lists_both_paths(self):
        from mdelegate import gitops
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp, "repo")
            (repo / "src").mkdir(parents=True)
            (repo / "src" / "core.py").write_text("x = 1\n" * 20)
            run = lambda *a, cwd=repo: subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True)
            run("init", "-q")
            run("add", "-A")
            run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
            os.environ["MISTRAL_DELEGATE_HOME"] = str(Path(tmp, "home"))
            self.addCleanup(os.environ.pop, "MISTRAL_DELEGATE_HOME", None)
            wt = gitops.prepare_worktree(str(repo), "mistral-x", snapshot=True, link_deps=False, extra_links=[])
            (Path(wt["path"]) / "tests").mkdir()
            run("mv", "src/core.py", "tests/core.py", cwd=wt["path"])
            self.assertEqual(sorted(gitops.changed_files(wt)), ["src/core.py", "tests/core.py"])

    def test_formatting_is_kept_only_on_changed_lines(self):
        from mdelegate import gitops
        with tempfile.TemporaryDirectory() as tmp:
            run = lambda *a: subprocess.run(["git", "-C", tmp, *a], check=True, capture_output=True)
            run("init", "-q")
            Path(tmp, "m.py").write_text("a=1\nb  =  2\nc=3\n")
            run("add", "-A")
            run("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "x")
            Path(tmp, "m.py").write_text("a=1\nb  =  2\nc=call(first_argument, second_argument)\n")  # Mistral
            before = gitops.read_texts(tmp, ["m.py"])
            Path(tmp, "m.py").write_text("a = 1\nb = 2\nc = call(\n    first_argument,\n    second_argument,\n)\n")
            self.assertEqual(gitops.keep_changed_lines_only(tmp, "HEAD", before), ["m.py"])
            self.assertEqual(Path(tmp, "m.py").read_text(),
                             "a=1\nb  =  2\nc = call(\n    first_argument,\n    second_argument,\n)\n")

    def test_check_timeouts_stop_the_whole_process_group(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import delegate
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp, "pid")
            results = delegate.run_checks([f"sleep 30 & echo $! > {pid_file}; wait"], tmp, 1)
            self.assertEqual(results[next(iter(results))][0], 124)
            pid = int(pid_file.read_text())
            deadline = time.time() + 5
            while time.time() < deadline and _alive(pid):
                time.sleep(0.05)
            self.assertFalse(_alive(pid))

    def test_checks_run_without_colour(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import delegate
        results = delegate.run_checks(["printf '\\033[31merror\\033[0m '; echo $NO_COLOR $FORCE_COLOR; exit 1"],
                                      tempfile.gettempdir(), 10)
        code, out = next(iter(results.values()))
        self.assertEqual((code, out), (1, "error 1 0"))

    def test_checks_with_non_utf8_output_dont_crash(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import delegate
        results = delegate.run_checks(["printf 'x\\377\\n'"], tempfile.gettempdir(), 10)
        self.assertEqual(results[next(iter(results))][0], 0)


class SessionWatcherTest(unittest.TestCase):
    def test_parallel_runs_track_their_own_session(self):
        from mdelegate import vibe
        with tempfile.TemporaryDirectory() as tmp:
            os.environ["VIBE_HOME"] = tmp
            self.addCleanup(os.environ.pop, "VIBE_HOME", None)
            unified = Path(tmp, "logs", "session", "unified")

            def session(name, marker, tokens):
                d = unified / name
                (d / "journal").mkdir(parents=True)
                (d / "meta.json").write_text(json.dumps({"session_id": name, "environment": {
                    "working_directory": tmp}}))
                prompt = {"type": "projection_delta", "payload": {"delta": [{"op": "append_entry", "entry": {
                    "type": "message", "role": "user", "content": [{"type": "text", "text": f"Task\n\n{marker}"}]}},
                    {"op": "set_envelope", "state": {"session": {"tokenUsage": {"inputTokens": tokens,
                                                                                "outputTokens": 0}}}}]}}
                (d / "journal" / "0001.jsonl").write_text(json.dumps(prompt) + "\n")

            session("old", "", 5)
            watcher = vibe.SessionWatcher(tmp, time.time() - 5, marker="read-bbbb")
            session("A-session", vibe.run_marker("read-aaaa"), 900_000)
            self.assertIsNone(watcher.poll())  # another run's session
            session("B-session", vibe.run_marker("read-bbbb"), 1_000)
            snap = watcher.poll()
            self.assertEqual(snap["session_id"], "B-session")
            self.assertEqual(snap["tokens_in"], 1_000)
            self.assertEqual(watcher.found_id, "B-session")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["MISTRAL_DELEGATE_HOME"] = self.tmp.name
        self.addCleanup(os.environ.pop, "MISTRAL_DELEGATE_HOME", None)
        self.addCleanup(self.tmp.cleanup)

    def test_odd_lines_dont_break_status_or_stats(self):
        from mdelegate import ledger
        path = Path(self.tmp.name, "ledger.jsonl")
        path.write_text("\n".join(["null", "[1, 2]", '{"id": ["a"]}', '{"event": "start", "id": "r1", "time": "x",'
                                    ' "kind": null, "task": 5}', '{"event": "end", "id": "r1", "status": "ok",'
                                    ' "effective": "lots", "denied": [1, "bash: x"]}', '{"event": "start", "id": "r2"'])
                        )  # the last line was cut short by a killed writer
        ledger.append({"event": "start", "id": "r3", "kind": "tests", "mode": "write", "pid": 0})
        ledger.append({"event": "end", "id": "r3", "status": "ok", "verification": "passed_except_preexisting"})
        runs = ledger.load_runs()
        self.assertEqual(runs["r3"]["status"], "ok")  # not swallowed by the torn line
        self.assertIn("r1", ledger.format_status(runs, currency="€"))
        self.assertIn("1/1", ledger.format_stats(runs))  # passed_except_preexisting counts as passed
        ledger.month_spend(runs)

    def test_token_averages_skip_runs_without_token_data(self):
        from mdelegate import ledger
        now = time.time()
        runs = {"a": {"id": "a", "kind": "docs", "status": "ok", "started": now, "cost": 0.4, "tokens": 9},
                "b": {"id": "b", "kind": "docs", "status": "ok", "started": now, "cost": 0.5, "tokens": 9,
                      "effective": 300_000}}
        row = next(line for line in ledger.format_stats(runs).splitlines() if line.startswith("docs"))
        self.assertIn("300,000", row)  # not 150,000: run a predates effective tokens

    def test_status_uses_the_currency(self):
        from mdelegate import ledger
        runs = {"a": {"id": "a", "status": "ok", "cost": 0.5, "tokens": 10, "started": time.time()}}
        self.assertIn("€0.500", ledger.format_status(runs, currency="€"))

    def test_credit_reset_day_past_the_end_of_a_month(self):
        from mdelegate import ledger
        at = lambda day: time.mktime(time.strptime(day, "%Y-%m-%d"))
        self.assertEqual(time.strftime("%Y-%m-%d", ledger.month_start(31, at("2026-01-29"))), "2025-12-31")
        self.assertEqual(time.strftime("%Y-%m-%d", ledger.month_start(31, at("2026-04-30"))), "2026-04-30")
        self.assertEqual(time.strftime("%Y-%m-%d", ledger.month_start(31, at("2026-03-15"))), "2026-02-28")
        self.assertEqual(time.strftime("%Y-%m-%d", ledger.month_start(30, at("2026-03-01"))), "2026-02-28")

    def test_a_reused_pid_isnt_taken_for_a_running_run(self):
        from mdelegate import ledger
        run = {"id": "a", "pid": os.getpid(), "pid_started": "Thu Jan  1 00:00:00 1970"}
        self.assertEqual(ledger.state(run), "died")
        self.assertEqual(ledger.state(dict(run, pid_started=ledger.process_started(os.getpid()))), "running")


class VibeStatsTest(unittest.TestCase):
    def test_zero_prices_count_only_when_configured(self):
        from mdelegate import vibe
        snap = {"model": "devstral-free", "price": (0.0, 0.0, 0.0), "tokens_in": 1000, "cached": 0, "tokens_out": 10}
        self.assertEqual(vibe.snapshot_cost(snap), (None, False))  # Vibe's 0 means "no price"
        vibe.EXTRA_PRICES["devstral-free"] = (0.0, 0.0, 0.0)
        self.addCleanup(vibe.EXTRA_PRICES.pop, "devstral-free")
        self.assertEqual(vibe.snapshot_cost(snap), (0.0, False))

    def test_journals_are_read_in_numeric_order(self):
        from mdelegate import vibe
        names = [Path(f"{n}.jsonl") for n in (10, 9, 2)]
        self.assertEqual([p.stem for p in sorted(names, key=vibe._journal_order)], ["2", "9", "10"])


class MiniTomlTest(unittest.TestCase):
    @unittest.skipIf(sys.version_info < (3, 11), "compares with tomllib")
    def test_reads_the_example_config_like_tomllib(self):
        import tomllib
        from mdelegate import minitoml
        example = (PLUGIN.parent.parent / "examples" / ".mistral-delegate.toml").read_text()
        self.assertEqual(minitoml.loads(example), tomllib.loads(example))
        text = textwrap.dedent('''
            verify = ["npm test", { cmd = "ruff check .", paths = ["backend/"] }]
            model_prices = { "glm-5-3" = [1.0, 4.0] }
            [write]
            max_price = 1.50
            token_budget = 1_000_000
            [[models]]
            name = "m"
            input_price = 0.4
            ''')
        self.assertEqual(minitoml.loads(text), tomllib.loads(text))
        with self.assertRaises(minitoml.TOMLDecodeError):
            minitoml.loads("policy = balanced\n")


class TestRunnerDetectionTest(unittest.TestCase):
    def test_runners_get_only_test_files_they_can_run(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import delegate
        tests = ["web/src/format.test.ts", "tests/test_store.py"]
        self.assertEqual(delegate.runner_tests("pytest", tests), ["tests/test_store.py"])
        self.assertEqual(delegate.runner_tests("vitest", tests), ["web/src/format.test.ts"])
        self.assertEqual(delegate.runner_tests("npm run test", tests), ["web/src/format.test.ts"])
        self.assertEqual(delegate.runner_tests("configured", tests), tests)
        self.assertEqual(delegate.runner_tests("pytest", ["web/src/format.test.ts"]), [])

    def test_only_real_test_runners_count(self):
        sys.path.insert(0, str(SCRIPT.parent))
        import delegate
        f = lambda cmd, extra=(): delegate._test_runner(cmd, list(extra))
        for cmd in ("ruff check src tests", "test -f x", "npx tsc --noEmit -p tests", "eslint spec/", "npm run lint"):
            with self.subTest(command=cmd):
                self.assertIsNone(f(cmd))
        self.assertEqual(f("uv run --no-sync pytest -q"), "pytest")
        self.assertEqual(f("python -m pytest tests"), "pytest")
        self.assertEqual(f("npx vitest run"), "vitest")
        self.assertEqual(f("npm --prefix frontend test"), "npm run test")
        self.assertEqual(f("cd api && go test ./..."), "go test")
        self.assertEqual(f("make check", ["make check"]), "configured")


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

    def test_normalised_spellings_expand(self):
        from mdelegate import commands
        with tempfile.TemporaryDirectory() as tmp:
            forms = commands.expand(["npm --prefix frontend test", "pnpm -C web run lint"], tmp)
        for form in ("npm run test", "pnpm test", "yarn run test", "npm t", "yarn run lint", "bun lint"):
            self.assertIn(form, forms)

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
