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

The session context may also show a **track record** per kind of task (adoption rate, checks passed, average cost). Delegate more of the kinds that do well. For kinds that are often discarded, write tighter specs, or keep them yourself.

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
- `--scope GLOB` (repeatable, relative to the repo root): the files Mistral may create or change. **Always set it for write tasks.** Mistral is told to stay inside it, changes outside it are flagged as `out_of_scope_changes`, and `--adopt` leaves them out unless you add `--include-out-of-scope`. A tests-only task gets only the test files as scope. `*` also matches `/`.
- `--kind`: one of `tests feature bugfix refactor migration boilerplate docs search other`. It feeds the track record, so always set it.
- `--spec FILE`: the plan, included in the prompt. Write it to a temp file outside the repo. `--context PATH` (repeatable) names files Mistral should read first.
- `--verify CMD` (repeatable): checks the wrapper runs after Mistral finishes. On a failure, the output goes back to the same Mistral session for a fix (`--fix-attempts N`, default 1). **Always pass the project's real checks when they exist.** Configured checks apply automatically (see below).
  - Before Mistral starts, the checks also run once on the untouched worktree (the baseline; `--no-baseline` turns it off). Checks that already fail there are reported as `already failing before Mistral changed anything`. Mistral is told not to work around them, and they never trigger a fix round.
  - A `baseline_warning` usually means the worktree environment differs from the checkout (dependencies, `.env`), or the checks were already broken. Check that before blaming Mistral's change.
- `--allow-command CMD` (repeatable): command prefixes Mistral may run itself while working, e.g. the test command, so it can iterate. Each part of a chained command must match. Anything else stays refused. `--allow-shell` allows every command; use it only with the user's OK.
- `--model ALIAS`: a Vibe model alias from the user's Vibe config, for this run.
- Caps come from the policy (`--show-config` shows them). Override with `--max-turns`, `--max-price` or `--max-tokens` and tell the user when you raise them. Fix rounds add half the price cap each.
- Follow-up on the same work: `--resume <session_id> --worktree-name <name>`, both from the report.

### Parallel work

Split a change into independent steps that touch different files, and start each as its own background run (Bash `run_in_background`) or its own `mistral-worker` subagent. Several can run at once (`max_parallel`, default 3). `python3 <wrapper> --status` lists them, and `--result <id>` prints a finished report. Adopt them one at a time.

### Project settings

`<repo>/.mistral-delegate.toml` (or `~/.mistral-delegate/config.toml` for all projects) sets defaults:

```toml
policy = "balanced"                         # conservative | balanced | aggressive
verify = ["npm test", "npx tsc --noEmit"]   # checks after every write run
allow_commands = ["npm test", "npx vitest"] # commands Mistral may run itself
fix_attempts = 1
max_parallel = 3
deps_mode = "hardlink"                      # hardlink | copy | symlink | none
baseline = true                             # run checks on the untouched worktree first
# model = "mistral-medium-3.5"
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

The wrapper adds the rules itself: which commands may run, which checks must pass, no package installs, no weakened tests, and a final summary. Mistral covers what you list and seldom more, so the cases list matters most.

## After the run

The report starts with `run_id`, `status` and `verification`, then `usage` (cost, steps, tokens), the change list, and the full diff when it is short.

- **verification: passed_except_preexisting.** Mistral broke nothing new, but some checks were already failing. Treat it like passed for Mistral's work, and look at the `baseline_warning`.
- **verification: passed.** Review the diff for scope and quality, then run `adopt_with`. That applies only Mistral's changes to the checkout, leaves the user's uncommitted work alone, and removes the worktree. Use `--paths` to take part of it.
- **verification: failed.** Read `failing_check_output`. Either follow up with `--resume … --worktree-name …` and a precise instruction, fix it yourself after adopting, or discard.
- **status: limit_reached / timeout.** Resume with a higher cap, or finish it yourself.
- **out_of_scope_changes.** Mistral edited files outside the scope. `--adopt` leaves them out. Look at them before deciding whether to add `--include-out-of-scope`, and never take them just to make a check pass.
- **denied_commands.** Commands Mistral tried to run but wasn't allowed to. If one is a check it needs (e.g. `npx vitest run`), suggest adding it to `allow_commands`. `--stats` lists the most denied commands.
- **usage.** `cost $…` is exact. `cost ~$…` is estimated from token counts at list prices. If it keeps saying "cost unknown", suggest `vibe_args = ["--legacy-harness"]` in the config.
- **Not worth keeping.** `--discard <id> --note "why"`. The note feeds the track record.
- Always adopt or discard, so worktrees don't pile up. The session context lists runs still awaiting a decision.

Tell the user briefly what went to Mistral, what came back, what it cost, and what you checked. Never present unchecked output as verified.
