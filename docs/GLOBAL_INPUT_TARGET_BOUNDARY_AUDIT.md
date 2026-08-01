# ScreenBot Global Input and Target Boundary Audit

## Verified baseline

Audit date: 2026-08-01 (Asia/Taipei).

- Repository: `F:\ScreenBot_dev`
- Stable source branch: `agents/runtime-stabilization-s1`
- Verified source commit: `8a77176f7292b730316d27921bebe1506ffeb5c3`
- Verified remote source commit: `origin/agents/runtime-stabilization-s1` at the same commit
- Audit branch: `agents/target-boundary-s2`
- Audit branch starting commit: the same `8a77176f7292b730316d27921bebe1506ffeb5c3`
- Working tree before branch creation: clean
- The audit branch was initially pushed without adding a commit or changing a file.

Evidence labels used below:

- **Confirmed fact**: directly established from this repository at the verified commit.
- **Code inference**: a conclusion from the confirmed implementation, but not a physical-runtime observation.
- **External research**: information from a linked official document or original project.
- **Not yet verified**: requires a focused test, instrumentation, or developer acceptance.

This audit does not claim to implement or repair Recorder boundaries, input
backends, background input, or emergency stop.

## Product boundary rule

The only product-level global control-plane functions are:

- F8 TargetSession lock/unlock;
- a future explicit emergency stop;
- target selection;
- management of ScreenBot's own UI.

Recorder, Macro, Script, Workflow, OCR, Trigger, stop condition, Capture,
Preview, keyboard input, mouse input, and coordinate conversion must require one
valid, frozen TargetSession.

The required data path is:

```text
Windows global event
→ passive sensor
→ TargetBoundaryFilter
→ session_id + generation + root HWND + process-instance validation
→ TargetSession authorization
→ feature-specific normalization or execution
```

If provenance cannot be established, the event or operation must be rejected.
The current foreground window must never become an implicit replacement target.

## Current dependency map

### Runtime dependencies

| Dependency | Current role | Scope | Boundary status |
|---|---|---|---|
| `keyboard>=0.13.5` | F8/F9/ESC registration and ScriptPlayer keyboard injection | Global | Valid for control-plane hotkeys; injection is guarded by `ForegroundInputSafetyGate` in the production composition |
| `mouse>=0.7.1` | ScriptPlayer pointer movement, click, press/release, wheel | Global OS input stream | Guarded before each emission, but the library is not a target-addressed backend |
| `pynput==1.8.2` | Recorder keyboard and mouse listeners | Global passive hooks | Sensor is acceptable; event-to-TargetSession boundary is incomplete |
| `pywin32>=302` | HWND/process queries, client coordinates, GDI capture | HWND/desktop Win32 APIs | Capture is target checked; Recorder mouse hit testing is incomplete |
| WinRT packages | Windows Graphics Capture | HWND capture item | Appropriate target-bound capture path |
| `pytesseract`, Pillow | OCR and image processing | Image supplied by target capture | No independent desktop acquisition found |
| PySide6 | UI, queued hotkey bridge, selection UI | ScreenBot process | Application/UI control plane |
| `psutil` | process identity fallback and diagnostics | Process | Identity support only; PID alone is not accepted by TargetSession |

No `pyautogui`, `pywinauto`, `mss`, or `dxcam` runtime import was found.
There is no direct `SendInput`, `PostMessage`, `SendMessage`,
`RegisterRawInputDevices`, or `SetWindowsHookEx` call in repository code.
Native implementation details below `keyboard`, `mouse`, and `pynput` are owned
by those packages and are therefore **not yet verified** from this repository.

### End-to-end trace matrix

| Path | Creation/start/stop owner | Thread/callback | Boundary and coordinates | Invalid-session behavior |
|---|---|---|---|---|
| F8 lock/unlock | `ScreenBotApp` creates `HotkeyManager`; manager registers and unregisters; Application owns toggle | `keyboard` callback emits `HotkeyBridge`; Qt invokes Application callback | Legal global control plane; TargetSession is authoritative | Unlock uses synchronous stop owners and `target_session.clear("f8_unlock")`; unsafe active owner blocks clear |
| Emergency stop | No dedicated emergency-stop hotkey or owner found | N/A | F9 is pause/resume; configured ESC requests application exit confirmation; OCR “global stop” is a workflow stop condition, not a global key | **Gap:** product emergency-stop contract is not implemented as a distinct owner |
| Recorder keyboard | `ScreenBotApp` creates `ActionRecorder`; `start_recording`/`stop_recording` own lifecycle | `pynput` listener callback → queue → `ScreenBot-MacroNormalizer` worker | Global listener; accepts key-down only when frozen raw HWND equals foreground HWND; F8/F9/ESC blocklisted | Does not freeze or validate session_id/generation/process instance; no TargetSession invalidation subscription |
| Recorder mouse | Same as keyboard Recorder | `pynput` listener callback → queue → worker | Global listener; converts desktop point with `ScreenToClient`; stores target-client ratios | Does not use `WindowFromPoint`/root HWND and cannot prove an overlaying window owns the event |
| Script/Macro keyboard output | `ScreenBotApp` creates one `ScriptPlayer`; Application/TriggerRunner/WorkflowRunner start it; player owns stop | Dedicated player thread | `keyboard.press/release`; production gate verifies frozen session/generation/identity/root foreground before each emission | Rejects and terminates with `INPUT_BLOCKED`; releases tracked held keys |
| Script/Macro mouse output | Same player owner | Dedicated player thread | ratio → current TargetSnapshot client origin/size → screen point; `mouse` emits global pointer input | Same fail-closed gate; releases tracked held mouse buttons |
| Workflow direct input | `WorkflowRunner` owns TriggerRunner and calls the shared ScriptPlayer | Workflow thread + TriggerRunner thread + player thread | No second direct input backend found; all workflow macro input flows through ScriptPlayer | Target change/input block claims terminal stop and prevents new work |
| OCR/Trigger capture | `TextDetector` owns `TargetCaptureService`; TriggerRunner/WorkflowRunner request observations | Calling TriggerRunner/WorkflowRunner worker; preview can call from UI | target HWND only; default WGC → PrintWindow → BitBlt; desktop fallback rejected | `window_tracker.refresh()` rejects missing/disconnected target; capture HWND mismatch rejects |
| Region selection/Preview | TriggerWizard or WorkflowEditor owns dialog; `SelectionSession` temporarily hides/restores ScreenBot UI | Qt main thread | Frozen target-client image and ratio region; no live desktop selector capture found | Missing target rejects; capture failure aborts selector |
| Shutdown cleanup | `ScreenBotApp.shutdown` is the unified owner | Qt/Application thread with bounded worker joins | Stops workflow, trigger, Recorder when state says recording, player, capture, overlay, TargetSession, and hotkeys | Mostly fail-closed; listener/player join timeouts are not followed by a hard termination verification |

## Global keyboard listeners

### HotkeyManager

**Confirmed fact:** `app/hotkeys.py` owns one F8 key-down hotkey and one F8
key-up hook, plus F9 and the configured exit key. F8 key-down dispatches once
per latch cycle; key-up only clears the latch. `pause`, `resume`, registration,
and unregistration are idempotent. Callbacks do not suppress the physical
event. `ScreenBotApp` connects the Qt bridge before registration and unregisters
the manager during shutdown.

The global semantics are currently:

- F8: legal target lifecycle control.
- F9: Script pause/resume, not emergency stop.
- ESC by default: application exit confirmation, not an immediate emergency
  input-release contract.

**External research:** Windows `RegisterHotKey` posts `WM_HOTKEY` to the
registered window/thread queue and supports `MOD_NOREPEAT`. It is a suitable
future control-plane alternative, but it does not provide general Recorder
events. See [Microsoft RegisterHotKey](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerhotkey).

### Recorder keyboard listener

**Confirmed fact:** `app/recorder.py` starts a global `pynput.keyboard.Listener`
with `suppress=False`. The callback only enqueues an event. Normalization occurs
on a separate worker. Key-down requires the Recorder's frozen raw `_target_hwnd`
to be the exact foreground HWND; key-up is emitted only for a key previously
accepted. F8, F9, and ESC are discarded.

Gaps:

- only HWND is frozen, not session_id, generation, root HWND, client HWND,
  process creation time, or identity strength;
- exact foreground HWND comparison is not root normalization;
- there is no event-time TargetSession refresh;
- there is no injected-event flag or ScreenBot injection marker;
- no dedicated emergency-stop key exists beyond the current blocklist.

**External research:** a native `WH_KEYBOARD_LL` event exposes
`LLKHF_INJECTED` and `LLKHF_LOWER_IL_INJECTED` in `KBDLLHOOKSTRUCT`; this is a
candidate capability if `pynput` cannot expose equivalent provenance. See
[Microsoft KBDLLHOOKSTRUCT](https://learn.microsoft.com/en-us/windows/win32/api/winuser/ns-winuser-kbdllhookstruct).

## Global mouse listeners

**Confirmed fact:** Recorder uses a global passive `pynput.mouse.Listener` with
`suppress=False`. Free mouse movement is deliberately not persisted. Mouse-down,
mouse-up, and scroll enter the normalization queue.

Current boundary behavior:

- `ScreenToClient(target_hwnd, desktop_point)` plus client-rectangle bounds
  decides whether a mouse point is accepted;
- mouse-down must also see the target as the exact foreground HWND;
- mouse-up requires a prior accepted down and a point inside the client
  rectangle;
- complete clicks and drags are stored as target-client ratios;
- intermediate drag movement is not observed, so a gesture that leaves the
  target and returns cannot be detected as cross-boundary.

**Code inference:** a ScreenBot overlay or unrelated top-level window physically
covering part of the target client rectangle can still produce a point that
passes `ScreenToClient`. The current code does not ask which window actually
owns that point. This is the principal Recorder self-event risk.

The required mouse filter is:

```text
event screen point
→ WindowFromPoint
→ GetAncestor(..., GA_ROOT)
→ compare with frozen target root HWND
→ revalidate frozen TargetSession identity
→ accept into gesture transaction
```

`WindowFromPoint` returns the window containing a point, but cannot by itself
prove TargetSession identity; root normalization and session validation remain
required. See [Microsoft WindowFromPoint](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-windowfrompoint).

## Recorder event path

```text
ScreenBotApp.start_recording
→ ActionRecorder.start
→ snapshot WindowTracker.target as one HWND
→ start normalizer worker
→ start passive pynput keyboard and mouse listeners
→ SetForegroundWindow(target HWND) once
→ listener callback enqueues _RawInputEvent
→ worker normalizes event
→ foreground/rectangle checks
→ target_client_ratio Macro event
```

Positive properties:

- recording requires `window_tracker.target`;
- callbacks are intentionally small, passive, and non-Qt;
- ScreenBot does not delete “the last event” as a stop-button workaround;
- mouse actions use target-client ratios rather than persisted desktop
  coordinates;
- incomplete mouse down without an accepted up is discarded;
- F8/F9/ESC are not stored;
- listener stop/join and worker stop/join exist.

Boundary defects:

1. Recorder does not receive `TargetSessionService`.
2. It freezes only `target.hwnd`.
3. It does not validate session_id/generation or process instance per event.
4. It does not use event-point window/root ownership.
5. It does not track intermediate motion as part of a gesture transaction.
6. It does not stop automatically on TargetSession invalidation.
7. It cannot distinguish physical input from injected input.
8. `_close_open_keys` writes synthetic key-up records at stop; these are
   internally generated data and should remain explicitly marked.
9. Listener `join(timeout=1.0)` is not followed by an `is_alive` assertion or a
   quarantine state.
10. Recorder startup calls `SetForegroundWindow(target_hwnd)` once. This is
    outside the passive callbacks, but it is still an explicit foreground
    mutation and should remain an Application-owned, user-visible start
    transition rather than a sensor responsibility.

The current UI stop-button click is normally outside the target client and is
therefore rejected. That does not eliminate the overlay/overlap case, and it is
not a sufficient product boundary.

## Input injection paths

### Shared ScriptPlayer

**Confirmed fact:** all repository production keyboard/mouse injection is in
`app/player.py`; Workflow and Trigger code call this shared player rather than
calling an input library directly.

At production construction, `ScriptPlayer` receives
`ForegroundInputSafetyGate`. The player freezes an `ExpectedTargetSession`
(session_id and generation), performs a full authorization at start, and
authorizes again immediately before each global emission. Full refreshes occur
periodically; fast checks use the current immutable snapshot between refreshes.
Authorization checks:

- TargetSession exists;
- expected session_id and generation still match;
- connection is attached;
- identity is valid and strong;
- target is not minimized or hidden;
- current foreground root HWND equals snapshot root HWND.

On rejection, playback requests stop, records `INPUT_BLOCKED`, prevents the
next emission, and releases tracked held keys/buttons.

Mouse coordinates are stored as normalized target-client ratios and converted
using the current TargetSnapshot client origin and size. `script_store.py`
rejects desktop-coordinate fields.

Gaps and caveats:

- `keyboard` and `mouse` emit into the global OS input stream; the repository
  does not address a specific HWND at the native injection layer;
- the precise native injection function used by those installed packages is
  not established in this audit;
- `_point` contains a compatibility fallback through `window_tracker.target`
  when no TargetSnapshot is available. Production composition has a safety
  gate, but the fallback should be removed or made explicitly test-only;
- `_start_script_from_handoff` receives `expected_target` but calls
  `player.start(frozen_script)` without forwarding it. The player's default
  snapshots the current session, and the handoff just revalidated the request,
  but forwarding the explicit frozen expectation would make ownership
  unambiguous.

### Standard controls versus self-drawn targets

**External research:** Microsoft UI Automation clients can discover and invoke
provider-exposed controls. Standard Win32/WPF/Windows Forms controls commonly
have providers; custom controls without providers can be opaque beyond basic
HWND/location information. See [UI Automation fundamentals](https://learn.microsoft.com/en-us/windows/win32/winauto/entry-uiautocore-overview)
and [UI Automation providers](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-providersoverview).

Therefore:

- standard desktop targets should prefer a UIA/Win32 control backend;
- Unity, DirectX, canvas, and otherwise self-drawn targets require visual
  location plus foreground-guarded physical-style input;
- no backend may reinterpret “background” as permission to inject globally.

**External research:** Windows `SendInput` serially inserts keyboard/mouse
events into the input stream and is subject to UIPI; it is not an HWND-addressed
API. See [Microsoft SendInput](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-sendinput).

## Capture and OCR paths

```text
Trigger/Workflow/Preview
→ TextDetector.capture_region
→ WindowTracker.target + refresh
→ TargetCaptureService.capture_target_client
→ exact requested HWND == locked target HWND
→ reject ScreenBot PID/HWND
→ winsdk-wgc
→ PrintWindow(PW_CLIENTONLY) fallback
→ client-DC BitBlt fallback
→ normalized region crop
→ OCRPipeline / TextTrigger
```

**Confirmed fact:** `allow_desktop_fallback=True` raises an error. No full
desktop OCR path was found. Capture validates the locked HWND, target identity
through `window_tracker.refresh`, ScreenBot ownership, client size, returned
HWND, and returned image dimensions.

The backend chain is:

1. Windows Graphics Capture using an HWND-created `GraphicsCaptureItem`;
2. `PrintWindow(PW_CLIENTONLY)`;
3. client-DC `BitBlt(SRCCOPY)`.

WGC sessions are cached per `(hwnd, client_size)` and invalidated at workflow
end, target replacement, or service close. `TextDetector.close` closes capture
and OCR resources.

**External research:**

- `IGraphicsCaptureItemInterop::CreateForWindow` explicitly creates an item for
  one HWND: [Microsoft CreateForWindow](https://learn.microsoft.com/en-us/windows/win32/api/windows.graphics.capture.interop/nf-windows-graphics-capture-interop-igraphicscaptureiteminterop-createforwindow).
- WGC supplies frames for a selected window/display:
  [Microsoft screen capture](https://learn.microsoft.com/en-us/windows/apps/develop/media-authoring-processing/screen-capture).
- `PrintWindow` asks the owning application to render a window and is
  synchronous: [Microsoft PrintWindow](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-printwindow).
- `BitBlt` copies pixels between device contexts:
  [Microsoft BitBlt](https://learn.microsoft.com/en-us/windows/win32/api/wingdi/nf-wingdi-bitblt).

### Region selection and Preview

TriggerWizard captures one frozen target-client image before hiding ScreenBot
windows and showing `OCRRegionSelectorDialog`. The stored region is normalized
to the image/client dimensions. WorkflowEditor also validates a target and
captures `DEFAULT_REGION` through `TextDetector` before selection.

No selector captures the whole desktop in the audited paths. `SelectionSession`
only manages ScreenBot UI visibility; it is not a capture backend.

## TargetSession enforcement gaps

| Finding | Evidence classification | Severity | Required direction |
|---|---|---:|---|
| Recorder freezes only HWND | Confirmed fact | Critical | Inject frozen TargetSession identity into Recorder |
| Recorder mouse ownership is rectangle-only | Confirmed fact | Critical | `WindowFromPoint → GA_ROOT` plus session validation |
| Recorder cannot identify injected input | Confirmed fact | High | expose native flags/tag or choose a sensor that can |
| Recorder does not subscribe to invalidation | Confirmed fact | High | stop accepting immediately, close transaction, stop safely |
| Cross-boundary drag can leave and return | Confirmed fact/code inference | High | full gesture transaction with movement ownership |
| Exact HWND foreground comparison is not root-aware | Confirmed fact | High | compare normalized foreground root to frozen root |
| Recorder sensor owns a one-time foreground mutation | Confirmed fact | Medium | move/authorize target activation at the Application start coordinator |
| Standalone `configure_text_trigger` omits expected session/gate | Confirmed fact | High | require frozen session at construction; remove compatibility path from product use |
| F8 unlock does not explicitly stop standalone `trigger_runner` | Confirmed fact | High | include trigger owner in stop-before-clear coordinator |
| Player compatibility fallback can use WindowTracker target without snapshot | Confirmed fact | Medium | remove production fallback |
| Direct script handoff does not forward its explicit expected target | Confirmed fact | Medium | pass the already validated frozen expectation |
| No distinct emergency-stop owner | Confirmed fact | High | developer must define key, scope, terminal contract, and UI behavior |
| Capture service accepts HWND rather than ExpectedTargetSession | Confirmed fact | Medium | pass a frozen capture authorization object or validate snapshot token per request |

The existing `TargetSessionService` is substantially stronger than downstream
Recorder usage: it owns session_id, monotonically increasing generation, root
and client HWND, PID, process creation time, executable path, window class,
visibility, and identity strength. Refresh disconnects on PID, process-instance,
executable, window-class, root-HWND, or client-HWND mismatch.

## Dangerous fallback inventory

### Present or reachable

- Recorder event acceptance without session_id/generation validation.
- Recorder rectangle-only mouse ownership.
- Recorder exact-foreground-HWND check without root normalization.
- Player compatibility coordinate fallback when constructed without a safety
  snapshot.
- Standalone TriggerRunner configuration without an explicit expected
  TargetSession.
- Trigger retry behavior can continue after capture errors when
  `stop_on_error=False`; with a cleared target this can repeatedly fail rather
  than terminate at the TargetSession boundary.
- The shared global input libraries can be observed by global Recorder hooks;
  no injected-event provenance is checked.

### Explicitly absent or blocked at this baseline

- target OCR desktop fallback: explicitly rejected;
- persisted desktop coordinates: rejected by ScriptStore; Recorder stores
  client ratios;
- same-PID-only authorization: TargetSession checks process instance, path,
  class, root, and client HWND; degraded identity is rejected for input;
- retargeting after target invalidation: no production input gate path found;
- ScreenBot as a TargetSession: rejected by process ID and capture also rejects
  ScreenBot top-level HWNDs;
- F8/F9/ESC in Recorder data: blocklisted.

### Not yet verified

- Whether the installed `pynput` version exposes low-level injected flags on
  Windows.
- Whether `keyboard`/`mouse` attach a usable `dwExtraInfo` marker to their
  injected events.
- Whether every supported game's anti-cheat/security policy permits any
  physical-style input injection. This is a product/legal compatibility
  decision, not merely a technical one.

## Hook and shutdown ownership

| Resource | Create/start owner | Stop owner | Shutdown evidence | Gap |
|---|---|---|---|---|
| F8 down/up and F9/ESC | `HotkeyManager` | `HotkeyManager.unregister_all` | Called by `ScreenBotApp.shutdown`; handles cleared; idempotent | Underlying library hook thread termination is delegated to `keyboard` |
| Recorder keyboard/mouse listeners | `ActionRecorder.start` | `ActionRecorder.stop` / `_stop_listeners` | Shutdown stops only when Application state is `RECORDING` | State/listener divergence could leak until process exit; joins are bounded but not verified |
| Recorder normalizer worker | `ActionRecorder.start` | sentinel + join | Same conditional Recorder shutdown | Join timeout not converted to a quarantine/error |
| Player thread and held inputs | `ScriptPlayer.start` | `ScriptPlayer.stop` and thread `finally` | Active player stopped; held input release called | `join(timeout=5)` does not prove termination before continuing |
| TriggerRunner | Application or WorkflowRunner | `stop_text_trigger` / WorkflowRunner | Both shutdown paths called | Standalone runner lacks frozen target token |
| WorkflowRunner | Application | `stop_workflow` | Unified shutdown calls bounded stop | Terminal observability is outside this audit |
| Capture/WGC resources | `TextDetector`/TargetCaptureService | `close`/invalidate | `text_detector.close` runs | Correct ownership found |
| Target-relative overlay hooks/timers | coordinator | `close` | coordinator closed before TargetSession clear | Correct ownership found |
| Single-instance mutex | `main._create_mutex` | process exit | No explicit `CloseHandle` | Low-risk OS cleanup, but explicit ownership would be clearer |

**External research:** Windows requires global hooks to be removed with
`UnhookWindowsHookEx` before termination and warns that hook callbacks/message
pumps must not block. See [Microsoft SetWindowsHookEx](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowshookexw)
and [LowLevelKeyboardProc](https://learn.microsoft.com/en-us/windows/win32/winmsg/lowlevelkeyboardproc).

## Comparison with mature automation tools

### AutoHotkey

Original project: [AutoHotkey repository](https://github.com/AutoHotkey/AutoHotkey).
Its official documentation repository demonstrates conditional hotkeys, client
versus screen coordinate modes, and control-addressed operations:
[AutoHotkeyDocs](https://github.com/AutoHotkey/AutoHotkeyDocs).

| Question | Assessment for ScreenBot |
|---|---|
| Suitable layer | Design reference for control-plane context, target naming, coordinate-mode explicitness |
| HWND boundary | Window/control criteria and HWND-oriented control functions can establish an address, but do not replace ScreenBot session/generation identity |
| Games/self-drawn UI | Control operations often do not apply; global input may still require foreground |
| Foreground/background | Some control operations can avoid activation; behavior is application-dependent |
| Background input | Not a general safety guarantee and must not be copied as a product promise |
| Main risk | Convenient implicit active-window and screen-coordinate modes conflict with ScreenBot's fail-closed rule |
| Migration cost | Low for conceptual borrowing; high and unjustified to embed/replace the current runtime |

Borrow: context-sensitive hotkeys, explicit client coordinates, explicit target
criteria. Do not borrow: implicit active-window fallback or global script
semantics.

### pywinauto

Original project and primary documentation:
[pywinauto repository](https://github.com/pywinauto/pywinauto) and
[backend guide](https://pywinauto.readthedocs.io/en/latest/getting_started.html).

| Question | Assessment for ScreenBot |
|---|---|
| Suitable layer | Standard desktop application backend |
| Backends | `win32` for legacy/native controls; `uia` for provider-exposed modern controls |
| HWND boundary | Can connect to a process/window/control; ScreenBot must still bind elements to the frozen root/session |
| Games/self-drawn UI | Usually unsuitable when no Win32/UIA control tree is exposed |
| Foreground | Semantic control patterns may not require foreground; physical `click_input`/typing can |
| Background input | Possible for some semantic controls, never universal |
| Main risk | Element lookup by title/process or `Desktop` scope can escape TargetSession if not rooted |
| Migration cost | Medium: backend adapter, capability probe, element-to-session validation, test matrix |

### SikuliX

Original project and documentation:
[SikuliX repository](https://github.com/oculix-org/SikuliX1) and
[Region documentation](https://docs.oculix.org/region/).

| Question | Assessment for ScreenBot |
|---|---|
| Suitable layer | Visual detection model and explicit search-region design |
| HWND boundary | Region itself is a screen rectangle and knows nothing about windows; ScreenBot must bind it to TargetSession |
| Games/self-drawn UI | Suitable for visual detection where accessibility trees are absent |
| Foreground | Visual input normally depends on visible pixels and target interaction state |
| Background input | Not established by image matching |
| Main risk | Default-screen regions and pixel coordinates become dangerous global semantics |
| Migration cost | Low for conceptual borrowing; high to adopt Java runtime/tooling |

Borrow the separation among image, search Region, Match, and action. ScreenBot's
Region must be target-client-relative, never an implicit desktop Region.

### Windows APIs

| Technology | Suitable ScreenBot layer | Establishes HWND boundary? | Game/background notes | Risk/cost |
|---|---|---|---|---|
| `RegisterHotKey` | F8/emergency control plane | No feature target; posts to ScreenBot | Works independently of target foreground; conflicts possible | Low/medium migration; retain current accepted manager for now |
| `WH_KEYBOARD_LL` / `WH_MOUSE_LL` | Recorder sensor | No; must add foreground/hit-test + session | Global; offers injected flags; callback must stay fast | Medium/high native lifecycle cost |
| Raw Input / `WM_INPUT` | Alternative physical sensor/device attribution | No | Can receive background with `RIDEV_INPUTSINK`; that is sensor scope, not product permission | Medium/high message-window and device-normalization cost |
| `WindowFromPoint` + `GetAncestor` | Mouse event boundary | Yes for point/root at event time | Works for visible window ownership, not hidden targets | Low; recommended S2-B primitive |
| `GetForegroundWindow` | Keyboard/output safety | Gives current foreground, then root must be resolved | Necessary for physical-style input | Low; already used |
| `SendInput` | Foreground-guarded game/self-drawn backend | No; global input stream | UIPI-limited; not guaranteed for games or protected targets | Medium; tagging and release ledger required |
| WGC `CreateForWindow` | Preferred target capture | Yes | Better fit for GPU/self-drawn content; OS/version/protected-content limits | Already implemented |
| `PrintWindow` | Target-window fallback | Yes | App-controlled rendering; can block or return unusable pixels | Already implemented |
| client-DC `BitBlt` | Last HWND compatibility fallback | DC is obtained from HWND | Occlusion/rendering behavior varies | Already implemented |
| Desktop Duplication | Monitor capture, not normal product path | No | Can capture visible full-screen DirectX at monitor scope | High boundary/privacy risk; only strict crop as exceptional compatibility |

Raw Input is documented as device data delivered through `WM_INPUT`; it can
receive in the background with `RIDEV_INPUTSINK`, but that does not identify the
application window to which the user intended an event. See
[Microsoft Raw Input overview](https://learn.microsoft.com/en-us/windows/win32/inputdev/about-raw-input)
and [RegisterRawInputDevices](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-registerrawinputdevices).

Desktop Duplication supplies monitor-level desktop frames, including full-screen
DirectX scenarios. It is therefore not a TargetSession boundary by itself. See
[Microsoft Desktop Duplication](https://learn.microsoft.com/en-us/windows/win32/direct3ddxgi/desktop-dup-api).

## Recommended ScreenBot architecture

```text
CONTROL PLANE
F8 / explicit Emergency Stop
→ accepted HotkeyManager
→ Qt HotkeyBridge
→ Application-owned lifecycle coordinator

RECORDER SENSOR PLANE
physical keyboard/mouse sensor
→ RawSensorEvent (timestamp, type, screen point, native flags)
→ TargetBoundaryFilter
→ AcceptedTargetEvent (frozen identity token, client ratio)
→ GestureTransaction
→ Macro schema

EXECUTION PLANE
Macro/Workflow action
→ frozen TargetAuthorization token
→ capability-selected backend
   ├─ UIA/Win32 control backend (standard controls)
   └─ foreground-guarded physical input backend (self-drawn/game)
→ authorize immediately before every emission
→ held-input ledger and fail-closed release

OBSERVATION PLANE
TargetAuthorization token
→ HWND WGC
→ PrintWindow
→ client-DC BitBlt
→ target-client Region
→ OCR/Trigger/Stop condition/Preview
```

### TargetBoundaryFilter contract

Input:

- frozen session_id and generation;
- frozen root HWND and client HWND;
- PID and process creation time;
- event timestamp/type;
- mouse screen point or keyboard foreground sample;
- native injected/provenance flags when available.

Mouse acceptance:

1. refresh/revalidate the expected TargetSession;
2. reject injected events unless explicitly generated by an authorized test
   mode;
3. `WindowFromPoint(event.point)`;
4. normalize with `GetAncestor(..., GA_ROOT)`;
5. require frozen root HWND equality;
6. require point to convert into the frozen client;
7. emit only client-relative/normalized coordinates.

Keyboard acceptance:

1. refresh/revalidate expected TargetSession;
2. reject control-plane keys and injected events;
3. normalize `GetForegroundWindow` to root;
4. require frozen root HWND equality;
5. maintain balanced key transactions.

Invalidation:

- atomically stop accepting new events;
- discard incomplete mouse/key transactions or close them only with an
  explicitly marked synthetic release policy;
- request Recorder stop through its owner;
- never relock or consult another foreground window.

### Backend policy

- Keep the accepted HotkeyManager unchanged in S2-B.
- Prefer semantic UIA/Win32 control actions for provider-exposed standard apps.
- Use foreground-guarded physical-style injection for self-drawn/game targets.
- Keep WGC-by-HWND as preferred capture.
- Keep PrintWindow and client-DC BitBlt as target-HWND fallbacks.
- Do not add Desktop Duplication unless a later compatibility decision accepts
  monitor-scope privacy/race risks and mandates immutable crop metadata.
- Never start Background Input merely because a backend can address a control.

## Migration phases

### S2-B1 — Recorder TargetBoundaryFilter

- Add a read-only frozen target authorization value.
- Inject TargetSession and WindowTracker dependencies into Recorder.
- Filter keyboard and mouse events before normalization.
- Add root hit testing, control-key exclusion, invalidation, and gesture
  transactions.
- Preserve passive `suppress=False`.
- Do not alter playback or capture backends.

### S2-B2 — Recorder injected-event provenance

- Determine whether `pynput` exposes required Windows injected flags.
- If not, evaluate a minimal owned `WH_KEYBOARD_LL`/`WH_MOUSE_LL` sensor or Raw
  Input adapter.
- Add a test-only injection tag and reject recursion in product mode.

### S2-C — Frozen authorization across all observers

- Pass the explicit ExpectedTargetSession to direct Script start, standalone
  TriggerRunner, OCR, Preview, and capture requests.
- Remove product compatibility paths that derive a new current session.
- Stop standalone TriggerRunner before TargetSession clear.

### S2-D — Backend abstraction

- Define semantic standard-control backend and foreground physical-input
  backend.
- Capability-probe UIA/Win32 without escaping the frozen target root.
- Keep background input paused until a separate threat model and acceptance
  contract exists.

### S2-E — Emergency stop

- Developer chooses the physical key/chord and whether it bypasses pause.
- Implement a single Application owner that blocks new input, requests all
  active owners to stop, releases held input, and emits one terminal outcome.
- Keep it outside Macro data and independent of TargetSession availability.

## Proposed S2-B file scope

First implementation target: Recorder TargetSession boundary, without changing
TargetSession core or input playback.

Proposed production files:

- new `app/target_boundary.py`: frozen token, mouse/keyboard authorization
  result, injectable Win32 boundary adapter;
- `app/recorder.py`: consume only accepted events, gesture transactions,
  invalidation stop;
- `app/application.py`: inject TargetSession/filter and coordinate Recorder
  lifecycle;
- optionally `app/window_tracker.py` only if a read-only `window_from_point`
  adapter cannot be isolated in the new module. No identity-rule changes.

Proposed tests:

- new `tools/test_target_boundary_filter.py`;
- new or expanded `tools/test_recorder_target_scope.py`;
- existing `tools/test_passive_input_recorder.py`;
- existing `tools/test_target_session.py`;
- existing `tools/test_f8_target_toggle.py`;
- existing `tools/test_input_safety.py`.

Required coverage:

- recording requires a strong attached TargetSession;
- frozen session_id/generation/root/client identity;
- target mouse down/up/scroll accepted;
- ScreenBot UI, overlay, other window, desktop, taskbar rejected;
- keyboard only when frozen target root is foreground;
- F8, pause, exit, and selected emergency key rejected;
- injected events rejected;
- cross-boundary gesture discarded;
- no orphan key/button transaction;
- coordinates remain target-client-relative;
- target invalidation stops acceptance and Recorder;
- stale generation cannot authorize a new session;
- shutdown removes both listeners and drains/terminates worker;
- no regression to F8 lock/unlock.

## Risks requiring developer decision

1. Define the dedicated emergency-stop key/chord and whether ESC remains
   application exit.
2. Decide whether target invalidation should silently finish and retain accepted
   Recorder data, or mark the recording invalid and require explicit recovery.
3. Decide how synthetic key-up records are represented on invalidation.
4. Approve a native low-level hook only if `pynput` cannot expose injected
   provenance.
5. Decide whether Raw Input device identity is useful enough to justify a
   ScreenBot-owned message window.
6. Define overlay ownership policy when an overlay intentionally covers the
   target client: default recommendation is reject all ScreenBot PID/root HWNDs.
7. Decide whether standard-app UIA actions may run while the target is
   background. Default recommendation for S2 is **no** until separately
   threat-modeled and accepted.
8. Define supported games and anti-cheat exclusions before promising a
   physical-input backend.
9. Decide whether Desktop Duplication is prohibited outright or allowed only as
   an opt-in, visibly disclosed, strictly cropped diagnostic fallback.

## Acceptance plan

### Automated

1. Construct two TargetSessions with different generations and prove stale
   events are rejected.
2. Simulate `WindowFromPoint` returning target child, ScreenBot, overlay, other
   app, desktop, and taskbar roots.
3. Verify complete mouse gesture commit and cross-boundary rollback.
4. Verify keyboard foreground-root acceptance and background rejection.
5. Verify injected-event and control-plane-key rejection.
6. Invalidate TargetSession during each open transaction and prove no further
   event enters Macro data.
7. Assert listener and worker shutdown.
8. Re-run TargetSession, F8, input safety, passive Recorder, Workflow, capture,
   and OCR focused suites.

### Developer interactive

1. Lock a standard desktop target, record target-only keyboard/mouse input, and
   stop through ScreenBot; verify the stop click is absent.
2. Place ScreenBot and its overlay over the target client; verify clicks on them
   are absent.
3. Use another app, desktop, and taskbar during recording; verify all are absent.
4. Hold a key or drag, cross the target boundary, and invalidate the session;
   verify no orphan transaction is stored.
5. Run playback while global sensors are instrumented; verify injected events
   are not recorded.
6. Verify F8 and the selected emergency key never enter Macro data.
7. Verify no operation retargets the current foreground window after
   invalidation.

Passing this audit means only that the current architecture and migration risks
have been documented. It does not mean the S2-B boundary implementation exists.
