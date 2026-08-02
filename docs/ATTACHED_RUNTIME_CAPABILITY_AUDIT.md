# ScreenBot Attached Runtime Capability Audit

Status: S2-D0 architecture audit
Repository baseline: `69d36476fc438e8994d092a22daea447788fb08d`
Audit branch: `agents/attached-runtime-d0`

This document is a repository-derived capability audit. It does not authorize
background input and does not change runtime behavior.

Evidence labels used throughout:

- **Confirmed**: directly established by repository code or tests.
- **Inference**: a conclusion from the implementation, but not yet proven on
  every supported target.
- **Unknown / probe required**: cannot be claimed without a capability probe
  against the actual target application and Windows environment.
- **Policy**: a proposed product contract, not current behavior.

## 1. Executive Summary

ScreenBot currently has a strong frozen `TargetSession`, direct target-HWND
capture backends, target-relative OCR regions, and a fail-closed physical-style
input player. It does **not** currently provide a complete attached background
runtime.

The present product behavior is:

```text
F8 locks a TargetSession
→ StartHandoff waits for that target to be foreground
→ Workflow starts
→ TriggerRunner requires foreground before every normal observation
→ ScriptPlayer requires foreground at start and before input emission
```

When the target loses foreground while waiting for a normal workflow trigger,
`TriggerRunner` suspends observation before calling `TextDetector`. This is
explicitly covered by
`tools/test_input_safety.py:test_background_wait_suspends_normal_step_observation`.
The runtime therefore cannot presently watch a normal trigger continuously
while the user works in another application.

At the same time, the global workflow stop trigger calls the same
`TextDetector` without a foreground precondition and then validates the frozen
session/generation. This is covered by
`test_global_stop_observes_while_step_input_is_suspended`. That existing path
demonstrates an important architectural fact:

> Target-bound observation can be separated from permission to emit input.

The safe target architecture is:

```text
Background observation may continue without foreground ownership.

Input execution must either:
1. acquire and verify a Foreground Lease, or
2. use an explicitly approved application-specific background adapter.

There is no global background-input fallback.
```

The recommended first capability profile is **Level 1 — Background Watch +
Foreground Action**:

```text
Attached TargetSession
→ target-HWND capture and observation in background
→ trigger confirmed
→ ACTION_PENDING
→ acquire and verify one bounded Foreground Lease
→ run the existing foreground-guarded ScriptPlayer
→ release/restore lease if safe
→ resume background observation
```

Current code is a partial Level 0 foundation, not a completed Level 0 product:
the capture layer is target-HWND based, but the normal TriggerRunner policy
prevents background polling. Current action execution is equivalent to a
`VisualForegroundAdapter` with:

```text
requires_foreground = true
can_act_background = false
```

No production code should claim `can_capture_background`,
`can_capture_occluded`, or `can_observe_minimized` until target-specific probes
pass. In particular, a direct HWND API is not proof that frames remain fresh
while occluded or minimized.

## 2. Current Runtime Control Flow

### 2.1 Current workflow path

```text
F8
→ TargetSessionService.lock_foreground_target
→ frozen session_id / generation / root HWND / process identity

Workflow Start
→ ScreenBotApp._arm_start_request
→ StartHandoffService ARMED
→ wait until ForegroundInputSafetyGate returns ALLOWED
→ full authorization
→ claim COMMITTING
→ final full authorization
→ WorkflowRunner.start
→ freeze ExpectedTargetSession
→ TriggerRunner.start

TriggerRunner poll
→ ForegroundInputSafetyGate.authorize_foreground_input
→ TARGET_NOT_FOREGROUND:
     set input_suspended
     do not call TextDetector
→ ALLOWED:
     TextDetector.observe_text
     post-capture fast foreground authorization
     TextTrigger evaluation
→ trigger matched
→ ScriptPlayer.start
→ player start gate
→ per-delay and per-emission foreground authorization
→ physical-style keyboard/mouse emission
```

Evidence:

- Application composition creates one TargetSession, input safety gate,
  TextDetector, ScriptPlayer and WorkflowRunner
  (`app/application.py:69-108`).
- StartHandoff remains armed on `TARGET_NOT_FOREGROUND` and performs first and
  final full authorizations before starting
  (`app/application.py:718-881`).
- WorkflowRunner freezes the expected session at start
  (`app/workflow_runner.py:157-166`).
- TriggerRunner checks foreground before observation
  (`app/trigger_runner.py:150-185`).
- ScriptPlayer checks at start and before each physical emission
  (`app/player.py:65-91`, `app/player.py:124-153`).

### 2.2 Existing background-capable exception

WorkflowRunner's global stop trigger:

```text
Workflow WAIT_TRIGGER / RUNNING_MACRO
→ WorkflowRunner._check_stop_trigger
→ TextDetector.observe_text
→ validate expected session_id / generation / identity
→ TextTrigger
→ request workflow stop
```

It does not call `ForegroundInputSafetyGate` before observation
(`app/workflow_runner.py:478-580`). This path does not start a Macro; it only
claims the existing stop owner. It is therefore an observation/control-plane
exception, not background input.

This exception is useful evidence that the direct capture stack is not
intrinsically coupled to the foreground guard. It is not proof that all
backends produce fresh background frames.

### 2.3 Current foreground-loss behavior

| Phase | Current result when target loses foreground |
|---|---|
| StartHandoff ARMED | Remains armed for up to 15 seconds; does not start |
| Normal TriggerRunner wait | Sets `input_suspended`; no OCR/OpenCV observation |
| Global stop trigger | Continues target-HWND observation |
| ScriptPlayer start | Returns `INPUT_BLOCKED/TARGET_NOT_FOREGROUND` |
| ScriptPlayer execution | Stops before the next unauthorized emission |
| Held input | Released by player cleanup |
| Workflow | Claims `input_blocked`; cannot advance or restart |
| Recorder keyboard | Event rejected unless frozen target root is foreground |
| Recorder mouse | Only frozen target-owned hit points are accepted |

## 3. Foreground Guard Inventory

### 3.1 Observation guards

| Guard | Owner | What it proves | Foreground dependency | Assessment |
|---|---|---|---|---|
| Target refresh | `TargetSessionService.refresh` | PID, process instance, executable, class, root/client HWND and visibility | None | Must preserve |
| Exact capture HWND | `TargetCaptureService` | requested HWND equals locked `WindowTracker.target` | None | Must preserve; should eventually consume an explicit frozen token |
| ScreenBot rejection | `TargetCaptureService` | target is not ScreenBot PID/top-level UI | None | Must preserve |
| Image dimensions and readability | `TargetCaptureService._validate_result` | client-sized, non-uniform, non-black/white image | None | Necessary but insufficient for freshness |
| ROI identity/freshness | TextDetector/S2-C verifier | ROI/client size, capture ID/time, session/generation/root | None | Must preserve |
| Pre-observation foreground gate | `TriggerRunner._run` | target is foreground | Yes | Incorrectly shared action guard |
| Post-observation fast foreground gate | `TriggerRunner._run` | foreground stayed on target across capture | Yes | Appropriate immediately before action, overly strict for observation |
| Stop-trigger identity check | `WorkflowRunner._check_stop_trigger` | expected session/generation remains current | No | Existing model for background observation boundary |

The principal current failure boundary is
`TriggerRunner._run` (`app/trigger_runner.py:150-185`). It calls the action
safety gate before capture and skips observation on
`TARGET_NOT_FOREGROUND`. The resulting runtime errors/statuses are reported as
`target_unavailable:TARGET_NOT_FOREGROUND` or `input_suspended`, even though
foreground ownership is not required to read an already frozen target-HWND
frame.

### 3.2 Action guards

| Guard | Owner | Frequency | Assessment |
|---|---|---|---|
| StartHandoff fast authorization | Application | every handoff timer attempt | Preserve for current foreground-start path |
| StartHandoff first full authorization | Application | immediately before commit | Preserve |
| StartHandoff final full authorization | Application | immediately before runtime start | Preserve |
| Player start authorization | ScriptPlayer | every `start` | Preserve |
| Full refresh authorization | ScriptPlayer | at least every 250 ms during delays/actions | Preserve |
| Fast authorization | ScriptPlayer | between full refreshes and before emission | Preserve |
| `_emit` lock and authorization | ScriptPlayer | every keyboard/mouse output call | Preserve |
| Held-input ledger and release | ScriptPlayer | terminal/failure/stop | Preserve |
| Workflow terminal claim | WorkflowRunner | first input block/stop/error wins | Preserve |

The player emits through global `keyboard` and `mouse` libraries. Those APIs do
not address an HWND. Therefore every existing ScriptPlayer foreground check is
an essential safety boundary, not an obstacle to remove.

### 3.3 Lifecycle guards

| Guard | Current owner | Behavior |
|---|---|---|
| Session identity/generation | `TargetSessionService` | monotonic generation; no automatic retarget |
| PID reuse/process replacement | `TargetSessionService.refresh` | disconnects on PID/process-instance/path/class/root/client mismatch |
| Visibility | `TargetSessionService` | foreground/background/minimized/hidden are state changes, not automatic retarget |
| F8 unlock | Application | stops Workflow/Script/Recorder, confirms idle, then clears |
| Recorder invalidation | Recorder subscription | stops accepting on clear/disconnect |
| Workflow stop | WorkflowRunner/Application | blocks new work, stops trigger/player, releases capture at workflow end |
| Capture session | TextDetector/TargetCaptureService | invalidated on target replacement, workflow end and shutdown |
| Overlay | TargetRelativeOverlayCoordinator | detaches target binding without hiding ScreenBot controls |
| Shutdown | Application | unified stop and resource cleanup owner |

### 3.4 Incorrectly shared guards

1. `ForegroundInputSafetyGate` combines:
   - identity/session validation suitable for both observation and action;
   - minimized/hidden/foreground policy suitable for physical input.

   TriggerRunner consumes the combined result as an observation gate.

2. StartHandoff currently means “wait for foreground, then start the whole
   runtime.” Attached Runtime needs to start the observer while background and
   defer only the action. StartHandoff should not be weakened or silently
   reinterpreted; a separate Attached Runtime owner is required.

3. `TARGET_MINIMIZED` and `TARGET_HIDDEN` are unconditional input rejections.
   That remains correct for ScriptPlayer. Observation needs backend capability
   and frame-health decisions instead of sharing the input result.

### 3.5 Must-preserve guards

- Frozen `session_id` and `generation`.
- PID plus process creation time, executable path and window class.
- Frozen root and client HWND.
- No current-foreground fallback or automatic retarget.
- Direct target-HWND capture only; no desktop fallback.
- Client-relative ROI and action coordinates.
- Player start and per-emission authorization.
- Stop-before-clear F8 behavior.
- Recorder TargetBoundaryFilter.
- Held key/button release on all action failures.
- First terminal claimant wins.

## 4. Observation, Action and Lifecycle Classification

### Observation

Observation includes:

- target-HWND capture;
- OCR and OpenCV visual fusion;
- TextTrigger state;
- workflow WAIT_TRIGGER evaluation;
- stop-condition observation;
- diagnostic capture health.

Observation requires:

```text
current frozen TargetSession identity
AND target-bound capture backend
AND current client/ROI identity
AND fresh, readable frame evidence
```

It does **not** inherently require target foreground. Whether observation can
continue while background, occluded or minimized is a capability decision for
the selected capture backend and target application.

### Action

Action includes:

- keyboard press/release;
- mouse move/click/down/up/wheel/drag;
- ScriptPlayer playback;
- any future control-level mutation.

Current physical-style action requires:

```text
valid frozen TargetSession
AND strong identity
AND non-minimized/non-hidden target
AND foreground root == frozen target root
AND authorization immediately before every emission
```

Future background action is permitted only through an explicitly approved,
application-specific adapter whose control is rooted under the frozen
TargetSession. No adapter may fall back to global injection.

### Lifecycle

Lifecycle includes:

- attach/detach;
- generation change;
- target invalidation;
- capture suspend/resume;
- trigger latch/rearm;
- action pending/cancelled;
- foreground lease acquisition/release;
- F8, emergency stop and shutdown.

Lifecycle events may be global control-plane events, but their effects must be
scoped to the frozen TargetSession and current action token.

## 5. Capture Backend Capability Matrix

The production chain is:

```text
winsdk-wgc → PrintWindow(PW_CLIENTONLY) → client-DC BitBlt → failure
```

`TargetCaptureService` rejects desktop fallback, refreshes the locked target,
requires exact HWND equality, validates client dimensions and rejects
near-black/near-white/near-uniform results
(`app/target_capture.py:28`, `app/target_capture.py:92-180`,
`app/target_capture.py:233-268`).

| Capability | WGC | PrintWindow | BitBlt |
|---|---|---|---|
| Explicit target HWND | Confirmed | Confirmed | Confirmed |
| Foreground capture | Implemented; physical target probe still required | Implemented for compatible apps | Implemented for compatible apps |
| Background, not occluded | Unknown per target; likely candidate | App-dependent, probe required | App/rendering dependent |
| Background, fully occluded | Unknown; primary probe target | App-controlled render, unknown | Not reliable as a product contract |
| Minimized | Unknown; frame delivery may stop | Unknown/unreliable | Unknown/unreliable |
| Client-relative crop | Confirmed; window frame is cropped to client | `PW_CLIENTONLY` | client DC |
| Native frame sequence | Confirmed | None | None |
| Capture timestamp | Confirmed | Service-generated | Service-generated |
| Persistent session | Confirmed per HWND/client size | No | No |
| Resize handling | WGC session recreated | New client size each capture | New client size each capture |
| Blank/uniform rejection | Shared service validation | Shared service validation | Shared service validation |
| Frozen valid-frame detection | Not implemented | Not implemented | Not implemented |
| Target-specific capability profile | Not implemented | Not implemented | Not implemented |

### 5.1 WGC facts

- Creates one `GraphicsCaptureItem` for the locked HWND.
- Keeps one session per HWND and client size.
- Publishes `capture_session_id`, `frame_sequence` and
  `captured_monotonic`.
- Crops a full window frame to the current client rectangle.
- Recreates the session on client-size change.
- Does not currently record “last new frame age” or declare a frozen session.

Evidence: `app/capture_backends.py:507-756`,
`app/capture_backends.py:764-856`.

### 5.2 PrintWindow facts

- Calls `PrintWindow(hwnd, ..., PW_CLIENTONLY)`.
- Rendering is controlled by the target application.
- A successful API return plus a non-uniform image does not prove that content
  is current.
- The service assigns a new synthetic capture ID on each call even if the
  returned pixels are unchanged.

### 5.3 BitBlt facts

- Obtains the target's client DC and copies with `SRCCOPY`.
- It is appropriate only as a compatibility fallback.
- The repository has no evidence that it stays correct for occluded,
  minimized, GPU-rendered or protected targets.
- It also receives synthetic capture IDs rather than a native frame sequence.

### 5.4 Frozen-frame gap

Current validation rejects structurally unusable frames, but a valid-looking
old frame can pass indefinitely. S2-C protects bounded disappearance voting
against repeated `capture_id`; that protection is condition-specific and does
not constitute a general capture health state.

Required health metadata:

```text
backend
capture_session_id
native_frame_sequence (when available)
captured_monotonic
observed_monotonic
frame_hash or bounded perceptual fingerprint
new_frame_age_ms
client_size
visibility_state
near_black / near_uniform / readability
```

Required result:

```text
FRESH
STALE
UNREADABLE
CAPTURE_FAILED
CAPABILITY_UNSUPPORTED
```

No `STALE`, `UNREADABLE`, or failed sample may vote PRESENT or ABSENT.

### 5.5 Required capability probes

Each adapter/target profile must be probed independently:

1. foreground, unobstructed;
2. background, unobstructed;
3. partially occluded;
4. fully occluded;
5. minimized;
6. restored after minimize;
7. resized;
8. DPI/client-size change;
9. rapidly changing known visual content;
10. stationary but readable content;
11. protected/elevated target;
12. target destruction and HWND reuse.

The probe must compare frame sequence/timestamp/fingerprint and a known visual
change. “Capture API returned success” is not a pass.

## 6. Background vs Minimized Semantics

These states are not interchangeable:

| Target state | Observation policy | Action policy |
|---|---|---|
| Foreground | allowed when session/capture valid | current ScriptPlayer allowed |
| Background, visible | allowed only if selected backend profile passed | physical action pending; no input |
| Background, occluded | allowed only if occlusion probe passed and frames are fresh | physical action pending; no input |
| Minimized | default `CAPTURE_SUSPENDED`; allow only with explicit target/backend proof | physical input prohibited |
| Hidden/cloaked | default `CAPTURE_SUSPENDED` | physical input prohibited |
| Invalid/disconnected | `TARGET_INVALID`; discard observations | abort/cancel all actions |

Minimized windows often have a valid TargetSession identity, so minimization
must not be treated as detach. Conversely, identity validity does not prove
capture capability.

`CAPTURE_SUSPENDED` means:

- keep the frozen TargetSession;
- stop accepting trigger evidence;
- cancel active burst candidates;
- retain latched/baseline state only according to explicit trigger policy;
- never reinterpret suspension as ABSENT;
- never execute or queue a Macro from stale evidence;
- resume only after fresh-frame capability is re-established.

## 7. TargetSession and Attached Runtime Ownership

`TargetSessionService` must remain the authoritative identity owner. It should
not absorb Workflow, capture backend, lease or UI state.

Proposed ownership:

```text
TargetSessionService
  owns: identity, generation, HWND/process tuple, visibility, clear/disconnect

AttachedRuntimeCoordinator
  owns: attached-runtime state, observation token, capability profile,
        capture health, ACTION_PENDING, lease request and cancellation

ActionArbiter
  owns: one pending/executing action slot and priority decisions

ForegroundLeaseService
  owns: bounded acquisition, verification, expiry and safe restoration

ScriptPlayer
  owns: physical emission, per-event guard and held-input ledger
```

The coordinator reads TargetSession snapshots and subscribes to lifecycle
events. It never mutates TargetSession identity or generation.

## 8. Proposed Attached Runtime State Machine

The following states are architecture vocabulary for S2-D1 through S2-D4.
S2-D0 does not add them to production code.

| State | Entry condition | Capture/OCR/Trigger | Macro | Exit condition |
|---|---|---|---|---|
| `DETACHED` | no TargetSession | off | prohibited | explicit F8 lock → `ATTACHING` |
| `ATTACHING` | explicit lock plus capability setup | probe only; no trigger votes | prohibited | valid identity and probe → attached state; failure → `TARGET_INVALID` |
| `ATTACHED_FOREGROUND` | valid target is foreground and capture is healthy | allowed | may request action through arbiter | background → `ATTACHED_BACKGROUND`; unhealthy → `CAPTURE_SUSPENDED` |
| `ATTACHED_BACKGROUND` | valid target is background and profile permits observation | allowed | prohibited; confirmation → `ACTION_PENDING` | foreground/suspend/invalidate |
| `CAPTURE_SUSPENDED` | minimized, hidden, stale, unreadable or failed capability | off; candidates cancelled | prohibited | fresh proof → attached state; identity failure → `TARGET_INVALID` |
| `ACTION_PENDING` | current trigger confirmed exactly once | read-only observation may continue or pause by policy | no input | arbiter grant → `FOREGROUND_ACQUIRING`; cancel/invalidate → aborted/invalid |
| `FOREGROUND_ACQUIRING` | lease request granted | no new actionable confirmation | prohibited | verified foreground → `EXECUTING`; denial/timeout → `ACTION_ABORTED` |
| `EXECUTING` | lease active and player start authorized | optional read-only diagnostics; no second action | existing ScriptPlayer only | completion → restoring/attached; stop/intervention → aborted |
| `FOREGROUND_RESTORING` | action terminal and restoration allowed | no actionable trigger | prohibited | restore or safe skip → attached state |
| `ACTION_ABORTED` | denied, timeout, intervention or cancellation | no action from rejected token | prohibited | terminal recorded → attached/suspended; invalid identity → `TARGET_INVALID` |
| `TARGET_INVALID` | session identity invalid or disconnected | off; discard all results | prohibited | clear to `DETACHED`; only a later explicit F8 lock may attach again |

Control-plane and presentation behavior:

| State | UI | F8 behavior | TargetSession invalidation |
|---|---|---|---|
| `DETACHED` | Not attached | attempt an explicit safe lock | remains detached |
| `ATTACHING` | Attaching / probing | cancel attempt; do not retarget in the same keypress | cancel probe and enter `TARGET_INVALID` |
| `ATTACHED_FOREGROUND` | Attached | stop/cancel owners, then formal clear | cancel candidates/actions and enter `TARGET_INVALID` |
| `ATTACHED_BACKGROUND` | Watching in Background | stop/cancel owners, then formal clear | cancel candidates/actions and enter `TARGET_INVALID` |
| `CAPTURE_SUSPENDED` | Capture Suspended with reason | formal clear; no automatic replacement | discard probe/results and enter `TARGET_INVALID` |
| `ACTION_PENDING` | Action Pending or Waiting for Foreground | cancel pending action, then formal clear | cancel token and enter `TARGET_INVALID` |
| `FOREGROUND_ACQUIRING` | Acquiring Foreground | revoke acquisition, then formal clear | revoke lease request and enter `TARGET_INVALID` |
| `EXECUTING` | Executing | existing stop-before-clear lifecycle | stop player/release inputs and enter `TARGET_INVALID` |
| `FOREGROUND_RESTORING` | Restoring Foreground | cancel restoration, then formal clear | skip restoration and enter `TARGET_INVALID` |
| `ACTION_ABORTED` | Action Aborted with terminal reason | formal clear or explicit later lock | discard rejected token and enter `TARGET_INVALID` |
| `TARGET_INVALID` | Target Invalid | clear invalid session; a later keypress may lock | stay invalid until formal clear |

### State invariants

- One `session_id/generation` token owns every transition.
- A stale trigger, lease, callback or capture result cannot affect a newer
  generation.
- `ACTION_PENDING` is one-shot and does not imply foreground ownership.
- No state transition automatically locks the current foreground window.
- `CAPTURE_SUSPENDED` never produces ABSENT.
- F8 unlock and emergency stop cancel pending/acquiring/executing work before
  clearing the session.

## 9. Capability Profiles

### Level 0 — Attached Monitor

```text
background target-HWND observation
OCR/OpenCV/Trigger diagnostics
no Macro execution
```

Use when capture capability is proven but action policy is not approved.

### Level 1 — Background Watch + Foreground Action

```text
background observation
trigger confirmation
ACTION_PENDING
bounded Foreground Lease
existing ScriptPlayer
safe release/restoration
```

This is the recommended first implementation for visual/self-drawn targets,
including games where semantic controls are unavailable.

### Level 2 — Approved Native Background Action

Permitted only through a target-specific adapter such as:

- UI Automation control patterns rooted under the frozen target;
- validated Win32 control messages to a concrete child control;
- an application/vendor-supported automation API.

Each adapter must declare and prove capabilities. General `PostMessage`,
`SendMessage`, title lookup, process-only lookup, desktop-scoped UIA and global
injection are not an approved fallback.

### Level 3 — Unsupported

- stale/frozen/black background capture;
- minimized capture without a passing probe;
- protected content that cannot be observed;
- focus acquisition that Windows denies;
- background action for a self-drawn/game surface without an approved adapter;
- any adapter that cannot stay inside the frozen TargetSession root.

### Capability descriptor

Future adapters should expose data, not optimistic behavior:

```text
can_capture_background
can_capture_occluded
can_observe_minimized
can_act_background
requires_foreground
can_acquire_foreground
can_restore_foreground
supports_client_relative_coordinates
supports_control_level_actions
probe_version
probe_result
```

The current physical-style profile is:

```text
adapter = VisualForegroundAdapter
requires_foreground = true
can_act_background = false
can_acquire_foreground = false  # no formal owner exists
can_restore_foreground = false  # no formal owner exists
```

## 10. Foreground Lease Contract

The Foreground Lease does not yet exist. Its contract must precede code.

Required immutable fields:

```text
lease_id
action_id
session_id
generation
target_root_hwnd
previous_foreground_hwnd
previous_foreground_identity (when recoverable)
requested_at
acquired_at
expires_at
owner
state
```

### Acquisition

```text
trigger confirmed
→ ACTION_PENDING
→ ActionArbiter grants one action
→ refresh/revalidate frozen TargetSession
→ snapshot previous foreground
→ request target foreground once
→ verify foreground root == frozen target root
→ wait bounded UI-settle interval
→ refresh/revalidate TargetSession again
→ start ScriptPlayer with the same expected session
```

`SetForegroundWindow` is a request, not proof. A false return, unchanged
foreground, changed target identity or timeout produces `FOREGROUND_DENIED` or
`LEASE_TIMEOUT`; it must not trigger retries that fight the user.

### During execution

- ScriptPlayer retains all current start/per-emission guards.
- Foreground loss terminates playback and releases held input.
- If foreground changes to a third window, classify `USER_INTERVENED`.
- Never reacquire automatically during the same action.
- F8/emergency/shutdown revoke the lease and block new input before cleanup.

### Release and restoration

Restoration is optional and conservative:

1. wait for ScriptPlayer terminal completion and held-input release;
2. verify the current foreground is still the leased target;
3. verify the previous foreground HWND and process instance still exist;
4. skip restore if the user intervened or identity is uncertain;
5. request previous foreground once;
6. verify result, but do not retry/fight Windows;
7. release lease regardless of restore outcome.

Terminal reasons:

```text
COMPLETED
FOREGROUND_DENIED
TARGET_INVALID
USER_INTERVENED
LEASE_TIMEOUT
CAPTURE_SUSPENDED
ACTION_CANCELLED
PLAYER_INPUT_BLOCKED
RESTORE_SKIPPED
RESTORE_FAILED
```

## 11. Action Arbiter Contract

One Application-owned `ActionArbiter` is required before multiple background
observers can request action.

Responsibilities:

- own exactly one pending/acquiring/executing action;
- bind every request to action ID, trigger ID, execution ID and frozen target
  token;
- reject duplicate trigger confirmations and stale callbacks;
- serialize Recorder, direct Script, Workflow and Attached Runtime action
  ownership;
- grant Foreground Leases;
- prevent a new observer from starting input;
- expose deterministic terminal state to UI/logging;
- cancel safely on F8, emergency stop, target invalidation and shutdown.

Priority:

```text
Emergency Stop
> F8 target lifecycle / application shutdown
> currently executing Macro
> current ACTION_PENDING
> new trigger observations
```

Recorder never executes through the arbiter; it conflicts with execution and
must block action acquisition while recording.

An Action Arbiter does not replace WorkflowRunner or ScriptPlayer. It
coordinates the boundary before the existing owners start.

## 12. UI and User Policy

Required distinct UI states:

```text
Attached
Watching in Background
Action Pending
Waiting for Foreground
Acquiring Foreground
Executing
Restoring Foreground
Capture Suspended
Target Invalid
Action Aborted
```

The UI must clearly state:

- observation may continue while the target is background;
- no Macro input occurs until foreground is acquired and verified;
- ScreenBot may request foreground only after an actionable trigger;
- user intervention cancels action rather than causing focus fighting;
- minimized/unsupported capture suspends decisions;
- F8 unlock and emergency stop remain available.

The product must not use a single ambiguous label such as “background
execution.” Observation and action capabilities must be shown independently.

## 13. Minimal Change Points

This is a proposed future scope, not work performed in S2-D0.

### New owners

- `app/attached_runtime.py`
  - `AttachedRuntimeCoordinator`
  - state enum and immutable runtime token
  - capability descriptor
- `app/action_arbiter.py`
  - single pending/executing action owner
- `app/foreground_lease.py`
  - bounded acquisition/verification/restoration
- `app/capture_capability.py`
  - probe result and frame-health classification

### Minimal existing integration points

- `app/application.py`
  - construct owners;
  - route F8/emergency/shutdown cancellation;
  - render state;
  - do not embed lease logic in UI callbacks.
- `app/input_safety.py`
  - retain `ForegroundInputSafetyGate`;
  - add a separate read-only observation authorization contract or factor
    shared identity checks without weakening input rules.
- `app/trigger_runner.py`
  - use observation authorization before capture;
  - revalidate observation token after capture;
  - emit `ACTION_PENDING` instead of starting a Macro directly when background.
- `app/workflow_runner.py`
  - accept arbiter results;
  - preserve current stop and terminal semantics;
  - cancel pending observations/actions on runtime stop.
- `app/target_capture.py` / `app/capture_backends.py`
  - expose capability and frame-health metadata;
  - do not change backend order in D1.
- `app/player.py`
  - preferably unchanged in D1-D3;
  - continue receiving the frozen expected target from the lease owner.
- `app/floating_widget.py`
  - presentation only in D4.

## 14. Phased Implementation Plan

### S2-D1 — Background Observation Boundary

Goal:

```text
valid attached target
→ target-HWND capture/OCR/OpenCV/Trigger may continue in background
→ ScriptPlayer cannot start
```

Work:

- introduce an immutable Observation Authorization token;
- split identity/capture authorization from foreground input authorization;
- add capture freshness health and capability profile;
- move normal TriggerRunner polling to the observation guard;
- retain post-capture identity/generation/ROI validation;
- cancel burst/state candidates on suspension;
- no Macro dispatch while background;
- prove foreground behavior has no regression.

Exit criteria:

- background target observation works on a capability-approved target;
- stale/minimized/unreadable frames cannot trigger;
- all existing ScriptPlayer background-input rejection tests remain green.

### S2-D2 — Action Pending

Goal:

```text
background trigger confirmed
→ exactly one ACTION_PENDING
→ no input
```

Work:

- create Application-owned ActionArbiter;
- freeze action/trigger/execution/session/generation identity;
- remove direct TriggerRunner-to-player start for attached-background
  confirmation;
- cancel on present rearm, target change, workflow stop, F8 and shutdown;
- add timeout and exactly-once telemetry.

Exit criteria:

- background confirmation never calls ScriptPlayer;
- stale pending token cannot act on a new TargetSession;
- multiple workflows/triggers cannot own action simultaneously.

### S2-D3 — Foreground Lease

Goal:

```text
ACTION_PENDING
→ one bounded foreground request
→ verified target foreground
→ existing ScriptPlayer
→ safe release/optional restore
```

Work:

- implement ForegroundLeaseService;
- add a Win32 activation adapter with injectable tests;
- validate target before/after acquisition;
- preserve player per-emission checks;
- handle user intervention without reacquisition;
- restore previous foreground only when safe;
- integrate F8/emergency/shutdown cancellation.

Exit criteria:

- denied focus produces no input;
- successful lease runs exactly one Macro;
- intervention stops before further input;
- held inputs are released;
- no repeated focus stealing.

### S2-D4 — Attached Runtime UI

- add the explicit UI states listed above;
- display observation and action capability separately;
- show capture suspension and foreground denial;
- expose cancel action without clearing TargetSession;
- preserve FloatingWidget and target overlay ownership.

### S2-D5 — Optional Capability Adapters

- probe UIA only inside the frozen target root/control tree;
- add target/application-specific adapters only after explicit approval;
- never expose a general PostMessage/SendMessage background fallback;
- keep self-drawn/game targets on Level 1 unless a supported API exists.

## 15. Risk Register

| # | Risk | Impact | Detection | Fail-closed behavior | Planned phase |
|---:|---|---|---|---|---|
| 1 | WGC may stop producing fresh frames while background or occluded | stale observation can authorize a false trigger | capability probe plus native frame sequence, age and known visual change | enter `CAPTURE_SUSPENDED`; accept no votes or action | D1 |
| 2 | Minimized target may freeze, black out or change client size | stale/invalid content can be mistaken for PRESENT or ABSENT | visibility, client size, sequence age, hash, near-black/uniform and readability | cancel candidates; no observation decision or action | D1 |
| 3 | Foreground acquisition surprises the user | focus theft and unintended input context | explicit policy, visible `ACTION_PENDING`, acquisition telemetry | no automatic acquisition unless policy permits; cancellation emits no input | D3/D4 |
| 4 | User changes foreground during Macro | later physical input could reach another application | existing per-event foreground root authorization | stop immediately, release held input, classify `USER_INTERVENED`, no reacquire | D3 |
| 5 | Restoration targets the wrong previous foreground | ScreenBot steals focus after completion | snapshot previous root/process identity and revalidate before restore | skip restoration when identity or ownership is uncertain | D3 |
| 6 | Previous foreground HWND disappears | invalid handle or unrelated reused HWND could receive focus | `IsWindow`, root/process identity and process-instance validation | skip restoration; release lease | D3 |
| 7 | Target foreground changes between lease verification and first input | first input could escape the target | target refresh plus existing ScriptPlayer start and per-emission gates | abort before emission; release any held input | D3 |
| 8 | Target window is rebuilt or PID/HWND is reused | stale work could attach to a different process/window instance | authoritative TargetSession session/generation/process creation/path/class/root/client tuple | invalidate runtime; discard observations, pending action and lease | D1-D3 |
| 9 | ScreenBot overlay is mistaken for target root | observation or action could bind to ScreenBot UI | root ownership check and frozen target identity; overlay never acts as authority | reject sample/action; do not retarget | D1 |
| 10 | Multiple workflows request action simultaneously | focus contention and interleaved input | one Application-owned arbiter slot and action IDs | accept one owner; reject or queue others without input | D2 |
| 11 | Trigger or lease callback arrives after rearm/cancel | stale callback could execute an obsolete condition | action/trigger/execution/session/generation token comparison | discard callback and emit stale-token terminal evidence | D2/D3 |
| 12 | DPI, client size or ROI changes during acquisition | coordinates and visual evidence no longer match target client | compare capture/client metadata before and after lease acquisition | invalidate ROI/template/action; return to observation | D1/D3 |
| 13 | Game captures in background but accepts only foreground input | observed trigger could be mistaken for background-action permission | capability profile separates observation and action capability | Level 1 only; emit `ACTION_PENDING`, never background input | D1/D2 |
| 14 | Minimize/restore or virtual-desktop transition changes layout | restored coordinates/focus may be unsafe | post-restore client size, DPI, foreground root and capture-health validation | suspend capture or skip focus restoration | D1/D3 |
| 15 | Windows denies `SetForegroundWindow` | action cannot safely start | verify actual foreground root; do not trust API return alone | one bounded attempt, then `FOREGROUND_DENIED`; zero input and no retry loop | D3 |
| 16 | User and ScriptPlayer input interleave | corrupted gesture or input delivered after intervention | player held-input ledger and per-event foreground gate | stop, release all held inputs, abort lease | D3 |
| 17 | UIA/Win32 adapter is applied to Unity or self-drawn game UI | control lookup may escape scope or silently do nothing | target-specific adapter probe rooted under frozen target control tree | mark unsupported and retain Level 1 foreground action | D5 |
| 18 | Elevated, protected or anti-cheat target blocks capture/control | unreliable capture or prohibited action | capability probe, access/error telemetry and product policy review | unsupported profile; no bypass, injection or fallback | D1/D5 |
| 19 | Standalone TriggerRunner lacks explicit observation/action token ownership | it could bypass the attached coordinator contract | composition audit and tests requiring an observation token | attached mode cannot start through this path until integrated | D1 |
| 20 | Recorder's one-time `SetForegroundWindow` is mistaken for a lease | Recorder behavior could bypass action ownership | owner audit; lease tests require arbiter-issued lease ID | do not reuse it; Recorder remains mutually exclusive with action | D2/D3 |

## 16. Acceptance Criteria

### D1 observation

- TargetSession stays the sole identity owner.
- Background observation requires a passing capability profile.
- Normal workflow trigger polling can continue when the target is background.
- No ScriptPlayer call occurs from background observation.
- Occluded/minimized/stale/unreadable samples cannot vote.
- Capture results carry session/generation/root/ROI/frame health.
- Old generation and out-of-order results are rejected.
- Foreground behavior and S2-C disappear verification remain unchanged.

### D2 action pending

- A confirmed background trigger creates exactly one pending action.
- Pending state emits no input and does not change foreground.
- Pending identity includes action/trigger/execution/session/generation.
- F8, stop, invalidation and shutdown cancel it exactly once.
- A stale pending action cannot affect a new target.

### D3 foreground lease

- Focus is requested at most once per action.
- Success is determined by verified foreground root, not API return alone.
- Target is revalidated before player start.
- Existing ScriptPlayer guards remain enabled.
- Focus denial, timeout and user intervention produce zero further input.
- Held input is released before lease release.
- Restoration never overrides observed user intervention.

### D4 UI

- Observation and action states are distinct.
- “Watching in Background” never implies background input.
- Capture suspension and action denial are visible.
- Cancel action does not silently retarget or clear TargetSession.

### Regression

- F8 lock/unlock and stop-before-clear remain unchanged.
- Recorder TargetBoundaryFilter remains unchanged.
- Overlay ownership/Z-order remains unchanged.
- Workflow stop/terminal semantics remain unchanged.
- No desktop capture or global background-input fallback is introduced.

## 17. Explicit Non-Goals

S2-D0 and the proposed D1-D4 plan do not:

- implement background input;
- add PostMessage, SendMessage, SendInput or desktop-scoped automation;
- weaken ScriptPlayer foreground authorization;
- change TargetSession identity/generation;
- change F8 lifecycle;
- change Recorder boundaries;
- change overlay ownership or Z-order;
- change the capture backend priority;
- promise minimized capture;
- promise support for protected/elevated/anti-cheat targets;
- implement UIA/Win32 adapters;
- allow PID-only, title-only or current-foreground targeting;
- permit multiple concurrent Macros;
- automatically fight the user for focus;
- make “background execution” a single undifferentiated product mode.

## Recommendation

Proceed in this order:

1. **S2-D1:** split observation authorization from foreground input
   authorization while leaving ScriptPlayer untouched.
2. Add capture capability/freshness probes before enabling background
   observations for any target profile.
3. **S2-D2:** add exactly-once `ACTION_PENDING` and an Application-owned
   ActionArbiter; still emit no input.
4. **S2-D3:** add a bounded, verified Foreground Lease around the existing
   ScriptPlayer.
5. **S2-D4:** expose explicit observation/action states in UI.
6. Consider D5 adapters only for applications with a proven control/API
   boundary.

The core safety conclusion is:

> ScreenBot may observe a frozen target in the background only when the
> selected target-HWND capture path proves fresh, valid frames. It may not use
> that observation as permission to emit global input. Physical-style action
> remains foreground-only and must be entered through a separately verified,
> bounded Foreground Lease.
