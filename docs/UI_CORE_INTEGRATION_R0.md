# UI/Core Integration R0

## Original UI reference

- Authoritative UI/UX source: `F:\ScreenBot`
- Primary goal for R0: restore the original user experience for clean shutdown and trigger authoring without replacing the Dev execution core.

## Dev core reference

- ScreenBot Dev remains the authoritative execution core for:
  - TargetSession identity and generation
  - overlay ownership and window stacking safeguards
  - foreground handoff and the foreground input gate
  - F8 semantics and start-collapse acknowledgement
  - workflow execution, resolver, and runner wiring

## Feature mapping

### R0a — Clean shutdown recovery

- Exit menu and window-close entry points now converge on a single shutdown coordinator.
- The shutdown path cancels any armed start request, stops workflow execution, stops text-trigger execution, stops the recorder and player, stops timers, unregisters hotkeys, closes UI windows, and exits the Qt loop.
- Shutdown is idempotent so repeated exit requests do not re-enter cleanup.

### R0b — Trigger creation UI recovery

- Trigger Manager now opens the richer OCR wizard for both create and edit flows.
- Existing trigger data is preloaded into the wizard so editing preserves the current schema and region configuration.
- Save operations round-trip through the existing Dev TriggerStore validation path.

## Protected boundaries

The following remain protected and were not reimplemented from Legacy:

- TargetSession / overlay ownership / Z-order / topmost / flashing safeguards
- Foreground handoff and the foreground input gate
- F8 semantics
- Start-collapse acknowledgement
- Workflow runner and resolver integration

## Clean shutdown lifecycle

1. User requests exit from the menu or by closing the main window.
2. ScreenBot requests a single shutdown path.
3. Any armed start request is cancelled.
4. Workflow and trigger runners are stopped.
5. Timers, hotkeys, workers, overlay, and UI windows are released.
6. Qt exits and the process terminates cleanly.

## Trigger UI flow

- Open Trigger Manager
- Create a new OCR trigger or edit an existing one
- Capture/select an OCR region
- Run OCR and select recognition candidates
- Save the trigger
- Reload or edit the same trigger
- Reference it from the workflow editor via the existing trigger store

## Developer interaction checklist

- Confirm that closing the app from the exit menu removes the UI and process cleanly.
- Confirm that creating and editing a trigger succeeds and the data reloads.
- Confirm that workflow-editor trigger references remain intact after reload.

## Known incomplete fields

- Background input foundation remains paused and is not part of this R0 scope.
- No formal live-acceptance execution was performed as part of this implementation pass.

## Performance observations

- No new polling loop, topmost loop, or foreground loop was introduced.
- The shutdown path remains single-entry and idempotent.
- The trigger UI uses the existing Dev store/validation stack rather than replacing it.
