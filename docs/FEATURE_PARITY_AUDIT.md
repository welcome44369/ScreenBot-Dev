# ScreenBot Dev Feature Parity Audit

## ScreenBot Dev Feature Parity Audit Result

**PARTIAL**

The Legacy-first inventory found no Legacy user or runtime capability that is
absent from ScreenBot Dev.  Legacy functionality is either retained, retained
through a safer design, or is now part of the protected Dev window core.

The result is PARTIAL rather than PASS because three Dev-only extensions are
not end-to-end functional by static inspection: condition objects cannot be
authored in the formal UI and are dropped when a referenced trigger is
resolved; resource-referenced stop triggers are not editable in the rich
workflow editor.  No production behavior was changed to address those
findings.

## Audit Base

| Item | Value |
|---|---|
| Official repository | `https://github.com/welcome44369/ScreenBot-Dev.git` |
| Dev branch | `origin/dev/screenbot-next` |
| Dev HEAD / audit base | `6e010f30338379e1848d2a8f1c8d7102316bf828` |
| Agent branch | `agents/feature-parity-audit-report` |
| Legacy remote | `screenbot-readonly` (`https://github.com/welcome44369/ScreenBot.git`) |
| Primary Legacy baseline | `screenbot-readonly/main` at `5fd9a3176967aefa2b06969ab0c7fb4bfe0b9c6c` |
| Additional Legacy feature-source refs | `screenbot-readonly/codex/phase-a1-copilot-handoff` (`412d8d0`), `screenbot-readonly/codex/phase-a1.3-target-relative-overlay` (`36c3c24`), `screenbot-readonly/release/screenbot-operational-recovery` (`58db892`), `screenbot-readonly/dev/screenbot-next` (`bf7d9bf`) |

`screenbot-readonly/main` is the last complete pre-refactor feature baseline:
it contains the original managers, editor, recorder, player, OCR resources,
workflow runner, diagnostics, and their tests as one coherent tree.  The
additional refs were inspected as feature sources, but primarily contain the
TargetSession, overlay, foreground-safety, and start-handoff work now
protected in Dev.  They are evidence sources, not cherry-pick candidates.

All Legacy evidence was read from immutable Git objects.  The Legacy worktree
was not used as evidence and no Legacy or Dev runtime was executed.

## Legacy Inventory

### UI and controller inventory

| Legacy UI capability | Callback / owner | Store or runtime path | Dev status |
|---|---|---|---|
| Script selector, load, start, stop, pause, resume, refresh, folder | `Application` script controls | `ScriptStore`, `ScriptPlayer` | PRESENT_EQUIVALENT |
| Macro recording, save, and macro manager | `start_recording`, `stop_recording`, `save_recording`, `open_macro_manager` | `InputRecorder`, `ScriptStore` | PRESENT_EQUIVALENT |
| Trigger manager and OCR trigger wizard | `open_trigger_manager`, `TriggerManager`, `TriggerWizard` | `TriggerStore`, `TriggerRunner` | PRESENT_REDESIGNED |
| Workflow manager | `open_workflow_manager`, `WorkflowManager` | `WorkflowStore`, `WorkflowRunner` | PRESENT_EQUIVALENT |
| Workflow editor, step editing, loop and inline stop-condition editing | `open_workflow_editor`, `WorkflowEditor` | `WorkflowStore`, `WorkflowResolver`, `WorkflowRunner` | PRESENT_EQUIVALENT |
| OCR region selection, preview, candidate correction | editor/wizard preview controls | target capture, OCR workers, correction store | PRESENT_EQUIVALENT |
| Runtime debug, diagnostics, log export and log folder | `open_runtime_debug_panel`, `_export_runtime_log`, `open_runtime_log_folder` | `RuntimeRunLog`, `WorkflowDiagnostics` | PRESENT_REDESIGNED |
| Overlay controls, opacity/menu controls | floating/overlay UI and settings | overlay owner and `SettingsStore` | PRESENT_REDESIGNED, protected core |
| F8 lock/unlock, F9 pause/resume, Escape stop | `HotkeyManager` and `Application` handlers | target/session, player, workflow runner | PRESENT_REDESIGNED, protected core |

### Schema and runtime inventory

| Area | Legacy capability discovered | Dev result |
|---|---|---|
| Trigger types | OCR text trigger | Retained; Dev adds `workflow_start` |
| Trigger transition modes | `appear`, `disappear` | Retained; Dev maps them into a six-condition model |
| Trigger timing | confirmation frames, cooldown, `min_absent_duration_ms` | Retained |
| Trigger lifecycle | one-fire and re-arm state handling | Retained |
| Script actions | `mouse_click`, `mouse_double`, `mouse_wheel`, `mouse_move`, `mouse_down`, `mouse_up`, `scroll`, `drag`, `key`, `key_down`, `key_up` | Retained |
| Workflow fields | steps, enabled trigger, loop mode, max cycles, inline/referenced stop trigger, restart step | Retained; Dev adds IDs and resource references |
| Loop modes | one-shot, `manual_stop`, `max_cycles`, `stop_trigger` | Retained |
| Terminal behavior | running, stopped, completed/error state and held-input cleanup | Retained and made generation-safe |
| Chaining/callbacks | no `next_workflow`, completion/failure callback/action, or timeout action in any fetched Legacy ref | Not a parity loss; no Legacy feature evidenced |
| Compatibility | legacy script events and trigger/workflow IDs | Retained, with generated IDs for new resources |

## Parity Summary

| Classification | Count | Meaning in this audit |
|---|---:|---|
| PRESENT_EQUIVALENT | 13 | Legacy behavior and an executable Dev path are present. |
| PRESENT_REDESIGNED | 10 | Legacy behavior is retained through a safer or expanded Dev design. |
| BACKEND_ONLY_NO_UI | 0 | No Legacy feature has this status. |
| UI_ONLY_NO_BACKEND | 0 | No Legacy UI control has this status. |
| PARTIAL | 2 | Dev-only extensions have incomplete UI/resolver wiring. |
| MISSING | 0 | No Legacy feature is missing. |
| INTENTIONALLY_REMOVED | 0 | No deletion was inferred without evidence. |
| UNKNOWN | 2 | Lifecycle chaining/callbacks and background-targeted input were not evidenced in Legacy history. |

The counts include the Dev-only extension rows so that incomplete Dev
features are visible rather than hidden by the positive Legacy parity result.
The machine-readable row count and evidence are in
`docs/feature_parity_matrix.json`.

## High-Priority Findings

### Dev condition contract is only partially wired

- **Legacy behavior:** Legacy has only `appear` and `disappear`; both remain
  supported.
- **Dev extension:** `initial_present`, `initial_absent`, `edge_present`,
  `edge_absent`, `state_present`, and `state_absent` are serialized by
  `trigger_conditions.py`.
- **Dev status:** PARTIAL, Dev-only extension; it is not a Legacy regression.
- **Missing layers:** `TriggerWizard` and `WorkflowEditor` expose only
  `appear`/`disappear`.  `WorkflowResolver._resolve_trigger_reference`
  copies legacy fields but does not copy `condition`, `id`, or `name`.
- **Evidence:** `tools/test_workflow_stop_trigger.py` asserts that a referenced
  stop trigger keeps `id` and `condition`, while the resolver field-copy list
  omits both.  `git blame` attributes that list to the Legacy baseline, and
  the condition contract was added by `412d8d0` without changing the resolver.
- **Last known commit:** `412d8d0` introduced the condition contract.
- **Disappearance commit:** none; this path was never completed.
- **Window-core dependency:** none.
- **Recovery difficulty:** MEDIUM.
- **Recommended action:** after review, add condition-capable UI controls,
  preserve identity and condition fields in the resolver, and make the
  existing no-I/O tests pass before any Background Input work.

### Resource-referenced stop trigger is not fully editable

- **Legacy behavior:** the Legacy editor supports an inline OCR stop trigger;
  that behavior remains available in Dev.
- **Dev extension:** `WorkflowStore`, resolver, runner, and `WorkflowManager`
  recognize `loop.stop_trigger_ref`.
- **Dev status:** PARTIAL, Dev-only extension.
- **Missing layer:** `WorkflowEditor` edits only the inline
  `loop.stop_trigger` shape.  The manager enables the resource picker only
  for `stop_trigger` loop mode; it does not provide the broader resource
  configuration expected by `tools/test_workflow_stop_trigger.py`.
- **Last known commit:** `412d8d0` contains the static test expectations.
- **Disappearance commit:** none; the editor was never updated with the
  resource-reference representation.
- **Window-core dependency:** none.
- **Recovery difficulty:** MEDIUM.
- **Recommended action:** choose one documented editor model for inline and
  referenced stops, preserve it round-trip, then align source and no-I/O
  tests.

### Manual stop is not statically capped by maximum cycles

The reported risk was checked against both baselines.  In both Legacy and Dev,
`WorkflowRunner._on_cycle_boundary` applies the cycle counter only in
`max_cycles`; `manual_stop` restarts without that check.  Static evidence does
not support a current manual-stop/max-cycle coupling regression.  No runtime
was run under this read-only audit.

## Known User-Reported Features

| Feature | Result |
|---|---|
| 0 -> 1 rising edge | PRESENT_REDESIGNED as Dev `edge_present`; Legacy `appear` remains compatible.  Referenced condition objects have the partial resolver limitation above. |
| 1 -> 0 falling edge | PRESENT_REDESIGNED as Dev `edge_absent`; Legacy `disappear` remains compatible.  Referenced condition objects have the partial resolver limitation above. |
| Present/absent state trigger | Dev-only condition extension, PARTIAL for UI/reference paths. |
| Confirmation duration | PRESENT_EQUIVALENT (`confirm_frames`). |
| Minimum absent duration | PRESENT_EQUIVALENT (`min_absent_duration_ms`). |
| Cooldown, single-fire, re-arm | PRESENT_EQUIVALENT. |
| Workflow completion trigger | No implementation in any fetched Legacy ref; no parity loss evidenced. |
| Workflow stopped/failed/timeout trigger | Legacy supports a stop condition, not lifecycle callback triggers; no parity loss evidenced. |
| Run-next/chain workflow | No implementation in any fetched Legacy ref; UNKNOWN rather than a claimed deletion. |
| Maximum cycle count | PRESENT_EQUIVALENT. |
| Manual stop mode | PRESENT_EQUIVALENT; separate from maximum-cycle accounting. |
| Trigger Manager | PRESENT_REDESIGNED; Dev adds workflow-start resources. |
| Script actions | PRESENT_EQUIVALENT; all eleven Legacy actions remain supported. |
| Workflow Editor | PRESENT_EQUIVALENT for Legacy fields; Dev resource-stop extension is PARTIAL. |
| Runtime Debug / Logs | PRESENT_REDESIGNED with structured run grouping and diagnostics. |

## Protected Dev Core

This audit did not change, recommend rollback of, or treat as a Legacy parity
requirement any of the following:

- TargetSession identity and generation;
- window and overlay ownership;
- overlay Z-order restoration, topmost policy, and flashing suppression;
- foreground handoff;
- F8 lock/unlock semantics;
- start-collapse acknowledgement and unique execution handoff;
- the foreground input safety gate;
- capture backend ownership; and
- the DEV autonomous test environment.

Legacy code that touches the old window core is marked
`PORT_LOGIC_ONLY / DO_NOT_PORT_WINDOW_CORE` in the matrix.  No large Legacy
commit is a direct port candidate.

## Deliverables and Audit Limits

| Item | Result |
|---|---|
| `docs/FEATURE_PARITY_AUDIT.md` | This report |
| `docs/feature_parity_matrix.json` | Complete machine-readable matrix |
| Optional audit tool | Not needed |
| Production files modified | None |
| Runtime executed | None |
| Legacy runtime executed | None |
| Real input/game workflow executed | None |
| Background Input Foundation B1a | PAUSED |

## Recovery Roadmap

- **R1: Condition reference propagation.** Make existing Dev condition objects
  survive resource resolution and add UI authoring, with focused no-I/O tests.
  This is independent of the window core and should precede B1a review because
  it defines action/trigger metadata that may be observed by routing.
- **R2: Resource-referenced stop-trigger editing.** Provide a single round-trip
  representation in the editor and manager, then align static tests.
- **R3: Re-review Background Input Foundation B1a.** Keep the proposed
  InputMode, BackendCapabilities, InputResult, router, recording backend, and
  foreground adapter scoped to the eleven existing action types.
- **Deferred high-risk features:** any Legacy window/overlay or global-input
  code.  If semantic recovery is ever required, extract it without importing
  the old ownership, topmost, foreground-stealing, or global-input assumptions.
- **Features that must not be ported directly:** Legacy window tracker,
  overlay/window-core behavior, foreground-stealing paths, and global-input
  assumptions.

## Background Input Status

- **B1a:** PAUSED.
- **Audit impact on Input Router:** no Legacy workflow terminal/chaining
  callback adds an InputResult requirement; no Legacy background backend
  exists to port.
- **Missing action types affecting capabilities:** none.  Legacy and Dev both
  expose the same eleven script actions.  Text input, hotkey, and conditional
  script actions were not evidenced.
- **Terminal behavior affecting InputResult:** none from Legacy.  The router
  still needs explicit per-action structured results as previously proposed.

## Git

The audit began from clean `6e010f30338379e1848d2a8f1c8d7102316bf828`
ancestry with `origin/dev/screenbot-next` as its merge-base and confirmed
ancestor.  The final commit, push, remote tip, and clean-tree verification are
recorded in the task completion message after these two files are committed.

## Final Verdict

ScreenBot Dev has retained the complete Legacy functional inventory while
replacing its fragile window model with the protected TargetSession and
foreground-safety architecture.  No Legacy manager, editor, action, trigger,
loop mode, recorder, capture, OCR-correction, log, or compatibility feature
was found missing.  The partial result is deliberately conservative: Dev-only
condition and resource-reference extensions contain static source/test/UI
inconsistencies that must be repaired in a later, focused task.  Their recovery
does not require and must not modify the repaired window stacking, flashing,
safe foreground-input, or TargetSession core.
