# ScreenBot Dev instructions

- Work only in `F:\ScreenBot_dev` on `dev/screenbot-next`.
- Official repository is `https://github.com/welcome44369/ScreenBot-Dev.git`;
  push only to `origin`.
- `screenbot-readonly` is historical reference. Never push to it or develop in
  Stable/Legacy worktrees.
- Read `docs/GITHUB_COPILOT_HANDOFF.md` before modifying code.
- Protect TargetSession, overlay ownership, Z-order/topmost/flashing fixes,
  foreground handoff, F8 semantics, and the foreground input gate.
- Start requires UI-collapse acknowledgement before workflow execution. F8 is
  lock/unlock only; it never starts a workflow.
- Use temporary Runtime plus Recording Input Backend for autonomous tests.
  Never use formal A15 Runtime in `all-non-input` and never commit Runtime JSON.
- Do not claim background input while global foreground-gated input is used.
- Stage exact files only. Do not use `git add .`, reset, restore, clean, rebase,
  force push, or push to `screenbot-readonly`.
- After Workflow, Trigger, ScriptPlayer, start-handoff, or UI-collapse changes,
  run `python tools/run_dev_acceptance.py all-non-input`.
- Automated PASS is not Controlled Live Acceptance PASS. Stop and report if a
  task needs a protected-boundary change.
