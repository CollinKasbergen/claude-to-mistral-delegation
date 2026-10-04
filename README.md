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

   A fully commented starting point is in [`examples/.mistral-delegate.toml`](examples/.mistral-delegate.toml).

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

1. **Worktree.** A new git worktree is made from your current code, including uncommitted and untracked files (committed there as a snapshot). Ignored dependency folders such as `node_modules` and `.venv` are hard-linked in. They are real folders inside the worktree whose files are shared with your checkout, so tools like Vite accept them. Cache folders (`.vite`, `.cache`, …) are skipped. Set `deps_mode` to `copy`, `symlink` or `none` to change this.
2. **Baseline.** Your checks run once on the untouched worktree; checks limited to paths outside the run's scope are skipped, and a check that fails is rerun once so a flaky one isn't mistaken for a broken one. Checks that already fail there aren't blamed on Mistral: they're reported as pre-existing, Mistral is told not to work around them, and they never trigger a fix round.
3. **Prompt.** The task, the spec (`--spec`), the files to read first (`--context`), and the rules: which files Mistral may change (`--scope`), which commands it may run, which checks must pass, no package installs, no weakened tests.
4. **Guard.** A Vibe `pre_tool` hook checks every tool call during the run. Disallowed commands, paths outside the project, writes outside the scope, secrets and network tools are refused with an error Mistral sees and can work around. Paths that point to the right file under the wrong base (`/src/app.ts`, or a worktree path missing its run folder) are corrected to the project. `sed` calls that only print are allowed alongside Vibe's read-only commands (no `-i`, script files or `w`/`r`/`e`), allowed commands are accepted in their common spellings (`npm test` = `npm run test` = `npx vitest` when that's the test script), and the prompt gives Mistral the absolute project root and steers it to the read_file and grep tools. Without it, Vibe's programmatic mode treats a refused approval as the user cancelling and ends the whole session.
5. **Vibe runs** with a generated agent profile that auto-approves file edits and only the commands you allowed (`--allow-command` / `allow_commands`). Each part of a chained command must be allowed, and everything else is refused.
6. **Checks.** The wrapper runs each `--verify` command in the worktree. If one fails, its output goes back to the same Vibe session for a fix (`--fix-attempts`, default 1), and the checks run again.
7. **Resumes.** `--resume <session> --worktree-name <name>` continues in the same worktree under a new run id. The report shows which run it continues, and adopting or discarding either id settles both. Its baseline is the one stored before Mistral's first changes. A check that wasn't measured then runs on a clean copy of the original snapshot, so Mistral's own failures are never counted as already failing.
8. **Report.** It shows status, verification, usage (effective tokens and cost) and month-to-date credit, the change list, files changed outside the scope, commands Mistral was refused, the diff when short, and `adopt_with` / `discard_with` commands.
9. **Adopt or discard.** `--adopt <id>` applies Mistral's changes inside the scope to your checkout (your own uncommitted work is left alone) and removes the worktree. `--paths` takes only some files, `--include-out-of-scope` also takes changes outside the scope, and `--discard <id> --note "why"` drops the run.

Read tasks (`--mode read`) run in place with read-only tools.

## Policy and settings

Settings come from `~/.mistral-delegate/config.toml` (all projects), then `<repo>/.mistral-delegate.toml`, then environment variables, then flags. `delegate.py --show-config` prints the result and where each value came from.

```toml
policy = "balanced"          # conservative | balanced | aggressive
model = "mistral-medium-3.5" # a model alias from your Vibe config; unset = Vibe's default, which
                             # a server-side experiment may route to a non-Mistral model
model_prices = { "glm-5-3" = [1.0, 4.0] }  # $/M tokens (input, output) for models Vibe has no price for
verify = [
  "npm run lint",                                     # always runs
  { cmd = "npx vitest run", paths = ["frontend/"] },  # only when the scope or changes touch frontend/
  { cmd = "pytest -q", paths = ["backend/"] },
]
allow_commands = ["npm test"]
fix_attempts = 1
max_parallel = 3
deps_mode = "hardlink"       # hardlink | copy | symlink | none
baseline = true              # run the checks on the untouched worktree first
# vibe_args = ["--legacy-harness"]   # extra Vibe flags; this one gives exact costs
# worktrees_dir = "/Volumes/SSD/.mistral-worktrees"  # default: ~/.mistral-delegate/worktrees, or
#                                                    # <repo parent>/.mistral-worktrees if the repo is on another disk

monthly_credit = 225         # your Vibe credit per month (e.g. Mistral Pro's), tracked in reports
currency = "€"
credit_reset_day = 1         # day of the month the credit renews
min_savings = 2              # stop delegating kinds of task whose measured savings fall below this
autofix = ["ruff format ."]  # run these when checks fail, before asking Mistral to fix
# token_weights = { input = 1.0, cached = 0.1, output = 5.0 }  # what counts as an effective token

[write]
token_budget = 1000000  # stop Mistral after this many effective tokens (enforced by the wrapper)
max_tool_calls = 80     # stop Mistral after this many tool calls (enforced by the wrapper)
# max_price = 2.00      # optional money cap on top, in the model's price units
max_turns = 30          # passed to Vibe

[read]
token_budget = 300000
```

| Policy | What Claude delegates | Default write cap | Default read cap |
|---|---|---|---|
| conservative | tests, docs, boilerplate, read-only searches | 400k effective tokens, 50 tool calls | 150k, 25 tool calls |
| balanced | any step with a short spec and an automatic check | 1M effective tokens, 80 tool calls | 300k, 40 tool calls |
| aggressive | every such step by default, in parallel | 2.5M effective tokens, 150 tool calls | 600k, 60 tool calls |

**Effective tokens.** Runs are measured and capped in effective tokens rather than money: fresh input tokens count in full, cached input tokens at a tenth, output tokens five times. These are Mistral Medium's price ratios, adjustable with `token_weights`. Agents re-send their context on every step, so most input is cached; at Mistral Medium's prices, 1M effective tokens is about $1.50. The wrapper watches each run's usage and tool calls while Vibe works and stops it at `token_budget` or `max_tool_calls` (`status: budget_exceeded` / `tool_call_limit`). `max_price` adds an optional money cap. A continuation, when a run stops without a closing summary, or a fix round gets half of each cap. A run stopped at a cap still gets its fix round (`fix_after_cap = false` turns that off), and `autofix` commands run first, so a formatting failure costs no Mistral round at all.

**Credit and costs.** Reports show each run's usage (fresh, cached and output tokens), its cost at the model's list prices with cached input at the cached rate, and, with `monthly_credit` set, how much of the month's credit is used. The session-start reminder tells Claude how much credit is left, so it delegates freely early in the month and selectively near the end. For a model with no known price, add it to `model_prices` as `[input, output, cached]` per million tokens; earlier runs are then priced from their recorded tokens.

**Savings.** Each run records an estimate of what delegating cost Claude (writing the task and spec, reading the report) and of the Claude work it replaced (Mistral's effective tokens × `claude_relative_effort`, 0.5 by default). `--stats` shows the ratio per kind of task, counting adopted runs as saved and discarded ones as wasted overhead. With `min_savings` set, the session-start reminder tells Claude which kinds haven't paid off, so it keeps those itself.

**Model.** The report's `model:` line says which model ran. With no `model` set, Vibe uses its own default, and a server-side experiment can route that to a non-Mistral model (e.g. `glm-5-3`); `model_note` flags it. Pin `model = "mistral-medium-3.5"` to always use Mistral.

The model must be an alias Vibe knows: the built-ins are `mistral-medium-3.5` and `local`, and you add others under `[[models]]` in `~/.vibe/config.toml`. The report warns when Vibe would fall back to its default model.

Environment variables: `MISTRAL_DELEGATE_POLICY`, `MISTRAL_DELEGATE_MODEL`, `MISTRAL_DELEGATE_MAX_TURNS`, `MISTRAL_DELEGATE_TOKEN_BUDGET`, `MISTRAL_DELEGATE_MAX_TOOL_CALLS`, `MISTRAL_DELEGATE_MAX_PRICE`, `MISTRAL_DELEGATE_MAX_TOKENS`, `MISTRAL_DELEGATE_TIMEOUT`, `MISTRAL_DELEGATE_HOME` (default `~/.mistral-delegate`), `MISTRAL_DELEGATE_WORKTREES` (same as `worktrees_dir`), `VIBE_BIN`.

## Parallel runs and the track record

- **Parallel runs.** Every run gets its own worktree, so Claude can start several at once, up to `max_parallel`. `--status` lists running and recent runs, and `--result <id>` prints a finished report.
- **Ledger.** Every run is recorded in `~/.mistral-delegate/ledger.jsonl`: kind of task, cost, check results, and whether it was adopted or discarded.
- **Track record.** `--stats` shows the track record per kind of task. The session-start hook gives Claude a summary, so it can delegate more of what works.

## Safety

- **Shell commands:** only the commands you allow, plus Vibe's read-only defaults, run without approval. In programmatic mode Vibe refuses everything else. `--allow-shell` (Vibe's `--auto-approve`) lifts that and is never on by default.
- **Limits:** every Vibe call has a turn and price cap and a 15-minute timeout.
- **Guard hook:** on the first run, the plugin copies its guard to `~/.mistral-delegate/vibe_guard.py` and adds one clearly marked entry to `~/.vibe/hooks.toml`. Your other hooks are kept, and a file that doesn't parse is left untouched. The hook does nothing unless a delegation run is active for that directory, so your own Vibe sessions are unaffected. It is stricter than Vibe's permissions, which stay on, so if it's missing, runs fall back to Vibe's behaviour rather than allowing more.
- **Worktrees:** write tasks run in a worktree, and your checkout changes only when you adopt. The linked dependency folders are shared, which is why the prompt forbids installing packages.
- **Project config:** commands in a project's `.mistral-delegate.toml` are executed. In a repository you don't trust, read that file first, just as you would its `package.json` scripts.

## Tests

```bash
python3 -m unittest discover -s tests
```

The tests use a fake `vibe` executable, so they need neither the real CLI nor an API key.
