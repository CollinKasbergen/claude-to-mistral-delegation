#!/usr/bin/env python3
"""SessionStart hook: remind Claude that Mistral delegation is available, with the
active policy, the wrapper's path, the track record and runs awaiting review.

Prints Claude Code's hook JSON with `additionalContext`. Never fails the session:
any error results in no output.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = PLUGIN_ROOT / "skills" / "delegate-to-mistral" / "scripts"
sys.path.insert(0, str(SCRIPTS))


def build_context(cwd: str) -> str:
    from mdelegate import config, gitops, ledger

    wrapper = SCRIPTS / "delegate.py"
    if shutil.which("vibe") is None:
        return ("mistral-delegate plugin: the Vibe CLI is not installed, so delegating to Mistral is "
                "unavailable. If the user asks for it, tell them to run `uv tool install mistral-vibe` "
                "and `vibe --setup`.")

    top = gitops.toplevel(cwd) if cwd else ""
    settings = config.load(top or cwd or None)
    policy = settings["policy"]
    lines = [
        "mistral-delegate plugin: you can hand implementation steps to Mistral (Vibe CLI) through the "
        "delegate-to-mistral skill or the mistral-worker subagent. Mistral runs cost cents to about a "
        "dollar and run alongside you, so your time goes to design and review.",
        f"Delegation policy: {policy}. {config.POLICY_GUIDANCE[policy]}",
        "Habit: after planning a change, mark each step 'mine' or 'Mistral's'. Start Mistral's steps "
        "in the background first (always with --verify when the project has tests or a type check), "
        "then work on yours, then review and --adopt.",
        f"Wrapper: python3 {wrapper}  (pass this path to mistral-worker subagents)",
    ]
    if settings["verify"] or settings["allow_commands"]:
        lines.append(f"Configured checks: {', '.join(settings['verify']) or '(none)'}; "
                     f"commands Mistral may run: {', '.join(settings['allow_commands']) or '(none)'}.")
    runs = ledger.load_runs()
    record = ledger.compact_stats(runs)
    if record:
        lines.append(f"Track record (90 days): {record}.")
    pending = ledger.pending_review(runs)
    if pending:
        lines.append("Awaiting --adopt or --discard: " + ", ".join(f"{r['id']} ({r.get('kind')})" for r in pending[:5]) + ".")
    return "\n".join(lines)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        data = {}
    try:
        context = build_context(data.get("cwd") or "")
    except Exception:
        return
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": context}}))


if __name__ == "__main__":
    main()
