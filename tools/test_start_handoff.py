"""Focused regression coverage for START_ARMED request ownership."""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
import sys
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.start_handoff import (
    StartHandoffCode,
    StartHandoffService,
    StartHandoffState,
    StartRequestKind,
)
from app.application import ScreenBotApp
from app.input_safety import (
    ExpectedTargetSession,
    InputAuthorizationCode,
    InputAuthorizationResult,
)
from app.state import AppState


class _DeferredStarter:
    """Models the application commit boundary without creating real workers."""

    def __init__(self, service):
        self.service = service
        self.direct_starts = 0
        self.workflow_starts = 0

    def commit_if_ready(self, ready):
        snapshot = self.service.get_snapshot(now=1.0)
        if not ready or snapshot.request is None:
            return False
        claim = self.service.claim_commit(snapshot.request.request_id, now=1.0)
        if claim.code is not StartHandoffCode.CLAIMED:
            return False
        if snapshot.request.kind is StartRequestKind.DIRECT_SCRIPT:
            self.direct_starts += 1
        else:
            self.workflow_starts += 1
        self.service.complete_started(snapshot.request.request_id)
        return True


class _Timer:
    def __init__(self):
        self.starts = 0
        self.stops = 0

    def start(self):
        self.starts += 1

    def stop(self):
        self.stops += 1


class _Gate:
    def __init__(self, target):
        self.target = target
        self.authorizations = 0
        self.allowed = True

    def expected_current_session(self):
        return ExpectedTargetSession(self.target.session_id, self.target.generation)

    def authorize_foreground_input_fast(self, expected):
        return self._result(expected)

    def authorize_foreground_input(self, expected):
        self.authorizations += 1
        return self._result(expected)

    def _result(self, expected):
        if expected.session_id != self.target.session_id or expected.generation != self.target.generation:
            return InputAuthorizationResult(InputAuthorizationCode.TARGET_CHANGED, self.target)
        if not self.allowed:
            return InputAuthorizationResult(
                InputAuthorizationCode.TARGET_NOT_FOREGROUND,
                self.target,
                foreground_hwnd=999,
                foreground_root_hwnd=999,
            )
        return InputAuthorizationResult(
            InputAuthorizationCode.ALLOWED,
            self.target,
            foreground_hwnd=self.target.root_hwnd,
            foreground_root_hwnd=self.target.root_hwnd,
        )


class _Player:
    def __init__(self):
        self.starts = []

    def start(self, script):
        self.starts.append(script)
        return SimpleNamespace(
            status=SimpleNamespace(name="STARTED", value="started"),
            reason=None,
        )


class _WorkflowRunner:
    def __init__(self):
        self.starts = 0
        self.loads = []

    def is_active(self):
        return False

    def load_workflow(self, workflow):
        self.loads.append(workflow)

    def start(self):
        self.starts += 1


class StartHandoffTests(unittest.TestCase):
    def setUp(self):
        self.service = StartHandoffService(timeout_seconds=15)

    def _request(self, kind=StartRequestKind.DIRECT_SCRIPT, payload=None, now=0.0):
        return self.service.create_request(
            kind=kind,
            target_session_id="target-session",
            target_generation=7,
            display_name="Frozen workflow" if kind is StartRequestKind.WORKFLOW else "Frozen script",
            frozen_payload=payload if payload is not None else {"actions": [{"key": "a"}]},
            now=now,
        )

    def test_arm_stores_frozen_target_and_payload(self):
        payload = {"actions": [{"key": "a"}]}
        request = self._request(payload=payload)
        payload["actions"][0]["key"] = "b"

        result = self.service.arm(request)

        self.assertEqual(result.code, StartHandoffCode.ARMED)
        snapshot = result.snapshot
        self.assertEqual(snapshot.state, StartHandoffState.ARMED)
        self.assertEqual(snapshot.request.target_session_id, "target-session")
        self.assertEqual(snapshot.request.target_generation, 7)
        self.assertEqual(snapshot.request.frozen_payload["actions"][0]["key"], "a")

    def test_arm_while_armed_preserves_original_request(self):
        first = self._request(payload={"name": "A"})
        second = self._request(payload={"name": "B"})
        self.service.arm(first)

        result = self.service.arm(second)

        self.assertEqual(result.code, StartHandoffCode.ALREADY_ARMED)
        self.assertEqual(result.snapshot.request.request_id, first.request_id)
        self.assertEqual(result.snapshot.request.frozen_payload, {"name": "A"})

    def test_not_ready_creates_no_direct_or_workflow_start(self):
        self.service.arm(self._request())
        starter = _DeferredStarter(self.service)

        self.assertFalse(starter.commit_if_ready(ready=False))
        self.assertEqual(starter.direct_starts, 0)
        self.assertEqual(starter.workflow_starts, 0)
        self.assertEqual(self.service.get_snapshot(now=1.0).state, StartHandoffState.ARMED)

    def test_direct_commit_starts_once(self):
        self.service.arm(self._request())
        starter = _DeferredStarter(self.service)

        self.assertTrue(starter.commit_if_ready(ready=True))
        self.assertFalse(starter.commit_if_ready(ready=True))
        self.assertEqual(starter.direct_starts, 1)
        self.assertEqual(starter.workflow_starts, 0)
        self.assertEqual(self.service.get_snapshot().state, StartHandoffState.STARTED)

    def test_workflow_commit_starts_only_after_claim(self):
        self.service.arm(self._request(StartRequestKind.WORKFLOW, {"steps": [{"id": "step-a"}]}))
        starter = _DeferredStarter(self.service)

        self.assertEqual(starter.workflow_starts, 0)
        self.assertTrue(starter.commit_if_ready(ready=True))
        self.assertEqual(starter.workflow_starts, 1)
        self.assertEqual(starter.direct_starts, 0)

    def test_cancel_prevents_later_commit(self):
        request = self._request()
        self.service.arm(request)

        result = self.service.cancel("start_request_cancelled")

        self.assertEqual(result.code, StartHandoffCode.CANCELLED)
        self.assertEqual(result.snapshot.state, StartHandoffState.CANCELLED)
        self.assertIsNone(result.snapshot.request)
        self.assertEqual(
            self.service.claim_commit(request.request_id).code,
            StartHandoffCode.NOT_ARMED,
        )

    def test_timeout_prevents_later_commit(self):
        request = self._request(now=10.0)
        self.service.arm(request)

        result = self.service.expire_if_due(now=25.0)

        self.assertEqual(result.code, StartHandoffCode.TIMED_OUT)
        self.assertEqual(result.snapshot.state, StartHandoffState.TIMED_OUT)
        self.assertEqual(
            self.service.claim_commit(request.request_id, now=25.0).code,
            StartHandoffCode.NOT_ARMED,
        )

    def test_target_change_and_unavailable_are_terminal(self):
        request = self._request()
        self.service.arm(request)
        changed = self.service.invalidate_target("target_changed")
        self.assertEqual(changed.snapshot.state, StartHandoffState.TARGET_CHANGED)

        self.service.arm(self._request())
        unavailable = self.service.invalidate_target("TARGET_MINIMIZED", unavailable=True)
        self.assertEqual(unavailable.snapshot.state, StartHandoffState.TARGET_UNAVAILABLE)
        self.assertEqual(unavailable.snapshot.reason, "TARGET_MINIMIZED")

    def test_ready_cancel_race_has_one_owner(self):
        request = self._request()
        self.service.arm(request)
        barrier = threading.Barrier(3)
        results = []

        def claim():
            barrier.wait()
            results.append(self.service.claim_commit(request.request_id, now=1.0).code)

        def cancel():
            barrier.wait()
            results.append(self.service.cancel("start_request_cancelled").code)

        claim_thread = threading.Thread(target=claim)
        cancel_thread = threading.Thread(target=cancel)
        claim_thread.start()
        cancel_thread.start()
        barrier.wait()
        claim_thread.join()
        cancel_thread.join()

        self.assertEqual(len(results), 2)
        self.assertIn(
            self.service.get_snapshot().state,
            {StartHandoffState.COMMITTING, StartHandoffState.CANCELLED},
        )
        self.assertNotIn(StartHandoffCode.ARMED, results)

    def test_ready_timeout_race_has_one_owner(self):
        request = self._request(now=0.0)
        self.service.arm(request)
        barrier = threading.Barrier(3)
        results = []

        def claim():
            barrier.wait()
            results.append(self.service.claim_commit(request.request_id, now=15.0).code)

        def expire():
            barrier.wait()
            results.append(self.service.expire_if_due(now=15.0).code)

        claim_thread = threading.Thread(target=claim)
        expire_thread = threading.Thread(target=expire)
        claim_thread.start()
        expire_thread.start()
        barrier.wait()
        claim_thread.join()
        expire_thread.join()

        self.assertEqual(self.service.get_snapshot().state, StartHandoffState.TIMED_OUT)
        self.assertIn(StartHandoffCode.TIMED_OUT, results)

    def test_failed_commit_does_not_retry(self):
        request = self._request()
        self.service.arm(request)
        self.assertEqual(
            self.service.claim_commit(request.request_id, now=1.0).code,
            StartHandoffCode.CLAIMED,
        )

        result = self.service.complete_failed(request.request_id, "TARGET_NOT_FOREGROUND")

        self.assertEqual(result.snapshot.state, StartHandoffState.FAILED)
        self.assertEqual(result.snapshot.reason, "TARGET_NOT_FOREGROUND")
        self.assertEqual(
            self.service.claim_commit(request.request_id, now=2.0).code,
            StartHandoffCode.NOT_ARMED,
        )

    def _application(self):
        target = SimpleNamespace(
            session_id="target-session",
            generation=7,
            root_hwnd=100,
            title="Target A",
        )
        app = ScreenBotApp.__new__(ScreenBotApp)
        app.start_handoff = StartHandoffService(timeout_seconds=15)
        app.start_handoff_timer = _Timer()
        app.target_session = SimpleNamespace(refresh=lambda: target)
        app.input_safety_gate = _Gate(target)
        app.player = _Player()
        app.workflow_runner = _WorkflowRunner()
        app.workflow_diagnostics = SimpleNamespace(
            record_start_handoff_event=lambda *_: None,
            mark_workflow_started=lambda *_args, **_kwargs: None,
        )
        app.text_detector = SimpleNamespace(
            get_capture_runtime_diagnostics=lambda: {},
        )
        app.window_tracker = SimpleNamespace(target=SimpleNamespace(title="Target A"))
        collapsed = {"value": False}
        app.widget = SimpleNamespace(
            set_workflow_running=lambda *_: None,
            collapse_for_workflow_execution=lambda: collapsed.__setitem__("value", True) or True,
            is_workflow_execution_collapsed=lambda: collapsed["value"],
            restore_after_workflow_execution=lambda: collapsed.__setitem__("value", False) or True,
        )
        app.logger = SimpleNamespace(info=lambda *_args, **_kwargs: None, error=lambda *_args, **_kwargs: None)
        app.state = AppState.IDLE
        app.workflow_data = None
        app.workflow_ui_state = "IDLE"
        app.workflow_execution_ui_state = "IDLE_EXPANDED"
        app.stop_text_trigger = lambda: None
        app._update_workflow_runtime_ui = lambda: None
        app._refresh_handoff_presentation = lambda: None
        app.set_state = lambda state: setattr(app, "state", state)
        app._record_start_handoff_event = lambda *_args, **_kwargs: None
        app._record_overlay_diagnostic = lambda *_args, **_kwargs: None
        return app

    def test_application_direct_path_commits_in_the_start_click(self):
        app = self._application()
        script = {"actions": [{"key": "a"}]}

        self.assertTrue(app._arm_start_request(StartRequestKind.DIRECT_SCRIPT, script, "Script A"))
        script["actions"][0]["key"] = "b"

        self.assertEqual(len(app.player.starts), 1)
        self.assertEqual(app.player.starts[0]["actions"][0]["key"], "a")
        self.assertEqual(app.start_handoff.get_snapshot().state, StartHandoffState.STARTED)
        self.assertEqual(app.input_safety_gate.authorizations, 2)

    def test_application_workflow_path_commits_in_the_start_click(self):
        app = self._application()
        workflow = {"name": "Workflow A", "steps": [{"id": "step-a"}]}

        self.assertTrue(app._arm_start_request(StartRequestKind.WORKFLOW, workflow, "Workflow A"))
        self.assertEqual(app.workflow_runner.loads, [workflow])
        self.assertEqual(app.workflow_runner.starts, 1)
        self.assertEqual(app.start_handoff.get_snapshot().state, StartHandoffState.STARTED)

    def test_background_start_remains_armed_until_foreground_recovers(self):
        app = self._application()
        app.input_safety_gate.allowed = False

        self.assertTrue(app._arm_start_request(
            StartRequestKind.DIRECT_SCRIPT, {"actions": []}, "Script A"
        ))

        self.assertEqual(app.player.starts, [])
        self.assertEqual(app.start_handoff.get_snapshot().state, StartHandoffState.ARMED)
        app.input_safety_gate.allowed = True
        app._on_start_handoff_tick()
        self.assertEqual(len(app.player.starts), 1)
        self.assertEqual(app.start_handoff.get_snapshot().state, StartHandoffState.STARTED)


if __name__ == "__main__":
    unittest.main(verbosity=2)
