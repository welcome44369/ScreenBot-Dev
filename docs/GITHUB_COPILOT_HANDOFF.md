# ScreenBot Dev GitHub Copilot Handoff

## Official development location

- Repository: `https://github.com/welcome44369/ScreenBot-Dev.git`
- Primary remote: `origin`
- Primary branch: `dev/screenbot-next`
- Primary worktree: `F:\ScreenBot_dev`

`screenbot-readonly` is the historical ScreenBot repository. It may be fetched,
inspected, compared, and used to selectively port an individually approved
feature. It must never receive a push or direct development.

## Product direction and protected boundaries

ScreenBot Dev is moving toward safe, foreground-guarded desktop automation:
the user chooses a target, explicitly starts one workflow, and every input is
authorized against the frozen TargetSession. Background input is not started
and must not be claimed while input remains globally foreground-gated.

Do not alter these protected boundaries without an explicit task:

- TargetSession identity and window ownership
- Overlay ownership, stacking, Z-order, topmost, and flashing safeguards
- Foreground handoff and foreground input safety gate
- F8 lock/unlock semantics
- Capture backend and real input backend

## Current Start contract

```text
User presses Start
-> one frozen workflow request and TargetSession
-> ScreenBot UI collapse request
-> collapse acknowledgement
-> foreground / START_ARMED handoff
-> workflow execution
-> terminal, cancel, target invalidation, or error
-> UI restores once
```

Collapse acknowledgement is a hard precondition. Collapse failure must prevent
workflow execution. Re-entrant Start is ignored while collapsing, armed, or
running. F8 remains lock/unlock only and never starts a workflow.

## Phase status

- A.1.5a, A.1.5b, A.1.5c: PASS
- A.1.5-T1: implementation and isolated suite ready; formal verdict remains
  FAIL because an early run loaded formal A15 Runtime through a recording fake.
  Real input remained zero.
- A.1.5e implementation and autonomous tests: PASS
- A.1.5e Controlled Live Acceptance: PENDING
- Phase A.1.5: ACCEPTANCE INCOMPLETE
- Background Input Foundation: NOT STARTED

## Runtime and test policy

A15 Runtime JSON is ignored local data. Never stage or commit it. The
autonomous suite uses only temporary Store/Resolver/WorkflowRunner/
TriggerRunner/ScriptPlayer data and a Recording Input Backend:

```powershell
python tools/run_dev_acceptance.py all-non-input
```

It must not execute formal A15 Runtime. A passing automated suite is not a
Controlled Live Acceptance pass.

## A15 reference

- Workflow: `A15 Clean Bootstrap` / `workflow_3b23e200b77e40afbf4f53f5d384ce18`
- Trigger: `A15 Workflow Start` / `trigger_526e1a8a4cf64423a801d9607d727451`
- Script: `A15 Safe Single Click` / `script_4d6bcaf7aea44d82bd290ba2edebe131`
- Action: one target-relative `mouse_click`, `delay: 2.0`, ratios `0.5 / 0.5`

Use it only for explicitly authorized Controlled Live Acceptance.

## Worktree and Git policy

- `F:\ScreenBot_dev` is the only active development worktree.
- `F:\ScreenBot_stable` is operational recovery only; do not develop there.
- Legacy/archive worktrees are historical reference only.
- Push only `dev/screenbot-next` to `origin`.
- Never push to `screenbot-readonly`, force-push, reset, restore, clean,
  rebase, or stage Runtime JSON.
- Stage exact paths and keep acceptance logs, temporary output, secrets, and
  game data out of Git.

## Next single task

`Phase A.1.5e-LA — Controlled Live Acceptance for Start-Collapse`.

The user performs exactly one F8 and one Start. Confirm one UI collapse, no
ScreenBot interception, one delayed target click, no observer click, one
terminal completion, and one UI restore. Do not repeat Start during the test.

## Important commits

- `157d85b` — trigger payload boundary preservation
- `fd4696c` — autonomous workflow test environment
- `bf7d9bf` — collapse UI before workflow execution
