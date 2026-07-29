"""Target-relative placement for the compact ScreenBot control surface."""
from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

from PySide6.QtCore import QObject, QTimer, Signal

from app.target_session import TargetConnectionState, TargetVisibilityState


GWL_EXSTYLE = -20
GA_ROOT = 2
WS_EX_TOPMOST = 0x00000008
HWND_TOP = 0
HWND_TOPMOST = -1
HWND_NOTOPMOST = -2
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_NOOWNERZORDER = 0x0200
SWP_NOSENDCHANGING = 0x0400
WINEVENT_OUTOFCONTEXT = 0x0000
OBJID_WINDOW = 0

EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_MINIMIZESTART = 0x0016
EVENT_SYSTEM_MINIMIZEEND = 0x0017
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_REORDER = 0x8004
EVENT_OBJECT_LOCATIONCHANGE = 0x800B

WATCHED_EVENTS = (
    EVENT_SYSTEM_FOREGROUND,
    EVENT_SYSTEM_MINIMIZESTART,
    EVENT_SYSTEM_MINIMIZEEND,
    EVENT_OBJECT_DESTROY,
    EVENT_OBJECT_SHOW,
    EVENT_OBJECT_HIDE,
    EVENT_OBJECT_REORDER,
    EVENT_OBJECT_LOCATIONCHANGE,
)


class TargetRelativeOverlayWin32Adapter:
    """Injectable Win32 boundary; no method in this class changes foreground."""

    def __init__(self):
        self._hook_callback = None
        self.user32 = None
        if sys.platform == "win32":
            self.user32 = ctypes.WinDLL("user32", use_last_error=True)
            self._configure()

    @property
    def is_supported(self):
        return self.user32 is not None

    def _configure(self):
        pointer = ctypes.c_ssize_t
        self.user32.IsWindow.argtypes = (wintypes.HWND,)
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = (wintypes.HWND,)
        self.user32.IsIconic.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = (wintypes.HWND,)
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetAncestor.argtypes = (wintypes.HWND, wintypes.UINT)
        self.user32.GetAncestor.restype = wintypes.HWND
        self.user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user32.GetWindowLongPtrW.restype = pointer
        self.user32.GetWindow.argtypes = (wintypes.HWND, wintypes.UINT)
        self.user32.GetWindow.restype = wintypes.HWND
        self.user32.GetWindowRect.argtypes = (
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        )
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.SetWindowPos.argtypes = (
            wintypes.HWND,
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        )
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user32.ShowWindow.restype = wintypes.BOOL

    def is_window(self, hwnd):
        return bool(self.user32 and hwnd and self.user32.IsWindow(wintypes.HWND(hwnd)))

    def window_rect(self, hwnd):
        if not self.is_window(hwnd):
            return None
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

    def root_hwnd(self, hwnd):
        if not self.is_window(hwnd):
            return 0
        root = self.user32.GetAncestor(wintypes.HWND(hwnd), GA_ROOT)
        return int(root or hwnd)

    def is_iconic(self, hwnd):
        return bool(
            self.is_window(hwnd)
            and self.user32.IsIconic(wintypes.HWND(hwnd))
        )

    def is_visible(self, hwnd):
        return bool(
            self.is_window(hwnd)
            and self.user32.IsWindowVisible(wintypes.HWND(hwnd))
        )

    def is_topmost(self, hwnd):
        if not self.is_window(hwnd):
            return False
        return bool(
            int(self.user32.GetWindowLongPtrW(wintypes.HWND(hwnd), GWL_EXSTYLE))
            & WS_EX_TOPMOST
        )

    def z_order_top_to_bottom(self):
        if not self.user32:
            return []
        windows = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        @callback_type
        def callback(hwnd, _lparam):
            windows.append(int(hwnd))
            return True

        self.user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
        self.user32.EnumWindows.restype = wintypes.BOOL
        self.user32.EnumWindows(callback, 0)
        return windows

    def set_window_pos(self, hwnd, insert_after, x, y, width, height, flags):
        if not self.is_window(hwnd) or not (flags & SWP_NOACTIVATE):
            return False
        return bool(
            self.user32.SetWindowPos(
                wintypes.HWND(hwnd),
                wintypes.HWND(insert_after),
                int(x),
                int(y),
                int(width),
                int(height),
                int(flags),
            )
        )

    def show_no_activate(self, hwnd):
        return bool(
            self.is_window(hwnd)
            and self.user32.ShowWindow(wintypes.HWND(hwnd), SW_SHOWNOACTIVATE)
        )

    def hide(self, hwnd):
        return bool(
            self.is_window(hwnd)
            and self.user32.ShowWindow(wintypes.HWND(hwnd), SW_HIDE)
        )

    def install_win_event_hook(self, callback):
        if not self.user32:
            return ()
        callback_type = ctypes.WINFUNCTYPE(
            None,
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.HWND,
            ctypes.c_long,
            ctypes.c_long,
            wintypes.DWORD,
            wintypes.DWORD,
        )

        @callback_type
        def hook(_handle, event, hwnd, object_id, child_id, _thread, _time):
            callback(
                {
                    "event": int(event),
                    "hwnd": int(hwnd or 0),
                    "object_id": int(object_id),
                    "child_id": int(child_id),
                }
            )

        self._hook_callback = hook
        self.user32.SetWinEventHook.argtypes = (
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HMODULE,
            callback_type,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
        )
        self.user32.SetWinEventHook.restype = wintypes.HANDLE
        handles = []
        for event in WATCHED_EVENTS:
            handle = self.user32.SetWinEventHook(
                event, event, None, hook, 0, 0, WINEVENT_OUTOFCONTEXT
            )
            if handle:
                handles.append(handle)
        return tuple(handles)

    def uninstall_win_event_hook(self, handles):
        if not self.user32:
            return
        self.user32.UnhookWinEvent.argtypes = (wintypes.HANDLE,)
        self.user32.UnhookWinEvent.restype = wintypes.BOOL
        for handle in handles or ():
            self.user32.UnhookWinEvent(handle)
        self._hook_callback = None


class TargetRelativeOverlayCoordinator(QObject):
    """Coordinates one compact HWND with one immutable TargetSession generation."""

    win_event_received = Signal(dict)
    target_event_received = Signal(str, object, dict)

    def __init__(
        self,
        target_session,
        widget,
        logger=None,
        adapter=None,
        timer_factory=None,
        debounce_ms=60,
        healing_interval_ms=750,
    ):
        super().__init__()
        self.target_session = target_session
        self.widget = widget
        self.logger = logger or logging.getLogger("ScreenBot")
        self.adapter = adapter or TargetRelativeOverlayWin32Adapter()
        self._timer_factory = timer_factory
        self._debounce_ms = int(debounce_ms)
        self._healing_interval_ms = int(healing_interval_ms)
        self._binding = None
        self._snapshot = None
        self._overlay_hwnd = 0
        self._offset = None
        self._native_minimized = False
        self._native_hidden = False
        self._effective_visible = None
        self._pending_reconcile_reason = None
        self._active_reconcile_reason = "unspecified"
        self._diagnostics = getattr(widget, "_overlay_diagnostics", None)
        visibility_intent = getattr(widget, "is_compact_visibility_intended", None)
        self._ui_wants_visible = (
            bool(visibility_intent()) if callable(visibility_intent) else True
        )
        self._started = False
        self._closed = False
        self._hook_handles = ()
        self._pending_generation = None
        self._debounce_timer = self._make_timer(single_shot=True)
        self._healing_timer = self._make_timer(single_shot=False)
        self._configure_timer(
            self._debounce_timer, self._debounce_ms, self._run_debounced
        )
        self._configure_timer(
            self._healing_timer,
            self._healing_interval_ms,
            self._on_healing_tick,
        )
        self.win_event_received.connect(self._on_win_event)
        self.target_event_received.connect(self._on_target_event)

    def _make_timer(self, single_shot):
        if self._timer_factory is not None:
            return self._timer_factory(single_shot)
        timer = QTimer(self)
        timer.setSingleShot(single_shot)
        return timer

    @staticmethod
    def _configure_timer(timer, interval, callback):
        timer.setInterval(interval)
        timer.timeout.connect(callback)

    def start(self):
        if self._started or self._closed:
            return
        self._started = True
        self.target_session.subscribe(self._receive_target_event)
        self._connect_widget_signals()
        self._overlay_hwnd = int(self.widget.winId() or 0)
        self._ensure_overlay_non_topmost()
        self._hook_handles = self.adapter.install_win_event_hook(
            self._receive_native_event
        )
        snapshot = self.target_session.get_snapshot()
        if snapshot is not None:
            self.bind_snapshot(snapshot)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._debounce_timer.stop()
        self._healing_timer.stop()
        self.adapter.uninstall_win_event_hook(self._hook_handles)
        self._hook_handles = ()
        if self._started:
            self.target_session.unsubscribe(self._receive_target_event)
        self._binding = None
        self._snapshot = None

    def _connect_widget_signals(self):
        for name, callback in (
            ("compact_hwnd_changed", self.on_compact_hwnd_changed),
            ("compact_geometry_changed", self.on_compact_geometry_changed),
            ("compact_visibility_intent_changed", self.on_visibility_intent),
        ):
            signal = getattr(self.widget, name, None)
            if signal is not None:
                signal.connect(callback)

    def bind_snapshot(self, snapshot):
        if not self._snapshot_is_attached(snapshot):
            self.detach(hide=True)
            return
        binding = (
            str(snapshot.session_id),
            int(snapshot.generation),
            int(snapshot.root_hwnd),
        )
        replacing = binding != self._binding
        self._binding = binding
        self._snapshot = snapshot
        if not self._healing_timer.isActive():
            self._healing_timer.start()
        if replacing:
            self._native_minimized = self.adapter.is_iconic(snapshot.root_hwnd)
            self._native_hidden = not self.adapter.is_visible(snapshot.root_hwnd)
            self._effective_visible = None
            self._offset = self._capture_offset(snapshot)
        self.request_reconcile(snapshot.generation, reason="target_bind")

    def detach(self, hide=True):
        if hide:
            self._set_effective_visible(False)
        self._binding = None
        self._snapshot = None
        self._offset = None
        self._native_minimized = False
        self._native_hidden = False
        self._pending_generation = None
        self._debounce_timer.stop()
        self._healing_timer.stop()

    def on_compact_hwnd_changed(self, _old_hwnd, new_hwnd):
        self._overlay_hwnd = int(new_hwnd or 0)
        self._ensure_overlay_non_topmost()
        if self._snapshot is not None:
            self._offset = self._capture_offset(self._snapshot)
            self.request_reconcile(
                self._snapshot.generation, reason="compact_hwnd_changed"
            )

    def on_compact_geometry_changed(self):
        if self._snapshot is not None:
            new_offset = self._capture_offset(self._snapshot)
            if new_offset is not None:
                self._offset = new_offset

    def on_visibility_intent(self, visible):
        self._ui_wants_visible = bool(visible)
        if not self._ui_wants_visible:
            self._set_effective_visible(False)
        if self._snapshot is not None:
            self.request_reconcile(
                self._snapshot.generation, reason="visibility_intent"
            )

    def _on_target_event(self, event, snapshot, _payload):
        if event in {"TARGET_SESSION_CLEARED", "TARGET_DISCONNECTED"}:
            if snapshot is None or self._is_current_binding(snapshot):
                self.detach(hide=True)
            return
        if snapshot is None:
            return
        if self._is_current_binding(snapshot):
            self._snapshot = snapshot
            if event != "TARGET_SNAPSHOT_UPDATED":
                self.request_reconcile(
                    snapshot.generation, reason=f"target_event:{event}"
                )
            return
        self.bind_snapshot(snapshot)

    def _receive_target_event(self, event, snapshot, payload):
        if not self._closed:
            self.target_event_received.emit(str(event), snapshot, dict(payload))

    def _receive_native_event(self, payload):
        if not self._closed:
            self.win_event_received.emit(dict(payload))

    def _on_win_event(self, payload):
        if self._binding is None:
            return
        event = int(payload.get("event", 0))
        hwnd = int(payload.get("hwnd", 0))
        object_id = int(payload.get("object_id", OBJID_WINDOW))
        if object_id not in {OBJID_WINDOW, 0}:
            return
        target_hwnd = self._binding[2]
        event_root = self.adapter.root_hwnd(hwnd) if hwnd else 0
        targets_current_root = event_root == target_hwnd
        if event == EVENT_OBJECT_DESTROY and targets_current_root:
            self.detach(hide=True)
            return
        if event == EVENT_SYSTEM_MINIMIZESTART and targets_current_root:
            self._native_minimized = True
            self._set_effective_visible(False)
            self.request_reconcile(
                self._binding[1], reason="winevent:minimize_start"
            )
            return
        if event == EVENT_SYSTEM_MINIMIZEEND and targets_current_root:
            self._native_minimized = False
            self.request_reconcile(
                self._binding[1], reason="winevent:minimize_end"
            )
            return
        if event == EVENT_OBJECT_HIDE and targets_current_root:
            self._native_hidden = True
            self._set_effective_visible(False)
            self.request_reconcile(
                self._binding[1], reason="winevent:hide"
            )
            return
        if event == EVENT_OBJECT_SHOW and targets_current_root:
            self._native_hidden = False
            self.request_reconcile(
                self._binding[1], reason="winevent:show"
            )
            return
        if event == EVENT_SYSTEM_FOREGROUND or hwnd in {
            0,
            target_hwnd,
            self._overlay_hwnd,
        } or targets_current_root:
            self.request_reconcile(
                self._binding[1], reason=f"winevent:{event:#x}"
            )

    def request_reconcile(self, generation=None, reason="request"):
        if self._closed or self._binding is None:
            return
        if generation is not None and int(generation) != self._binding[1]:
            return
        self._pending_generation = self._binding[1]
        self._pending_reconcile_reason = str(reason)
        if not self._debounce_timer.isActive():
            self._debounce_timer.start()

    def _run_debounced(self):
        generation = self._pending_generation
        reason = self._pending_reconcile_reason or "debounce"
        self._pending_generation = None
        self._pending_reconcile_reason = None
        if self._binding is not None and generation == self._binding[1]:
            self.reconcile(generation, reason=reason)

    def _on_healing_tick(self):
        if self._binding is not None:
            self.reconcile(self._binding[1], reason="healing_timer")

    def reconcile(self, generation=None, reason="direct"):
        self._active_reconcile_reason = str(reason)
        if self._closed or self._binding is None:
            return False
        if generation is not None and int(generation) != self._binding[1]:
            return False
        try:
            snapshot = self.target_session.refresh()
        except Exception:
            self._set_effective_visible(False)
            return False
        if not self._is_current_binding(snapshot):
            if snapshot is None or not self._snapshot_is_attached(snapshot):
                self.detach(hide=True)
            return False
        self._snapshot = snapshot
        if (
            not self.adapter.is_iconic(snapshot.root_hwnd)
            and snapshot.visibility_state != TargetVisibilityState.MINIMIZED
        ):
            self._native_minimized = False
        if (
            self.adapter.is_visible(snapshot.root_hwnd)
            and snapshot.visibility_state != TargetVisibilityState.HIDDEN
        ):
            self._native_hidden = False
        suppressed = self._must_suppress(snapshot)
        self._observe_reconcile("before", snapshot, suppressed=suppressed)
        if suppressed:
            self._set_effective_visible(False)
            self._observe_reconcile(
                "after",
                snapshot,
                suppressed=True,
                skip_reason="target_suppressed",
            )
            return False
        if not self.adapter.is_window(self._overlay_hwnd):
            self._overlay_hwnd = int(self.widget.winId() or 0)
        if not self.adapter.is_window(self._overlay_hwnd):
            return False
        if self._offset is None:
            self._offset = self._capture_offset(snapshot)
        if self._offset is None:
            return False
        self._ensure_overlay_non_topmost()
        self._set_effective_visible(self._ui_wants_visible)
        if not self._ui_wants_visible:
            self._observe_reconcile(
                "after",
                snapshot,
                suppressed=False,
                skip_reason="user_visibility_intent_false",
            )
            return True
        self._apply_geometry(snapshot)
        self._apply_z_order(snapshot)
        self._observe_reconcile("after", snapshot, suppressed=False)
        return True

    def _capture_offset(self, snapshot):
        rect = self.adapter.window_rect(self._overlay_hwnd)
        if rect is None:
            return None
        origin_x, origin_y = snapshot.client_screen_origin
        return (int(rect[0] - origin_x), int(rect[1] - origin_y))

    def _apply_geometry(self, snapshot):
        rect = self.adapter.window_rect(self._overlay_hwnd)
        if rect is None:
            return False
        width = rect[2] - rect[0]
        height = rect[3] - rect[1]
        origin_x, origin_y = snapshot.client_screen_origin
        x = int(origin_x + self._offset[0])
        y = int(origin_y + self._offset[1])
        if rect[0] == x and rect[1] == y:
            return True
        flags = (
            SWP_NOZORDER
            | SWP_NOACTIVATE
            | SWP_NOOWNERZORDER
            | SWP_NOSENDCHANGING
        )
        return self._set_window_pos(
            "geometry",
            self._overlay_hwnd,
            HWND_TOP,
            x,
            y,
            width,
            height,
            flags,
        )

    def _apply_z_order(self, snapshot):
        base_flags = (
            SWP_NOMOVE
            | SWP_NOSIZE
            | SWP_NOACTIVATE
            | SWP_NOOWNERZORDER
            | SWP_NOSENDCHANGING
        )
        self._ensure_overlay_non_topmost()
        order = self.adapter.z_order_top_to_bottom()
        if self.adapter.is_topmost(snapshot.root_hwnd):
            # A topmost target cannot have a non-topmost overlay directly
            # above it.  Preserve the safer non-topmost policy and do not
            # raise the overlay over unrelated foreground windows.
            return True
        try:
            target_index = order.index(int(snapshot.root_hwnd))
        except ValueError:
            target_index = -1
        if target_index > 0 and order[target_index - 1] == self._overlay_hwnd:
            self._observe_reconcile(
                "decision",
                snapshot,
                suppressed=False,
                computed_predecessor=(
                    order[target_index - 2]
                    if target_index > 1
                    else HWND_TOP
                ),
                already_settled=True,
                skip_reason="overlay_directly_above_target",
            )
            return True
        insert_after = self._z_insert_after(snapshot)
        self._observe_reconcile(
            "decision",
            snapshot,
            suppressed=False,
            computed_predecessor=insert_after,
            already_settled=False,
            set_window_pos_action="execute",
        )
        return self._set_window_pos(
            "z_order",
            self._overlay_hwnd,
            insert_after,
            0,
            0,
            0,
            0,
            base_flags,
        )

    def _z_insert_after(self, snapshot):
        target = int(snapshot.root_hwnd)
        order = self.adapter.z_order_top_to_bottom()
        try:
            target_index = order.index(target)
        except ValueError:
            return HWND_TOP
        for hwnd in reversed(order[:target_index]):
            if hwnd == self._overlay_hwnd or not self.adapter.is_window(hwnd):
                continue
            if not self.adapter.is_topmost(hwnd):
                return int(hwnd)
        return HWND_TOP

    def _ensure_overlay_non_topmost(self):
        if not self.adapter.is_window(self._overlay_hwnd):
            return False
        if not self.adapter.is_topmost(self._overlay_hwnd):
            return True
        flags = (
            SWP_NOMOVE
            | SWP_NOSIZE
            | SWP_NOACTIVATE
            | SWP_NOOWNERZORDER
            | SWP_NOSENDCHANGING
        )
        self._set_window_pos(
            "clear_topmost",
            self._overlay_hwnd,
            HWND_NOTOPMOST,
            0,
            0,
            0,
            0,
            flags,
        )
        return not self.adapter.is_topmost(self._overlay_hwnd)

    def _set_window_pos(
        self, reason, hwnd, insert_after, x, y, width, height, flags
    ):
        diagnostics = self._diagnostics
        observer = getattr(diagnostics, "observe_set_window_pos", None)
        enabled = bool(
            callable(observer) and getattr(diagnostics, "enabled", False)
        )
        if enabled:
            observer(
                "before",
                reason=reason,
                hwnd=hwnd,
                insert_after=insert_after,
                x=x,
                y=y,
                width=width,
                height=height,
                flags=flags,
            )
        result = self.adapter.set_window_pos(
            hwnd, insert_after, x, y, width, height, flags
        )
        if enabled:
            observer(
                "after",
                reason=reason,
                hwnd=hwnd,
                insert_after=insert_after,
                x=x,
                y=y,
                width=width,
                height=height,
                flags=flags,
                result=bool(result),
                last_error=ctypes.get_last_error(),
            )
        return result

    def _observe_reconcile(
        self,
        phase,
        snapshot,
        *,
        suppressed,
        computed_predecessor=None,
        already_settled=None,
        set_window_pos_action=None,
        skip_reason=None,
    ):
        diagnostics = self._diagnostics
        observer = getattr(diagnostics, "observe_coordinator_reconcile", None)
        if not (
            callable(observer)
            and getattr(diagnostics, "enabled", False)
            and snapshot is not None
        ):
            return
        observer(
            phase,
            reason=self._active_reconcile_reason,
            session_id=getattr(snapshot, "session_id", None),
            generation=getattr(snapshot, "generation", None),
            overlay_hwnd=self._overlay_hwnd,
            target_hwnd=getattr(snapshot, "root_hwnd", 0),
            binding_valid=self._is_current_binding(snapshot),
            target_suppressed=suppressed,
            user_visibility_intent=self._ui_wants_visible,
            effective_visibility=self._effective_visible,
            computed_predecessor=computed_predecessor,
            already_settled=already_settled,
            set_window_pos_action=set_window_pos_action,
            skip_reason=skip_reason,
            widget=self.widget,
        )

    def _set_effective_visible(self, visible):
        visible = bool(visible)
        if self._effective_visible is visible:
            return
        self._effective_visible = visible
        setter = getattr(self.widget, "set_target_visibility_suppressed", None)
        if callable(setter):
            setter(not visible)
        elif visible:
            self.adapter.show_no_activate(self._overlay_hwnd)
        else:
            self.adapter.hide(self._overlay_hwnd)

    def _is_current_binding(self, snapshot):
        return bool(
            snapshot is not None
            and self._binding is not None
            and str(snapshot.session_id) == self._binding[0]
            and int(snapshot.generation) == self._binding[1]
            and int(snapshot.root_hwnd) == self._binding[2]
        )

    @staticmethod
    def _snapshot_is_attached(snapshot):
        return bool(
            snapshot is not None
            and snapshot.connection_state == TargetConnectionState.ATTACHED
            and snapshot.identity_valid
        )

    def _must_suppress(self, snapshot):
        return (
            snapshot.connection_state != TargetConnectionState.ATTACHED
            or not snapshot.identity_valid
            or snapshot.visibility_state
            in {
                TargetVisibilityState.MINIMIZED,
                TargetVisibilityState.HIDDEN,
                TargetVisibilityState.UNKNOWN,
            }
            or self._native_minimized
            or self._native_hidden
            or self.adapter.is_iconic(snapshot.root_hwnd)
            or not self.adapter.is_visible(snapshot.root_hwnd)
        )
