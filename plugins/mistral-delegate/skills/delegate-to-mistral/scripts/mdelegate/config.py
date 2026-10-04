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

CAPS = {
    "conservative": {"read": {"max_turns": 10, "max_price": 0.15}, "write": {"max_turns": 20, "max_price": 0.50}},
    "balanced": {"read": {"max_turns": 15, "max_price": 0.25}, "write": {"max_turns": 30, "max_price": 1.00}},
    "aggressive": {"read": {"max_turns": 20, "max_price": 0.50}, "write": {"max_turns": 50, "max_price": 2.50}},
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

KEYS = ("policy", "model", "verify", "allow_commands", "fix_attempts", "max_parallel", "deps_mode", "baseline",
        "scope", "vibe_args", "worktrees_dir")

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
                settings[mode].update({k: v for k, v in data[mode].items() if k in ("max_turns", "max_price", "max_tokens")})

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
    settings["verify"] = _as_list(settings["verify"])
    settings["allow_commands"] = _as_list(settings["allow_commands"])
    settings["scope"] = _as_list(settings["scope"])
    settings["vibe_args"] = _as_list(settings["vibe_args"])
    settings["model"] = settings["model"] or None
    if settings["deps_mode"] not in DEPS_MODES:
        settings["warnings"].append(f"unknown deps_mode {settings['deps_mode']!r}, using 'hardlink'")
        settings["deps_mode"] = "hardlink"
    settings["baseline"] = bool(settings["baseline"])
    try:
        settings["fix_attempts"] = max(0, int(settings["fix_attempts"]))
        settings["max_parallel"] = max(1, int(settings["max_parallel"]))
    except (TypeError, ValueError):
        settings["warnings"].append("fix_attempts and max_parallel must be integers; using defaults")
        settings["fix_attempts"], settings["max_parallel"] = 1, 3
    return settings


def caps(settings: dict, mode: str) -> dict:
    """Turn/price/token caps for a mode: policy defaults, then config, then env."""
    result = dict(CAPS[settings["policy"]][mode])
    result["max_tokens"] = None
    result.update(settings.get(mode, {}))
    env = os.environ
    for key, var, cast in (("max_turns", "MISTRAL_DELEGATE_MAX_TURNS", int),
                           ("max_price", "MISTRAL_DELEGATE_MAX_PRICE", float),
                           ("max_tokens", "MISTRAL_DELEGATE_MAX_TOKENS", int)):
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
        f"verify: {', '.join(settings['verify']) or '(none)'}",
        f"allow_commands: {', '.join(settings['allow_commands']) or '(none)'}",
        f"fix_attempts: {settings['fix_attempts']}",
        f"max_parallel: {settings['max_parallel']}",
        f"deps_mode: {settings['deps_mode']}",
        f"baseline: {'on' if settings['baseline'] else 'off'} (run checks on the untouched worktree first)",
        f"scope: {', '.join(settings['scope']) or '(set per task with --scope)'}",
        f"vibe_args: {' '.join(settings['vibe_args']) or '(none)'}",
        f"worktrees_dir: {settings['worktrees_dir'] or '(automatic: ~/.mistral-delegate/worktrees, or <repo parent>/.mistral-worktrees when the repo is on another disk)'}",
    ]
    for mode in ("read", "write"):
        c = caps(settings, mode)
        lines.append(f"{mode}_caps: max_turns={c['max_turns']} max_price=${c['max_price']:.2f}"
                     + (f" max_tokens={c['max_tokens']}" if c.get("max_tokens") else ""))
    for key, source in settings["sources"].items():
        lines.append(f"source of {key}: {source}")
    for warning in settings["warnings"]:
        lines.append(f"warning: {warning}")
    return "\n".join(lines)
