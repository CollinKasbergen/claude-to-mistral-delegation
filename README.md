# claude-to-mistral-delegation

A Claude Code plugin that lets Claude hand self-contained tasks to [Mistral Vibe](https://github.com/mistralai/mistral-vibe) through its programmatic mode (`vibe --prompt`). Claude decides when a task is worth delegating, writes a standalone prompt, runs Vibe with a hard budget, and gets back a short report instead of Vibe's full transcript.

## Install

1. Install Vibe and store your Mistral API key:

   ```bash
   uv tool install mistral-vibe   # or: pip install mistral-vibe (Python 3.12+)
   vibe --setup
   ```

2. In Claude Code, add this repo as a marketplace and install the plugin:

   ```
   /plugin marketplace add collinkasbergen/claude-to-mistral-delegation
   /plugin install mistral-delegate@claude-to-mistral-delegation
   ```

## Use

- Ask Claude to delegate ("have Mistral write tests for `utils/`"). Claude may also delegate on its own for low-risk mechanical work, as described in the skill.
- Or run the slash command: `/delegate-mistral write Add docstrings to every public function in src/api/`

## How it works

| Piece | Path | Role |
|---|---|---|
| Skill | `plugins/mistral-delegate/skills/delegate-to-mistral/SKILL.md` | Tells Claude when to delegate, how to write the prompt and how to review the result |
| Wrapper | `plugins/mistral-delegate/skills/delegate-to-mistral/scripts/delegate.py` | Runs `vibe -p ... --output json` with limits, parses the history, prints a compact report |
| Command | `plugins/mistral-delegate/commands/delegate-mistral.md` | `/delegate-mistral [read\|write] <task>` |

Two modes:

- **read** (default): Vibe's `plan` agent with only `read_file`, `grep` and `todo` enabled. Nothing changes on disk. Default cap 15 turns, $0.25.
- **write**: Vibe's `accept-edits` agent in a new git worktree on branch `mistral-<id>`, so your checkout is untouched until Claude reviews and adopts the changes. Default cap 30 turns, $1.00. `--in-place` edits the current checkout instead.

Safety defaults:

- Shell commands are denied. In programmatic mode, Vibe refuses any tool call that needs approval, and `--auto-approve` is passed only with `--allow-shell`.
- Every run has a turn and price cap and a 15-minute timeout.
- Project `.vibe/` config and `AGENTS.md` are ignored in folders Vibe doesn't already trust, unless you pass `--trust`.

Defaults can be changed with the environment variables `MISTRAL_DELEGATE_MAX_TURNS`, `MISTRAL_DELEGATE_MAX_PRICE`, `MISTRAL_DELEGATE_MAX_TOKENS`, `MISTRAL_DELEGATE_TIMEOUT` and `VIBE_BIN`.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use a fake `vibe` executable, so they need neither the real CLI nor an API key.
