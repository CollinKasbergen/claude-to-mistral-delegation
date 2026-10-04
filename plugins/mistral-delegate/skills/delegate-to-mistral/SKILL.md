---
name: delegate-to-mistral
description: Hand a self-contained coding task to Mistral's Vibe CLI (`vibe --prompt`) and get back a compact report. Use when the user asks to delegate to Mistral or Vibe, or for well-specified, low-risk, mechanical work that does not need this conversation's context (writing tests for existing code, boilerplate, docstrings, bulk renames, read-only searches and summaries of the codebase). Do not use for design decisions, data models, security-sensitive code, or anything you already hold most of the context for.
---

# Delegate to Mistral Vibe

This skill runs one task through Vibe's programmatic mode with a hard turn and price cap. It returns Vibe's final answer, what the run cost, its session id and, for write tasks, the changes it made. You stay responsible for the result: review what comes back before you rely on it or adopt it.

**Good fits:** tests for code that already exists (committed or not), boilerplate, docstrings and mechanical edits, and read-only questions about the codebase.

**Keep for yourself:** design decisions, data models and migrations, rules that need judgement, anything visual, and anything that depends on this conversation.

## Before the first run

Check `vibe --version`. If Vibe is missing, tell the user to run `uv tool install mistral-vibe` (or `pip install mistral-vibe`) and then `vibe --setup` to store their Mistral API key. Don't install it yourself unless they ask.

## Running a task

The wrapper is `scripts/delegate.py` in this skill's base directory. Call it with Bash:

```bash
python3 "<skill base dir>/scripts/delegate.py" --mode read  "<task>"
python3 "<skill base dir>/scripts/delegate.py" --mode write "<task>"
```

For long prompts, pass `-` as the task and pipe the text through stdin with a heredoc. Runs usually take one to a few minutes. Run longer ones in the background and keep working.

### Read mode (default)

Vibe's `plan` agent, restricted to `read_file`, `grep` and `todo`. It runs in place and can't change anything. Default cap is 15 turns and $0.25.

### Write mode

Vibe's `accept-edits` agent in a new git worktree on branch `mistral-<id>`, so the user's checkout isn't touched. Default cap is 30 turns and $1.00. The wrapper prepares the worktree so Vibe works on the current state of the code:

- **Uncommitted work is included.** Modified and untracked (non-ignored) files are copied in and committed as a snapshot inside the worktree. You don't need to commit before delegating. `--no-snapshot` starts from HEAD instead.
- **Dependencies are linked.** Ignored `node_modules`, `.venv`, `venv`, `vendor` and `bower_components` folders, including nested ones, are symlinked from the checkout. Add others with `--link PATH` (repeatable, relative to the repo root, e.g. `--link .env`), or turn this off with `--no-link-deps`. The links point at the user's real folders, so tell Vibe not to install or upgrade packages.
- **Vibe changes are reported against the snapshot,** so the change list holds only what Vibe did.

`--in-place` skips the worktree and lets Vibe edit the checkout directly. Use it only when the user asks.

### Options

- `--max-turns N`, `--max-price DOLLARS`, `--max-tokens N`: override the caps. Raise them only when the task clearly needs more, and say so to the user.
- `--allow-shell`: passes `--auto-approve`. Without it, Vibe's shell commands are denied, because programmatic mode refuses every tool call that needs approval. That means Vibe can't run the tests it writes. Use it in write mode when the task needs a test run and the user is fine with that, or run the tests yourself afterwards.
- `--trust`: in read mode, loads the project's `.vibe/` config and `AGENTS.md`. Write-mode worktrees are always trusted.
- **Follow-ups:** `--resume <session_id> --worktree-name <name>`, both copied from the earlier report. Vibe then keeps its memory of the task and the same worktree is reused.
- `--timeout SECONDS` (default 900).

## Writing the task prompt

Vibe starts with none of your context. Write the prompt like a ticket for a capable contractor:

```
Goal: <one sentence>.
Files: <paths to read>, <paths to create or change>.
Follow: <existing file whose style to match>.
Cases to cover: <bulleted list>.
Don't: change files outside <paths>; install or upgrade packages.
Finish with: each file you changed and one line on why; anything you couldn't do.
```

A precise list of cases matters most: Vibe covers what you list and seldom more.

## After the run

Read the report:

- `status`: `ok`, `limit_reached`, `timeout` or `error`. On `limit_reached`, decide whether to resume with a higher cap or finish the work yourself.
- `usage`: what this run cost, against its cap, plus the number of steps and tokens. Mention the cost when you report back.
- `tool_calls_not_completed`: denied or failed tool calls. If shell calls were denied, the tests haven't been run, so say so rather than presenting the work as verified.
- **Write mode:** read the diff with the `review_with` command. Run the tests in `worktree_path`. To adopt the result, run the `apply_to_checkout_with` command, which applies only Vibe's changes to the user's checkout and leaves their uncommitted work as it is. Then run `cleanup_with`. If Vibe failed before changing anything, the wrapper has already removed the worktree.

Tell the user, briefly, that the task went to Mistral, what came back, what it cost, and what you checked. Never present Vibe's output as verified when you haven't checked it.
