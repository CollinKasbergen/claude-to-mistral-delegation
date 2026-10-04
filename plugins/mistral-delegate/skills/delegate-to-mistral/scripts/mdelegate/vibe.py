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
import re
from fnmatch import fnmatch
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    from . import minitoml as tomllib

READ_ONLY_TOOLS = ["read_file", "grep", "todo"]

# Vibe's default bash allowlist (POSIX). A profile's allowlist replaces it, so keep it.
DEFAULT_BASH_ALLOWLIST = [
    "cd", "echo", "git diff", "git log", "git status", "tree", "whoami",
    "basename", "cat", "comm", "cut", "date", "diff", "dirname", "du", "file", "find", "fmt",
    "fold", "grep", "head", "join", "less", "ls", "md5sum", "more", "nl", "od", "paste", "pwd",
    "readlink", "sha1sum", "sha256sum", "shasum", "sort", "stat", "sum", "tac", "tail", "tr",
    "uname", "uniq", "wc", "which",
]

# Model aliases Vibe ships with, and their prices per million tokens: (input, output, cached input).
# Users add models under [[models]] in $VIBE_HOME/config.toml (input_price, output_price, cached_input_price).
BUILTIN_MODELS = {"mistral-medium-3.5": (1.5, 7.5, 0.15), "local": (0.0, 0.0, 0.0)}
DEFAULT_MODEL_ALIAS = "mistral-medium-3.5"
# Cached input is billed at a fraction of the input price; used when a model's cached price is unknown.
DEFAULT_CACHED_FRACTION = 0.1

# Effective tokens: one number for "how much work did Mistral do", independent of prices.
# Fresh input counts in full, cached input at a tenth, output five times (Mistral Medium's price ratios).
DEFAULT_WEIGHTS = {"input": 1.0, "cached": 0.1, "output": 5.0}
WEIGHTS: dict[str, float] = dict(DEFAULT_WEIGHTS)

MAX_SPEC_CHARS = 20_000


def vibe_home() -> Path:
    home = os.environ.get("VIBE_HOME")
    return Path(home).expanduser() if home else Path.home() / ".vibe"


def vibe_config() -> dict:
    try:
        with (vibe_home() / "config.toml").open("rb") as f:
            return tomllib.load(f)
    except (OSError, ValueError):
        return {}


# Prices from the plugin config (model_prices), keyed by alias: (input, output, cached or None) per M tokens.
EXTRA_PRICES: dict[str, tuple] = {}

MISTRAL_MODEL_PREFIXES = ("mistral", "devstral", "codestral", "magistral", "ministral", "pixtral", "leanstral", "local")


def _price_triple(data: dict) -> tuple | None:
    try:
        cached = data.get("cached_input_price")
        return (float(data.get("input_price", 0.0)), float(data.get("output_price", 0.0)),
                None if cached is None else float(cached))
    except (TypeError, ValueError):
        return None


def model_prices(config: dict | None = None) -> dict[str, tuple]:
    prices = dict(BUILTIN_MODELS)
    for m in (config if config is not None else vibe_config()).get("models", []):
        if isinstance(m, dict) and m.get("alias") and (triple := _price_triple(m)):
            prices[m["alias"]] = triple
    prices.update(EXTRA_PRICES)
    return prices


def _find_model_price(data, alias: str) -> tuple | None:
    """Search a JSON structure (e.g. a session's experiments) for a model definition with prices."""
    if isinstance(data, dict):
        if data.get("alias") == alias and "input_price" in data and "output_price" in data:
            return _price_triple(data)
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


def price_cost(tokens_in: int, cached: int, tokens_out: int, price: tuple) -> float:
    """Cost of a usage at a (input, output, cached-or-None) price; cached input at its own rate."""
    cached = min(max(cached, 0), max(tokens_in, 0))
    cached_price = price[2] if len(price) > 2 and price[2] is not None else price[0] * DEFAULT_CACHED_FRACTION
    return ((tokens_in - cached) * price[0] + cached * cached_price + tokens_out * price[1]) / 1_000_000


def effective_tokens(tokens_in: int, cached: int, tokens_out: int) -> int:
    cached = min(max(cached, 0), max(tokens_in, 0))
    return int((tokens_in - cached) * WEIGHTS["input"] + cached * WEIGHTS["cached"] + tokens_out * WEIGHTS["output"])


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
                   "shell commands."
                 + (" The absolute path contains spaces: use relative paths in shell commands, and quote any "
                    "absolute path." if " " in root else ""))
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
    ]
    if caps.get("max_price") is not None:
        cmd += ["--max-price", f"{caps['max_price']:.4f}"]
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
                  display.get("title"), entry.get("toolName"),
                  detail.get("kind") if detail.get("kind") not in ("tool", None) else None):
        if isinstance(value, str) and value.strip():
            return value
    return "tool"


def _label(entry: dict) -> str:
    """'tool: command-or-path' for a tool call; falls back to its raw input so it is never just 'tool'."""
    tool, target = _effect_tool(entry), _effect_target(entry)
    if target:
        return f"{tool}: {target}"
    data = (entry.get("detail") or {}).get("input")
    if data:
        return f"{tool}: {json.dumps(data, ensure_ascii=False)[:160]}"
    return tool


def _callback_label(entry: dict) -> str | None:
    """Label for a refused approval request (a 'callback' entry): the tool call it was about."""
    detail = entry.get("detail") or {}
    if detail.get("kind") != "approval":
        return None
    effect = detail.get("effect") or {}
    label = _label({"detail": effect, "title": entry.get("title")})
    if label in ("tool", None) or label.endswith(": "):
        perms = detail.get("requiredPermissions") or detail.get("required_permissions") or []
        pattern = next((p.get("invocationPattern") or p.get("label") for p in perms if isinstance(p, dict)), None)
        if pattern:
            label = f"{_effect_tool({'detail': effect}) if effect else 'tool'}: {pattern}"
    return label


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
    callback_denied: list[str] = []
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
            if _is_denied(state):
                denied.append(_label(entry))
            elif state.get("status") not in ("completed", None):
                reason = state.get("reason") or (state.get("error") or {}).get("message") or ""
                problems.append(f"{_label(entry)} -> {state.get('status')}" + (f" ({reason})" if reason else ""))
        elif kind == "callback":
            state = entry.get("state") or {}
            decision = ((state.get("output") or {}).get("decision") or {}).get("type") if isinstance(state.get("output"), dict) else None
            if state.get("status") in ("cancelled", "expired") or decision in ("deny", "denied", "reject"):
                label = _callback_label(entry)
                if label:
                    callback_denied.append(label)
        elif kind == "notice" and entry.get("level") in ("warning", "error"):
            notices.append(f"{entry.get('level')}: {entry.get('message', '')}")

    # A refused approval shows up twice: as the approval request and as the skipped tool call.
    # The request names the call properly, so when there are any, they are the list.
    if callback_denied:
        denied = callback_denied
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


class JournalReader:
    """Reads a Unified Harness session journal incrementally.

    Tracks the session's cumulative token usage (session.tokenUsage), cached input
    tokens (tokenUsage.cachedInputTokens when the harness reports it, otherwise the
    sum of cached_input_tokens over the completions it recorded) and tool calls.
    """

    def __init__(self, session_dir: Path):
        self.dir = session_dir
        self._offsets: dict[Path, int] = {}
        self._partial: dict[Path, str] = {}
        self.usage: dict | None = None
        self.completion_cached = 0
        self.effects: set[str] = set()

    def read(self) -> None:
        for journal in sorted((self.dir / "journal").glob("*.jsonl")):
            try:
                with journal.open("r", encoding="utf-8", errors="replace") as f:
                    f.seek(self._offsets.get(journal, 0))
                    chunk = f.read()
                    self._offsets[journal] = f.tell()
            except OSError:
                continue
            lines = (self._partial.pop(journal, "") + chunk).split("\n")
            self._partial[journal] = lines.pop()  # an incomplete last line waits for the next read
            for line in lines:
                if '"tokenUsage"' not in line and '"effect"' not in line and "cached_input_tokens" not in line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                payload = record.get("payload") or {}
                for delta in payload.get("delta") or []:
                    if not isinstance(delta, dict):
                        continue
                    session = (delta.get("state") or {}).get("session") or {}
                    if isinstance(session.get("tokenUsage"), dict):
                        self.usage = session["tokenUsage"]
                    entry = delta.get("entry") or {}
                    if entry.get("type") == "effect" and entry.get("id"):
                        self.effects.add(entry["id"])
                if record.get("type") != "projection_delta" and "cached_input_tokens" in line:
                    self.completion_cached += _sum_key(payload, "cached_input_tokens")

    def tokens(self) -> tuple[int, int, int]:
        """(input incl. cached, cached, output)."""
        usage = self.usage or {}
        tokens_in = int(usage.get("inputTokens") or usage.get("input_tokens") or 0)
        cached = usage.get("cachedInputTokens", usage.get("cached_input_tokens"))
        cached = int(cached) if cached is not None else self.completion_cached
        return tokens_in, min(cached, tokens_in), int(usage.get("outputTokens") or usage.get("output_tokens") or 0)


def _sum_key(data, key: str, depth: int = 0) -> int:
    if depth > 12:
        return 0
    if isinstance(data, dict):
        total = int(data[key]) if isinstance(data.get(key), (int, float)) else 0
        return total + sum(_sum_key(v, key, depth + 1) for k, v in data.items() if k != key)
    if isinstance(data, list):
        return sum(_sum_key(v, key, depth + 1) for v in data)
    return 0


def _unified_model(session_dir: Path) -> str | None:
    generations = sorted((session_dir / "generations").glob("*/runtime-state.json"))
    for path in reversed(generations):
        try:
            return json.loads(path.read_text())["session_metadata"]["active_model"]
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return None


def _legacy_snapshot(meta: dict, model_hint: str | None, config: dict) -> dict | None:
    stats = meta.get("stats")
    if not isinstance(stats, dict):
        return None
    model = ((meta.get("config") or {}).get("active_model") if isinstance(meta.get("config"), dict) else None) \
        or model_hint or DEFAULT_MODEL_ALIAS
    price = None
    if stats.get("input_price_per_million") or stats.get("output_price_per_million"):
        price = (float(stats.get("input_price_per_million") or 0), float(stats.get("output_price_per_million") or 0),
                 None if stats.get("cached_input_price_per_million") is None
                 else float(stats["cached_input_price_per_million"]))
    price = model_prices(config).get(model) or price
    return {
        "session_id": meta.get("session_id"),
        "model": model,
        "tokens_in": int(stats.get("session_prompt_tokens") or 0),
        "cached": int(stats.get("session_cached_tokens") or 0),
        "tokens_out": int(stats.get("session_completion_tokens") or 0),
        "steps": stats.get("steps"),
        "tool_calls": sum(int(stats.get(k) or 0) for k in ("tool_calls_succeeded", "tool_calls_failed",
                                                            "tool_calls_rejected", "tool_calls_hook_denied")),
        "price": price,
    }


def _unified_snapshot(session_dir: Path, meta: dict, reader: "JournalReader", model_hint: str | None,
                      config: dict) -> dict:
    model = _unified_model(session_dir) or model_hint or DEFAULT_MODEL_ALIAS
    # Prices: Vibe's config and the plugin's model_prices, then the model definition a
    # server-side experiment supplied for this session (saved in its meta.json).
    price = model_prices(config).get(model) or _find_model_price([meta.get("experiments"), meta.get("config")], model)
    tokens_in, cached, tokens_out = reader.tokens()
    return {"session_id": meta.get("session_id") or session_dir.name, "model": model, "tokens_in": tokens_in,
            "cached": cached, "tokens_out": tokens_out, "steps": None, "tool_calls": len(reader.effects),
            "price": price}


def _candidates(root: Path, session_id: str | None, since: float | None) -> list[tuple[Path, str]]:
    unified = root / "unified"
    if session_id:
        found = [(unified / session_id, "unified")] if (unified / session_id).is_dir() else []
        return found + [(m.parent, "legacy") for m in root.glob(f"*_{session_id[:8]}/meta.json")]
    out = []
    for base, kind in ((unified, "unified"), (root, "legacy")):
        if not base.is_dir():
            continue
        for d in base.iterdir():
            try:
                if d.is_dir() and d.name != "unified" and (since is None or d.stat().st_mtime >= since - 1):
                    out.append((d.stat().st_mtime, d, kind))
            except OSError:
                continue
    return [(d, kind) for _m, d, kind in sorted(out, reverse=True)[:50]]


RUN_MARKER = re.compile(rb"\(delegation run ([A-Za-z0-9._-]+)\)")


def run_marker(run_id: str) -> str:
    """Appended to a new session's prompt so the wrapper can find that session among parallel runs'."""
    return f"(delegation run {run_id})"


def _session_dirs(root: Path) -> set[Path]:
    out = set()
    for base in (root / "unified", root):
        try:
            out.update(d for d in base.iterdir() if d.is_dir())
        except OSError:
            continue
    return out


def _run_marker(session_dir: Path) -> str | None:
    """The delegation run id in a session's stored prompt, if Vibe has written it yet."""
    files = [session_dir / "meta.json", session_dir / "messages.jsonl",
             *sorted((session_dir / "journal").glob("*.jsonl"))[:2]]
    for path in files:
        try:
            with path.open("rb") as f:
                match = RUN_MARKER.search(f.read(1_000_000))
        except OSError:
            continue
        if match:
            return match.group(1).decode()
    return None


def read_session_stats(session_id: str | None, cwd: str, since: float | None = None,
                       model_hint: str | None = None) -> dict | None:
    """A session's cumulative usage from Vibe's session storage (legacy or Unified Harness layout).

    With a session id, match it exactly. Without one (e.g. when a limit stopped the
    run and nothing was printed), take the newest session written since `since`
    for the same working directory.
    """
    config = vibe_config()
    root = session_root(config)
    if not root.is_dir():
        return None
    for session_dir, kind in _candidates(root, session_id, since):
        try:
            meta = json.loads((session_dir / "meta.json").read_text())
        except (OSError, ValueError):
            continue
        if session_id and meta.get("session_id") not in (session_id, None):
            continue
        if not session_id and not _same_dir((meta.get("environment") or {}).get("working_directory"), cwd):
            continue
        if kind == "legacy":
            snap = _legacy_snapshot(meta, model_hint, config)
        else:
            reader = JournalReader(session_dir)
            reader.read()
            if reader.usage is None:
                continue
            snap = _unified_snapshot(session_dir, meta, reader, model_hint, config)
        if snap:
            return snap
    return None


def snapshot_cost(snap: dict | None, base: dict | None = None, *, fallback: bool = False) -> tuple[float | None, bool]:
    """(cost of snap minus base, priced_with_fallback). Unknown price -> None, or Mistral Medium's if fallback."""
    if not snap:
        return 0.0, False
    if snap.get("model") == "local":
        return 0.0, False
    base = base or {}
    price, used_fallback = snap.get("price"), False
    if not price or not any(price[:2]):
        if not fallback:
            return None, False
        price, used_fallback = BUILTIN_MODELS[DEFAULT_MODEL_ALIAS], True
    d_in = snap["tokens_in"] - base.get("tokens_in", 0)
    d_cached = snap["cached"] - base.get("cached", 0)
    d_out = snap["tokens_out"] - base.get("tokens_out", 0)
    return price_cost(d_in, d_cached, d_out, price), used_fallback


def stats_cost(stats: dict | None) -> float | None:
    return snapshot_cost(stats)[0]


def usage(after: dict | None, before: dict | None) -> dict | None:
    """This run's usage (after minus before, for resumed sessions)."""
    if not after:
        return None
    before = before or {}
    d_in = after["tokens_in"] - before.get("tokens_in", 0)
    d_cached = after["cached"] - before.get("cached", 0)
    d_out = after["tokens_out"] - before.get("tokens_out", 0)
    cost, _ = snapshot_cost(after, before)
    steps = after.get("steps")
    if steps is not None and before.get("steps") is not None:
        steps -= before["steps"]
    return {
        "cost": cost,
        "estimated": True,
        "model": after.get("model"),
        "tokens_in": d_in, "cached": d_cached, "tokens_out": d_out,
        "tokens": d_in + d_out,
        "effective": effective_tokens(d_in, d_cached, d_out),
        "steps": steps,
        "session_total": snapshot_cost(after)[0] if before else None,
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


# --- live budget tracking -------------------------------------------------------------

class SessionWatcher:
    """Follows a running Vibe session's usage, incrementally.

    Vibe enforces --max-price only with a known model price, and its turn limit
    counts prompts rather than tool calls, so the wrapper watches the session
    itself and stops it at the plugin's caps.
    """

    def __init__(self, cwd: str, since: float, session_id: str | None = None, model_hint: str | None = None,
                 marker: str | None = None):
        self.cwd, self.since, self.session_id, self.model_hint = cwd, since, session_id, model_hint
        self.marker = marker
        self.config = vibe_config()
        self.root = session_root(self.config)
        self.dir: Path | None = None
        self.kind = ""
        self.meta: dict = {}
        self.reader: JournalReader | None = None
        # A new session is told apart from other runs' sessions in the same directory: sessions that
        # existed before this call are skipped, and so are sessions whose prompt names another run.
        self.confirmed = bool(session_id)
        self.exclude = set() if session_id else _session_dirs(self.root)

    @property
    def found_id(self) -> str | None:
        """The session this call ran in, when it was identified for certain (or was the only candidate)."""
        return (self.meta.get("session_id") or self.dir.name) if self.dir is not None else None

    def _locate(self) -> bool:
        if self.dir is not None and self.confirmed:
            return True
        if not self.root.is_dir():
            return False
        if self.session_id:
            for d, kind in _candidates(self.root, self.session_id, None):
                try:
                    meta = json.loads((d / "meta.json").read_text())
                except (OSError, ValueError):
                    continue
                self._use(d, kind, meta)
                return True
            return False
        mine, unmarked = None, []
        for d, kind in _candidates(self.root, None, self.since):
            if d in self.exclude:
                continue
            try:
                meta = json.loads((d / "meta.json").read_text())
            except (OSError, ValueError):
                continue
            if not _same_dir((meta.get("environment") or {}).get("working_directory"), self.cwd):
                continue
            owner = _run_marker(d) if self.marker else None
            if owner == self.marker:
                mine = (d, kind, meta)
                break
            if owner is None:
                unmarked.append((d, kind, meta))
        choice = mine or (unmarked[0] if len(unmarked) == 1 else None)
        self.confirmed = mine is not None
        if choice is None:
            self.dir = None
            return False
        if choice[0] != self.dir:
            self._use(*choice)
        return True

    def _use(self, d: Path, kind: str, meta: dict) -> None:
        self.dir, self.kind, self.meta = d, kind, meta
        self.reader = JournalReader(d) if kind == "unified" else None

    def poll(self) -> dict | None:
        """The session's cumulative usage so far, or None before it shows up."""
        if not self._locate():
            return None
        if self.kind == "unified":
            self.reader.read()
            return _unified_snapshot(self.dir, self.meta, self.reader, self.model_hint, self.config)
        try:
            meta = json.loads((self.dir / "meta.json").read_text())
        except (OSError, ValueError):
            return None
        return _legacy_snapshot(meta, self.model_hint, self.config)
