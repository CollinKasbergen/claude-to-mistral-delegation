---
description: Delegate a task to Mistral's Vibe CLI, or show delegation status and stats
argument-hint: "[read|write] <task> | status | stats | config"
---

Arguments: $ARGUMENTS

- If the arguments are `status`, `stats` or `config`, run the delegate-to-mistral wrapper with `--status`, `--stats` or `--show-config` and show the output.
- If the task has several steps, write one plan file and run it with `--plan` (see the skill), then review the plan's report and recommend adopting or discarding it.
- Otherwise delegate this task using the `delegate-to-mistral` skill. If the first word is `read` or `write`, use it as the mode and treat the rest as the task. If not, choose: `read` unless the task asks for files to be created or changed. Write a spec, pass the project's checks with `--verify`, set `--kind`, run it, review the result, and report back with the run id, cost and your adopt/discard recommendation.
