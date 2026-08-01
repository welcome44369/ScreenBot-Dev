# ScreenBot Dev GitHub Copilot Handoff

## Official development location

- Repository: `https://github.com/welcome44369/ScreenBot-Dev.git`
- Primary remote: `origin`
- Primary branch: `dev/screenbot-next`
- Primary worktree: `F:\ScreenBot_dev`

`screenbot-readonly` is the historical ScreenBot repository. It may be fetched,
inspected, compared, and used to selectively port an individually approved
feature. It must never receive a push or direct development.

## Cross-agent handoff source

- Canonical cross-agent handoff document: [CODEX_HANDOFF.md](./CODEX_HANDOFF.md)
- This Copilot handoff remains for Copilot-specific runtime guidance and must
  not contradict the Codex handoff baseline.

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
- Background Input Foundation B1a: PAUSED

### R0 tracking note

- `ui-core-r0a` todo ID was previously reused by mistake during the R0e-0a
  rollback task. R0a Clean Shutdown true status remains determined by developer
  interactive PASS evidence.

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

`UI/Core Integration R0 — Clean Shutdown and Trigger UI Recovery`.

The task is to restore the original user experience for clean shutdown and
trigger authoring without regressing the protected ScreenBot Dev execution core.
Do not overwrite TargetSession, overlay ownership, Z-order/topmost/flashing
safeguards, F8 semantics, the foreground input gate, or the start-collapse flow.

The actual `F:\ScreenBot` working copy is the authoritative UI/UX reference.
ScreenBot Dev remains the authoritative performance and execution-core
reference. Restore UI behavior by adapting it to Dev services; never overwrite
Dev's window core.

Developer interactive acceptance is the primary product acceptance. Autonomous
tests remain optional internal diagnostics and are not authoritative product
acceptance. Do not resume Background Input Foundation until UI Recovery R0 is
accepted.

## High-priority known issue

- OPEN: F8 currently remains lock/refresh only in Dev and cannot unlock an
  existing TargetSession. The R0e-0a toggle fix was rolled back pending a
  larger follow-up budget and developer interactive acceptance.

## Important commits

- `eb82e73 feat(ui): allow workflow monitoring during collapsed execution`:
  validated combined persistence of R0a Clean Shutdown and R0d-2 reversible
  workflow monitoring.

- `157d85b` — trigger payload boundary preservation
- `fd4696c` — autonomous workflow test environment
- `bf7d9bf` — collapse UI before workflow execution
