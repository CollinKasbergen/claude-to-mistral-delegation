# claude-to-mistral-delegation

A Claude Code plugin that lets Claude hand implementation steps to [Mistral Vibe](https://github.com/mistralai/mistral-vibe) through its programmatic mode (`vibe --prompt`). Claude splits the work, writes a spec for each step it delegates, and runs Mistral in an isolated git worktree while it keeps working. The wrapper then runs your project's checks, sends failures back to Mistral for a fix, and reports a short summary with the diff, what it cost, and a one-line adopt command.

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

3. Optional but recommended: tell it how to check work in your project by adding `.mistral-delegate.toml` to the repo root:

   ```toml
   verify = ["npm test", "npx tsc --noEmit"]   # run after every write task; failures go back to Mistral
   allow_commands = ["npm test"]               # commands Mistral may run itself while working
   ```

## Use

Claude delegates on its own, following the active policy. You can also ask ("have Mistral add the teams endpoint"), or use the command:

```
/delegate-mistral write Add docstrings to every public function in src/api/
/delegate-mistral status
/delegate-mistral stats
```

## What's in the plugin

| Piece | Path | Role |
|---|---|---|
| Skill | `skills/delegate-to-mistral/SKILL.md` | When to delegate, how to write specs, how to review and adopt |
| Subagent | `agents/mistral-worker.md` | Runs one delegation end to end (spec, run, check, review) and recommends adopt or discard. Launch several in parallel. |
| Hook | `hooks/session_start.py` | At session start, tells Claude the policy, the wrapper path, the track record and runs awaiting review |
| Wrapper | `skills/delegate-to-mistral/scripts/delegate.py` | Runs Vibe and checks, manages worktrees, records the ledger |
| Command | `commands/delegate-mistral.md` | `/delegate-mistral [read\|write] <task> \| status \| stats \| config` |

(All paths are relative to `plugins/mistral-delegate/`.)

## How a write task runs

1. **Worktree.** A new git worktree is made from your current code, including uncommitted and untracked files (committed there as a snapshot). Dependency folders such as `node_modules` and `.venv` are symlinked in.
2. **Prompt.** The task, the spec (`--spec`), the files to read first (`--context`), and the rules: which commands Mistral may run, which checks must pass, no package installs, no weakened tests.
3. **Vibe runs** with a generated agent profile that auto-approves file edits and only the commands you allowed (`--allow-command` / `allow_commands`). Each part of a chained command must be allowed, and everything else is refused.
4. **Checks.** The wrapper runs each `--verify` command in the worktree. If one fails, its output goes back to the same Vibe session for a fix (`--fix-attempts`, default 1), and the checks run again.
5. **Report.** It shows status, verification, cost, steps and tokens, the change list, the diff when short, and `adopt_with` / `discard_with` commands.
6. **Adopt or discard.** `--adopt <id>` applies only Mistral's changes to your checkout (your own uncommitted work is left alone) and removes the worktree. `--paths` takes only some files, and `--discard <id> --note "why"` drops the run.

Read tasks (`--mode read`) run in place with read-only tools.

## Policy and settings

Settings come from `~/.mistral-delegate/config.toml` (all projects), then `<repo>/.mistral-delegate.toml`, then environment variables, then flags. `delegate.py --show-config` prints the result and where each value came from.

```toml
policy = "balanced"          # conservative | balanced | aggressive
model = "mistral-medium-3.5" # a model alias from your Vibe config
verify = ["npm test"]
allow_commands = ["npm test"]
fix_attempts = 1
max_parallel = 3

[write]
max_turns = 30
max_price = 1.00

[read]
max_price = 0.25
```

| Policy | What Claude delegates | Default write cap | Default read cap |
|---|---|---|---|
| conservative | tests, docs, boilerplate, read-only searches | 20 turns, $0.50 | 10 turns, $0.15 |
| balanced | any step with a short spec and an automatic check | 30 turns, $1.00 | 15 turns, $0.25 |
| aggressive | every such step by default, in parallel | 50 turns, $2.50 | 20 turns, $0.50 |

Each fix round may spend up to half the cap again. The model must be an alias Vibe knows: the built-ins are `mistral-medium-3.5` and `local`, and you add others under `[[models]]` in `~/.vibe/config.toml`. The report warns when Vibe would fall back to its default model.

Environment variables: `MISTRAL_DELEGATE_POLICY`, `MISTRAL_DELEGATE_MODEL`, `MISTRAL_DELEGATE_MAX_TURNS`, `MISTRAL_DELEGATE_MAX_PRICE`, `MISTRAL_DELEGATE_MAX_TOKENS`, `MISTRAL_DELEGATE_TIMEOUT`, `MISTRAL_DELEGATE_HOME` (default `~/.mistral-delegate`), `MISTRAL_DELEGATE_WORKTREES`, `VIBE_BIN`.

## Parallel runs and the track record

- **Parallel runs.** Every run gets its own worktree, so Claude can start several at once, up to `max_parallel`. `--status` lists running and recent runs, and `--result <id>` prints a finished report.
- **Ledger.** Every run is recorded in `~/.mistral-delegate/ledger.jsonl`: kind of task, cost, check results, and whether it was adopted or discarded.
- **Track record.** `--stats` shows the track record per kind of task. The session-start hook gives Claude a summary, so it can delegate more of what works.

## Safety

- **Shell commands:** only the commands you allow, plus Vibe's read-only defaults, run without approval. In programmatic mode Vibe refuses everything else. `--allow-shell` (Vibe's `--auto-approve`) lifts that and is never on by default.
- **Limits:** every Vibe call has a turn and price cap and a 15-minute timeout.
- **Worktrees:** write tasks run in a worktree, and your checkout changes only when you adopt. The linked dependency folders are shared, which is why the prompt forbids installing packages.
- **Project config:** commands in a project's `.mistral-delegate.toml` are executed. In a repository you don't trust, read that file first, just as you would its `package.json` scripts.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use a fake `vibe` executable, so they need neither the real CLI nor an API key.
