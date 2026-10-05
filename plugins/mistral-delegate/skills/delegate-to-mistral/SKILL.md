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

If both are yes, delegate it, even if you know exactly how you'd write it. Writing the code yourself is the expensive path. Keep design decisions, data-model choices, anything visual, and anything you can't check. Also keep small changes with subtle logic, such as URL or state synchronisation and edge-case handling, where reviewing Mistral's version properly takes about as long as writing it.

**Habit:** after planning a change, label each step "mine" or "Mistral's". Put Mistral's steps in **one plan file** and start it with `--plan` in the background (see Plans below), then do your steps, then review the plan's one report and adopt. A single isolated step can be a plain run instead.

The session context may also show:

- **A track record** per kind of task: adoption rate, checks passed, and **savings**, meaning the Claude work that adopted runs replaced per token you spent delegating. Delegate more of the kinds that pay off. A kind listed under "hasn't paid off" (below the user's `min_savings`) is one to keep yourself unless its spec is tiny.
- **Mistral credit:** how much of the month's subscription credit is used. Delegate freely while plenty is left. Near the end, delegate only the clearest, best-paying kinds, and when it's nearly gone, keep work yourself unless the user says otherwise.

## Documents: what lives where

| Document | Where | Written by | Read by |
|---|---|---|---|
| Plan (several steps) | `<repo>/.mistral-delegate/plans/<name>.md` | you | the wrapper; each Mistral run sees only its own step |
| Spec (one step) | `<repo>/.mistral-delegate/specs/<name>.md` | you | the wrapper, into Mistral's prompt |
| Standing rules for Mistral's code | the project's `AGENTS.md` | you or the user, committed | Mistral, on every write run (the wrapper puts it in the prompt as "Project rules") |
| Settings | `<repo>/.mistral-delegate.toml` | the user | the wrapper |
| Run records: `report.md`, `spec.md`, `changes.diff`, `guard.jsonl`, a plan's `plan.md` and step specs and logs | `~/.mistral-delegate/runs/<run or plan id>/` | the wrapper | you, through `--result <id>` or the `diff:` line |

- **Plans and specs go in `.mistral-delegate/`**, never in a scratch or temp folder: they survive a session restart, git ignores the folder, and it's never copied into Mistral's worktrees, so no run reads another run's instructions. Pass the name: `--plan teams`, `--spec teams-endpoint` (a path works too). Name them after the change, e.g. `teams-page.md`.
- **Don't write plans, specs or notes anywhere else in the repo.** A stray file is copied into every worktree, and Mistral may take it for instructions. Mistral's prompt tells it that only the prompt and `AGENTS.md` are instructions.
- **A recurring problem in Mistral's code** (wordy queries, filtering in Python instead of SQL, loose assertions) is a rule for `AGENTS.md`, written once, rather than a line repeated in every spec. Suggest it to the user when you see it twice.

## Running a task

The wrapper path is in the session context (or `scripts/delegate.py` in this skill's base directory).

```bash
python3 <wrapper> --mode write --kind feature \
  --spec teams-endpoint --context src/api/users.ts --context src/api/users.test.ts \
  --scope "src/api/teams.ts" --scope "src/api/teams.test.ts" \
  --verify "npm test" --verify "npx tsc --noEmit" \
  --allow-command "npm test" \
  "Add the /api/teams endpoint as described in the spec"
```

- `--mode read` (default): read-only tools, runs in place. Use it for codebase questions. Mistral may also use read-only shell commands (`ls`, `find`, `cat`, `grep`, `sed -n`) while the guard is active.
- `--mode write`: a new worktree starting from the current code (uncommitted and untracked files included). Ignored `node_modules`, `.venv` and similar folders come in as real folders by default (copy-on-write clones where the disk supports it, else hard links shared with the checkout), so tools like Vite accept them. `--deps-mode copy|symlink|none` changes that, and `--link .env` adds a symlink to another path. `--in-place` edits the checkout directly; use it only when the user asks.
- `--scope GLOB` (repeatable, relative to the repo root): the files Mistral may create or change. **Always set it for write tasks.** Mistral is told to stay inside it, and changes outside it are flagged near the top of the report as `out_of_scope_changes`. `--adopt` then stops and lists them until you choose `--include-out-of-scope` or `--skip-out-of-scope`. A tests-only task gets only the test files as scope. `*` also matches `/`. New files named literally in the scope must exist when the run ends: a missing one gets Mistral one request to create it, and otherwise the run is `incomplete`. A resume may use a narrower scope for its fix; the worktree's scope is then the combination of its runs' scopes, so the earlier run's files aren't treated as out of scope.
- `--kind`: one of `tests feature bugfix refactor migration boilerplate docs search other`. It feeds the track record, so always set it.
- `--spec NAME`: the spec, included in the prompt. Write it to `.mistral-delegate/specs/<name>.md` and pass the name (see Documents). `--context PATH` (repeatable) names files Mistral should read first.
- `--verify CMD` (repeatable): checks the wrapper runs after Mistral finishes, on top of the configured ones (lint, type check), which always run too; `--no-verify` drops the configured ones. On a failure, the output goes back to the same Mistral session for a fix (`--fix-attempts N`, default 1). **Always pass the project's real checks when they exist.** Configured checks apply automatically (see below).
  - Before Mistral starts, the checks also run once on the untouched worktree (the baseline; `--no-baseline` turns it off). Checks that already fail there are reported as `already failing before Mistral changed anything`. Mistral is told not to work around them, and they never trigger a fix round.
  - When the worktree is on a different disk than the repo, `node_modules` can't be hard-linked and becomes a symlink, which breaks some tools (Vite, vitest mocks). By default the plugin then puts worktrees in `<repo parent>/.mistral-worktrees`. `worktrees_dir` in the config sets the location explicitly.
  - A `baseline_warning` usually means the worktree environment differs from the checkout (dependencies, `.env`), or the checks were already broken. Check that before blaming Mistral's change.
- Other run flags: `--no-verify` skips the configured checks, `--no-snapshot` starts from HEAD without the user's uncommitted work, `--timeout SECONDS` (default 900) limits each Vibe call (raise it, and the Bash tool's timeout, for long tasks), and `--diff-lines N` (default 300) sets how long a diff the report shows inline.
- `--allow-command CMD` (repeatable): command prefixes Mistral may run itself while working, e.g. the test command, so it can iterate. Equivalent spellings are accepted too: `npm test` also allows `npm run test`, `npm t`, `pnpm test`, the same with runner options such as `--prefix frontend`, and, when the test script runs vitest, `npx vitest`; `pytest` also allows `python -m pytest` and `uv run pytest`. Each part of a chained command must match. Anything else stays refused. `--allow-shell` allows every command; use it only with the user's OK. The run's checks (configured and `--verify`) are always runnable by Mistral, so it can see a lint or type error itself before it says it's done.
- **Guard hook.** During a run, a Vibe hook checks every tool call before Vibe would ask for approval:
  - Disallowed commands, paths outside the project, writes outside `--scope` or into the shared dependency folders, secrets (in shell commands too), `$VARIABLES`, environment overrides and network tools are refused with an error message Mistral sees, so it can try another way. Without the hook, Vibe treats a refused approval as the user cancelling and ends the session.
  - A path that points to the right file under the wrong base (`/src/app.ts`, or a worktree path missing its run folder) is corrected to the project, in file tools and in shell commands.
  - `sed` calls that only print are allowed alongside Vibe's read-only commands (`sed -n '1,40p' f`, `sed -nE '/a/,/b/p' f`, `sed 's/x/y/g' f`). `-i`, script files, and `w`/`r`/`e` commands are refused. `read_file` and `grep` are still better.
  - The report's `guard:` line shows what it checked, and `refused_by_guard` lists the refusals.
- `--model ALIAS`: a Vibe model alias from the user's Vibe config, for this run. Without `model` in the config or this flag, Vibe uses its own default, which a server-side experiment may route to a non-Mistral model (the report's `model:` line says which ran, and `model_note` flags it). Suggest pinning `model = "mistral-medium-3.5"` when the user wants Mistral.
- Caps come from the policy (`--show-config` shows them), in **effective tokens**: fresh input in full, cached input at a tenth, output five times. The wrapper enforces `--token-budget` and `--max-tool-calls` while Mistral runs, by watching the session's usage, and `--max-price` too if one is set. A continuation or fix round gets half of each cap. A run stopped at a cap still gets one fix round, and configured `autofix` commands (formatters) run before that. Raise caps only when the task clearly needs it, and tell the user.
- Follow-up on the same work: `--mode write --resume <session_id> --worktree-name <name>`, as the report's `session_id:` line gives it. Copy that line: a `--resume` without `--mode` continues in the mode of the run it resumes, and `--worktree-name` implies write mode. The follow-up gets a new run id but keeps the same worktree, and the report shows `continues: <earlier id>`. Both ids refer to everything in the worktree, and `--adopt` or `--discard` with either settles both. Its baseline comes from before Mistral's first changes, so Mistral's own failures are never counted as already failing. A follow-up gets half the caps (the report's `budget:` line says so): keep its instruction narrow.
- Allowed commands may carry their runner's harmless options: `uv run --directory . --no-sync pytest` counts as `uv run pytest`, and `npm --prefix frontend test` as `npm test`. Paths in those options must still be inside the project. Options that install or run other code (`uv run --with`, `--python`, `npm --node-options`/`--script-shell`, `npx --package`) are refused unless the allowed command itself contains them.
- `--worktree-name` only reuses a worktree the plugin made. Never pass the name of a branch the user works on.

### Plans: several steps in one call

For two or more steps, write one plan file instead of a spec per step, and run it with one call. The wrapper runs each step as its own Mistral run in its own worktree (up to `max_parallel` at a time, each after the steps it `depends` on, starting from their results), with the step's checks and fix rounds. It then merges the steps that succeeded into one worktree, runs the checks on the combined result, and if only the combination fails, has Mistral fix it once. You get one report and adopt once. This costs you far fewer tokens than a `mistral-worker` subagent per step, which repeats the reading and reviewing for every step.

```markdown
# Teams page
verify: npm test
kind: feature

Shared context every step gets: conventions, files to follow (src/api/users.ts), test setup, what's out of scope.

## step: api - Teams endpoint
scope: src/api/teams.ts, src/api/teams.test.ts
context: src/api/users.ts
verify: npx vitest run src/api
allow: npx vitest run

What to build in this step: endpoints, cases to cover, insertion points.

## step: ui - Teams page
depends: api
scope: src/pages/Teams.vue, src/pages/Teams.test.ts

What to build in this step.
```

- **Format:** `# Title`, then optional plan settings (`verify:` checks for the merged result, added to the configured checks and every merged step's checks, which always run there too; `kind:` default kind), then the shared context. Each `## step: <id> - <title>` starts with its settings: `scope`, `context`, `depends` (comma-separated), `verify`, `allow` (one command per line, may repeat), `kind`. A step's `verify:` adds to the configured checks (lint, type check), it doesn't replace them. The rest is that step's instructions. A step gets the shared context, its own instructions, and one line about every other step; write each step as you'd write a spec.
- **Split by file:** steps that change the same file conflict when merged (the report says so, and the later step isn't merged). Give each step its own files, and make a step that builds on another `depends` on it.
- **Run it in the background:** write it to `.mistral-delegate/plans/teams-page.md`, then `python3 <wrapper> --plan teams-page` (Bash `run_in_background`; it takes as long as its slowest chain of steps). `--steps api,ui` runs only some. Caps and flags (`--fix-attempts`, `--max-price`, ...) apply to each step. `--status` shows the plan and its steps while they run.
- **The report:** `status: ok` (every step merged and the merged result passes its checks), `checks_failed` (every step merged, but together they fail a check), `partial` (some steps didn't merge) or `failed` (none did). One line per step (`-> merged`, `not merged: conflicts with ... in <files>`, `skipped: needs <step>`, or how to resume it), `review_first` (steps that ended without a closing summary and were merged on passing checks alone: read their diffs), one `baseline_warning` for checks that already failed on the starting code, `step_notes` (warnings from the steps' own reports), `verification` of the merged result (the plan's `verify:`, every merged step's checks and the configured checks), usage and credit, the combined diff, and `adopt_with`. `--result <step run id>` prints a step's own report when you need detail.
- **A step line reads `checks failed`** when Mistral finished but the step's checks still failed; such a step isn't merged and holds back the steps that depend on it.
- **Finishing a partial plan:** resume a failed step with the `--mode write --resume <session> --worktree-name <name>` command its line gives, then run `--integrate <plan id>`. It runs the steps that were skipped because of it (and any step whose worktree is gone), merges everything again and rechecks, with one report like the first. Don't run held-back steps as plain runs or with `--plan --steps`: they wouldn't be part of the plan.
- **Adopt or discard the plan, never a step:** `--adopt <plan id>` applies the merged result; `--adopt <plan id> --steps api` only those steps (with what they depend on); `--discard <plan id>` drops it all. `--include-out-of-scope` / `--skip-out-of-scope` work as for a run.

### Single steps and subagents

A single step is a plain run, in the background or through the `mistral-worker` subagent when you want its review done outside your context. Several plain runs can run at once (`max_parallel`, default 3); `--status` lists this project's runs (`--all-repos` for every project) and `--result <id>` prints a finished report. Adopt them one at a time.

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
# autofix = ["uv run ruff format {files:*.py}", "uv run ruff check --fix {files:*.py}"]  # before a fix round
# test_strength = true                        # run new tests against the original code (default on)
# test_commands = ["make test"]               # extra commands that count as test runners for test_strength
# scope = ["src/**"]                          # default --scope when a run doesn't pass one
# continue_attempts = 1                       # 0: never ask Mistral to finish work that looks unfinished
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
- **Ask for exact assertions.** Mistral's tests tend to check that a value is somewhere ("the row contains a 1 and a 0") instead of the exact value in the exact place, and to copy whole setups (a router, a store) instead of the existing helpers. Say which exact values each test must assert and which helper to use.
- **Mistral copies the spec word for word,** mistakes included. Proofread names, paths and any prose it could paste into code or docs, and label examples as examples.

**For tests, spell out the harness setup.** Name the existing test file to copy, how to mount or render the unit, which modules to stub and how (e.g. a parent layout, the clipboard, timers), and how to read the result (DOM queries, toasts, emitted events). Specs with this setup succeed first time; specs without it send Mistral guessing and often end empty.

The wrapper adds the rules itself: which commands may run, which checks must pass, no package installs, no weakened tests, and a final summary. Mistral covers what you list and seldom more, so the cases list matters most.

## After the run

The report starts with `run_id`, `status` and `verification`, then `usage` (cost, steps, tokens), the change list, and the full diff when it is short.

- **final_message_warning.** Mistral's last step was a tool call, it left no final message, or its summary stops mid-sentence: the run was likely cut short. Treat the result as unfinished even if checks pass, and resume or redo it.
- **checks_skipped / flaky_checks.** Configured checks limited to paths this run doesn't touch are skipped. A check that failed and then passed on an immediate rerun before Mistral started is flaky, not broken.
- **status: incomplete.** Files named literally in `--scope` were never created (`missing_files`), even after one request. Passing checks don't cover files that don't exist. Resume with a precise instruction, or write them yourself.
- **status: no_changes.** Mistral finished without changing any file. Read its result to see why (a blocked task, a misunderstanding, or the work already existed) before retrying.
- **status: stopped_by_refusal.** Vibe ended the session after a refused approval, which the guard normally prevents. Check the `guard:` line, then `--resume` to let Mistral continue.
- **verification: passed_except_preexisting.** Mistral broke nothing new, but some checks were already failing. Treat it like passed for Mistral's work, and look at the `baseline_warning`.
- **test_strength / test_strength_warning.** When a run changes both code and tests, the wrapper also runs Mistral's tests against the original code, with only the test changes applied, on a clean copy. They should fail there.
  - Only test-runner checks count (pytest, vitest, jest, `go test`, `npm test`, ... or a `test_commands` entry), whose baseline passed, and whose `paths` cover the changed tests. Lint and `test -f` checks are ignored; pytest, vitest and jest run just the changed test files. Docs, configs and lockfiles don't count as code changes.
  - The warning appears only when every selected suite passes there. A timeout or a missing directory gives `test_strength: not checked (...)`.
  - A `test_strength_warning` means they still pass without the change, so they don't test it: a guarded assertion (`if (button) expect(...)`), a missing assertion, or the wrong thing under test. Fix that before adopting, or resume Mistral with the exact problem.
  - A `test_strength` line means they fail as they should. `test_strength: not conclusive` means they only fail because they can't load without the new code (an import error), which says nothing about their assertions: read them. When all the code under test is new, there's no line at all: the tests can't run without it, as expected.
- **assertion_hint.** New test lines that check presence, size or truthiness (`assert x in y`, `len(...)`, `toContain`), or that work out the expected value with a condition (`a if a.id < b.id else b`), rather than asserting exact values; also new tests that assert nothing, tests about skipping or filtering that only check nothing is returned (code that skips everything passes those), and setup inside `pytest.raises` / `assertRaises` (an error in the setup passes too). A plain "not found" test that checks for None isn't flagged. Mistral falls back to these even when the rules forbid it; fix them before adopting, or resume with the lines to tighten.
- **Formatting failures cost Mistral rounds.** A long line or import order shouldn't take a fix round: suggest an `autofix` with `{files}` (only the files the run changed; the wrapper then keeps the formatter's edits only on the lines Mistral wrote, so the user's own lines keep their layout), e.g. `autofix = ["uv run ruff format {files:*.py}", "uv run ruff check --fix {files:*.py}"]`.
- **verification: passed.** Passing checks don't prove the tests check the right thing. For tests Mistral wrote, read each assertion and ask whether it would fail if the feature were broken; watch for setups that test the wrong object, or duplicated fixtures. Then review the rest of the diff for scope and fit with the surrounding code, and run `adopt_with`. That applies only Mistral's changes to the checkout, leaves the user's uncommitted work alone, and removes the worktree. Use `--paths` to take part of it; the worktree then stays with the rest (`--discard` it when done).
- **verification: failed.** Read `failing_check_output`. Either follow up with `--resume … --worktree-name …` and a precise instruction, fix it yourself after adopting, or discard.
- **status: budget_exceeded / tool_call_limit.** The wrapper stopped Mistral at a cap (`note:` says which, and the `budget:` line shows what was used). The work so far is in the worktree and has been checked, with one fix round. Review it, resume with a higher cap, or discard it.
- **usage / credit.** Effective tokens (fresh, cached and output), the cost at list prices, and the month's credit used so far. Mention the credit when it's getting low.
- **continued.** Mistral stopped without a closing summary while its work looked unfinished (checks failing, nothing changed, or no checks to tell), and was asked once to finish. When its changes are in and pass the checks, the report only notes the missing summary instead of paying for a round. A remaining `final_message_warning` means it still didn't finish.
- **Diffs.** Short diffs are inline. Every run's full diff is saved next to its report (`diff_file:` / "Read it from …") and stays there after `--adopt` removes the worktree.
- **Review before you adopt.** `--adopt` removes the worktree, which ends any chance to `--resume` Mistral. If you might want Mistral to fix something you spot later, review first or adopt with `--keep-worktree`; a resume after that adopts only its new changes.
- **python_path.** The project's virtualenv installs it in editable mode from the checkout; the wrapper made checks import the worktree's copy instead. Nothing to do, but mention it if checks behave differently in the checkout.
- **status: limit_reached / timeout.** Resume with a higher cap, or finish it yourself.
- **status: interrupted.** The wrapper itself was stopped (a tool timeout, a closed terminal). It stopped Vibe and what Vibe started, and recorded the cost so far. Run the delegation with a longer Bash timeout, or in the background, and check it with `--status` / `--result`.
- **config_warning.** A setting in the config was misspelled, mistyped or in the wrong place, and was ignored. Tell the user which one; a config file that isn't valid TOML stops runs entirely ("Fix the config first").
- **budget_warning.** The wrapper couldn't find this run's Vibe session, so the token budget and price cap weren't enforced (tool calls and turns still were). Mention it to the user; it usually means Vibe's `session_logging` is off or moved.
- **out_of_scope_changes.** Mistral edited files outside the scope of the worktree's runs. `--adopt` won't apply anything until you decide: look at them, then use `--include-out-of-scope` to take them too or `--skip-out-of-scope` to leave them out. Never take them just to make a check pass.
- **baseline_warning.** Checks that failed before Mistral changed anything can come from the worktree environment (`deps_mode`) or from your own uncommitted changes, which the worktree starts from, such as a half-done regeneration.
- **baseline_note.** A check that couldn't run before Mistral's change because the files it tests didn't exist yet (pytest "file or directory not found"). It has no baseline: if it fails afterwards, that's Mistral's failure and gets a fix round.
- **project_rules.** Which AGENTS.md files (the project root's and any in the folders down to the working folder) were repeated in Mistral's prompt, and where the full prompt is saved. `none` means Mistral got no project rules from the prompt: add an AGENTS.md before blaming it for ignoring them.
- **denied_commands.** Commands Mistral tried to run but wasn't allowed to. If one is a check it needs (e.g. `npx vitest run`), suggest adding it to `allow_commands`. `--stats` lists the most denied commands with the run each was last denied in, and lists apart the ones that are allowed now (denials from before they were).
- **refused_tool_calls.** Tool calls other than shell commands that Vibe's own permissions refused (an edit or a write). They aren't missing `allow_commands`; check whether Mistral tried to touch something outside its worktree.
- **A check that already failed before Mistral:** its error lines are compared with the baseline's. `FAIL ... with N new error line(s) now` means Mistral added errors to it: that counts as a new failure, gets a fix round, and the fix prompt lists the new lines. Only an unchanged failure is `already failing`. Judge a check by its exit code, never by grepping its output.
- **usage.** The cost is estimated from token counts at list prices (`~$…`). If it keeps saying "cost unknown", suggest `vibe_args = ["--legacy-harness"]` in the config.
- **Not worth keeping.** `--discard <id> --note "why"`. The note feeds the track record.
- Always adopt or discard, so worktrees don't pile up. The session context lists runs still awaiting a decision.

Tell the user briefly what went to Mistral, what came back, what it cost, and what you checked. Never present unchecked output as verified.
