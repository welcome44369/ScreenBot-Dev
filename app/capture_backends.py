"""Direct HWND capture backend implementations.

None of these backends capture the desktop or resolve the foreground window.
"""
from __future__ import annotations

import asyncio
import ctypes
from ctypes import wintypes
from dataclasses import dataclass, field
import logging
import threading
import time
from typing import Any, Protocol
import uuid

from PIL import Image


user32 = ctypes.WinDLL("user32", use_last_error=True)
combase = ctypes.WinDLL("combase", use_last_error=True)
PrintWindow = user32.PrintWindow
PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
PrintWindow.restype = wintypes.BOOL
PW_CLIENTONLY = 0x00000001
MAXIMUM_ALLOWED = 0x02000000
RO_INIT_MULTITHREADED = 1

user32.OpenInputDesktop.argtypes = [
    wintypes.DWORD,
    wintypes.BOOL,
    wintypes.DWORD,
]
user32.OpenInputDesktop.restype = wintypes.HANDLE
user32.SetThreadDesktop.argtypes = [wintypes.HANDLE]
user32.SetThreadDesktop.restype = wintypes.BOOL
user32.CloseDesktop.argtypes = [wintypes.HANDLE]
user32.CloseDesktop.restype = wintypes.BOOL
combase.RoInitialize.argtypes = [wintypes.UINT]
combase.RoInitialize.restype = ctypes.c_long
combase.RoUninitialize.argtypes = []
combase.RoUninitialize.restype = None


def attach_input_desktop() -> int:
    """Attach the current windowless thread to the interactive input desktop."""
    ctypes.set_last_error(0)
    desktop = user32.OpenInputDesktop(0, False, MAXIMUM_ALLOWED)
    if not desktop:
        raise ctypes.WinError(ctypes.get_last_error())
    if not user32.SetThreadDesktop(desktop):
        error = ctypes.get_last_error()
        user32.CloseDesktop(desktop)
        raise ctypes.WinError(error)
    return int(desktop)


def close_desktop(desktop: int | None) -> None:
    if desktop:
        user32.CloseDesktop(wintypes.HANDLE(desktop))


class _GUID(ctypes.Structure):
    _fields_ = [
        ("data1", wintypes.DWORD),
        ("data2", wintypes.WORD),
        ("data3", wintypes.WORD),
        ("data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, value: str):
        raw = uuid.UUID(value).bytes_le
        return cls.from_buffer_copy(raw)


def _com_release(pointer):
    if not pointer:
        return
    vtable = ctypes.cast(
        pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
    ).contents
    release = ctypes.WINFUNCTYPE(
        wintypes.ULONG, ctypes.c_void_p
    )(vtable[2])
    release(pointer)


def _create_pywinrt_d3d_device():
    """Create a BGRA-capable D3D11 device and wrap it as a WinRT device."""
    from winrt.windows.graphics.directx.direct3d11.interop import (
        create_direct3d11_device_from_dxgi_device,
    )

    d3d11 = ctypes.WinDLL("d3d11", use_last_error=True)
    create_device = d3d11.D3D11CreateDevice
    create_device.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint,
        wintypes.HMODULE,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_uint),
        wintypes.UINT,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    create_device.restype = ctypes.c_long

    D3D_DRIVER_TYPE_HARDWARE = 1
    D3D11_CREATE_DEVICE_BGRA_SUPPORT = 0x20
    D3D11_SDK_VERSION = 7
    device = ctypes.c_void_p()
    context = ctypes.c_void_p()
    feature_level = ctypes.c_uint()
    dxgi_device = ctypes.c_void_p()
    try:
        hresult = int(
            create_device(
                None,
                D3D_DRIVER_TYPE_HARDWARE,
                None,
                D3D11_CREATE_DEVICE_BGRA_SUPPORT,
                None,
                0,
                D3D11_SDK_VERSION,
                ctypes.byref(device),
                ctypes.byref(feature_level),
                ctypes.byref(context),
            )
        )
        if hresult < 0:
            raise OSError(
                f"D3D11CreateDevice failed HRESULT="
                f"0x{hresult & 0xFFFFFFFF:08X}"
            )
        iid_idxgi_device = _GUID.from_string(
            "54EC77FA-1377-44E6-8C32-88FD5F44C84C"
        )
        vtable = ctypes.cast(
            device, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        query_interface = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(vtable[0])
        hresult = int(
            query_interface(
                device,
                ctypes.byref(iid_idxgi_device),
                ctypes.byref(dxgi_device),
            )
        )
        if hresult < 0:
            raise OSError(
                f"ID3D11Device::QueryInterface(IDXGIDevice) failed "
                f"HRESULT=0x{hresult & 0xFFFFFFFF:08X}"
            )
        return (
            create_direct3d11_device_from_dxgi_device(dxgi_device.value),
            feature_level.value,
        )
    finally:
        _com_release(dxgi_device)
        _com_release(context)
        _com_release(device)


def _native_create_for_window_hresult(hwnd: int) -> int:
    """Probe the native interop HRESULT without creating a projected object."""
    combase.WindowsCreateString.argtypes = [
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    combase.WindowsCreateString.restype = ctypes.c_long
    combase.WindowsDeleteString.argtypes = [ctypes.c_void_p]
    combase.WindowsDeleteString.restype = ctypes.c_long
    combase.RoGetActivationFactory.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_GUID),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    combase.RoGetActivationFactory.restype = ctypes.c_long
    runtime_class = "Windows.Graphics.Capture.GraphicsCaptureItem"
    hstring = ctypes.c_void_p()
    factory = ctypes.c_void_p()
    item = ctypes.c_void_p()
    try:
        hresult = int(
            combase.WindowsCreateString(
                runtime_class,
                len(runtime_class),
                ctypes.byref(hstring),
            )
        )
        if hresult < 0:
            return hresult
        interop_iid = _GUID.from_string(
            "3628E81B-3CAC-4C60-B7F4-23CE0E0C3356"
        )
        hresult = int(
            combase.RoGetActivationFactory(
                hstring,
                ctypes.byref(interop_iid),
                ctypes.byref(factory),
            )
        )
        if hresult < 0:
            return hresult
        vtable = ctypes.cast(
            factory, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        ).contents
        create_for_window = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            ctypes.c_void_p,
            wintypes.HWND,
            ctypes.POINTER(_GUID),
            ctypes.POINTER(ctypes.c_void_p),
        )(vtable[3])
        item_iid = _GUID.from_string(
            "79C3F95B-31F7-4EC2-A464-632EF5D30760"
        )
        return int(
            create_for_window(
                factory,
                wintypes.HWND(hwnd),
                ctypes.byref(item_iid),
                ctypes.byref(item),
            )
        )
    finally:
        _com_release(item)
        _com_release(factory)
        if hstring:
            combase.WindowsDeleteString(hstring)


@dataclass
class CaptureResult:
    image: Image.Image
    backend: str
    hwnd: int
    client_size: tuple[int, int]
    capture_duration_ms: int
    metadata: dict[str, Any] = field(default_factory=dict)


class CaptureBackendError(RuntimeError):
    def __init__(self, backend: str, stage: str, message: str, *, original=None):
        super().__init__(message)
        self.backend = backend
        self.stage = stage
        self.original = original
        for name in ("hresult", "winerror", "errno"):
            value = getattr(original, name, None)
            if value is not None:
                setattr(self, name, value)


class CaptureBackend(Protocol):
    name: str

    def capture_client(self, hwnd: int) -> CaptureResult:
        ...

    def close(self) -> None:
        ...


def get_client_size(hwnd: int) -> tuple[int, int]:
    import win32gui

    left, top, right, bottom = win32gui.GetClientRect(hwnd)
    width, height = right - left, bottom - top
    if width <= 1 or height <= 1:
        raise CaptureBackendError("window", "client_rect", f"Invalid client size {width}x{height}")
    return width, height


def _bitmap_to_image(bitmap) -> Image.Image:
    info = bitmap.GetInfo()
    bits = bitmap.GetBitmapBits(True)
    return Image.frombuffer(
        "RGB",
        (info["bmWidth"], info["bmHeight"]),
        bits,
        "raw",
        "BGRX",
        0,
        1,
    ).copy()


class PrintWindowBackend:
    name = "printwindow"

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("ScreenBot.TargetCapture")

    def capture_client(self, hwnd: int) -> CaptureResult:
        import win32gui
        import win32ui

        width, height = get_client_size(hwnd)
        started = time.perf_counter()
        hwnd_dc = win32gui.GetDC(hwnd)
        source_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        old_bitmap = memory_dc.SelectObject(bitmap)
        try:
            ctypes.set_last_error(0)
            success = bool(
                PrintWindow(
                    wintypes.HWND(hwnd),
                    wintypes.HDC(memory_dc.GetSafeHdc()),
                    PW_CLIENTONLY,
                )
            )
            last_error = ctypes.get_last_error()
            self.logger.info(
                "CAPTURE_BACKEND_RESULT backend=%s success=%s last_error=%s flags=0x%X",
                self.name,
                success,
                last_error,
                PW_CLIENTONLY,
            )
            if not success:
                raise CaptureBackendError(
                    self.name,
                    "PrintWindow",
                    f"PrintWindow returned false (GetLastError={last_error})",
                )
            image = _bitmap_to_image(bitmap)
        except CaptureBackendError:
            raise
        except Exception as exc:
            raise CaptureBackendError(
                self.name, "PrintWindow", str(exc), original=exc
            ) from exc
        finally:
            try:
                memory_dc.SelectObject(old_bitmap)
            except Exception:
                pass
            win32gui.DeleteObject(bitmap.GetHandle())
            memory_dc.DeleteDC()
            source_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        return CaptureResult(
            image=image,
            backend=self.name,
            hwnd=hwnd,
            client_size=(width, height),
            capture_duration_ms=int((time.perf_counter() - started) * 1000),
            metadata={"flags": PW_CLIENTONLY},
        )

    def close(self):
        return None


class BitBltBackend:
    name = "bitblt"

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("ScreenBot.TargetCapture")

    def capture_client(self, hwnd: int) -> CaptureResult:
        import win32con
        import win32gui
        import win32ui

        width, height = get_client_size(hwnd)
        started = time.perf_counter()
        hwnd_dc = win32gui.GetDC(hwnd)
        source_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        memory_dc = source_dc.CreateCompatibleDC()
        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(source_dc, width, height)
        old_bitmap = memory_dc.SelectObject(bitmap)
        try:
            memory_dc.BitBlt(
                (0, 0), (width, height), source_dc, (0, 0), win32con.SRCCOPY
            )
            image = _bitmap_to_image(bitmap)
        except Exception as exc:
            raise CaptureBackendError(
                self.name, "BitBlt", str(exc), original=exc
            ) from exc
        finally:
            try:
                memory_dc.SelectObject(old_bitmap)
            except Exception:
                pass
            win32gui.DeleteObject(bitmap.GetHandle())
            memory_dc.DeleteDC()
            source_dc.DeleteDC()
            win32gui.ReleaseDC(hwnd, hwnd_dc)
        return CaptureResult(
            image=image,
            backend=self.name,
            hwnd=hwnd,
            client_size=(width, height),
            capture_duration_ms=int((time.perf_counter() - started) * 1000),
            metadata={"raster_operation": "SRCCOPY"},
        )

    def close(self):
        return None


class WindowsCapBackend:
    """Legacy diagnostic adapter for the alpha ``windows-cap`` package."""

    name = "windows-cap"

    def __init__(self, logger=None):
        self.logger = logger or logging.getLogger("ScreenBot.TargetCapture")

    def capture_client(self, hwnd: int) -> CaptureResult:
        import win32gui
        import win32process

        try:
            from windows_cap import WindowCapture
        except ImportError as exc:
            raise CaptureBackendError(
                self.name, "import", "windows-cap is unavailable", original=exc
            ) from exc

        if not win32gui.IsWindow(hwnd):
            raise CaptureBackendError(self.name, "validate_hwnd", "Invalid HWND")
        title = win32gui.GetWindowText(hwnd)
        class_name = win32gui.GetClassName(hwnd)
        pid = win32process.GetWindowThreadProcessId(hwnd)[1]
        matches: list[int] = []

        def visit(candidate, _):
            if (
                win32gui.IsWindowVisible(candidate)
                and win32gui.GetWindowText(candidate) == title
                and win32gui.GetClassName(candidate) == class_name
            ):
                matches.append(candidate)
            return True

        win32gui.EnumWindows(visit, None)
        if matches != [hwnd]:
            raise CaptureBackendError(
                self.name,
                "resolve_window",
                f"Expected one exact title/class match for HWND 0x{hwnd:08X}; got {matches}",
            )
        started = time.perf_counter()
        try:
            capture = WindowCapture(title, class_name)
            data = capture.next()
            width, height = capture.client_size
        except Exception as exc:
            raise CaptureBackendError(
                self.name, "WindowCapture", str(exc), original=exc
            ) from exc
        expected = width * height * 4
        if len(data) != expected:
            raise CaptureBackendError(
                self.name,
                "frame_buffer",
                f"Returned {len(data)} bytes, expected {expected}",
            )
        image = Image.frombuffer(
            "RGBA", (width, height), data, "raw", "BGRA", 0, 1
        ).convert("RGB")
        return CaptureResult(
            image=image,
            backend=self.name,
            hwnd=hwnd,
            client_size=(width, height),
            capture_duration_ms=int((time.perf_counter() - started) * 1000),
            metadata={
                "title": title,
                "class_name": class_name,
                "pid": pid,
                "package": "windows-cap",
                "version": "0.1.2",
            },
        )

    def close(self):
        return None


class _WinsdkWgcSession:
    """One WGC session that continuously publishes the most recent frame.

    WGC session creation can be visible for some full-screen/game windows.  A
    session is therefore tied to a locked HWND rather than created for each
    OCR poll.  The session owns its COM/WinRT apartment and is closed only on
    target change, resize, backend error, or application shutdown.
    """

    def __init__(self, backend, hwnd: int, client_size: tuple[int, int]):
        self.backend = backend
        self.logger = backend.logger
        self.hwnd = hwnd
        self.client_size = client_size
        self.session_id = uuid.uuid4().hex
        self.ready = threading.Event()
        self.frame_ready = threading.Event()
        self.stop_requested = threading.Event()
        self.error: BaseException | None = None
        self.metadata: dict[str, Any] = {}
        self._image: Image.Image | None = None
        self._image_lock = threading.Lock()
        self._loop = None
        self._thread = threading.Thread(
            target=self._worker,
            name=f"ScreenBot-WGC-{hwnd:X}",
            daemon=True,
        )
        self._device = None
        self._item = None
        self._frame_pool = None
        self._capture_session = None
        self._frame_token = None

    def start(self, timeout_seconds: float) -> None:
        self._thread.start()
        if not self.ready.wait(timeout_seconds):
            self.close()
            raise CaptureBackendError(
                "winsdk-wgc", "session_start", "Timed out starting WGC session"
            )
        if self.error is not None:
            error = self.error
            self.close()
            if isinstance(error, CaptureBackendError):
                raise error
            raise CaptureBackendError("winsdk-wgc", "session_start", str(error), original=error)

    @staticmethod
    async def _get_direct3d_device():
        return _create_pywinrt_d3d_device()

    async def _setup_async(self) -> None:
        from winrt.windows.graphics.capture import (
            Direct3D11CaptureFrame,
            Direct3D11CaptureFramePool,
        )
        from winrt.windows.graphics.capture.interop import create_for_window
        from winrt.windows.graphics.directx import DirectXPixelFormat
        from winrt.windows.graphics.imaging import (
            SoftwareBitmap,
        )
        from winrt.windows.storage.streams import Buffer, DataReader

        self.logger.info("WGC_WINSDK_CREATE_DEVICE START session_id=%s", self.session_id)
        device, feature_level = await self._get_direct3d_device()
        self._device = device
        self.logger.info(
            "WGC_WINSDK_CREATE_DEVICE PASS feature_level=0x%X",
            feature_level,
        )
        self.logger.info("WGC_WINSDK_CREATE_FOR_WINDOW START hwnd=0x%08X", self.hwnd)
        try:
            item = create_for_window(self.hwnd)
        except Exception as exc:
            hresult = _native_create_for_window_hresult(self.hwnd)
            self.logger.error(
                "WGC_CREATE_FOR_WINDOW FAIL projected_exception=%r "
                "native_hresult=0x%08X",
                exc,
                hresult & 0xFFFFFFFF,
            )
            error = OSError(
                f"CreateForWindow failed HRESULT="
                f"0x{hresult & 0xFFFFFFFF:08X}: {exc}"
            )
            error.hresult = hresult
            raise CaptureBackendError(
                "winsdk-wgc",
                "WGC_CREATE_FOR_WINDOW",
                str(error),
                original=error,
            ) from exc
        self._item = item
        self.logger.info(
            "WGC_WINSDK_CREATE_FOR_WINDOW PASS size=%sx%s",
            item.size.width,
            item.size.height,
        )
        frame_width, frame_height = item.size.width, item.size.height
        expected_bytes = frame_width * frame_height * 4
        self.logger.info(
            "WGC_ITEM_SIZE width=%s height=%s bytes_per_pixel=4 "
            "expected_buffer_bytes=%s",
            frame_width,
            frame_height,
            expected_bytes,
        )
        if (
            frame_width <= 1
            or frame_height <= 1
            or expected_bytes > 1024 * 1024 * 1024
        ):
            raise CaptureBackendError(
                "winsdk-wgc",
                "item_size",
                f"Invalid GraphicsCaptureItem size "
                f"{frame_width}x{frame_height}",
            )
        frame_pool = Direct3D11CaptureFramePool.create_free_threaded(
            device,
            DirectXPixelFormat.B8_G8_R8_A8_UINT_NORMALIZED,
            1,
            item.size,
        )
        self.logger.info("WGC_WINSDK_FRAME_POOL PASS")
        self._frame_pool = frame_pool
        session = frame_pool.create_capture_session(item)
        self._capture_session = session
        try:
            session.is_border_required = False
        except Exception:
            pass
        try:
            session.is_cursor_capture_enabled = False
        except Exception:
            pass
        self.logger.info("WGC_WINSDK_CAPTURE_SESSION PASS")

        def frame_arrived(pool: Direct3D11CaptureFramePool, _):
            try:
                frame = pool.try_get_next_frame()
                if frame is not None and self._loop is not None and not self.stop_requested.is_set():
                    asyncio.run_coroutine_threadsafe(self._copy_frame(frame), self._loop)
            except Exception as exc:
                self.logger.exception("WGC_FRAME_RECEIVED FAIL session_id=%s", self.session_id)

        self._frame_token = frame_pool.add_frame_arrived(frame_arrived)
        session.start_capture()
        self.logger.info("WGC_WINSDK_START_CAPTURE PASS session_id=%s", self.session_id)
        self.metadata = {
            "item_size": [item.size.width, item.size.height],
            "package": "pywinrt",
            "projection": "pywinrt",
            "version": "3.2.1",
            "feature_level": feature_level,
            "capture_session_id": self.session_id,
        }

    async def _copy_frame(self, frame) -> None:
        from winrt.windows.graphics.imaging import SoftwareBitmap
        from winrt.windows.storage.streams import Buffer, DataReader

        software_bitmap = None
        data_reader = None
        try:
            software_bitmap = await SoftwareBitmap.create_copy_from_surface_async(
                frame.surface
            )
            width = software_bitmap.pixel_width
            height = software_bitmap.pixel_height
            pixel_buffer = Buffer(width * height * 4)
            software_bitmap.copy_to_buffer(pixel_buffer)
            data_reader = DataReader.from_buffer(pixel_buffer)
            raw = bytearray(pixel_buffer.length)
            data_reader.read_bytes(raw)
            image = Image.frombytes(
                "RGBA", (width, height), bytes(raw), "raw", "BGRA"
            ).convert("RGB")
            with self._image_lock:
                self._image = image
                self.metadata["frame_size"] = [width, height]
            self.frame_ready.set()
            self.logger.debug("WGC_WINSDK_FRAME_RECEIVED PASS session_id=%s", self.session_id)
        except Exception as exc:
            self.error = CaptureBackendError("winsdk-wgc", "WGC_SURFACE_COPY", str(exc), original=exc)
            self.frame_ready.set()
        finally:
            for resource in (data_reader, software_bitmap, frame):
                if resource is not None:
                    try:
                        resource.close()
                    except Exception:
                        pass

    def wait_for_image(self, timeout_seconds: float) -> Image.Image:
        if not self.frame_ready.wait(timeout_seconds):
            raise CaptureBackendError("winsdk-wgc", "WGC_FRAME_WAIT", "Frame timeout")
        if self.error is not None:
            raise self.error if isinstance(self.error, CaptureBackendError) else CaptureBackendError(
                "winsdk-wgc", "WGC_FRAME_WAIT", str(self.error), original=self.error
            )
        with self._image_lock:
            if self._image is None:
                raise CaptureBackendError("winsdk-wgc", "WGC_FRAME_WAIT", "No frame received")
            return self._image.copy()

    def _worker(self) -> None:
        desktop = None
        ro_initialized = False
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self.logger.info(
                "WGC_COM_INITIALIZATION START thread_id=%s apartment=MTA session_id=%s",
                threading.get_native_id(), self.session_id,
            )
            desktop = attach_input_desktop()
            self.logger.info("WGC_INPUT_DESKTOP PASS handle=0x%X", desktop)
            hresult = int(combase.RoInitialize(RO_INIT_MULTITHREADED))
            if hresult not in (0, 1):
                raise OSError(f"RoInitialize(MTA) failed HRESULT=0x{hresult & 0xFFFFFFFF:08X}")
            ro_initialized = True
            loop.run_until_complete(self._setup_async())
        except BaseException as exc:
            self.error = exc
        finally:
            self.ready.set()
        try:
            if self.error is None:
                loop.run_forever()
        finally:
            loop.run_until_complete(self._cleanup_async())
            loop.close()
            if ro_initialized:
                combase.RoUninitialize()
            if desktop:
                close_desktop(desktop)
            self.logger.info("WGC_CLEANUP PASS session_id=%s", self.session_id)

    async def _cleanup_async(self) -> None:
        if self._frame_pool is not None and self._frame_token is not None:
            try:
                self._frame_pool.remove_frame_arrived(self._frame_token)
            except Exception:
                pass
        for resource in (self._capture_session, self._frame_pool, self._item, self._device):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass

    def close(self) -> None:
        self.stop_requested.set()
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(timeout=3.0)


class WinsdkWgcBackend:
    """Direct HWND WGC with one long-lived session per target HWND."""

    name = "winsdk-wgc"

    def __init__(self, logger=None, timeout_seconds=3.0):
        self.logger = logger or logging.getLogger("ScreenBot.TargetCapture")
        self.timeout_seconds = timeout_seconds
        self._sessions: dict[int, _WinsdkWgcSession] = {}
        self._session_recreate_count = 0

    @staticmethod
    def _crop_to_client(hwnd: int, image: Image.Image) -> tuple[Image.Image, dict]:
        import win32gui

        client_width, client_height = get_client_size(hwnd)
        if image.size == (client_width, client_height):
            return image, {"client_crop": "not_required"}
        window_left, window_top, _, _ = win32gui.GetWindowRect(hwnd)
        client_left, client_top = win32gui.ClientToScreen(hwnd, (0, 0))
        offset_x, offset_y = client_left - window_left, client_top - window_top
        right, bottom = offset_x + client_width, offset_y + client_height
        if (
            offset_x < 0
            or offset_y < 0
            or right > image.width
            or bottom > image.height
        ):
            raise CaptureBackendError(
                "winsdk-wgc",
                "client_crop",
                f"Cannot map WGC frame {image.size} to client "
                f"{client_width}x{client_height} at offset {offset_x},{offset_y}",
            )
        return image.crop((offset_x, offset_y, right, bottom)), {
            "client_crop": [offset_x, offset_y, right, bottom]
        }

    def capture_client(self, hwnd: int) -> CaptureResult:
        if not user32.IsWindow(wintypes.HWND(hwnd)):
            raise CaptureBackendError(self.name, "validate_hwnd", "Invalid HWND")
        started = time.perf_counter()
        client_size = get_client_size(hwnd)
        session = self._sessions.get(hwnd)
        recreated = False
        if session is not None and session.client_size != client_size:
            self.logger.info("WGC_SESSION_RECREATE reason=client_size_changed hwnd=0x%08X", hwnd)
            session.close()
            self._sessions.pop(hwnd, None)
            session = None
        if session is None:
            session = _WinsdkWgcSession(self, hwnd, client_size)
            self._sessions[hwnd] = session
            self._session_recreate_count += 1
            recreated = True
            self.logger.info("WGC_SESSION_CREATE hwnd=0x%08X session_id=%s", hwnd, session.session_id)
            try:
                session.start(self.timeout_seconds)
            except Exception:
                self._sessions.pop(hwnd, None)
                raise
        try:
            image = session.wait_for_image(self.timeout_seconds)
        except Exception:
            self.invalidate_target(hwnd)
            raise
        image, crop_metadata = self._crop_to_client(hwnd, image)
        metadata = dict(session.metadata)
        metadata.update(crop_metadata)
        metadata.update({
            "capture_session_id": session.session_id,
            "session_recreated": recreated,
            "session_recreate_count": self._session_recreate_count,
        })
        return CaptureResult(
            image=image,
            backend=self.name,
            hwnd=hwnd,
            client_size=image.size,
            capture_duration_ms=int((time.perf_counter() - started) * 1000),
            metadata=metadata,
        )

    def close(self):
        self.invalidate_target()

    def invalidate_target(self, hwnd=None):
        keys = tuple(self._sessions) if hwnd is None else (int(hwnd),)
        for key in keys:
            session = self._sessions.pop(key, None)
            if session is not None:
                self.logger.info("WGC_SESSION_CLOSE hwnd=0x%08X session_id=%s", key, session.session_id)
                session.close()


BACKEND_TYPES = {
    PrintWindowBackend.name: PrintWindowBackend,
    WindowsCapBackend.name: WindowsCapBackend,
    WinsdkWgcBackend.name: WinsdkWgcBackend,
    BitBltBackend.name: BitBltBackend,
}


def create_backend(name: str, logger=None) -> CaptureBackend:
    aliases = {
        "print_window_ctypes": "printwindow",
        "windows_graphics_capture": "winsdk-wgc",
        "wgc": "winsdk-wgc",
        "client_dc_bitblt": "bitblt",
    }
    normalized = aliases.get(name.lower(), name.lower())
    backend_type = BACKEND_TYPES.get(normalized)
    if backend_type is None:
        raise ValueError(f"Unknown capture backend: {name}")
    return backend_type(logger)
