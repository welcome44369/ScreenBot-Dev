# ScreenBot Architecture

# Overview

ScreenBot follows a modular architecture.

Each module has a single responsibility.

Business logic should remain separated from the user interface.

The Application module acts as the central coordinator.

---

# High-Level Architecture

```
                         main.py
                             │
                             ▼
                     application.py
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
 FloatingWidget         Recorder         WindowTracker
        │                    │
        │                    ▼
        │              Script Store
        │
        ▼
     App State
```

---

# Runtime Flow

Application Startup

```
main.py

↓

Application

↓

Initialize Modules

↓

Connect Signals

↓

Show Floating Widget

↓

Ready
```

---

Recording Flow

```
User

↓

Floating Widget

↓

Recorder

↓

Script Store

↓

scripts/*.json
```

---

Playback Flow

```
User

↓

Floating Widget

↓

Application

↓

Player

↓

Execute Script
```

---

Script Loading Flow

```
Application

↓

Script Store

↓

scripts/*.json

↓

Floating Widget

↓

Script Selection
```

---

Window Tracking Flow

```
Application

↓

Window Tracker

↓

Target Window

↓

Coordinate Updates
```

---

# Module Responsibilities

## main.py

Application entry point.

Responsibilities

- Create QApplication
- Launch Application
- Exit safely

Must remain lightweight.

---

## application.py

Application coordinator.

Responsibilities

- Initialize modules
- Connect modules
- Dispatch events
- Synchronize runtime state

Should contain orchestration only.

Avoid business logic whenever possible.

---

## floating_widget.py

Presentation Layer.

Responsibilities

- User interaction
- Buttons
- Status display
- Script selection

Should never contain storage logic.

Should never directly manipulate JSON files.

---

## recorder.py

Recording Layer.

Responsibilities

- Keyboard capture
- Mouse capture
- Screenshot capture
- Script serialization

Should not communicate directly with UI.

---

## script_store.py

Persistence Layer.

Responsibilities

- Load scripts
- Save scripts
- Enumerate scripts
- Validate script metadata

This is the only module responsible for script storage.

---

## window_tracker.py

Window Service.

Responsibilities

- Detect target windows
- Track position
- Monitor active window

Should not modify application state directly.

---

## state.py

Application State.

Responsibilities

- Runtime state
- State transitions

State should be modified through Application only.

---

# Dependency Rules

Allowed

```
FloatingWidget

↓

Application

↓

Recorder
```

Allowed

```
Application

↓

Script Store
```

Allowed

```
Application

↓

Window Tracker
```

Not Allowed

```
FloatingWidget

↓

Script Store
```

Not Allowed

```
FloatingWidget

↓

Recorder
```

Not Allowed

```
Recorder

↓

FloatingWidget
```

Not Allowed

```
Script Store

↓

FloatingWidget
```

Communication between modules should always be coordinated through Application whenever practical.

---

# Data Ownership

UI State

Owned by

FloatingWidget

---

Application State

Owned by

Application

---

Recorder State

Owned by

Recorder

---

Script Files

Owned by

Script Store

---

Window Information

Owned by

Window Tracker

---

# Design Principles

Each module should have one primary responsibility.

Prefer composition over coupling.

Keep modules independent.

Keep interfaces stable.

Avoid circular dependencies.

Avoid duplicated logic.

Prefer extending existing modules over creating new modules.

---

# Current Architecture Status

Completed

- Application Coordinator
- Floating Widget
- Recorder
- Script Store
- Window Tracker
- Runtime State

In Progress

- Script List Synchronization

Planned

- Multi Script Selection
- Parallel Script Execution
- Script Scheduler

---

# Architecture Constraints

Do not redesign module responsibilities unless explicitly requested.

Do not move business logic into UI.

Do not bypass Script Store when accessing scripts.

Do not bypass Application when coordinating modules.

Keep modifications localized to the affected feature whenever possible.

---

# Validation

After modifying architecture-related code:

Run

python -m compileall app

Resolve all compilation errors before completion.

---

# Direct Target Capture

All OCR creation and runtime polling use `TargetCaptureService` with an
explicit locked HWND. The service never resolves the foreground window and
never falls back to a desktop screenshot.

Backend implementations conform to the shared `CaptureBackend` interface and
return a `CaptureResult` containing the image, backend name, HWND, client size,
duration, and backend metadata.

The normal backend chain is:

1. `winsdk-wgc` — direct Windows Graphics Capture through the maintained
   PyWinRT 3.2.1 projections (MIT license, CPython 3.11 wheels). The legacy
   backend name is retained for configuration compatibility.
2. `printwindow` — `user32!PrintWindow(PW_CLIENTONLY)`.
3. `bitblt` — target client DC capture for compatible traditional windows.
4. Explicit failure.

`windows-cap==0.1.2` is not a production dependency. Its alpha API identifies
windows by title and class rather than by the immutable locked HWND, and the
independent diagnostic tool reproduces a native capture failure outside the
ScreenBot application.

The successful backend is cached by `(HWND, PID)`. A cached failure invalidates
the entry and re-runs the direct backend chain. `TextDetector.close()` releases
all backend state on application shutdown.

Use the independent diagnostic utility without loading OCR or ScreenBot UI:

```powershell
F:\ScreenBot\.venv\Scripts\python.exe tools\test_window_capture.py --list
F:\ScreenBot\.venv\Scripts\python.exe tools\test_window_capture.py --hwnd 0x123456 --backend winsdk-wgc
```
