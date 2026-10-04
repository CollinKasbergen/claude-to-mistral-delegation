---
name: delegate-to-mistral
description: Hand implementation steps to Mistral's Vibe CLI and get back checked results. Use whenever a step can be described in a short spec and checked automatically (tests, type check, lint, or a small diff): features that follow existing patterns, endpoints, refactors, multi-file migrations, bug fixes with a reproducing test, tests, fixtures, boilerplate, docs, and read-only codebase questions. Use it even when you hold the context: write that context into the spec. Also when the user asks to delegate to Mistral or Vibe. Keep for yourself: design decisions, visual judgement, and steps whose spec would be longer than the code.
---

# Delegate to Mistral Vibe

Mistral runs cost cents to about a dollar, take one to a few minutes, and work in an isolated git worktree while you keep going. The wrapper runs the project's checks on the result and sends failures back to Mistral for a fix, so what reaches you is already tested. Your job is to split the work, write good specs, review, and adopt.

## When to delegate

The session context names the active **policy** (`conservative`, `balanced` or `aggressive`); follow it. Under `balanced`, the default, the test is:

1. **Can I write the spec in a few lines?** Goal, files, a pattern to follow, cases to cover.
2. **Can it be checked automatically?** Tests, type check, lint, or a diff small enough to read.

If both are yes, delegate it, even if you know exactly how you'd write it. Writing the code yourself is the expensive path. Keep design decisions, data-model choices, anything visual, and anything you can't check.

**Habit:** after planning a change, label each step "mine" or "Mistral's". Start Mistral's steps in the background first, then do yours, then review and adopt.

The session context may also show:

- **A track record** per kind of task: adoption rate, checks passed, and **savings**, meaning the Claude work that adopted runs replaced per token you spent delegating. Delegate more of the kinds that pay off. A kind listed under "hasn't paid off" (below the user's `min_savings`) is one to keep yourself unless its spec is tiny.
- **Mistral credit:** how much of the month's subscription credit is used. Delegate freely while plenty is left. Near the end, delegate only the clearest, best-paying kinds, and when it's nearly gone, keep work yourself unless the user says otherwise.

## Running a task

The wrapper path is in the session context (or `scripts/delegate.py` in this skill's base directory).

```bash
python3 <wrapper> --mode write --kind feature \
  --spec /tmp/spec.md --context src/api/users.ts --context src/api/users.test.ts \
  --scope "src/api/teams.ts" --scope "src/api/teams.test.ts" \
  --verify "npm test" --verify "npx tsc --noEmit" \
  --allow-command "npm test" \
  "Add the /api/teams endpoint as described in the spec"
```

- `--mode read` (default): read-only tools, runs in place. Use it for codebase questions.
- `--mode write`: a new worktree starting from the current code (uncommitted and untracked files included). Ignored `node_modules`, `.venv` and similar folders are hard-linked in by default: real folders whose files are shared with the checkout, so tools like Vite accept them. `--deps-mode copy|symlink|none` changes that, and `--link .env` adds a symlink to another path. `--in-place` edits the checkout directly; use it only when the user asks.
- `--scope GLOB` (repeatable, relative to the repo root): the files Mistral may create or change. **Always set it for write tasks.** Mistral is told to stay inside it, and changes outside it are flagged near the top of the report as `out_of_scope_changes`. `--adopt` then stops and lists them until you choose `--include-out-of-scope` or `--skip-out-of-scope`. A tests-only task gets only the test files as scope. `*` also matches `/`. New files named literally in the scope must exist when the run ends: a missing one gets Mistral one request to create it, and otherwise the run is `incomplete`. A resume may use a narrower scope for its fix; the worktree's scope is then the combination of its runs' scopes, so the earlier run's files aren't treated as out of scope.
- `--kind`: one of `tests feature bugfix refactor migration boilerplate docs search other`. It feeds the track record, so always set it.
- `--spec FILE`: the plan, included in the prompt. Write it to a temp file outside the repo. `--context PATH` (repeatable) names files Mistral should read first.
- `--verify CMD` (repeatable): checks the wrapper runs after Mistral finishes. On a failure, the output goes back to the same Mistral session for a fix (`--fix-attempts N`, default 1). **Always pass the project's real checks when they exist.** Configured checks apply automatically (see below).
  - Before Mistral starts, the checks also run once on the untouched worktree (the baseline; `--no-baseline` turns it off). Checks that already fail there are reported as `already failing before Mistral changed anything`. Mistral is told not to work around them, and they never trigger a fix round.
  - When the worktree is on a different disk than the repo, `node_modules` can't be hard-linked and becomes a symlink, which breaks some tools (Vite, vitest mocks). By default the plugin then puts worktrees in `<repo parent>/.mistral-worktrees`. `worktrees_dir` in the config sets the location explicitly.
  - A `baseline_warning` usually means the worktree environment differs from the checkout (dependencies, `.env`), or the checks were already broken. Check that before blaming Mistral's change.
- `--allow-command CMD` (repeatable): command prefixes Mistral may run itself while working, e.g. the test command, so it can iterate. Equivalent spellings are accepted too: `npm test` also allows `npm run test`, `pnpm test` and, when the test script runs vitest, `npx vitest`; `pytest` also allows `python -m pytest` and `uv run pytest`. Each part of a chained command must match. Anything else stays refused. `--allow-shell` allows every command; use it only with the user's OK.
- **Guard hook.** During a run, a Vibe hook checks every tool call before Vibe would ask for approval:
  - Disallowed commands, paths outside the project, writes outside `--scope`, secrets and network tools are refused with an error message Mistral sees, so it can try another way. Without the hook, Vibe treats a refused approval as the user cancelling and ends the session.
  - A path that points to the right file under the wrong base (`/src/app.ts`, or a worktree path missing its run folder) is corrected to the project, in file tools and in shell commands.
  - `sed` calls that only print are allowed alongside Vibe's read-only commands (`sed -n '1,40p' f`, `sed -nE '/a/,/b/p' f`, `sed 's/x/y/g' f`). `-i`, script files, and `w`/`r`/`e` commands are refused. `read_file` and `grep` are still better.
  - The report's `guard:` line shows what it checked, and `refused_by_guard` lists the refusals.
- `--model ALIAS`: a Vibe model alias from the user's Vibe config, for this run. Without `model` in the config or this flag, Vibe uses its own default, which a server-side experiment may route to a non-Mistral model (the report's `model:` line says which ran, and `model_note` flags it). Suggest pinning `model = "mistral-medium-3.5"` when the user wants Mistral.
- Caps come from the policy (`--show-config` shows them), in **effective tokens**: fresh input in full, cached input at a tenth, output five times. The wrapper enforces `--token-budget` and `--max-tool-calls` while Mistral runs, by watching the session's usage, and `--max-price` too if one is set. A continuation or fix round gets half of each cap. A run stopped at a cap still gets one fix round, and configured `autofix` commands (formatters) run before that. Raise caps only when the task clearly needs it, and tell the user.
- Follow-up on the same work: `--resume <session_id> --worktree-name <name>`, both from the report. The follow-up gets a new run id but keeps the same worktree, and the report shows `continues: <earlier id>`. Both ids refer to everything in the worktree, and `--adopt` or `--discard` with either settles both. Its baseline comes from before Mistral's first changes, so Mistral's own failures are never counted as already failing.
- Allowed commands may carry their runner's options: `uv run --directory . --no-sync pytest` counts as `uv run pytest`, and `npm --prefix frontend test` as `npm test`. Paths in those options must still be inside the project.

### Parallel work

Split a change into independent steps that touch different files, and start each as its own background run (Bash `run_in_background`) or its own `mistral-worker` subagent. Several can run at once (`max_parallel`, default 3). `python3 <wrapper> --status` lists them, and `--result <id>` prints a finished report. Adopt them one at a time.

### Project settings

`<repo>/.mistral-delegate.toml` (or `~/.mistral-delegate/config.toml` for all projects) sets defaults:

```toml
policy = "balanced"                         # conservative | balanced | aggressive
verify = [                                   # checks after every write run
  "npm run lint",                                          # always
  { cmd = "npx vitest run", paths = ["frontend/"] },        # only when the scope or changes touch frontend/
  { cmd = "pytest -q", paths = ["backend/"] },
]
allow_commands = ["npm test", "npx vitest"] # commands Mistral may run itself
fix_attempts = 1
max_parallel = 3
deps_mode = "hardlink"                      # hardlink | copy | symlink | none
baseline = true                             # run checks on the untouched worktree first
# worktrees_dir = "/Volumes/SSD/.mistral-worktrees"  # keep worktrees on the repo's disk
# model = "mistral-medium-3.5"              # pin Mistral; otherwise Vibe's default may route elsewhere
# model_prices = { "glm-5-3" = [1.0, 4.0, 0.1] }  # per million tokens (input, output, cached) for unpriced models
# monthly_credit = 225                       # subscription credit per month, shown in reports
# min_savings = 2                            # flag kinds of task whose delegation doesn't pay off
# autofix = [{ cmd = "ruff format .", paths = ["backend/"] }]  # run before a fix round when checks fail
[write]
max_price = 1.50
```

If the project has tests but no config, suggest creating one to the user. In an unfamiliar repository, check the commands in an existing `.mistral-delegate.toml` before the first run, because they get executed.

## Writing the spec

Mistral starts with none of your context. The spec carries it:

```
Goal: <one sentence>.
Files: <paths to read>, <paths to create or change>.
Follow: <existing file whose patterns and style to match>.
Requirements / cases: <bulleted, exhaustive list>.
Out of scope: <what not to touch>.
```

**Lessons from real runs:**

- **Generated code:** don't let Mistral hand-write what a generator produces (API types, schemas, clients). Name the generator command in the spec and allow it, or run it yourself after adopting. Don't assume what the generator outputs, such as whether doc comments carry over.

- **Parallel runs on one file:** when two runs edit the same file, give each an exact insertion point (after which function or heading, or before which line) and keep their edits apart. Better still, split the work by file.
- **New files:** name every new file literally in `--scope` (the wrapper creates its folders). When parallel runs both need a new shared file, such as an index or barrel, create an empty placeholder in the checkout before starting them, so each run adds to it instead of creating it.
- **Mistral copies the spec word for word,** mistakes included. Proofread names, paths and any prose it could paste into code or docs, and label examples as examples.

**For tests, spell out the harness setup.** Name the existing test file to copy, how to mount or render the unit, which modules to stub and how (e.g. a parent layout, the clipboard, timers), and how to read the result (DOM queries, toasts, emitted events). Specs with this setup succeed first time; specs without it send Mistral guessing and often end empty.

The wrapper adds the rules itself: which commands may run, which checks must pass, no package installs, no weakened tests, and a final summary. Mistral covers what you list and seldom more, so the cases list matters most.

## After the run

The report starts with `run_id`, `status` and `verification`, then `usage` (cost, steps, tokens), the change list, and the full diff when it is short.

- **final_message_warning.** Mistral's last step was a tool call, or its summary stops mid-sentence: the run was likely cut short. Treat the result as unfinished even if checks pass, and resume or redo it.
- **checks_skipped / flaky_checks.** Configured checks limited to paths this run doesn't touch are skipped. A check that failed and then passed on an immediate rerun before Mistral started is flaky, not broken.
- **status: incomplete.** Files named literally in `--scope` were never created (`missing_files`), even after one request. Passing checks don't cover files that don't exist. Resume with a precise instruction, or write them yourself.
- **status: no_changes.** Mistral finished without changing any file. Read its result to see why (a blocked task, a misunderstanding, or the work already existed) before retrying.
- **status: stopped_by_refusal.** Vibe ended the session after a refused approval, which the guard normally prevents. Check the `guard:` line, then `--resume` to let Mistral continue.
- **verification: passed_except_preexisting.** Mistral broke nothing new, but some checks were already failing. Treat it like passed for Mistral's work, and look at the `baseline_warning`.
- **verification: passed.** Passing checks don't prove the tests check the right thing. For tests Mistral wrote, read each assertion and ask whether it would fail if the feature were broken; watch for setups that test the wrong object, or duplicated fixtures. Then review the rest of the diff for scope and fit with the surrounding code, and run `adopt_with`. That applies only Mistral's changes to the checkout, leaves the user's uncommitted work alone, and removes the worktree. Use `--paths` to take part of it.
- **verification: failed.** Read `failing_check_output`. Either follow up with `--resume … --worktree-name …` and a precise instruction, fix it yourself after adopting, or discard.
- **status: budget_exceeded / tool_call_limit.** The wrapper stopped Mistral at a cap (`note:` says which, and the `budget:` line shows what was used). The work so far is in the worktree and has been checked, with one fix round. Review it, resume with a higher cap, or discard it.
- **usage / credit.** Effective tokens (fresh, cached and output), the cost at list prices, and the month's credit used so far. Mention the credit when it's getting low.
- **continued.** Mistral stopped without a closing summary while its work looked unfinished (checks failing, nothing changed, or no checks to tell), and was asked once to finish. When its changes are in and pass the checks, the report only notes the missing summary instead of paying for a round. A remaining `final_message_warning` means it still didn't finish.
- **Diffs.** Short diffs are inline. Every run's full diff is saved next to its report (`diff_file:` / "Read it from …") and stays there after `--adopt` removes the worktree.
- **Review before you adopt.** `--adopt` removes the worktree, which ends any chance to `--resume` Mistral. If you might want Mistral to fix something you spot later, review first or adopt with `--keep-worktree`.
- **status: limit_reached / timeout.** Resume with a higher cap, or finish it yourself.
- **out_of_scope_changes.** Mistral edited files outside the scope of the worktree's runs. `--adopt` won't apply anything until you decide: look at them, then use `--include-out-of-scope` to take them too or `--skip-out-of-scope` to leave them out. Never take them just to make a check pass.
- **baseline_warning.** Checks that failed before Mistral changed anything can come from the worktree environment (`deps_mode`) or from your own uncommitted changes, which the worktree starts from, such as a half-done regeneration.
- **denied_commands.** Commands Mistral tried to run but wasn't allowed to. If one is a check it needs (e.g. `npx vitest run`), suggest adding it to `allow_commands`. `--stats` lists the most denied commands.
- **usage.** `cost $…` is exact. `cost ~$…` is estimated from token counts at list prices. If it keeps saying "cost unknown", suggest `vibe_args = ["--legacy-harness"]` in the config.
- **Not worth keeping.** `--discard <id> --note "why"`. The note feeds the track record.
- Always adopt or discard, so worktrees don't pile up. The session context lists runs still awaiting a decision.

Tell the user briefly what went to Mistral, what came back, what it cost, and what you checked. Never present unchecked output as verified.
