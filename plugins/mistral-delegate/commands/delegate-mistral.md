---
description: Delegate a task to Mistral's Vibe CLI
argument-hint: "[read|write] <task description>"
---

Delegate this task to Mistral Vibe using the `delegate-to-mistral` skill:

$ARGUMENTS

If the first word is `read` or `write`, use it as the mode and treat the rest as the task. Otherwise choose the mode yourself: `read` unless the task asks for files to be created or changed. Turn the task into a self-contained prompt for Vibe, run it, review the result, and report back.
