---
name: delegate-to-mistral
description: Hand a self-contained coding task to Mistral's Vibe CLI (`vibe --prompt`) and get back a compact report. Use when the user asks to delegate to Mistral or Vibe, or for well-specified, low-risk, mechanical work that does not need this conversation's context (boilerplate, writing tests for existing code, docstrings, bulk renames, read-only searches and summaries of the codebase). Do not use for architecture decisions, security-sensitive code, or anything you already hold most of the context for.
---

# Delegate to Mistral Vibe

This skill runs one task through Vibe's programmatic mode with a hard turn and price cap, and returns only Vibe's final answer, its session id, and (for write tasks) the files it changed. You stay responsible for the result: review what comes back before you rely on it or merge it.

## Before the first run

Check `vibe --version`. If Vibe is missing, tell the user to run `uv tool install mistral-vibe` (or `pip install mistral-vibe`) and then `vibe --setup` to store their Mistral API key. Don't install it yourself unless they ask.

## Running a task

The wrapper is `scripts/delegate.py` in this skill's base directory. Call it with Bash:

```bash
python3 "<skill base dir>/scripts/delegate.py" --mode read  "<task>"
python3 "<skill base dir>/scripts/delegate.py" --mode write "<task>"
```

For long prompts, pass `-` as the task and pipe the text through stdin with a heredoc.

**Modes**

- `--mode read` (default): Vibe's `plan` agent, restricted to `read_file`, `grep` and `todo`. It runs in place and can't change anything. Default cap is 15 turns and $0.25.
- `--mode write`: Vibe's `accept-edits` agent in a new git worktree on branch `mistral-<id>`, so your working tree isn't touched. Default cap is 30 turns and $1.00. Add `--in-place` only when the user wants Vibe to edit the current checkout directly.

**Options**

- `--max-turns N`, `--max-price DOLLARS`, `--max-tokens N`: override the caps. Raise them only when the task clearly needs more, and say so to the user.
- `--allow-shell`: passes `--auto-approve`. Without it, Vibe's shell commands are denied, because programmatic mode refuses every tool call that needs approval. Use it only when the user agreed, or the task needs to run tests and you are in write mode (worktree).
- `--trust`: loads the project's `.vibe/` config and `AGENTS.md`. Without it, Vibe ignores them in folders it doesn't already trust.
- `--resume <session_id>`: continue the same Vibe session with a follow-up instruction (use the `session_id` from the earlier report).
- `--timeout SECONDS` (default 900). For tasks that may take several minutes, run the command in the background and keep working.

## Writing the task prompt

Vibe starts with none of your context. Write the prompt like a ticket for a capable contractor:

- The goal and the exact files or directories involved.
- Constraints: style to follow, what not to touch, test command if it may run one.
- What "done" looks like, and what to put in the final message (for example "list each file you changed with one line on why").

## After the run

Read the report:

- `status`: `ok`, `limit_reached`, `timeout` or `error`. On `limit_reached`, decide whether to resume with a higher cap or finish the work yourself.
- `tool_calls_not_completed`: denied or failed tool calls. If shell calls were denied and they mattered, say so rather than assuming the work was verified.
- **Write mode:** inspect the changes in `worktree_path` (`git -C <path> diff`, run the tests there) before bringing anything over. To adopt the work, commit in the worktree and merge or cherry-pick the `mistral-<id>` branch, or copy specific files. Afterwards remove it with `git worktree remove <path>` and `git branch -D <branch>`. Vibe doesn't clean up worktrees from programmatic runs.

Tell the user, briefly, that the task went to Mistral, what came back, and what you checked. Never present Vibe's output as verified when you haven't checked it.
