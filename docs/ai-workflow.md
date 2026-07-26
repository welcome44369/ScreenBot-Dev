# ScreenBot AI Workflow

## Purpose

This document defines the standard workflow for AI-assisted development in the ScreenBot project.

The AI must follow this process for every implementation task.

---

## Step 1: Load Project Context

Use the Read tool to read:

- ./docs/project-handoff.md
- ./docs/architecture.md
- ./docs/ai-workflow.md

Do not guess file contents.

---

## Step 2: Inspect the Current Implementation

Read only the files directly related to the requested task.

Do not scan unrelated files unless necessary.

Identify:

- Current behavior
- Existing data flow
- Target modification points
- Possible side effects

---

## Step 3: Confirm Scope

Implement exactly one independent feature.

Do not add unrelated functionality.

Do not redesign the UI.

Do not refactor unrelated modules.

Do not create new files unless explicitly requested.

Before editing, confirm that the task:

- contains only one implementation goal;
- modifies no more than 2 source files;
- has one clear validation condition.

If the task is larger than these limits:

- do not attempt the full task;
- report how it should be divided;
- wait for a smaller task.

---

## Step 4: Modify Code

Use workspace-relative paths.

Correct:

./app/application.py

Incorrect:

/app/application.py

Before editing:

Read the target file.

After editing:

Read the modified section again.

Keep edits minimal.

Do not rewrite an entire file when a localized edit is sufficient.

---

## Step 5: Recover From Tool Failure

If a tool operation fails:

1. Read the target file again.
2. Verify the exact current content.
3. Retry the operation once.
4. Ask the user only if the retry also fails.

Do not stop after the first failure.

---

## Step 6: Validate

Run:

python -m compileall app

If validation fails:

- Read the error output
- Fix the problem
- Run validation again

Do not report PASS while compilation errors remain.

---

## Step 7: Final Report

Always return:

PASS / FAIL

Modified files

Summary of changes

Validation results

Known limitations

Do not claim success unless the requested work and validation are complete.

## Processing Time Safety Rule

If no edit, terminal command, or meaningful progress occurs after repeated analysis:

1. Stop further planning.
2. Report the blocking point.
3. List the files successfully inspected.
4. Recommend the smallest next task.

Do not remain in indefinite processing.