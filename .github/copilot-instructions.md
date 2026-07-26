# ScreenBot Workspace Instructions

You are working on the ScreenBot project.

This is a Python desktop automation application.

## General Rules

Implement only what is requested.

Do not redesign the UI.

Do not add extra features.

Do not refactor unrelated code.

Ask before making assumptions.

## File Operations

Always use workspace relative paths.

Correct:

./app/application.py

./app/floating_widget.py

Never use:

/app/application.py

Modify existing files only.

Do not create new files unless explicitly requested.

Never rewrite an entire file.

Only edit the necessary section.

## Workflow

Read before Edit.

Read again after Edit.

If Edit fails:

Read again and retry.

Do not stop after the first failure.

## Validation

Run:

python -m compileall app

before finishing.

## Final Report

Always report:

PASS / FAIL

Modified files

Summary

Validation result

## Project Documentation

Before implementing a task, use the Read tool to read:

- ./docs/project-handoff.md
- ./docs/architecture.md
- ./docs/ai-workflow.md

Use:

- ./docs/task-template.md

as the preferred task structure.

Do not update:

- ./docs/changelog.md

unless the current task explicitly requests it.