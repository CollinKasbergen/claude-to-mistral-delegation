"""Equivalent spellings of allowed commands.

Allowing `npm test` should also allow `npm run test`, and `npx vitest` when the
test script is `vitest run`. Only package scripts and well-known check tools are
expanded: a script's body is never trusted beyond naming one of those tools.
"""

from __future__ import annotations

import json
import os

from .guard import PACKAGE_MANAGER_BUILTINS, normalize_command

PACKAGE_MANAGERS = ("npm", "pnpm", "yarn", "bun")
# Tools that only test, lint, type-check or format; `npx <tool>` is allowed when a
# script that is already allowed runs one of them.
CHECK_TOOLS = {"vitest", "jest", "mocha", "ava", "playwright", "tsc", "vue-tsc", "svelte-check", "eslint",
               "prettier", "biome", "stylelint", "astro", "nuxi", "ng"}
PYTHON_TOOLS = {"pytest", "ruff", "mypy", "black", "flake8", "pylint", "pyright", "isort", "tox", "nox"}
NPX_FORMS = ("npx {tool}", "pnpm exec {tool}", "pnpm {tool}", "yarn {tool}", "bunx {tool}", "npm exec {tool}")
PYTHON_FORMS = ("{tool}", "python -m {tool}", "python3 -m {tool}", "uv run {tool}", "uv run python -m {tool}",
                "poetry run {tool}")


def package_scripts(root: str) -> dict[str, str]:
    """Scripts from package.json files at the root and up to two levels down (not node_modules)."""
    scripts: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        depth = os.path.relpath(dirpath, root).count(os.sep) + (dirpath != root)
        dirnames[:] = [d for d in dirnames if d not in ("node_modules", ".git") and not d.startswith(".") and depth < 2]
        if "package.json" in filenames:
            try:
                with open(os.path.join(dirpath, "package.json"), encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            for name, body in (data.get("scripts") or {}).items():
                if isinstance(body, str):
                    scripts.setdefault(name, body)
    return scripts


def _script_name(words: list[str]) -> str | None:
    """The package script of a command already passed through normalize_command (`pm run <script>`)."""
    if len(words) < 2 or words[0] not in PACKAGE_MANAGERS:
        return None
    if words[1] == "run" and len(words) >= 3:
        return words[2]
    if words[0] == "npm" and words[1] in ("start", "stop", "restart"):
        return words[1]
    return None


def _tool_of(words: list[str]) -> str | None:
    if words[:1] in (["npx"], ["bunx"]) and len(words) >= 2:
        return words[1]
    if words[:2] in (["pnpm", "exec"], ["npm", "exec"], ["pnpm", "dlx"], ["yarn", "dlx"]) and len(words) >= 3:
        return words[2]
    return None


def _script_tool(body: str) -> str | None:
    """The check tool a script runs first, if any (skipping env assignments and cross-env)."""
    for word in body.split():
        if "=" in word and not word.startswith("-"):
            continue
        if word in ("cross-env", "dotenv", "npx", "--"):
            continue
        return word if word in CHECK_TOOLS else None
    return None


def _python_tool(words: list[str]) -> str | None:
    if words[:1] and words[0] in PYTHON_TOOLS:
        return words[0]
    if words[:2] in (["python", "-m"], ["python3", "-m"], ["uv", "run"], ["poetry", "run"]) and len(words) >= 3:
        tool = words[2]
        if tool in ("python", "python3") and words[3:4] == ["-m"] and len(words) >= 5:
            tool = words[4]
        return tool if tool in PYTHON_TOOLS else None
    return None


def expand(allowed: list[str], root: str) -> list[str]:
    scripts = package_scripts(root) if any(c.split()[:1] and c.split()[0] in (*PACKAGE_MANAGERS, "npx", "bunx")
                                           for c in allowed) else {}
    out = list(allowed)
    for command in allowed:
        words = normalize_command(command.split())  # `npm --prefix frontend test` -> `npm run test`
        script = _script_name(words)
        tool = _tool_of(words)
        if script:
            out += [f"{pm} run {script}" for pm in PACKAGE_MANAGERS] + [f"{pm} {script}" for pm in ("pnpm", "yarn", "bun")]
            if script == "test":
                out += [f"{pm} test" for pm in PACKAGE_MANAGERS] + ["npm t"]
            tool = _script_tool(scripts.get(script, "")) or tool
        if tool in CHECK_TOOLS:
            out += [form.format(tool=tool) for form in NPX_FORMS]
            out += [f"{pm} run {name}" for name, body in scripts.items() if _script_tool(body) == tool
                    for pm in PACKAGE_MANAGERS]
        if (py := _python_tool(words)):
            out += [form.format(tool=py) for form in PYTHON_FORMS]
    return list(dict.fromkeys(out))
