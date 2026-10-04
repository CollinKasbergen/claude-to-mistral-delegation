"""Plan files: one Markdown file that describes several delegated steps.

    # Teams page                              <- title
    verify: npm test                          <- plan settings (optional): checks for the merged result
    kind: feature                             <-   default kind for the steps

    Shared context for every step: conventions, files to follow, test setup, what's out of scope.

    ## step: api - Teams endpoint             <- a step: an id, and optionally a title
    scope: src/api/teams.ts, tests/api/teams.test.ts
    context: src/api/users.ts
    verify: npx vitest run tests/api
    allow: npx vitest run

    What to build in this step.

    ## step: ui - Teams page
    depends: api
    ...

A step's settings are the `key: value` lines right after its heading. scope,
context and depends take comma-separated lists; verify and allow take one command
per line and may repeat. Everything before the first step (after the plan
settings) is shared context that every step gets.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STEP_HEADING = re.compile(r"^##\s+step\s*:?\s*([A-Za-z0-9_-]+)\s*(?:[-–—:]\s*(.*?))?\s*$", re.I)
TITLE = re.compile(r"^#\s+(.+?)\s*$")
SETTING = re.compile(r"^([a-z_]+)\s*:\s*(.*?)\s*$")
LIST_KEYS = {"scope", "context", "depends"}
COMMAND_KEYS = {"verify", "allow"}
STEP_KEYS = LIST_KEYS | COMMAND_KEYS | {"kind"}
PLAN_KEYS = {"verify", "kind"}


class PlanError(ValueError):
    pass


@dataclass
class Step:
    id: str
    title: str = ""
    kind: str = ""
    scope: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)
    verify: list[str] = field(default_factory=list)
    allow: list[str] = field(default_factory=list)
    depends: list[str] = field(default_factory=list)
    text: str = ""

    def summary(self) -> str:
        """One line describing the step, for the other steps and for reports."""
        if self.title:
            return self.title
        first = next((line.strip(" #-*") for line in self.text.splitlines() if line.strip()), "")
        return first[:120] or self.id


@dataclass
class Plan:
    title: str
    shared: str = ""
    verify: list[str] = field(default_factory=list)
    kind: str = ""
    steps: list[Step] = field(default_factory=list)

    def step(self, step_id: str) -> Step:
        return next(s for s in self.steps if s.id == step_id)

    def order(self) -> list[Step]:
        """The steps with every step after the steps it depends on (file order otherwise)."""
        done: list[str] = []
        remaining = list(self.steps)
        while remaining:
            ready = [s for s in remaining if all(d in done for d in s.depends)]
            if not ready:
                raise PlanError("steps depend on each other in a cycle: " + ", ".join(s.id for s in remaining))
            done.append(ready[0].id)
            remaining.remove(ready[0])
        return [self.step(i) for i in done]

    def dependents(self, step_id: str) -> list[str]:
        """Every step that needs step_id, directly or through other steps."""
        out: list[str] = []
        for s in self.order():
            if step_id in s.depends or any(d in out for d in s.depends):
                out.append(s.id)
        return out


def _settings(lines: list[str], allowed: set[str], where: str) -> tuple[dict, list[str]]:
    """Leading `key: value` lines with known keys, and the lines after them."""
    values: dict[str, list[str]] = {}
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    while i < len(lines):
        match = SETTING.match(lines[i].strip())
        if not match or match.group(1) not in allowed:
            break
        key, value = match.groups()
        if key in LIST_KEYS:
            values.setdefault(key, []).extend(v.strip().strip("`") for v in value.split(",") if v.strip())
        elif value:
            if key == "kind" and "kind" in values:
                raise PlanError(f"{where}: kind is set twice")
            values.setdefault(key, []).append(value.strip("`") if key in COMMAND_KEYS else value)
        i += 1
    return values, lines[i:]


def parse(text: str) -> Plan:
    lines = text.replace("\r\n", "\n").split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    title_match = TITLE.match(lines[i]) if i < len(lines) else None
    if not title_match:
        raise PlanError("a plan starts with a `# Title` line")
    plan = Plan(title=title_match.group(1))
    starts = [n for n, line in enumerate(lines) if STEP_HEADING.match(line)]
    if not starts:
        raise PlanError("a plan needs at least one `## step: <id>` section")
    head, rest = _settings(lines[i + 1:starts[0]], PLAN_KEYS, "plan")
    plan.verify = head.get("verify", [])
    plan.kind = (head.get("kind") or [""])[0]
    plan.shared = "\n".join(rest).strip()
    for n, start in enumerate(starts):
        match = STEP_HEADING.match(lines[start])
        body = lines[start + 1:starts[n + 1] if n + 1 < len(starts) else len(lines)]
        values, rest = _settings(body, STEP_KEYS, f"step {match.group(1)}")
        step = Step(id=match.group(1), title=(match.group(2) or "").strip(), kind=(values.get("kind") or [""])[0],
                    scope=values.get("scope", []), context=values.get("context", []),
                    verify=values.get("verify", []), allow=values.get("allow", []),
                    depends=values.get("depends", []), text="\n".join(rest).strip())
        plan.steps.append(step)
    _validate(plan)
    return plan


def _validate(plan: Plan) -> None:
    problems = []
    ids = [s.id for s in plan.steps]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        problems.append(f"step id {dup!r} is used twice")
    for s in plan.steps:
        if not s.text:
            problems.append(f"step {s.id} has no instructions")
        for dep in s.depends:
            if dep not in ids:
                problems.append(f"step {s.id} depends on {dep!r}, which isn't a step in this plan")
            elif dep == s.id:
                problems.append(f"step {s.id} depends on itself")
    if problems:
        raise PlanError("; ".join(problems))
    plan.order()  # raises on a cycle


def select(plan: Plan, wanted: list[str]) -> list[Step]:
    """The steps to run: all, or the named ones (which must include what they depend on)."""
    if not wanted:
        return plan.order()
    unknown = [w for w in wanted if w not in {s.id for s in plan.steps}]
    if unknown:
        raise PlanError("no such step: " + ", ".join(unknown))
    missing = sorted({d for w in wanted for d in plan.step(w).depends if d not in wanted})
    if missing:
        raise PlanError("the chosen steps depend on steps you left out: " + ", ".join(missing)
                        + " (to finish a plan that already ran, use --integrate <plan id>: it runs the steps "
                          "that didn't run)")
    return [s for s in plan.order() if s.id in wanted]


def step_spec(plan: Plan, step: Step) -> str:
    """What one step's Mistral run gets: the shared context, its own step, and the others in one line each."""
    parts = [f"# Plan: {plan.title}",
             "You are doing one step of a larger plan. The other steps are done separately, in parallel or "
             "after you: don't do their work, and don't change what they own unless your step says so."]
    if plan.shared:
        parts.append("## Shared context (applies to every step)\n\n" + plan.shared)
    parts.append(f"## Your step: {step.id}" + (f" - {step.title}" if step.title else "") + "\n\n" + step.text)
    if step.depends:
        parts.append("## Already done in the code you start from\n\n"
                     + "\n".join(f"- {d}: {plan.step(d).summary()}" for d in step.depends))
    others = [s for s in plan.steps if s.id != step.id and s.id not in step.depends]
    if others:
        parts.append("## Other steps of this plan (for context only; don't implement them)\n\n"
                     + "\n".join(f"- {s.id}: {s.summary()}" for s in others))
    return "\n\n".join(parts) + "\n"
