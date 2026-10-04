#!/usr/bin/env python3
"""Vibe pre_tool hook that keeps a delegated run inside its rules without ending it.

In programmatic mode, Vibe asks for approval for any tool call its permissions
don't cover, and the refused approval cancels the whole session. This hook runs
before that prompt and refuses disallowed calls itself, with a reason that
Mistral gets back as a tool error, so it can work around it:

  * shell commands must be built from allowed command prefixes (no command
    substitution, subshells or file redirection; every part of a pipeline or
    chain is checked);
  * paths must stay inside the project; a path written as if the project were
    the filesystem root (e.g. /src/app.ts) is corrected instead of refused;
  * writes must stay inside the run's --scope;
  * sensitive files (.env, keys) and network tools are refused.

The hook is installed once in $VIBE_HOME/hooks.toml and does nothing unless a
delegation run is active for the session's directory: delegate.py writes a
policy file keyed by that directory and removes it when the run ends. It is
deliberately stricter than Vibe's own permissions, which stay on, so if the hook
is missing, runs fall back to Vibe's behaviour instead of allowing more.

This file has no dependencies outside the standard library: delegate.py copies
it to ~/.mistral-delegate/vibe_guard.py so the hook path survives plugin updates.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import shlex
import sys
import time
from pathlib import Path

HOOK_NAME = "mistral-delegate-guard"
BLOCK_START = "# >>> mistral-delegate guard (managed by the mistral-delegate plugin; inactive outside its runs)"
BLOCK_END = "# <<< mistral-delegate guard"

PATH_KEYS = ("path", "file_path", "filePath", "target_file", "directory", "dir", "cwd")
WRITE_WORDS = ("write", "edit", "replace", "patch", "delete", "remove", "move", "rename", "create", "mkdir")
WRITE_KEYS = ("content", "new_string", "old_string", "changes")
NETWORK_TOOLS = ("web_fetch", "web_search", "webfetch", "websearch")
SENSITIVE = (".env", ".env.*", "*.pem", "*.key", "id_rsa*", "id_ed25519*", ".npmrc", ".netrc")
SEPARATORS = {"&&", "||", ";", "|", "&", "|&", ";;"}
FIND_UNSAFE = {"-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprint0", "-fprintf", "-fls"}


# --- policy files (written by delegate.py, read by the hook) ---------------------

def guard_home() -> Path:
    custom = os.environ.get("MISTRAL_DELEGATE_HOME")
    if custom:
        return Path(custom).expanduser()
    here = Path(__file__).resolve().parent
    return here if here.name == ".mistral-delegate" else Path.home() / ".mistral-delegate"


def policy_path(cwd: str, home: Path | None = None) -> Path:
    key = hashlib.sha1(os.path.realpath(cwd).encode()).hexdigest()[:16]
    return (home or guard_home()) / "guards" / f"{key}.json"


def write_policy(cwd: str, policy: dict, home: Path | None = None) -> Path:
    path = policy_path(cwd, home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(policy))
    tmp.replace(path)
    return path


def remove_policy(cwd: str, run_id: str, home: Path | None = None) -> None:
    path = policy_path(cwd, home)
    try:
        if json.loads(path.read_text()).get("run_id") == run_id:
            path.unlink()
    except (OSError, ValueError):
        pass


def find_policy(cwd: str) -> dict | None:
    """The active policy for cwd or the nearest parent directory that has one."""
    current = os.path.realpath(cwd)
    while True:
        try:
            policy = json.loads(policy_path(current).read_text())
            if time.time() < policy.get("expires", 0):
                return policy
        except (OSError, ValueError):
            pass
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def read_log(path: str | Path) -> list[dict]:
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


# --- installing the hook ----------------------------------------------------------

def hook_block(script: Path, python: str) -> str:
    command = f"{shlex.quote(python)} {shlex.quote(str(script))}"
    return "\n".join([
        BLOCK_START,
        "[[hooks]]",
        f'name = "{HOOK_NAME}"',
        'type = "pre_tool"',
        'match = "*"',
        f"command = {json.dumps(command)}",
        "timeout = 15.0",
        'description = "Keeps mistral-delegate runs inside their rules; refused calls come back to the model as errors."',
        BLOCK_END,
    ])


def install(vibe_home: Path, home: Path, python: str | None = None) -> str | None:
    """Copy this script to home/vibe_guard.py and register it in vibe_home/hooks.toml.

    Returns None on success, or a warning explaining why the guard isn't installed.
    """
    home.mkdir(parents=True, exist_ok=True)
    script = home / "vibe_guard.py"
    source = Path(__file__).read_text(encoding="utf-8")
    try:
        if not script.exists() or script.read_text(encoding="utf-8") != source:
            script.write_text(source, encoding="utf-8")
    except OSError as e:
        return f"could not write {script}: {e}"

    hooks = vibe_home / "hooks.toml"
    try:
        current = hooks.read_text(encoding="utf-8") if hooks.exists() else ""
    except OSError as e:
        return f"could not read {hooks}: {e}"
    block = hook_block(script, python or sys.executable or "python3")
    if BLOCK_START in current and BLOCK_END in current:
        start = current.index(BLOCK_START)
        end = current.index(BLOCK_END) + len(BLOCK_END)
        updated = current[:start] + block + current[end:]
    elif f'"{HOOK_NAME}"' in current:
        return None  # registered by hand; leave it alone
    else:
        updated = (current.rstrip() + "\n\n" if current.strip() else "") + block + "\n"
    if updated == current:
        return None
    try:
        import tomllib
        tomllib.loads(updated)
    except ImportError:
        pass
    except ValueError as e:
        return f"left {hooks} untouched: it doesn't parse as TOML ({e})"
    try:
        hooks.parent.mkdir(parents=True, exist_ok=True)
        tmp = hooks.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(updated, encoding="utf-8")
        tmp.replace(hooks)
    except OSError as e:
        return f"could not write {hooks}: {e}"
    return None


# --- checks -----------------------------------------------------------------------

def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def _resolve(value: str, cwd: str) -> str:
    return os.path.realpath(os.path.join(cwd, os.path.expanduser(value)))


def _matches(rel: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(rel, p) or rel == p.rstrip("/") or (p.endswith("/") and rel.startswith(p))
               for p in patterns)


def _sensitive(path: str) -> bool:
    name = os.path.basename(path)
    return any(fnmatch.fnmatch(name, p) for p in SENSITIVE) and not name.endswith((".example", ".sample", ".template"))


def correct_path(value: str, root: str, write: bool = False) -> str | None:
    """Map a path that points outside the project back into it, if a suffix of it exists there.

    Catches paths written as if the project were the filesystem root (/src/app.ts)
    and paths built from the wrong base, such as the worktrees folder without the
    run's own folder (.mistral-worktrees/<repo>-<hash>/frontend/x.ts).
    """
    parts = [p for p in value.replace("\\", "/").split("/") if p not in ("", ".", "~")]
    for i in range(len(parts)):
        rest = parts[i:]
        if ".." in rest or (i > 0 and len(rest) < 2):
            continue
        candidate = os.path.join(root, *rest)
        if os.path.exists(candidate) or (write and os.path.isdir(os.path.dirname(candidate))):
            return candidate
    return None


SED_SAFE_LONG_FLAGS = {"--quiet", "--silent", "--regexp-extended", "--separate", "--null-data", "--posix",
                       "--debug", "--sandbox"}
SED_SAFE_SHORT = set("nErsz")
_SED_ADDR = r"(?:\d+(?:~\d+)?|\$|/(?:[^/\\]|\\.)*/[IM]*|\\(.)(?:(?!\1)[^\\]|\\.)*\1[IM]*)"
SED_ADDRESS = re.compile(rf"^{_SED_ADDR}(?:\s*,\s*(?:{_SED_ADDR}|[+~]\d+))?\s*!?\s*")
SED_SUBST = re.compile(r"^s(.)(?:(?!\1)[^\\]|\\.)*\1(?:(?!\1)[^\\]|\\.)*\1([gpiImM0-9]*)$")
SED_YANK = re.compile(r"^y(.)(?:(?!\1)[^\\]|\\.)*\1(?:(?!\1)[^\\]|\\.)*\1$")
SED_SAFE_COMMANDS = {"", "p", "P", "=", "l", "q", "Q", "n", "N", "d", "D", "h", "H", "g", "G", "x", "z"}


def _split_sed(script: str) -> list[str]:
    """Split a sed script into commands at ; newlines and braces, skipping over /regex/ and s///, y/// parts."""
    pieces, buf, i, delim, remaining = [], "", 0, "", 0
    while i < len(script):
        c = script[i]
        if remaining:
            buf += c
            if c == "\\" and i + 1 < len(script):
                buf += script[i + 1]
                i += 2
                continue
            if c == delim:
                remaining -= 1
            i += 1
            continue
        if c in ";\n{}":
            pieces.append(buf)
            buf = ""
        elif c == "/":
            buf += c
            delim, remaining = "/", 1
        elif c in "sy" and i + 1 < len(script) and (not buf.strip() or SED_ADDRESS.fullmatch(buf.strip() + " ")):
            buf += c + script[i + 1]
            delim, remaining = script[i + 1], 2
            i += 1
        else:
            buf += c
        i += 1
    pieces.append(buf)
    return pieces


def _sed_script_safe(script: str) -> bool:
    """Whether a sed script only prints: no w/W/r/R/e commands and no s///w or s///e."""
    for piece in _split_sed(script):
        piece = piece.strip()
        match = SED_ADDRESS.match(piece)
        command = piece[match.end():].strip() if match else piece
        if command in SED_SAFE_COMMANDS or re.fullmatch(r"[qQ]\s*\d*", command):
            continue
        if SED_SUBST.match(command) or SED_YANK.match(command):
            continue
        return False
    return True


def _sed_files(words: list[str]) -> list[str] | None:
    """The file arguments of a sed call that only prints (no -i, no writing commands), or None."""
    scripts, files, expect_script = [], [], False
    for word in words[1:]:
        if expect_script:
            scripts.append(word)
            expect_script = False
        elif word in ("-e", "--expression"):
            expect_script = True
        elif word.startswith("--expression="):
            scripts.append(word.split("=", 1)[1])
        elif word.startswith("--"):
            if word not in SED_SAFE_LONG_FLAGS:
                return None
        elif word.startswith("-") and len(word) > 1:
            letters = word[1:]
            if letters.endswith("e") and set(letters[:-1]) <= SED_SAFE_SHORT:
                expect_script = True  # e.g. -ne 'script'
            elif not set(letters) <= SED_SAFE_SHORT:
                return None  # -i, -f, -l, ... (in place, script files)
        elif not scripts:
            scripts.append(word)
        else:
            files.append(word)
    if not scripts or not all(_sed_script_safe(s) for s in scripts):
        return None
    return files


def _shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    return list(lexer)


def check_shell(command: str, policy: dict, cwd: str) -> str | None:
    """Return a refusal reason, or None if the command may run as written."""
    reason, corrections = analyze_shell(command, policy, cwd)
    if reason is None and corrections:
        return "paths need correcting: " + ", ".join(f"{a} -> {b}" for a, b in corrections.items())
    return reason


def analyze_shell(command: str, policy: dict, cwd: str) -> tuple[str | None, dict[str, str]]:
    """(refusal reason or None, {path as written: corrected path inside the project})."""
    corrections: dict[str, str] = {}
    if policy.get("allow_shell"):
        return None, corrections
    allowed = policy.get("allow_commands") or []
    hint = ("Use the read_file and grep tools to inspect files and the edit/write tools to change them. "
            "Commands you may run: " + ", ".join(f"`{c}`" for c in allowed if c not in policy.get("default_commands", []))
            if any(c not in policy.get("default_commands", []) for c in allowed)
            else "Use the read_file and grep tools to inspect files and the edit/write tools to change them.")
    if "`" in command or "$(" in command or "<(" in command or ">(" in command:
        return f"Command substitution isn't allowed in this run. {hint}", corrections
    try:
        tokens = _shell_tokens(command)
    except ValueError:
        return f"Couldn't parse that command. {hint}", corrections

    segments: list[list[str]] = [[]]
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in SEPARATORS:
            segments.append([])
        elif tok in ("(", ")", "((", "))"):
            return f"Subshells and grouping aren't allowed in this run. {hint}", corrections
        elif set(tok) <= set("<>&|") and ("<" in tok or ">" in tok):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if segments[-1] and segments[-1][-1].isdigit():
                segments[-1].pop()  # the fd number in 2>&1
            if tok in (">&", "<&") and target in ("1", "2"):
                i += 2
                continue
            if tok in (">", ">>", "&>", ">&", "&>>") and target == "/dev/null":
                i += 2
                continue
            if tok == "<" and target and _inside(_resolve(target, cwd), policy["root"]):
                i += 2
                continue
            return (f"Redirection (`{tok}`) isn't allowed in this run; write files with the edit/write tools. "
                    f"{hint}"), corrections
        else:
            segments[-1].append(tok)
        i += 1

    for words in segments:
        while words and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[0]):
            words = words[1:]
        if not words:
            continue
        sed_files = _sed_files(words) if words[0] == "sed" else None
        if sed_files is None and not any(words[:len(p.split())] == p.split() for p in allowed):
            return f"`{words[0]}` isn't allowed in this run. {hint}", corrections
        if words[0] == "find" and FIND_UNSAFE & set(words):
            return "`find` with -exec/-delete isn't allowed in this run. Use plain `find` to list files.", corrections
        for word in (sed_files if sed_files is not None else words[1:]):
            looks_like_path = word.startswith(("/", "~")) or ".." in word.split("/") or (
                "/" in word and not word.startswith("-") and not os.path.exists(os.path.join(cwd, word)))
            if not looks_like_path or word == "/dev/null":
                continue
            resolved = _resolve(word, cwd)
            if _inside(resolved, policy["root"]):
                if not os.path.exists(resolved) and (fixed := correct_path(word, policy["root"])):
                    corrections[word] = fixed
                continue
            fixed = correct_path(word, policy["root"])
            if fixed and (word.startswith(("/", "~")) or ".." in word.split("/") or os.path.exists(fixed)):
                corrections[word] = fixed
                continue
            if word.startswith(("/", "~")) or ".." in word.split("/"):
                return (f"`{word}` is outside the project. Use paths relative to the project root "
                        f"({policy['root']})."), corrections
    return None, corrections


def _is_write(tool: str, tool_input: dict) -> bool:
    name = tool.lower()
    return any(w in name for w in WRITE_WORDS) or any(k in tool_input for k in WRITE_KEYS)


def check_tool(tool: str, tool_input: dict, policy: dict, cwd: str) -> tuple[str, str | None, dict | None]:
    """Decide on one tool call: ("allow"|"deny"|"rewrite", reason, new input)."""
    name = tool.lower()
    if any(n in name for n in NETWORK_TOOLS):
        return "deny", "Network tools aren't available in this run; work from the code and the task.", None
    if "ask_user" in name or "question" in name:
        return "deny", ("No one can answer questions during this run. Make a reasonable choice, "
                        "and list it in your final summary."), None

    root = policy["root"]
    new_input = dict(tool_input)
    rewritten = False
    command = tool_input.get("command")
    if isinstance(command, str):
        reason, corrections = analyze_shell(command, policy, tool_input.get("cwd") or cwd)
        if reason:
            return "deny", reason, None
        for wrong, fixed in corrections.items():
            if f'"{wrong}"' in command or f"'{wrong}'" in command:
                command = command.replace(wrong, fixed)
            elif wrong in command:
                command = command.replace(wrong, shlex.quote(fixed))
            else:
                return "deny", (f"`{wrong}` is outside the project; the file is at `{fixed}`. "
                                f"Use paths relative to the project root ({root})."), None
        if corrections:
            new_input["command"] = command
            rewritten = True
    write = _is_write(tool, tool_input)
    for key in PATH_KEYS:
        values = tool_input.get(key)
        if not isinstance(values, str) or not values:
            continue
        resolved = _resolve(values, cwd)
        if _inside(resolved, root) and not os.path.exists(resolved) and not write:
            candidate = correct_path(values, root)
            if candidate and os.path.realpath(candidate) != resolved:
                new_input[key] = candidate
                resolved = os.path.realpath(candidate)
                rewritten = True
        elif not _inside(resolved, root):
            candidate = correct_path(values, root, write)
            if candidate:
                new_input[key] = candidate
                resolved = os.path.realpath(candidate)
                rewritten = True
            else:
                return "deny", (f"`{values}` is outside the project. Use paths relative to the project root "
                                f"({root})."), None
        if _sensitive(resolved):
            return "deny", f"`{values}` may contain secrets and is off limits in this run.", None
        if write:
            if policy.get("mode") == "read":
                return "deny", "This is a read-only run; don't change files.", None
            scope = policy.get("scope") or []
            rel = os.path.relpath(resolved, root)
            if scope and not _matches(rel, scope):
                return "deny", (f"`{rel}` is outside this task's scope. Only change files matching: "
                                + ", ".join(f"`{s}`" for s in scope)
                                + ". If the task needs other changes, explain that in your final summary."), None
    if rewritten:
        return "rewrite", "path corrected to the project", new_input
    return "allow", None, None


def changed_value(old: dict, new: dict | None) -> str | None:
    if not new:
        return None
    return next((str(new[k]) for k in ("command", *PATH_KEYS) if k in new and new.get(k) != old.get(k)), None)


def main() -> None:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return
    cwd = event.get("cwd") or os.getcwd()
    policy = find_policy(cwd)
    if not policy:
        return  # not a delegation run: do nothing
    tool = str(event.get("tool_name") or "")
    tool_input = event.get("tool_input") if isinstance(event.get("tool_input"), dict) else {}
    try:
        action, reason, new_input = check_tool(tool, tool_input, policy, cwd)
    except Exception as e:  # never break a run on a guard bug; Vibe's own permissions still apply
        action, reason, new_input = "allow", f"guard error: {e}", None
    target = tool_input.get("command") or next((tool_input[k] for k in PATH_KEYS if isinstance(tool_input.get(k), str)), "")
    try:
        os.makedirs(os.path.dirname(policy["log"]), exist_ok=True)
        with open(policy["log"], "a", encoding="utf-8") as f:
            f.write(json.dumps({"tool": tool, "target": str(target)[:300], "action": action, "reason": reason,
                                "new": changed_value(tool_input, new_input)}) + "\n")
    except (OSError, KeyError):
        pass
    if action == "deny":
        print(json.dumps({"decision": "deny", "reason": reason}))
    elif action == "rewrite":
        print(json.dumps({"hook_specific_output": {"tool_input": new_input}}))


if __name__ == "__main__":
    main()
