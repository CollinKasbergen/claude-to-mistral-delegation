"""Talking to Vibe: prompt, agent profile, command line, output and session stats.

Vibe behaviour relied on (checked against mistral-vibe 2.25.x):
  * `--output json` prints a JSON list of history entries (camelCase keys). Tool
    calls are "effect" entries; shell calls carry `detail.input.command`.
  * In programmatic mode, any tool call that needs approval is denied.
  * Custom agent profiles are TOML files in $VIBE_HOME/agents/<name>.toml. They
    can set `active_model` and per-tool permissions, including
    `[tools.bash] allowlist` (command prefixes; every part of a compound
    command must match for it to be auto-approved).
  * Hitting --max-turns / --max-price / --max-tokens exits 1 with the last
    assistant text on stderr and nothing on stdout.
  * Sessions are stored under the session-logging save_dir ($VIBE_HOME/logs/session
    by default), in one of two layouts:
      - legacy harness: <save_dir>/<prefix>_<time>_<id[:8]>/meta.json, whose
        "stats" hold session_cost, steps and token counts;
      - Unified Harness: <save_dir>/unified/<id>/journal/*.jsonl, whose
        projection records hold session.tokenUsage {inputTokens, outputTokens}.
        There is no cost there, so it is estimated from the model's prices.
"""

from __future__ import annotations

import hashlib
import json
import os
from fnmatch import fnmatch
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

READ_ONLY_TOOLS = ["read_file", "grep", "todo"]

# Vibe's default bash allowlist (POSIX). A profile's allowlist replaces it, so keep it.
DEFAULT_BASH_ALLOWLIST = [
    "cd", "echo", "git diff", "git log", "git status", "tree", "whoami",
    "basename", "cat", "comm", "cut", "date", "diff", "dirname", "du", "file", "find", "fmt",
    "fold", "grep", "head", "join", "less", "ls", "md5sum", "more", "nl", "od", "paste", "pwd",
    "readlink", "sha1sum", "sha256sum", "shasum", "sort", "stat", "sum", "tac", "tail", "tr",
    "uname", "uniq", "wc", "which",
]

# Model aliases Vibe ships with, and their prices in $ per million tokens (input, output).
# Users add models under [[models]] in $VIBE_HOME/config.toml, with input_price/output_price.
BUILTIN_MODELS = {"mistral-medium-3.5": (1.5, 7.5), "local": (0.0, 0.0)}
DEFAULT_MODEL_ALIAS = "mistral-medium-3.5"

MAX_SPEC_CHARS = 20_000


def vibe_home() -> Path:
    home = os.environ.get("VIBE_HOME")
    return Path(home).expanduser() if home else Path.home() / ".vibe"


def vibe_config() -> dict:
    if tomllib is None:
        return {}
    try:
        with (vibe_home() / "config.toml").open("rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError):
        return {}


# Prices the user gave in the plugin config (model_prices), keyed by alias: (input, output) $/M tokens.
EXTRA_PRICES: dict[str, tuple[float, float]] = {}

MISTRAL_MODEL_PREFIXES = ("mistral", "devstral", "codestral", "magistral", "ministral", "pixtral", "leanstral", "local")


def model_prices(config: dict | None = None) -> dict[str, tuple[float, float]]:
    prices = dict(BUILTIN_MODELS)
    for m in (config if config is not None else vibe_config()).get("models", []):
        if isinstance(m, dict) and m.get("alias"):
            prices[m["alias"]] = (float(m.get("input_price", 0.0)), float(m.get("output_price", 0.0)))
    prices.update(EXTRA_PRICES)
    return prices


def _find_model_price(data, alias: str) -> tuple[float, float] | None:
    """Search a JSON structure (e.g. a session's experiments) for a model definition with prices."""
    if isinstance(data, dict):
        if data.get("alias") == alias and "input_price" in data and "output_price" in data:
            try:
                return float(data["input_price"]), float(data["output_price"])
            except (TypeError, ValueError):
                return None
        values = data.values()
    elif isinstance(data, list):
        values = data
    elif isinstance(data, str) and alias in data and data.lstrip().startswith(("{", "[")):
        try:
            return _find_model_price(json.loads(data), alias)
        except ValueError:
            return None
    else:
        return None
    for value in values:
        found = _find_model_price(value, alias)
        if found:
            return found
    return None


def is_mistral_model(alias: str | None) -> bool:
    return not alias or alias.lower().startswith(MISTRAL_MODEL_PREFIXES)


def unknown_model_warning(model: str | None) -> str | None:
    """Vibe silently falls back to its default model for an alias it doesn't know; say so."""
    if not model:
        return None
    aliases = model_prices()
    if model in aliases:
        return None
    return (f"model {model!r} is not in Vibe's configured models ({', '.join(sorted(aliases))}); "
            "Vibe falls back to its default model. Add it under [[models]] in ~/.vibe/config.toml.")


def _toml_str(value: str) -> str:
    return json.dumps(value)  # JSON string escapes are valid TOML basic-string escapes.


def write_agent_profile(mode: str, model: str | None, allow_commands: list[str]) -> str | None:
    """Write a Vibe agent profile for this run and return its name, or None to use a built-in agent."""
    if mode == "read" and not model:
        return None
    lines = [
        'display_name = "Claude delegate"',
        'description = "Generated by the mistral-delegate Claude Code plugin"',
        'disabled_tools = ["exit_plan_mode"]',
    ]
    if model:
        lines.append(f"active_model = {_toml_str(model)}")
    edit_permission = "always" if mode == "write" else "never"
    lines += ["", "[tools.write_file]", f'permission = "{edit_permission}"',
              "", "[tools.edit]", f'permission = "{edit_permission}"']
    if mode == "write":
        allowlist = list(dict.fromkeys(DEFAULT_BASH_ALLOWLIST + allow_commands))
        lines += ["", "[tools.bash]", "allowlist = [" + ", ".join(_toml_str(c) for c in allowlist) + "]"]
    content = "\n".join(lines) + "\n"

    name = f"claude-delegate-{hashlib.sha1(content.encode()).hexdigest()[:10]}"
    path = vibe_home() / "agents" / f"{name}.toml"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(content)
        tmp.replace(path)
    return name


def build_prompt(task: str, *, mode: str, spec: str | None, context: list[str], verify: list[str],
                 allow_commands: list[str], allow_shell: bool, scope: list[str] | None = None,
                 preexisting_failures: list[str] | None = None, root: str | None = None,
                 cwd: str | None = None) -> str:
    parts = [task.strip()]
    if spec:
        if len(spec) > MAX_SPEC_CHARS:
            spec = spec[:MAX_SPEC_CHARS] + "\n[... spec truncated ...]"
        parts.append("## Spec\n\n" + spec.strip())
    if context:
        parts.append("## Read these files first (paths relative to the project root)\n\n"
                     + "\n".join(f"- {c}" for c in context))
    if root:
        where = (f"The project root is `{root}`"
                 + (f"; your working directory is `{cwd}`" if cwd and cwd != root else " and it is your working directory")
                 + ". Use paths relative to the project root (e.g. `src/app.ts`) or absolute paths inside it; "
                   "never go above it. Read and search files with the read_file and grep tools rather than "
                   "shell commands.")
        parts.append("## Workspace\n\n" + where)

    if mode == "write":
        rules = []
        if scope:
            rules.append("Only create or change files matching: " + ", ".join(f"`{s}`" for s in scope)
                         + ". Don't edit any other file, not even to make a check pass. If the task "
                           "seems to need other changes, stop and explain that in your summary.")
        if allow_shell:
            rules.append("You may run shell commands.")
        elif allow_commands:
            rules.append("You may run these shell commands yourself: "
                         + ", ".join(f"`{c}`" for c in allow_commands)
                         + ". Other spellings of them work too (e.g. `npm run test` for `npm test`). Read-only "
                           "commands (`ls`, `cat`, `grep`, `find`, `sed -n`) are available as well, but prefer the "
                           "read_file and grep tools. Anything else will be refused with an explanation.")
        else:
            rules.append("Shell commands will be refused, so don't try to run anything; just write the code.")
        if verify:
            rules.append("When you finish, these checks will be run, and they must pass: "
                         + ", ".join(f"`{c}`" for c in verify) + ".")
        if preexisting_failures:
            rules.append("These checks already fail before your change, for reasons outside your task: "
                         + ", ".join(f"`{c}`" for c in preexisting_failures)
                         + ". Don't change code outside your task to make them pass; mention them in your summary.")
        rules += [
            "Don't install, upgrade or remove packages: dependencies are shared with the user's checkout.",
            "Only change what the task needs. Don't delete or weaken existing tests.",
            "Finish with a short list of every file you changed and why, then anything you couldn't do.",
        ]
        parts.append("## Rules\n\n" + "\n".join(f"- {r}" for r in rules))
    return "\n\n".join(parts)


def fix_prompt(failures: list[tuple[str, int, str]], scope: list[str] | None = None) -> str:
    sections = [f"The check `{cmd}` failed (exit code {code}). The end of its output:\n\n```\n{out}\n```"
                for cmd, code, out in failures]
    rule = (" Only change files matching " + ", ".join(f"`{s}`" for s in scope) + ".") if scope else ""
    return ("\n\n".join(sections) + "\n\nFix the code so these checks pass. Don't change the checks, and "
            "don't delete or weaken tests to make them pass." + rule
            + " Finish with the files you changed and what you fixed.")


def build_command(vibe_bin: str, prompt: str, *, mode: str, agent: str | None, caps: dict,
                  allow_shell: bool, trust: bool, resume: str | None, extra: list[str] | None = None) -> list[str]:
    cmd = [
        vibe_bin,
        "--prompt", prompt,
        "--output", "json",
        "--max-turns", str(caps["max_turns"]),
        "--max-price", f"{caps['max_price']:.4f}",
    ]
    if caps.get("max_tokens"):
        cmd += ["--max-tokens", str(caps["max_tokens"])]
    cmd += ["--agent", agent or ("plan" if mode == "read" else "accept-edits")]
    if mode == "read":
        for tool in READ_ONLY_TOOLS:
            cmd += ["--enabled-tools", tool]
    elif allow_shell:
        cmd += ["--auto-approve"]
    if trust:
        cmd += ["--trust"]
    if resume:
        cmd += ["--resume", resume]
    return cmd + list(extra or [])


# --- output ---------------------------------------------------------------------

def text_of(entry: dict) -> str:
    texts: list[str] = []
    for block in entry.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            text = (block.get("text") or "").strip()
            if text and text not in texts:  # some histories repeat the same block
                texts.append(text)
    return "\n\n".join(texts)


TARGET_KEYS = ("command", "cmd", "file_path", "filePath", "path", "url", "pattern", "query")


def _find_string(data, keys: tuple[str, ...], depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
            if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
                return " ".join(value)
        for key, value in data.items():
            if key in ("state", "output", "result", "content"):
                continue
            found = _find_string(value, keys, depth + 1)
            if found:
                return found
    return ""


def _effect_target(entry: dict) -> str:
    """The command or file a tool call was about, for reporting."""
    return _find_string(entry.get("detail") or {}, TARGET_KEYS) or _find_string(
        {k: v for k, v in entry.items() if k not in ("state",)}, TARGET_KEYS)


def _effect_tool(entry: dict) -> str:
    detail = entry.get("detail") or {}
    display = detail.get("display") if isinstance(detail.get("display"), dict) else {}
    for value in (detail.get("toolName"), detail.get("tool_name"), detail.get("name"), entry.get("title"),
                  display.get("title"), detail.get("kind"), entry.get("toolName")):
        if isinstance(value, str) and value.strip():
            return value
    return "tool"


def _is_denied(state: dict) -> bool:
    status = state.get("status")
    if state.get("decision") == "skip" or status == "skipped":
        return True
    reason = f"{state.get('reason') or ''} {(state.get('error') or {}).get('message') or ''}".lower()
    return status in ("cancelled", "failed") and any(w in reason for w in ("denied", "not approved", "rejected"))


def summarize_history(history: list) -> dict:
    session_id = None
    final_text = ""
    tool_calls = 0
    assistant_messages = 0
    denied: list[str] = []
    problems: list[str] = []
    notices: list[str] = []

    for entry in history:
        if not isinstance(entry, dict):
            continue
        session_id = session_id or entry.get("sessionId")
        kind = entry.get("type")
        if kind == "message" and entry.get("role") == "assistant":
            assistant_messages += 1
            text = text_of(entry)
            if text:
                final_text = text
        elif kind == "effect":
            tool_calls += 1
            state = entry.get("state") or {}
            tool = _effect_tool(entry)
            target = _effect_target(entry)
            if _is_denied(state):
                denied.append(f"{tool}: {target}" if target else str(entry.get("title") or tool))
            elif state.get("status") not in ("completed", None):
                reason = state.get("reason") or (state.get("error") or {}).get("message") or ""
                label = f"{tool}: {target}" if target else str(entry.get("title") or tool)
                problems.append(f"{label} -> {state.get('status')}" + (f" ({reason})" if reason else ""))
        elif kind == "notice" and entry.get("level") in ("warning", "error"):
            notices.append(f"{entry.get('level')}: {entry.get('message', '')}")

    return {"session_id": session_id, "final_text": final_text, "tool_calls": tool_calls,
            "assistant_messages": assistant_messages, "denied": denied, "problems": problems,
            "notices": notices}


def parse_output(stdout: str) -> list:
    stdout = stdout.strip()
    if not stdout:
        return []
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        start = stdout.find("[")  # Tolerate stray lines before the JSON payload.
        if start == -1:
            return []
        try:
            data = json.loads(stdout[start:])
        except json.JSONDecodeError:
            return []
    if isinstance(data, dict):
        data = data.get("history", [])
    return data if isinstance(data, list) else []


# --- session stats ----------------------------------------------------------------

def session_root(config: dict | None = None) -> Path:
    config = vibe_config() if config is None else config
    save_dir = (config.get("session_logging") or {}).get("save_dir")
    return Path(save_dir).expanduser() if save_dir else vibe_home() / "logs" / "session"


def _same_dir(a: str | None, b: str) -> bool:
    return not a or Path(a).resolve() == Path(b).resolve()


def _legacy_stats(root: Path, session_id: str | None, cwd: str, since: float | None) -> dict | None:
    if session_id:
        metas = list(root.glob(f"*_{session_id[:8]}/meta.json"))
    else:
        dirs = sorted((d for d in root.iterdir() if d.is_dir() and d.name != "unified"),
                      key=lambda d: d.stat().st_mtime)[-50:]
        metas = [d / "meta.json" for d in dirs]
    for meta_path in sorted(metas, key=lambda m: m.stat().st_mtime if m.exists() else 0, reverse=True):
        try:
            if since is not None and meta_path.stat().st_mtime < since - 1:
                continue
            meta = json.loads(meta_path.read_text())
        except (OSError, ValueError):
            continue
        if session_id and meta.get("session_id") != session_id:
            continue
        if not session_id and not _same_dir((meta.get("environment") or {}).get("working_directory"), cwd):
            continue
        stats = meta.get("stats")
        if isinstance(stats, dict):
            model = (meta.get("config") or {}).get("active_model") if isinstance(meta.get("config"), dict) else None
            return {"session_id": meta.get("session_id"), "model": model or None, **stats}
    return None


def _unified_token_usage(session_dir: Path) -> dict | None:
    """The latest session.tokenUsage recorded in a Unified Harness session's journal."""
    usage = None
    for journal in sorted((session_dir / "journal").glob("*.jsonl")):
        try:
            lines = journal.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"tokenUsage"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            for delta in (record.get("payload") or {}).get("delta") or []:
                session = ((delta or {}).get("state") or {}).get("session") or {}
                if isinstance(session.get("tokenUsage"), dict):
                    usage = session["tokenUsage"]
    return usage


def _unified_model(session_dir: Path) -> str | None:
    generations = sorted((session_dir / "generations").glob("*/runtime-state.json"))
    for path in reversed(generations):
        try:
            return json.loads(path.read_text())["session_metadata"]["active_model"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def _unified_stats(root: Path, session_id: str | None, cwd: str, since: float | None,
                   model_hint: str | None, config: dict) -> dict | None:
    base = root / "unified"
    if not base.is_dir():
        return None
    if session_id:
        candidates = [base / session_id]
    else:
        candidates = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime,
                            reverse=True)[:50]
    for session_dir in candidates:
        try:
            if since is not None and session_dir.stat().st_mtime < since - 1:
                continue
            meta = json.loads((session_dir / "meta.json").read_text())
        except (OSError, ValueError):
            continue
        if not session_id and not _same_dir((meta.get("environment") or {}).get("working_directory"), cwd):
            continue
        usage = _unified_token_usage(session_dir)
        if usage is None:
            continue
        model = _unified_model(session_dir) or model_hint or DEFAULT_MODEL_ALIAS
        # Prices: Vibe's config and the plugin's model_prices, then the model definition a
        # server-side experiment supplied for this session (saved in its meta.json).
        price = model_prices(config).get(model) or _find_model_price(
            [meta.get("experiments"), meta.get("config")], model)
        stats = {
            "session_id": meta.get("session_id") or session_dir.name,
            "session_prompt_tokens": int(usage.get("inputTokens") or 0),
            "session_completion_tokens": int(usage.get("outputTokens") or 0),
            "steps": None,
            "model": model,
            "estimated": True,
        }
        if price:
            stats["input_price_per_million"], stats["output_price_per_million"] = price
        else:
            stats["price_unknown"] = True
        return stats
    return None


def read_session_stats(session_id: str | None, cwd: str, since: float | None = None,
                       model_hint: str | None = None) -> dict | None:
    """Find a session's stats in Vibe's session storage (legacy or Unified Harness layout).

    With a session id, match it exactly. Without one (e.g. when a limit stopped the
    run and nothing was printed), take the newest session written since `since`
    for the same working directory.
    """
    config = vibe_config()
    root = session_root(config)
    if not root.is_dir():
        return None
    return (_legacy_stats(root, session_id, cwd, since)
            or _unified_stats(root, session_id, cwd, since, model_hint, config))


def stats_cost(stats: dict | None) -> float | None:
    if not stats:
        return 0.0
    if stats.get("price_unknown"):
        return None
    if "session_cost" in stats:
        return float(stats["session_cost"])
    prompt = stats.get("session_prompt_tokens", 0)
    cached = min(stats.get("session_cached_tokens", 0), prompt)
    in_price = stats.get("input_price_per_million", 0.0)
    cached_price = stats.get("cached_input_price_per_million")
    cached_price = in_price if cached_price is None else cached_price
    out = stats.get("session_completion_tokens", 0) * stats.get("output_price_per_million", 0.0)
    return ((prompt - cached) * in_price + cached * cached_price + out) / 1_000_000


def usage(after: dict | None, before: dict | None) -> dict | None:
    """This run's cost, steps and tokens (after minus before, for resumed sessions)."""
    if not after:
        return None
    before = before or {}
    cost_after, cost_before = stats_cost(after), stats_cost(before) if before else 0.0
    steps = after.get("steps")
    if steps is not None and before.get("steps") is not None:
        steps -= before["steps"]
    return {
        "cost": None if cost_after is None or cost_before is None else cost_after - cost_before,
        "estimated": bool(after.get("estimated")),
        "model": after.get("model"),
        "tokens_in": after.get("session_prompt_tokens", 0) - before.get("session_prompt_tokens", 0),
        "tokens_out": after.get("session_completion_tokens", 0) - before.get("session_completion_tokens", 0),
        "steps": steps,
        "tokens": (after.get("session_prompt_tokens", 0) + after.get("session_completion_tokens", 0)
                   - before.get("session_prompt_tokens", 0) - before.get("session_completion_tokens", 0)),
        "session_total": cost_after if before else None,
    }


def matches_scope(path: str, scope: list[str]) -> bool:
    return any(fnmatch(path, pattern) or path == pattern.rstrip("/")
               or (pattern.endswith("/") and path.startswith(pattern)) for pattern in scope)


def _literal_prefix(pattern: str) -> str:
    """The part of a glob before its first wildcard."""
    cut = min((pattern.find(c) for c in "*?[" if c in pattern), default=len(pattern))
    return pattern[:cut]


def check_applies(check_paths: list[str], scope: list[str], files: list[str]) -> bool:
    """Whether a check limited to check_paths is relevant to this run's scope or changed files."""
    if not check_paths:
        return True
    if any(matches_scope(f, check_paths) or any(f.startswith(_literal_prefix(p)) for p in check_paths)
           for f in files):
        return True
    for s in scope:
        a = _literal_prefix(s)
        for p in check_paths:
            b = _literal_prefix(p)
            if a.startswith(b) or b.startswith(a):
                return True
    return False
