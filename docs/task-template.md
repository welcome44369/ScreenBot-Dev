# ScreenBot Development Task

## Objective

Describe exactly one independently verifiable code change.

Do not describe an entire feature containing multiple implementation stages.
---

## Background

Explain the current problem and why this task is needed.

---

## Required Reading

Use the Read tool to read:

- ./docs/project-handoff.md
- ./docs/architecture.md
- ./docs/ai-workflow.md

---

## Allowed Files

-

-

---

## Forbidden Files

-

-

---

## Requirements

1.

2.

3.

---

## Constraints

Implement only the requested functionality.

Do not redesign the UI.

Do not add extra features.

Do not refactor unrelated modules.

Do not change public behavior unless explicitly requested.

Keep modifications minimal.

Modify no more than 2 source files unless explicitly approved.

Do not continue into the next implementation stage after completing the current task.
---

## Validation

Run:

python -m compileall app

---

## Expected Report

PASS / FAIL

Modified files

Summary of changes

Validation results

Known limitations

## Task Size Limit

Each task must complete only one independently verifiable change.

Default limits:

- Modify no more than 2 source files.
- Do not combine investigation, architecture design, implementation, and integration into one task.
- If the feature requires more than 2 files, split it into multiple tasks.
- Each task must have one clear completion condition.
- Prefer tasks that can be completed and validated within one agent session.

Large features must be divided into stages such as:

1. Inspect current implementation.
2. Add one isolated method.
3. Connect one caller.
4. Add state preservation.
5. Run integration validation.