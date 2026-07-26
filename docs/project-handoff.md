# ScreenBot Project Handoff

## Project Overview

Project Name

ScreenBot

---

## Project Goal

ScreenBot is a Windows desktop automation application built with Python.

Its purpose is to automate repetitive desktop operations by recording user actions, recognizing screen content, and replaying automation scripts.

The project emphasizes:

- Modular architecture
- Maintainability
- Stable runtime
- Incremental feature development

This project is intended to grow into a reusable desktop automation platform instead of a single-purpose script recorder.

---

# Current Development Status

## Completed

- Application startup
- Floating Widget
- Recorder
- Script Store
- Window Tracker
- Runtime State
- JSON Script Persistence

---

## Current Phase

Workflow Runtime: loop policies and global stop conditions.

The workflow runtime supports the following optional `loop` modes while preserving
legacy single-run workflow JSON files:

- `manual_stop`
- `max_cycles`
- `stop_trigger` (text/OCR)

Cycle counters increment only at a completed cycle boundary. A manual stop ends
immediately; a matched global stop condition observed during a macro is recorded
as pending and ends the workflow after that macro finishes, before another step
starts. Runtime snapshots and diagnostics expose loop counters, finish reasons,
and stop-trigger confirmation state.

Environment validation (2026-07-26): Python 3.11.9 was installed and a fresh
`.venv` was created; the previous broken environment is retained as
`.venv-broken`. Dependencies from `requirements.txt`, compile checks, module
imports, and fake-runtime loop regression checks all pass. Tesseract 5.4.0 is
installed at `C:\Program Files\Tesseract-OCR\tesseract.exe` and is resolved by
the application at startup.

---

## Planned Features

High Priority

- Script List Synchronization
- Recorder UX Improvements

Medium Priority

- Multiple Script Selection
- Parallel Script Execution

Low Priority

- Script Scheduler
- Batch Execution
- Script Categories

Future

- Plugin System
- OCR Integration
- Advanced Image Matching

---

# Technology Stack

Language

Python 3.x

GUI

PySide6

Libraries

- OpenCV
- PyAutoGUI
- pynput

Script Format

JSON

---

# Project Structure

main.py

Application entry point.

Responsible only for startup.

---

app/application.py

Application coordinator.

Responsible for:

- module initialization
- signal wiring
- runtime lifecycle
- global coordination

---

app/floating_widget.py

Floating control panel.

Responsible for:

- Start
- Stop
- Recording
- Script Selection
- Runtime Status

UI redesign is prohibited unless explicitly requested.

---

app/recorder.py

Recorder module.

Responsible for:

- keyboard recording
- mouse recording
- screenshots
- script serialization

---

app/script_store.py

Persistence layer.

Responsible for:

- loading scripts
- saving scripts
- script metadata

This is the only module responsible for script storage.

---

app/window_tracker.py

Tracks target application windows.

Responsible for:

- active window detection
- window synchronization
- coordinate updates

---

app/state.py

Defines runtime state.

State transitions should not be modified unless explicitly requested.

---

scripts/

Stores automation scripts.

Users may manually add, remove or edit JSON files.

The application should synchronize with this folder whenever required.

---

# Development Principles

Implement only requested functionality.

Prefer minimal changes.

Avoid unrelated modifications.

Avoid unnecessary refactoring.

Preserve backward compatibility.

Keep architecture modular.

Never redesign the UI unless explicitly requested.

Never modify recorder or player logic unless the task specifically requires it.

---

# AI Development Rules

Before implementing any feature:

1.

Read

- docs/project-handoff.md

2.

Read

- docs/architecture.md

3.

Read

- docs/ai-workflow.md

4.

Inspect only the files related to the requested task.

5.

Implement one independent feature.

6.

Run validation.

7.

Return completion report.

---

# Validation

Always execute:

python -m compileall app

Compilation errors must be resolved before reporting success.

---

# Completion Report

Always provide

PASS / FAIL

Modified files

Summary of changes

Validation results

Known limitations
