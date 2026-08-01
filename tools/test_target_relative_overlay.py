"""Focused tests for target-relative compact overlay coordination."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import logging
from types import SimpleNamespace
import unittest

from app.application import ScreenBotApp
from app.target_relative_overlay import (
    EVENT_OBJECT_DESTROY,
    EVENT_OBJECT_LOCATIONCHANGE,
    EVENT_OBJECT_REORDER,
    EVENT_SYSTEM_FOREGROUND,
    EVENT_SYSTEM_MINIMIZEEND,
    EVENT_SYSTEM_MINIMIZESTART,
    HWND_NOTOPMOST,
    HWND_TOP,
    HWND_TOPMOST,
    SWP_NOACTIVATE,
    SWP_NOZORDER,
    TargetRelativeOverlayCoordinator,
)
from app.target_session import (
    TargetConnectionState,
    TargetSnapshot,
    TargetVisibilityState,
)


OVERLAY = 100
TARGET = 200
OTHER = 300
TOPMOST_OTHER = 400


class FakeSignal:
    def __init__(self):
        self.callbacks = []

    def connect(self, callback):
        self.callbacks.append(callback)

    def emit(self, *args):
        for callback in tuple(self.callbacks):
            callback(*args)


class FakeCoordinatorDiagnostics:
    enabled = True

    def __init__(self):
        self.events = []

    def observe_coordinator_event(self, event, **data):
        self.events.append((event, data))


class FakeTimer:
    def __init__(self, single_shot):
        self.single_shot = single_shot
        self.interval = None
        self.active = False
        self.start_count = 0
        self.stop_count = 0
        self.timeout = FakeSignal()

    def setInterval(self, interval):
        self.interval = interval

    def start(self):
        self.active = True
        self.start_count += 1

    def stop(self):
        self.active = False
        self.stop_count += 1

    def isActive(self):
        return self.active

    def fire(self):
        if not self.active:
            return
        if self.single_shot:
            self.active = False
        self.timeout.emit()


class FakeWidget:
    def __init__(self, hwnd=OVERLAY, diagnostics=None):
        self.hwnd = hwnd
        self._overlay_diagnostics = diagnostics
        self.compact_hwnd_changed = FakeSignal()
        self.compact_geometry_changed = FakeSignal()
        self.compact_visibility_intent_changed = FakeSignal()
        self.suppression = []
        self.visibility_intent = True

    def winId(self):
        return self.hwnd

    def is_compact_visibility_intended(self):
        return self.visibility_intent

    def set_target_visibility_suppressed(self, suppressed):
        self.suppression.append(bool(suppressed))


class FakeTargetSession:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.listeners = []
        self.refresh_count = 0

    def subscribe(self, listener):
        self.listeners.append(listener)

    def unsubscribe(self, listener):
        if listener in self.listeners:
            self.listeners.remove(listener)

    def get_snapshot(self):
        return self.snapshot

    def refresh(self):
        self.refresh_count += 1
        return self.snapshot

    def publish(self, event, snapshot=None):
        if snapshot is not None:
            self.snapshot = snapshot
        for listener in tuple(self.listeners):
            listener(event, self.snapshot, {})


class FakeWin32Adapter:
    def __init__(self):
        self.valid = {OVERLAY, TARGET, OTHER, TOPMOST_OTHER}
        self.rects = {
            OVERLAY: (130, 150, 550, 240),
            TARGET: (100, 100, 900, 700),
            OTHER: (0, 0, 800, 600),
            TOPMOST_OTHER: (0, 0, 500, 500),
        }
        self.topmost = {TOPMOST_OTHER}
        self.iconic = set()
        self.hidden = set()
        self.order = [TOPMOST_OTHER, OTHER, OVERLAY, TARGET]
        self.calls = []
        self.pair_calls = []
        self.pair_reorder_succeeds = True
        self.foreground = OTHER
        self.hook_callback = None
        self.unhooked = None

    def is_window(self, hwnd):
        return int(hwnd or 0) in self.valid

    def window_rect(self, hwnd):
        return self.rects.get(int(hwnd or 0))

    def root_hwnd(self, hwnd):
        return int(hwnd or 0) if self.is_window(hwnd) else 0

    def is_iconic(self, hwnd):
        return int(hwnd or 0) in self.iconic

    def is_visible(self, hwnd):
        return self.is_window(hwnd) and int(hwnd or 0) not in self.hidden

    def is_topmost(self, hwnd):
        return int(hwnd or 0) in self.topmost

    def z_order_top_to_bottom(self):
        return list(self.order)

    def set_window_pos(self, hwnd, insert_after, x, y, width, height, flags):
        hwnd = int(hwnd)
        insert_after = int(insert_after)
        self.calls.append(
            {
                "hwnd": hwnd,
                "insert_after": insert_after,
                "x": int(x),
                "y": int(y),
                "width": int(width),
                "height": int(height),
                "flags": int(flags),
            }
        )
        if not flags & SWP_NOACTIVATE:
            return False
        if not flags & SWP_NOZORDER:
            if insert_after == HWND_TOPMOST:
                self.topmost.add(hwnd)
                self._place_at_top_of_band(hwnd, True)
            elif insert_after == HWND_NOTOPMOST:
                self.topmost.discard(hwnd)
                self._place_at_top_of_band(hwnd, False)
            elif insert_after == HWND_TOP:
                self._place_at_top_of_band(hwnd, self.is_topmost(hwnd))
            elif insert_after in self.order:
                self.order.remove(hwnd)
                self.order.insert(self.order.index(insert_after) + 1, hwnd)
        if not flags & 0x0002:
            self.rects[hwnd] = (
                int(x),
                int(y),
                int(x + width),
                int(y + height),
            )
        return True

    def reorder_target_pair(self, overlay_hwnd, target_hwnd, flags):
        overlay_hwnd = int(overlay_hwnd)
        target_hwnd = int(target_hwnd)
        self.pair_calls.append(
            {
                "overlay_hwnd": overlay_hwnd,
                "target_hwnd": target_hwnd,
                "flags": int(flags),
            }
        )
        if (
            not self.pair_reorder_succeeds
            or not flags & SWP_NOACTIVATE
            or self.is_topmost(overlay_hwnd)
            or self.is_topmost(target_hwnd)
        ):
            return False
        if not self.set_window_pos(
            overlay_hwnd, target_hwnd, 0, 0, 0, 0, flags
        ):
            return False
        return self.set_window_pos(
            target_hwnd, overlay_hwnd, 0, 0, 0, 0, flags
        )

    def foreground_hwnd(self):
        return self.foreground

    def _place_at_top_of_band(self, hwnd, topmost):
        if hwnd in self.order:
            self.order.remove(hwnd)
        if topmost:
            self.order.insert(0, hwnd)
            return
        index = 0
        while index < len(self.order) and self.is_topmost(self.order[index]):
            index += 1
        self.order.insert(index, hwnd)

    def show_no_activate(self, hwnd):
        return self.is_window(hwnd)

    def hide(self, hwnd):
        return self.is_window(hwnd)

    def install_win_event_hook(self, callback):
        self.hook_callback = callback
        return ("hook",)

    def uninstall_win_event_hook(self, handles):
        self.unhooked = handles
        self.hook_callback = None


def make_snapshot(
    *,
    session_id="session-a",
    generation=1,
    root_hwnd=TARGET,
    visibility=TargetVisibilityState.FOREGROUND,
    connection=TargetConnectionState.ATTACHED,
    identity_valid=True,
    origin=(100, 100),
):
    return TargetSnapshot(
        session_id=session_id,
        generation=generation,
        hwnd=root_hwnd,
        root_hwnd=root_hwnd,
        client_hwnd=root_hwnd,
        pid=1234,
        process_creation_time=1,
        executable_path="target.exe",
        process_name="target.exe",
        window_class="TargetWindow",
        window_title="Safe Target",
        window_rect=(origin[0], origin[1], origin[0] + 800, origin[1] + 600),
        client_screen_origin=origin,
        initial_client_size=(800, 600),
        current_client_size=(800, 600),
        dpi=96,
        locked_at=1.0,
        last_validated_at=2.0,
        connection_state=connection,
        visibility_state=visibility,
        identity_valid=identity_valid,
        disconnect_reason=None,
        identity_strength="strong",
        identity_degraded_reason=None,
    )


class CoordinatorHarness:
    def __init__(self, snapshot=None, diagnostics=None):
        self.snapshot = snapshot or make_snapshot()
        self.target_session = FakeTargetSession(self.snapshot)
        self.widget = FakeWidget(diagnostics=diagnostics)
        self.adapter = FakeWin32Adapter()
        self.timers = []

        def timer_factory(single_shot):
            timer = FakeTimer(single_shot)
            self.timers.append(timer)
            return timer

        self.coordinator = TargetRelativeOverlayCoordinator(
            self.target_session,
            self.widget,
            adapter=self.adapter,
            timer_factory=timer_factory,
            debounce_ms=60,
            healing_interval_ms=750,
        )

    @property
    def debounce(self):
        return self.timers[0]

    @property
    def healing(self):
        return self.timers[1]

    def start_and_reconcile(self):
        self.coordinator.start()
        self.debounce.fire()


class TargetRelativeOverlayTests(unittest.TestCase):
    def test_compact_window_no_longer_requests_global_topmost(self):
        source = (
            Path(__file__).resolve().parents[1] / "app" / "floating_widget.py"
        ).read_text(encoding="utf-8-sig")
        constructor = source[source.index("class FloatingWidget"):source.index("def _diagnostics_enabled")]
        self.assertNotIn("WindowStaysOnTopHint", constructor)
        self.assertIn("WA_ShowWithoutActivating", constructor)

    def test_non_topmost_target_places_overlay_directly_above_target(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        self.assertNotIn(OVERLAY, harness.adapter.topmost)
        self.assertEqual(
            harness.adapter.order.index(OVERLAY) + 1,
            harness.adapter.order.index(TARGET),
        )
        self.assertLess(
            harness.adapter.order.index(OTHER),
            harness.adapter.order.index(OVERLAY),
        )
        self.assertTrue(
            all(call["flags"] & SWP_NOACTIVATE for call in harness.adapter.calls)
        )

    def test_enabled_lifecycle_diagnostics_do_not_interrupt_reconcile(self):
        diagnostics = FakeCoordinatorDiagnostics()
        harness = CoordinatorHarness(diagnostics=diagnostics)
        harness.start_and_reconcile()

        events = [event for event, _data in diagnostics.events]
        self.assertIn("COORDINATOR_BIND", events)
        self.assertIn("COORDINATOR_VISIBILITY_DECISION", events)
        self.assertFalse(harness.widget.suppression[-1])
        self.assertEqual(
            harness.adapter.order.index(OVERLAY) + 1,
            harness.adapter.order.index(TARGET),
        )

    def test_target_at_top_of_normal_band_uses_atomic_pair_reorder(self):
        harness = CoordinatorHarness()
        harness.adapter.order = [TOPMOST_OTHER, TARGET, OVERLAY, OTHER]
        harness.adapter.foreground = TARGET
        harness.start_and_reconcile()
        z_calls = [
            call for call in harness.adapter.calls if not call["flags"] & SWP_NOZORDER
        ]
        self.assertFalse(
            any(
                call["hwnd"] == OVERLAY and call["insert_after"] == HWND_TOP
                for call in z_calls
            )
        )
        self.assertEqual(1, len(harness.adapter.pair_calls))
        self.assertTrue(
            harness.adapter.pair_calls[0]["flags"] & SWP_NOACTIVATE
        )
        pair_native_calls = [
            call
            for call in z_calls
            if call["hwnd"] in {OVERLAY, TARGET}
        ]
        self.assertEqual(
            [(OVERLAY, TARGET), (TARGET, OVERLAY)],
            [
                (call["hwnd"], call["insert_after"])
                for call in pair_native_calls
            ],
        )
        self.assertEqual([TOPMOST_OTHER, OVERLAY, TARGET], harness.adapter.order[:3])
        self.assertEqual(TARGET, harness.adapter.foreground)

    def test_real_predecessor_path_does_not_use_pair_reorder(self):
        harness = CoordinatorHarness()
        harness.adapter.order = [TOPMOST_OTHER, OTHER, TARGET, OVERLAY]
        harness.start_and_reconcile()
        self.assertEqual([], harness.adapter.pair_calls)
        z_calls = [
            call for call in harness.adapter.calls if not call["flags"] & SWP_NOZORDER
        ]
        self.assertEqual(OTHER, z_calls[-1]["insert_after"])

    def test_pair_reorder_failure_preserves_binding_and_visibility(self):
        harness = CoordinatorHarness()
        harness.adapter.order = [TOPMOST_OTHER, TARGET, OVERLAY, OTHER]
        harness.adapter.foreground = TARGET
        harness.adapter.pair_reorder_succeeds = False
        harness.start_and_reconcile()
        self.assertEqual(("session-a", 1, TARGET), harness.coordinator._binding)
        self.assertTrue(harness.coordinator._effective_visible)
        self.assertEqual([TOPMOST_OTHER, TARGET, OVERLAY, OTHER], harness.adapter.order)

    def test_settled_top_pair_does_not_repeat_native_mutation(self):
        harness = CoordinatorHarness()
        harness.adapter.order = [TOPMOST_OTHER, TARGET, OVERLAY, OTHER]
        harness.adapter.foreground = TARGET
        harness.start_and_reconcile()
        self.assertEqual(1, len(harness.adapter.pair_calls))
        harness.adapter.calls.clear()
        harness.adapter.pair_calls.clear()
        harness.healing.fire()
        self.assertEqual([], harness.adapter.calls)
        self.assertEqual([], harness.adapter.pair_calls)

    def test_topmost_target_never_promotes_overlay(self):
        harness = CoordinatorHarness()
        harness.adapter.topmost.add(TARGET)
        harness.adapter.order = [TOPMOST_OTHER, OTHER, OVERLAY, TARGET]
        harness.start_and_reconcile()
        self.assertNotIn(OVERLAY, harness.adapter.topmost)
        self.assertNotIn(
            HWND_TOPMOST,
            [call["insert_after"] for call in harness.adapter.calls],
        )

    def test_geometry_preserves_client_relative_offset_across_moves(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        harness.target_session.snapshot = replace(
            harness.snapshot, client_screen_origin=(-900, 240)
        )
        harness.coordinator.reconcile(1)
        self.assertEqual((-870, 290, -450, 380), harness.adapter.rects[OVERLAY])

    def test_minimize_hides_and_restore_shows_without_changing_intent(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        minimized = replace(
            harness.snapshot, visibility_state=TargetVisibilityState.MINIMIZED
        )
        harness.target_session.snapshot = minimized
        harness.coordinator.reconcile(1)
        self.assertTrue(harness.widget.suppression[-1])

        restored = replace(
            minimized, visibility_state=TargetVisibilityState.BACKGROUND
        )
        harness.target_session.snapshot = restored
        harness.coordinator.reconcile(1)
        self.assertFalse(harness.widget.suppression[-1])

    def test_intentional_hidden_is_not_forced_visible_on_restore(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        harness.widget.visibility_intent = False
        harness.widget.compact_visibility_intent_changed.emit(False)
        harness.debounce.fire()
        harness.target_session.snapshot = replace(
            harness.snapshot, visibility_state=TargetVisibilityState.MINIMIZED
        )
        harness.coordinator.reconcile(1)
        harness.target_session.snapshot = harness.snapshot
        harness.coordinator.reconcile(1)
        self.assertTrue(harness.widget.suppression[-1])
        self.assertFalse(harness.coordinator._ui_wants_visible)

    def test_native_minimize_latch_hides_until_minimize_end(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        harness.adapter.iconic.add(TARGET)
        harness.coordinator._on_win_event(
            {
                "event": EVENT_SYSTEM_MINIMIZESTART,
                "hwnd": TARGET,
                "object_id": 0,
            }
        )
        self.assertTrue(harness.widget.suppression[-1])
        for event in (
            EVENT_OBJECT_REORDER,
            EVENT_OBJECT_LOCATIONCHANGE,
            EVENT_SYSTEM_FOREGROUND,
        ):
            harness.coordinator._on_win_event(
                {"event": event, "hwnd": TARGET, "object_id": 0}
            )
        harness.debounce.fire()
        harness.healing.fire()
        self.assertTrue(harness.widget.suppression[-1])

        harness.adapter.iconic.remove(TARGET)
        harness.coordinator._on_win_event(
            {
                "event": EVENT_SYSTEM_MINIMIZEEND,
                "hwnd": TARGET,
                "object_id": 0,
            }
        )
        harness.debounce.fire()
        self.assertFalse(harness.widget.suppression[-1])

    def test_destroy_detaches_and_stops_healing(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        self.assertTrue(harness.healing.isActive())
        harness.coordinator._on_win_event(
            {"event": EVENT_OBJECT_DESTROY, "hwnd": TARGET, "object_id": 0}
        )
        self.assertIsNone(harness.coordinator._binding)
        self.assertFalse(harness.healing.isActive())
        self.assertTrue(harness.widget.suppression[-1])

    def test_disconnected_snapshot_detaches_without_rebinding(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        disconnected = replace(
            harness.snapshot,
            connection_state=TargetConnectionState.DISCONNECTED,
            visibility_state=TargetVisibilityState.UNKNOWN,
            identity_valid=False,
            disconnect_reason="INVALID_HWND",
        )
        harness.target_session.publish("TARGET_DISCONNECTED", disconnected)
        self.assertIsNone(harness.coordinator._binding)
        self.assertFalse(harness.widget.suppression[-1])

    def test_relock_changes_generation_and_stale_event_is_ignored(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        replacement = make_snapshot(
            session_id="session-b", generation=2, root_hwnd=OTHER, origin=(0, 0)
        )
        harness.target_session.publish("TARGET_SESSION_CREATED", replacement)
        self.assertEqual(("session-b", 2, OTHER), harness.coordinator._binding)
        starts_before = harness.debounce.start_count
        harness.coordinator.request_reconcile(1)
        self.assertEqual(starts_before, harness.debounce.start_count)

    def test_compact_hwnd_change_discards_old_handle(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        new_overlay = 101
        harness.adapter.valid.add(new_overlay)
        harness.adapter.valid.remove(OVERLAY)
        harness.adapter.rects[new_overlay] = (140, 160, 560, 250)
        harness.adapter.order.remove(OVERLAY)
        harness.adapter.order.append(new_overlay)
        harness.widget.hwnd = new_overlay
        harness.widget.compact_hwnd_changed.emit(OVERLAY, new_overlay)
        harness.debounce.fire()
        self.assertEqual(new_overlay, harness.coordinator._overlay_hwnd)
        self.assertTrue(
            any(call["hwnd"] == new_overlay for call in harness.adapter.calls)
        )

    def test_compact_hwnd_recreation_explicitly_clears_topmost(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        new_overlay = 102
        harness.adapter.valid.add(new_overlay)
        harness.adapter.rects[new_overlay] = (150, 170, 570, 260)
        harness.adapter.topmost.add(new_overlay)
        harness.adapter.order.insert(0, new_overlay)
        harness.widget.hwnd = new_overlay
        harness.widget.compact_hwnd_changed.emit(OVERLAY, new_overlay)
        self.assertNotIn(new_overlay, harness.adapter.topmost)
        self.assertTrue(
            any(
                call["hwnd"] == new_overlay
                and call["insert_after"] == HWND_NOTOPMOST
                and call["flags"] & SWP_NOACTIVATE
                for call in harness.adapter.calls
            )
        )

    def test_event_burst_is_coalesced_and_healing_requires_binding(self):
        harness = CoordinatorHarness()
        harness.coordinator.start()
        self.assertEqual(60, harness.debounce.interval)
        self.assertEqual(750, harness.healing.interval)
        initial_starts = harness.debounce.start_count
        for _ in range(20):
            harness.coordinator._on_win_event(
                {
                    "event": EVENT_OBJECT_LOCATIONCHANGE,
                    "hwnd": TARGET,
                    "object_id": 0,
                }
            )
        self.assertEqual(initial_starts, harness.debounce.start_count)
        self.assertTrue(harness.healing.isActive())
        harness.coordinator.detach()
        self.assertFalse(harness.healing.isActive())

    def test_settled_healing_performs_no_redundant_window_mutation(self):
        harness = CoordinatorHarness()
        harness.start_and_reconcile()
        harness.adapter.calls.clear()
        harness.healing.fire()
        self.assertEqual([], harness.adapter.calls)

    def test_shutdown_unhooks_and_stops_timers(self):
        harness = CoordinatorHarness()
        harness.coordinator.start()
        harness.coordinator.close()
        self.assertEqual(("hook",), harness.adapter.unhooked)
        self.assertFalse(harness.debounce.isActive())
        self.assertFalse(harness.healing.isActive())
        self.assertEqual([], harness.target_session.listeners)

    def test_new_module_contains_no_foreground_changing_api(self):
        source = (
            Path(__file__).resolve().parents[1]
            / "app"
            / "target_relative_overlay.py"
        ).read_text(encoding="utf-8-sig")
        forbidden_fragments = (
            "SetFore" + "groundWindow",
            "BringWindow" + "ToTop",
            "SwitchToThis" + "Window",
            "AttachThread" + "Input",
            "SetActive" + "Window",
            "Set" + "Focus",
        )
        for fragment in forbidden_fragments:
            self.assertNotIn(fragment, source)

    def test_f8_handler_is_lock_only_with_no_deferred_start(self):
        app = ScreenBotApp.__new__(ScreenBotApp)
        app.logger = logging.getLogger("test.overlay.f8")
        calls = []
        app.workflow_runner = SimpleNamespace(
            is_active=lambda: False,
            start=lambda: self.fail("F8 must not start Workflow"),
        )
        app.player = SimpleNamespace(
            start=lambda *_args, **_kwargs: self.fail("F8 must not start Player")
        )
        app.lock_or_refresh_target_only = lambda: calls.append("bind")

        app._handle_f8()
        for _fake_tick in range(50):
            pass

        self.assertEqual(["bind"], calls)


if __name__ == "__main__":
    unittest.main()
