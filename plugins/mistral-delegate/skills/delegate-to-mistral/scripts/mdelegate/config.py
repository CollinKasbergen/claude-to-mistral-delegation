"""Effective settings for a delegation.

Precedence, lowest first: built-in defaults, ~/.mistral-delegate/config.toml,
<repo>/.mistral-delegate.toml, environment variables, command-line flags (applied
by delegate.py).
"""

from __future__ import annotations

import os
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11: config files are skipped.
    tomllib = None

POLICIES = ("conservative", "balanced", "aggressive")

# Caps per run. token_budget (effective tokens: fresh input + 0.1 x cached + 5 x output, by default) and
# max_tool_calls are enforced by the wrapper while Vibe runs. max_price is an optional money cap on top
# (unset by default); max_turns is passed to Vibe. A balanced write budget of 1M effective tokens is
# about $1.50 at Mistral Medium's list prices.
CAPS = {
    "conservative": {"read": {"max_turns": 10, "token_budget": 150_000, "max_tool_calls": 25},
                     "write": {"max_turns": 20, "token_budget": 400_000, "max_tool_calls": 50}},
    "balanced": {"read": {"max_turns": 15, "token_budget": 300_000, "max_tool_calls": 40},
                 "write": {"max_turns": 30, "token_budget": 1_000_000, "max_tool_calls": 80}},
    "aggressive": {"read": {"max_turns": 20, "token_budget": 600_000, "max_tool_calls": 60},
                   "write": {"max_turns": 50, "token_budget": 2_500_000, "max_tool_calls": 150}},
}

POLICY_GUIDANCE = {
    "conservative": (
        "Delegate only tests for existing code, docs, boilerplate and read-only searches. "
        "Keep all feature work yourself."
    ),
    "balanced": (
        "Delegate any step you can describe in a short spec and check automatically "
        "(tests, type check, lint, or a small diff): features that follow existing patterns, "
        "endpoints, refactors, multi-file migrations, tests, fixtures, docs. Keep decisions, "
        "visual work, and anything whose spec would be longer than the code."
    ),
    "aggressive": (
        "Default to delegating every implementation step that has a spec and an automatic "
        "check, and run independent steps in parallel. Keep only design decisions, visual "
        "judgement and cross-cutting changes."
    ),
}

DEFAULT_WEIGHTS = {"input": 1.0, "cached": 0.1, "output": 5.0}

KEYS = ("policy", "model", "verify", "allow_commands", "fix_attempts", "max_parallel", "deps_mode", "baseline",
        "scope", "vibe_args", "worktrees_dir", "model_prices", "continue_attempts", "token_weights",
        "monthly_credit", "currency", "credit_reset_day", "min_savings", "autofix", "fix_after_cap",
        "claude_relative_effort", "test_strength")

DEPS_MODES = ("hardlink", "copy", "symlink", "none")


def home() -> Path:
    custom = os.environ.get("MISTRAL_DELEGATE_HOME")
    return Path(custom).expanduser() if custom else Path.home() / ".mistral-delegate"


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(v) for v in value if str(v).strip()]


def _as_checks(value, warnings: list[str]) -> list[dict]:
    """verify/autofix entries: "cmd" or {cmd = "...", paths = ["frontend/", ...]} (run only when those paths are involved)."""
    checks = []
    for item in value if isinstance(value, list) else [value]:
        if isinstance(item, str) and item.strip():
            checks.append({"cmd": item, "paths": []})
        elif isinstance(item, dict) and isinstance(item.get("cmd"), str) and item["cmd"].strip():
            checks.append({"cmd": item["cmd"], "paths": _as_list(item.get("paths"))})
        elif item:
            warnings.append(f"ignored verify entry {item!r}: use a string or {{cmd = ..., paths = [...]}}")
    return checks


def check_label(check: dict) -> str:
    return check["cmd"] + (f" (when {', '.join(check['paths'])})" if check["paths"] else "")


def _load_toml(path: Path, warnings: list[str]) -> dict:
    if not path.is_file():
        return {}
    if tomllib is None:
        warnings.append(f"ignored {path}: needs Python 3.11+ to read TOML")
        return {}
    try:
        with path.open("rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as e:
        warnings.append(f"ignored {path}: {e}")
        return {}


def load(repo_root: str | None) -> dict:
    settings: dict = {
        "policy": "balanced",
        "model": None,
        "verify": [],
        "allow_commands": [],
        "fix_attempts": 1,
        "max_parallel": 3,
        "deps_mode": "hardlink",
        "baseline": True,
        "scope": [],
        "vibe_args": [],
        "worktrees_dir": None,
        "model_prices": {},
        "continue_attempts": 1,
        "token_weights": {},
        "monthly_credit": None,
        "currency": "$",
        "credit_reset_day": 1,
        "min_savings": None,
        "autofix": [],
        "fix_after_cap": True,
        # How much of Mistral's work Claude would need to do the same task itself (an assumption).
        "claude_relative_effort": 0.5,
        # Run Mistral's new tests against the original code: they should fail there.
        "test_strength": True,
        "read": {},
        "write": {},
        "sources": {},
        "warnings": [],
    }
    files = [("user", home() / "config.toml")]
    if repo_root:
        files.append(("project", Path(repo_root) / ".mistral-delegate.toml"))

    for label, path in files:
        data = _load_toml(path, settings["warnings"])
        for key in KEYS:
            if key in data:
                settings[key] = data[key]
                settings["sources"][key] = f"{label}: {path}"
        for mode in ("read", "write"):
            if isinstance(data.get(mode), dict):
                settings[mode].update({k: v for k, v in data[mode].items() if k in ("max_turns", "max_price", "max_tokens", "max_tool_calls", "token_budget")})

    env = os.environ
    if env.get("MISTRAL_DELEGATE_POLICY"):
        settings["policy"] = env["MISTRAL_DELEGATE_POLICY"]
        settings["sources"]["policy"] = "env: MISTRAL_DELEGATE_POLICY"
    if env.get("MISTRAL_DELEGATE_WORKTREES"):
        settings["worktrees_dir"] = env["MISTRAL_DELEGATE_WORKTREES"]
        settings["sources"]["worktrees_dir"] = "env: MISTRAL_DELEGATE_WORKTREES"
    if env.get("MISTRAL_DELEGATE_MODEL"):
        settings["model"] = env["MISTRAL_DELEGATE_MODEL"]
        settings["sources"]["model"] = "env: MISTRAL_DELEGATE_MODEL"

    if settings["policy"] not in POLICIES:
        settings["warnings"].append(f"unknown policy {settings['policy']!r}, using 'balanced'")
        settings["policy"] = "balanced"
    settings["verify"] = _as_checks(settings["verify"], settings["warnings"])
    settings["allow_commands"] = _as_list(settings["allow_commands"])
    settings["scope"] = _as_list(settings["scope"])
    settings["vibe_args"] = _as_list(settings["vibe_args"])
    prices = {}
    for alias, value in (settings["model_prices"] or {}).items() if isinstance(settings["model_prices"], dict) else []:
        try:
            if isinstance(value, dict):
                cached = value.get("cached")
                prices[alias] = (float(value["input"]), float(value["output"]), None if cached is None else float(cached))
            else:
                prices[alias] = (float(value[0]), float(value[1]), float(value[2]) if len(value) > 2 else None)
        except (KeyError, IndexError, TypeError, ValueError):
            settings["warnings"].append(f"ignored model_prices.{alias}: use [input, output] or [input, output, cached] per million tokens")
    settings["model_prices"] = prices
    weights = {}
    for key, value in (settings["token_weights"] or {}).items() if isinstance(settings["token_weights"], dict) else []:
        if key in ("input", "cached", "output"):
            try:
                weights[key] = float(value)
            except (TypeError, ValueError):
                settings["warnings"].append(f"ignored token_weights.{key}: use a number")
    settings["token_weights"] = weights
    settings["autofix"] = _as_checks(settings["autofix"], settings["warnings"])
    settings["fix_after_cap"] = bool(settings["fix_after_cap"])
    settings["test_strength"] = bool(settings["test_strength"])
    try:
        settings["monthly_credit"] = float(settings["monthly_credit"]) if settings["monthly_credit"] else None
        settings["min_savings"] = float(settings["min_savings"]) if settings["min_savings"] else None
        settings["credit_reset_day"] = min(28, max(1, int(settings["credit_reset_day"])))
        settings["claude_relative_effort"] = max(0.0, float(settings["claude_relative_effort"]))
    except (TypeError, ValueError):
        settings["warnings"].append("monthly_credit, min_savings and credit_reset_day must be numbers; ignored")
        settings["monthly_credit"], settings["min_savings"], settings["credit_reset_day"] = None, None, 1
    settings["currency"] = str(settings["currency"] or "$")
    settings["model"] = settings["model"] or None
    if settings["deps_mode"] not in DEPS_MODES:
        settings["warnings"].append(f"unknown deps_mode {settings['deps_mode']!r}, using 'hardlink'")
        settings["deps_mode"] = "hardlink"
    settings["baseline"] = bool(settings["baseline"])
    try:
        settings["fix_attempts"] = max(0, int(settings["fix_attempts"]))
        settings["continue_attempts"] = max(0, min(1, int(settings["continue_attempts"])))
        settings["max_parallel"] = max(1, int(settings["max_parallel"]))
    except (TypeError, ValueError):
        settings["warnings"].append("fix_attempts and max_parallel must be integers; using defaults")
        settings["fix_attempts"], settings["max_parallel"] = 1, 3
    return settings


def caps(settings: dict, mode: str) -> dict:
    """Turn/price/token caps for a mode: policy defaults, then config, then env."""
    result = dict(CAPS[settings["policy"]][mode])
    result["max_tokens"] = None
    result["max_price"] = None
    result.update(settings.get(mode, {}))
    env = os.environ
    for key, var, cast in (("max_turns", "MISTRAL_DELEGATE_MAX_TURNS", int),
                           ("max_price", "MISTRAL_DELEGATE_MAX_PRICE", float),
                           ("max_tokens", "MISTRAL_DELEGATE_MAX_TOKENS", int),
                           ("max_tool_calls", "MISTRAL_DELEGATE_MAX_TOOL_CALLS", int),
                           ("token_budget", "MISTRAL_DELEGATE_TOKEN_BUDGET", int)):
        try:
            if env.get(var):
                result[key] = cast(env[var])
        except ValueError:
            pass
    return result


def describe(settings: dict) -> str:
    lines = [
        f"policy: {settings['policy']}  -> {POLICY_GUIDANCE[settings['policy']]}",
        f"model: {settings['model'] or '(Vibe default)'}",
        f"verify: {'; '.join(check_label(c) for c in settings['verify']) or '(none)'}",
        f"allow_commands: {', '.join(settings['allow_commands']) or '(none)'}",
        f"fix_attempts: {settings['fix_attempts']}",
        f"max_parallel: {settings['max_parallel']}",
        f"deps_mode: {settings['deps_mode']}",
        f"baseline: {'on' if settings['baseline'] else 'off'} (run checks on the untouched worktree first)",
        f"scope: {', '.join(settings['scope']) or '(set per task with --scope)'}",
        f"vibe_args: {' '.join(settings['vibe_args']) or '(none)'}",
        f"model_prices: {', '.join(f'{a} = {list(p)} per M tokens' for a, p in settings['model_prices'].items()) or '(none)'}",
        f"token_weights: {dict(DEFAULT_WEIGHTS, **settings['token_weights'])} (effective tokens = fresh input, cached and output tokens times these)",
        f"monthly_credit: " + (f"{settings['currency']}{settings['monthly_credit']:.2f}, resets on day {settings['credit_reset_day']}"
                               if settings['monthly_credit'] else "(not set)"),
        f"min_savings: {settings['min_savings'] or '(not set)'}",
        f"autofix: {'; '.join(check_label(c) for c in settings['autofix']) or '(none)'}",
        f"worktrees_dir: {settings['worktrees_dir'] or '(automatic: ~/.mistral-delegate/worktrees, or <repo parent>/.mistral-worktrees when the repo is on another disk)'}",
    ]
    for mode in ("read", "write"):
        c = caps(settings, mode)
        lines.append(f"{mode}_caps: token_budget={c['token_budget']:,} effective tokens, "
                     f"max_tool_calls={c['max_tool_calls']}, max_turns={c['max_turns']}"
                     + (f", max_price={settings['currency']}{c['max_price']:.2f}" if c.get("max_price") else "")
                     + (f" max_tokens={c['max_tokens']}" if c.get("max_tokens") else ""))
    for key, source in settings["sources"].items():
        lines.append(f"source of {key}: {source}")
    for warning in settings["warnings"]:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)
