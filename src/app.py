from __future__ import annotations

import ctypes
import math
import os
import random
import subprocess
import threading
import time
import urllib.request
from dataclasses import dataclass
from functools import partial
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QItemSelectionModel, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFileDialog,
    QFrame,
    QGroupBox,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSlider,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .adb_client import (
    AdbError,
    close_facebook,
    close_all_recent_apps,
    clear_facebook_data,
    clear_proxy,
    clear_college_proxy_data,
    go_facebook_home,
    like_random_post_or_reel,
    login_facebook,
    launch_scrcpy,
    list_devices,
    set_proxy,
    connect_college_proxy,
    check_proxy_status,
    set_wifi,
    connect_wifi,
    open_facebook,
    open_facebook_reels,
    open_google_app,
    _read_news_in_google,
    open_link,
    input_text,
    ensure_adb_keyboard,
    install_apk,
    push_image_to_device,
    change_facebook_avatar,
    update_fb_avatar_and_bio,
    download_bulk_avatars,
    swipe,
    suggest_add_friends,
    accept_friend_requests,
    tap,
    keyevent,
    follow_facebook_page,
    join_facebook_group,
    get_fb_info,
    farm_story,
    check_fb_uid_live_die,
    request_stop_serials,
    request_stop_all,
    reset_stop_event,
    is_stop_requested,
)
from .models import DeviceInfo
from .profiles import ProfileStore
from .follow_store import FollowHistoryStore


if os.name == "nt":
    USER32 = ctypes.windll.user32
    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    SW_SHOW = 5
    SW_HIDE = 0
    SWP_NOMOVE = 0x0002
    SWP_NOSIZE = 0x0001
    SWP_NOZORDER = 0x0004
    SWP_FRAMECHANGED = 0x0020
    WS_CHILD = 0x40000000
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    WS_SYSMENU = 0x00080000
    WS_POPUP = 0x80000000
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TOOLWINDOW = 0x00000080

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]


if os.name == "nt":
    WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [
            ("pt", POINT),
            ("mouseData", ctypes.c_uint32),
            ("flags", ctypes.c_uint32),
            ("time", ctypes.c_uint32),
            ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))
        ]


class ScrcpyInputHook:
    """Win32 low-level hooks: captures keystrokes and mouse clicks when Screen Wall is
    the foreground window and forwards them to the active device via ADB."""

    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    WM_SYSKEYDOWN = 0x0104
    WM_SYSKEYUP = 0x0105
    WM_MOUSEMOVE = 0x0200
    WM_LBUTTONDOWN = 0x0201
    WM_LBUTTONUP = 0x0202
    HC_ACTION = 0

    if os.name == "nt":
        _KB_HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_byte * 24)
        )
        
        _MS_HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(MSLLHOOKSTRUCT)
        )

        USER32.WindowFromPoint.argtypes = [POINT]
        USER32.WindowFromPoint.restype = ctypes.c_void_p
        USER32.ScreenToClient.argtypes = [ctypes.c_void_p, ctypes.POINTER(POINT)]
        USER32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        USER32.GetAncestor.restype = ctypes.c_void_p
        USER32.SetFocus.argtypes = [ctypes.c_void_p]
        USER32.SetFocus.restype = ctypes.c_void_p
        USER32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]

    def __init__(self) -> None:
        self._kb_hook = None
        self._ms_hook = None
        self._kb_cb = None
        self._ms_cb = None
        self.wall_hwnd: int = 0
        self.hwnd_to_serial: dict[int, str] = {}
        self._cached_serial: str | None = None
        
        self.is_sync_enabled = False
        self.master_serial = ""
        self._drag_start = None
        self._executor = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=200)
        self.sync_allowed_serials = set()
        
        # Manually track modifier states
        self._ctrl_held = False
        self._alt_held = False

    def set_wall_hwnd(self, hwnd: int) -> None:
        self.wall_hwnd = hwnd

    def update_scrcpy_hwnds(self, hwnds: dict[str, int]) -> None:
        self.hwnd_to_serial = {h: s for s, h in hwnds.items()}

    def set_sync_enabled(self, enabled: bool) -> None:
        self.is_sync_enabled = enabled
        
    def set_sync_master(self, serial: str) -> None:
        self.master_serial = serial

    def install(self) -> None:
        if os.name != "nt" or self._kb_hook:
            return
        
        self._kb_cb = self._KB_HOOKPROC(self._ll_keyboard_proc)
        self._kb_hook = USER32.SetWindowsHookExW(
            self.WH_KEYBOARD_LL, self._kb_cb, None, 0
        )
        
        self._ms_cb = self._MS_HOOKPROC(self._ll_mouse_proc)
        self._ms_hook = USER32.SetWindowsHookExW(
            self.WH_MOUSE_LL, self._ms_cb, None, 0
        )

    def uninstall(self) -> None:
        if self._kb_hook:
            USER32.UnhookWindowsHookEx(self._kb_hook)
            self._kb_hook = None
        if self._ms_hook:
            USER32.UnhookWindowsHookEx(self._ms_hook)
            self._ms_hook = None

    def _get_active_slaves(self, exclude: str = "") -> list[str]:
        return [s for s in self.hwnd_to_serial.values() if s != exclude and s in self.sync_allowed_serials]

    def set_sync_allowed(self, serial: str, allowed: bool) -> None:
        if allowed:
            self.sync_allowed_serials.add(serial)
        else:
            self.sync_allowed_serials.discard(serial)

    def _ll_mouse_proc(self, nCode, wParam, lParam):
        if nCode == self.HC_ACTION:
            if lParam:
                try:
                    pt = lParam.contents.pt
                    curr_hwnd = USER32.WindowFromPoint(pt)
                    found_serial = None
                    check_hwnd = curr_hwnd
                    while check_hwnd:
                        serial = self.hwnd_to_serial.get(check_hwnd)
                        if not serial:
                            try:
                                serial = self.hwnd_to_serial.get(int(check_hwnd))
                            except Exception:
                                pass
                        if serial:
                            found_serial = serial
                            self._cached_serial = serial
                            if wParam == self.WM_LBUTTONDOWN:
                                try:
                                    USER32.SetFocus(check_hwnd)
                                except Exception:
                                    pass
                            break
                        check_hwnd = USER32.GetParent(check_hwnd)
                except Exception:
                    pass

                if (wParam == self.WM_LBUTTONDOWN or wParam == self.WM_LBUTTONUP) and found_serial:
                    # Handling Sync
                    if self.is_sync_enabled and found_serial == self.master_serial:
                        rect = RECT()
                        if USER32.GetClientRect(check_hwnd, ctypes.byref(rect)):
                            w = rect.right - rect.left
                            h = rect.bottom - rect.top
                            client_pt = POINT(pt.x, pt.y)
                            USER32.ScreenToClient(check_hwnd, ctypes.byref(client_pt))
                            if 0 <= client_pt.x <= w and 0 <= client_pt.y <= h:
                                px = client_pt.x / w
                                py = client_pt.y / h
                                if wParam == self.WM_LBUTTONDOWN:
                                    self._drag_start = (px, py, time.time())
                                elif wParam == self.WM_LBUTTONUP and self._drag_start:
                                    sx, sy, st = self._drag_start
                                    self._drag_start = None
                                    dt = time.time() - st
                                    slaves = self._get_active_slaves(exclude=found_serial)
                                    if slaves:
                                        if abs(px - sx) < 0.02 and abs(py - sy) < 0.02:
                                            self._dispatch_sync_tap(slaves, px, py)
                                        else:
                                            duration = int(dt * 1000)
                                            self._dispatch_sync_swipe(slaves, sx, sy, px, py, max(100, min(2000, duration)))
        return USER32.CallNextHookEx(self._ms_hook, nCode, wParam, lParam)

    def _dispatch_sync_tap(self, slaves: list[str], px: float, py: float) -> None:
        def work(serial):
            from .adb_client import get_screen_size, adb_shell
            w, h = get_screen_size(serial)
            if w > 0 and h > 0:
                tx, ty = int(w * px), int(h * py)
                adb_shell(serial, "input", "tap", str(tx), str(ty), check=False, timeout=5)
        for s in slaves:
            self._executor.submit(work, s)

    def _dispatch_sync_swipe(self, slaves: list[str], sx: float, sy: float, px: float, py: float, duration: int) -> None:
        def work(serial):
            from .adb_client import get_screen_size, adb_shell
            w, h = get_screen_size(serial)
            if w > 0 and h > 0:
                x1, y1 = int(w * sx), int(h * sy)
                x2, y2 = int(w * px), int(h * py)
                adb_shell(serial, "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration), check=False, timeout=5)
        for s in slaves:
            self._executor.submit(work, s)

    def _get_clipboard_text(self) -> str:
        # 1. Try PySide6 QApplication clipboard first
        try:
            from PySide6.QtWidgets import QApplication
            cb = QApplication.clipboard()
            if cb:
                txt = cb.text()
                if txt:
                    return txt
        except Exception:
            pass

        # 2. Win32 API fallback with CF_UNICODETEXT (13) & CF_TEXT (1)
        import ctypes
        import time
        USER32 = ctypes.windll.user32
        KERNEL32 = ctypes.windll.kernel32
        
        opened = False
        for _ in range(15):
            if USER32.OpenClipboard(0):
                opened = True
                break
            time.sleep(0.01)
            
        if not opened:
            return ""
            
        try:
            # Try CF_UNICODETEXT (13)
            handle = USER32.GetClipboardData(13)
            if handle:
                ptr = KERNEL32.GlobalLock(handle)
                if ptr:
                    try:
                        text = ctypes.c_wchar_p(ptr).value
                        if text:
                            return text
                    finally:
                        KERNEL32.GlobalUnlock(handle)

            # Try CF_TEXT (1)
            handle_ansi = USER32.GetClipboardData(1)
            if handle_ansi:
                ptr = KERNEL32.GlobalLock(handle_ansi)
                if ptr:
                    try:
                        text_bytes = ctypes.c_char_p(ptr).value
                        if text_bytes:
                            return text_bytes.decode("utf-8", errors="replace")
                    finally:
                        KERNEL32.GlobalUnlock(handle_ansi)
        except Exception as exc:
            print(f"[Clipboard] Error reading clipboard: {exc}")
        finally:
            USER32.CloseClipboard()
            
        return ""

    def _serial_under_cursor(self) -> str | None:
        """Detect which scrcpy device the mouse cursor is currently hovering over."""
        try:
            pt = POINT()
            USER32.GetCursorPos(ctypes.byref(pt))
            hwnd = USER32.WindowFromPoint(pt)
            checked = set()
            while hwnd and hwnd not in checked:
                checked.add(hwnd)
                serial = self.hwnd_to_serial.get(hwnd)
                if serial:
                    return serial
                # Also try int conversion in case of type mismatch
                hwnd_int = int(hwnd) if hwnd else 0
                serial = self.hwnd_to_serial.get(hwnd_int)
                if serial:
                    return serial
                hwnd = USER32.GetParent(hwnd)
        except Exception:
            pass
        return None

    def _ll_keyboard_proc(self, nCode, wParam, lParam):
        try:
            if nCode == self.HC_ACTION:
                raw = ctypes.cast(lParam, ctypes.POINTER(ctypes.c_uint32))
                vk = raw[0]

                # Track modifiers manually to bypass any OS/UIPI/Thread async issues
                if wParam in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN):
                    if vk in (0x11, 0xA2, 0xA3): self._ctrl_held = True
                    elif vk in (0x12, 0xA4, 0xA5): self._alt_held = True
                elif wParam in (self.WM_KEYUP, self.WM_SYSKEYUP):
                    if vk in (0x11, 0xA2, 0xA3): self._ctrl_held = False
                    elif vk in (0x12, 0xA4, 0xA5): self._alt_held = False

                if wParam == self.WM_KEYDOWN and self._is_wall_foreground():
                    serial = self._serial_under_cursor() or self._cached_serial
                    if serial:
                        self._cached_serial = serial
                        
                        # Bypass hook for lone modifier presses so the OS properly tracks state
                        if vk in (0x11, 0xA2, 0xA3, 0x10, 0xA0, 0xA1, 0x12, 0xA4, 0xA5, 0x5B, 0x5C):
                            return USER32.CallNextHookEx(self._kb_hook, nCode, wParam, lParam)

                        if self._ctrl_held or self._alt_held:
                            # Ctrl+V or Alt+V -> PASTE
                            if vk == 0x56:
                                text = self._get_clipboard_text()
                                if text:
                                    targets = [serial]
                                    if self.is_sync_enabled and serial == self.master_serial:
                                        targets.extend(self._get_active_slaves(exclude=serial))
                                    for t in set(targets):
                                        from .adb_client import input_text
                                        threading.Thread(target=lambda s=t, txt=text: input_text(s, txt), daemon=True).start()
                                return 1
                            
                            # Ctrl+C -> COPY
                            elif self._ctrl_held and vk == 0x43:
                                targets = [serial]
                                if self.is_sync_enabled and serial == self.master_serial:
                                    targets.extend(self._get_active_slaves(exclude=serial))
                                for t in set(targets):
                                    from .adb_client import keyevent
                                    threading.Thread(target=lambda s=t: keyevent(s, 278), daemon=True).start()
                                return 1

                            # Ctrl+A -> SELECT ALL
                            elif self._ctrl_held and vk == 0x41:
                                targets = [serial]
                                if self.is_sync_enabled and serial == self.master_serial:
                                    targets.extend(self._get_active_slaves(exclude=serial))
                                for t in set(targets):
                                    from .adb_client import keyevent
                                    threading.Thread(target=lambda s=t: keyevent(s, 288), daemon=True).start()
                                return 1

                            # Ctrl+X -> CUT
                            elif self._ctrl_held and vk == 0x58:
                                targets = [serial]
                                if self.is_sync_enabled and serial == self.master_serial:
                                    targets.extend(self._get_active_slaves(exclude=serial))
                                for t in set(targets):
                                    from .adb_client import keyevent
                                    threading.Thread(target=lambda s=t: keyevent(s, 277), daemon=True).start()
                                return 1

                            else:
                                return USER32.CallNextHookEx(self._kb_hook, nCode, wParam, lParam)

                        targets = [serial]
                        if self.is_sync_enabled and serial == self.master_serial:
                            targets.extend(self._get_active_slaves(exclude=serial))
                            
                        for t in set(targets):
                            self._dispatch_key(t, vk)
                        return 1
        except Exception as e:
            print(f"[KeyboardHook] ERROR in _ll_keyboard_proc: {e}")
        return USER32.CallNextHookEx(self._kb_hook, nCode, wParam, lParam)

    def _is_wall_foreground(self) -> bool:
        try:
            fg = USER32.GetForegroundWindow()
            if not fg:
                return False
            if self.wall_hwnd:
                root_wall = USER32.GetAncestor(self.wall_hwnd, 2)
                root_fg = USER32.GetAncestor(fg, 2)
                if root_wall and root_fg and root_wall == root_fg:
                    return True
                if fg == self.wall_hwnd or fg == root_wall:
                    return True
            if fg in self.hwnd_to_serial or int(fg) in self.hwnd_to_serial:
                return True
            p = USER32.GetParent(fg)
            while p:
                if p == self.wall_hwnd or p in self.hwnd_to_serial or int(p) in self.hwnd_to_serial:
                    return True
                p = USER32.GetParent(p)
        except Exception:
            pass
        return False

    def _dispatch_key(self, serial: str, vk: int) -> None:
        from .adb_client import keyevent, input_text
        if vk == 0x08: threading.Thread(target=lambda: keyevent(serial, 67), daemon=True).start()
        elif vk == 0x0D: threading.Thread(target=lambda: keyevent(serial, 66), daemon=True).start()
        elif vk == 0x09: threading.Thread(target=lambda: keyevent(serial, 61), daemon=True).start()
        elif vk == 0x2E: threading.Thread(target=lambda: keyevent(serial, 67), daemon=True).start()
        elif vk == 0x20: threading.Thread(target=lambda: input_text(serial, " "), daemon=True).start()
        elif 0x30 <= vk <= 0x39:
            ch = chr(vk)
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif 0x41 <= vk <= 0x5A:
            shift = USER32.GetAsyncKeyState(0x10) & 0x8000
            caps = USER32.GetKeyState(0x14) & 1
            upper = bool(shift) ^ bool(caps)
            ch = chr(vk) if upper else chr(vk + 32)
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif vk == 0xBE: threading.Thread(target=lambda: input_text(serial, "."), daemon=True).start()
        elif vk == 0xBC: threading.Thread(target=lambda: input_text(serial, ","), daemon=True).start()
        elif vk == 0xBD:
            ch = "_" if (USER32.GetAsyncKeyState(0x10) & 0x8000) else "-"
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif vk == 0xBB:
            ch = "+" if (USER32.GetAsyncKeyState(0x10) & 0x8000) else "="
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif vk == 0xBA:
            ch = ":" if (USER32.GetAsyncKeyState(0x10) & 0x8000) else ";"
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif vk == 0xDE:
            ch = '"' if (USER32.GetAsyncKeyState(0x10) & 0x8000) else "'"
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif vk == 0xBF:
            ch = "?" if (USER32.GetAsyncKeyState(0x10) & 0x8000) else "/"
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif vk == 0xC0:
            ch = "~" if (USER32.GetAsyncKeyState(0x10) & 0x8000) else "`"
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif vk == 0xDB:
            ch = "{" if (USER32.GetAsyncKeyState(0x10) & 0x8000) else "["
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif vk == 0xDC:
            ch = "|" if (USER32.GetAsyncKeyState(0x10) & 0x8000) else "\\"
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif vk == 0xDD:
            ch = "}" if (USER32.GetAsyncKeyState(0x10) & 0x8000) else "]"
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif 0x60 <= vk <= 0x69:
            ch = chr(vk - 0x60 + 0x30)
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()

            pass  # already handled above


def _enum_windows() -> list[int]:
    if os.name != "nt":
        return []
    hwnds: list[int] = []

    def _callback(hwnd, _lparam):
        hwnds.append(int(hwnd))
        return True

    cb = WNDENUMPROC(_callback)
    USER32.EnumWindows(cb, 0)
    return hwnds


def _window_text(hwnd: int) -> str:
    if os.name != "nt":
        return ""
    buffer = ctypes.create_unicode_buffer(512)
    USER32.GetWindowTextW(hwnd, buffer, len(buffer))
    return buffer.value


def _window_pid(hwnd: int) -> int:
    if os.name != "nt":
        return 0
    pid = ctypes.c_uint(0)
    USER32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _is_valid_scrcpy_window(hwnd: int) -> bool:
    if not USER32.IsWindowVisible(hwnd):
        return False
    rect = RECT()
    if USER32.GetWindowRect(hwnd, ctypes.byref(rect)):
        w = abs(rect.right - rect.left)
        h = abs(rect.bottom - rect.top)
        if w < 50 or h < 50:
            return False
        return True
    return False


def _find_scrcpy_window(serial: str, pid: int = 0, exclude_hwnds: set[int] | None = None) -> int | None:
    if os.name != "nt":
        return None
    exclude = exclude_hwnds or set()

    # 1. Match PID + scrcpy title
    if pid:
        for hwnd in _enum_windows():
            if hwnd in exclude or not _is_valid_scrcpy_window(hwnd):
                continue
            if _window_pid(hwnd) == pid:
                title = _window_text(hwnd)
                if serial in title or "scrcpy" in title.lower():
                    return hwnd

    # 2. Match serial in title
    for hwnd in _enum_windows():
        if hwnd in exclude or not _is_valid_scrcpy_window(hwnd):
            continue
        title = _window_text(hwnd)
        if serial in title:
            return hwnd

    return None


def _reparent_window(hwnd: int, parent_hwnd: int) -> None:
    if os.name != "nt" or not hwnd or not parent_hwnd:
        return

    # Attach hwnd directly to parent QFrame HWND
    USER32.SetParent(hwnd, parent_hwnd)

    style = USER32.GetWindowLongW(hwnd, GWL_STYLE)
    style = (style | WS_CHILD) & ~(WS_CAPTION | WS_THICKFRAME | WS_MINIMIZEBOX | WS_MAXIMIZEBOX | WS_SYSMENU | WS_POPUP)
    USER32.SetWindowLongW(hwnd, GWL_STYLE, style)

    # Remove taskbar button for embedded scrcpy windows.
    ex_style = USER32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    ex_style = (ex_style | WS_EX_TOOLWINDOW) & ~WS_EX_APPWINDOW
    USER32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style)

    USER32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_FRAMECHANGED)
    USER32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE


def _move_window(hwnd: int, width: int, height: int) -> None:
    if os.name != "nt":
        return
    USER32.MoveWindow(hwnd, 0, 0, width, height, True)


def _focus_embedded_window(hwnd: int) -> None:
    if os.name != "nt" or not hwnd:
        return
    try:
        USER32.SetFocus(hwnd)
        USER32.SetForegroundWindow(hwnd)
        USER32.SendMessageW(hwnd, 0x0007, 0, 0)  # WM_SETFOCUS
        USER32.SendMessageW(hwnd, 0x0006, 1, 0)  # WM_ACTIVATE (WA_ACTIVE)
    except Exception:
        pass


class ScreenHostWidget(QFrame):
    """Container for embedded scrcpy window."""

    def __init__(self, serial: str, parent=None) -> None:
        super().__init__(parent)
        self.serial = serial
        self.hwnd: int | None = None
        self.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)

    def set_hwnd(self, hwnd: int) -> None:
        self.hwnd = hwnd


class ClipboardTableWidget(QTableWidget):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)

    def _show_context_menu(self, pos) -> None:
        if not self.selectedIndexes():
            return
        menu = QMenu(self)
        copy_act = menu.addAction("Sao chép các ô đang bôi đen (Ctrl+C)")
        copy_act.triggered.connect(self._copy_selected_cells)
        menu.exec(self.viewport().mapToGlobal(pos))

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy) or (
            (event.modifiers() & Qt.KeyboardModifier.ControlModifier) and event.key() == Qt.Key.Key_C
        ):
            self._copy_selected_cells()
            return
        super().keyPressEvent(event)

    def _copy_selected_cells(self) -> None:
        indexes = self.selectedIndexes()
        if not indexes:
            return

        rows: dict[int, dict[int, str]] = {}
        for index in indexes:
            item = self.item(index.row(), index.column())
            if item is None:
                cell_text = ""
            else:
                cell_text = item.text().strip()
                if not cell_text and item.checkState() != Qt.CheckState.Unchecked:
                    cell_text = "☑"
            rows.setdefault(index.row(), {})[index.column()] = cell_text

        lines: list[str] = []
        for row in sorted(rows):
            columns = rows[row]
            ordered_values = [columns[column] for column in sorted(columns)]
            lines.append("\t".join(ordered_values))

        if lines:
            QApplication.clipboard().setText("\n".join(lines))


class ScreenWallWindow(QMainWindow):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.Window)
        self.setWindowTitle("Quản lý Màn hình Thiết bị (ADB Screen Wall)")
        self.resize(1100, 720)
        self._is_closing = False
        self._reflow_cb = None

        # Win32 keyboard hook for forwarding keystrokes to device
        self._keyboard_hook = ScrcpyInputHook()

        self.setStyleSheet(
            """
            QMainWindow { background: #0f172a; }
            QWidget { color: #e5e7eb; font-family: "Segoe UI"; font-size: 13px; }
            QLabel#wallTitle { font-size: 16px; font-weight: bold; color: #38bdf8; }
            QFrame#screenCard { background: #0f172a; border: 1px solid #334155; border-radius: 10px; }
            QFrame#screenHost { background: #020617; border: 1px solid #1f2937; border-radius: 8px; }
            QLabel#slotStatus { color: #93c5fd; font-size: 11px; }
            QPushButton {
                background-color: #2563eb; color: white; border: none; border-radius: 6px; padding: 6px 14px; font-weight: bold;
            }
            QPushButton:hover { background-color: #3b82f6; }
            QPushButton:pressed { background-color: #1d4ed8; }
            QSlider::groove:horizontal { border: 1px solid #334155; height: 6px; background: #1e293b; border-radius: 3px; }
            QSlider::handle:horizontal { background: #3b82f6; border: none; width: 14px; height: 14px; margin: -4px 0; border-radius: 7px; }
            QScrollArea { background: #0b1220; border: 1px solid #1f2937; border-radius: 12px; }
            """
        )

        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        header_layout = QHBoxLayout()
        header_layout.setSpacing(12)

        self.title_label = QLabel("Màn hình điều khiển thiết bị")
        self.title_label.setObjectName("wallTitle")

        self.chk_sync = QCheckBox("Đồng bộ (Sync)")
        self.chk_sync.setToolTip("Gửi thao tác phím/chuột từ máy Master tới các máy khác")
        self.chk_sync.toggled.connect(self._on_sync_toggled)

        self.scale_label = QLabel("Kích thước màn: 55%")
        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(30, 100)
        self.scale_slider.setValue(55)
        self.scale_slider.setFixedWidth(150)

        self.btn_stop_all = QPushButton("🛑 Dừng tiến trình")
        self.btn_stop_all.setCursor(Qt.PointingHandCursor)
        self.btn_stop_all.setStyleSheet("background-color: #dc2626; color: white; font-weight: bold; border-radius: 6px; padding: 6px 14px;")

        self.btn_clear_all = QPushButton("Đóng toàn bộ màn hình")
        self.btn_clear_all.setCursor(Qt.PointingHandCursor)

        header_layout.addWidget(self.title_label)
        header_layout.addStretch(1)
        header_layout.addWidget(self.btn_stop_all)
        header_layout.addWidget(self.chk_sync)
        header_layout.addWidget(self.scale_label)
        header_layout.addWidget(self.scale_slider)
        header_layout.addWidget(self.btn_clear_all)

        self.screen_wall = QWidget()
        self.screen_grid = QGridLayout(self.screen_wall)
        self.screen_grid.setContentsMargins(8, 8, 8, 8)
        self.screen_grid.setHorizontalSpacing(10)
        self.screen_grid.setVerticalSpacing(10)

        self.screen_scroll = QScrollArea()
        self.screen_scroll.setWidgetResizable(True)
        self.screen_scroll.setWidget(self.screen_wall)

        layout.addLayout(header_layout)
        layout.addWidget(self.screen_scroll)

        self.setCentralWidget(central)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # Install keyboard hook and set wall HWND when window is first shown
        self._keyboard_hook.set_wall_hwnd(int(self.winId()))
        self._keyboard_hook.install()

    def set_reflow_callback(self, cb) -> None:
        self._reflow_cb = cb

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._reflow_cb and callable(self._reflow_cb):
            QTimer.singleShot(0, self._reflow_cb)

    def close_permanently(self) -> None:
        self._is_closing = True
        self._keyboard_hook.uninstall()
        self.close()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._is_closing:
            super().closeEvent(event)
        else:
            event.ignore()
            self.hide()

    def _on_sync_toggled(self, checked: bool) -> None:
        self._keyboard_hook.set_sync_enabled(checked)

    def update_master_list(self, serials: list[str]) -> None:
        if serials:
            # Luôn chọn máy đầu tiên làm master
            self._keyboard_hook.set_sync_master(serials[0])
        else:
            self._keyboard_hook.set_sync_master("")



@dataclass
class ScreenSlot:
    serial: str
    root: QFrame
    host: QFrame
    title: QLabel
    status: QLabel
    refresh_btn: QPushButton
    close_btn: QPushButton
    sync_chk: __import__("PySide6.QtWidgets").QtWidgets.QCheckBox
    base_w: int
    base_h: int


class TaskThread(QThread):
    succeeded = Signal(object)
    failed = Signal(str)

    def __init__(self, func) -> None:
        super().__init__()
        self.func = func

    def run(self) -> None:
        try:
            result = self.func()
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(result)

class FarmConfigDialog(QDialog):
    def __init__(self, parent=None, mode: str = "farm") -> None:
        super().__init__(parent)
        self.mode = (mode or "farm").lower()
        self.setWindowTitle("Cấu hình nghiệp vụ Mail" if self.mode == "mail" else "Cấu hình Tương tác & Nuôi nick Facebook")
        self.resize(980, 760)
        
        self.setStyleSheet("""
            QDialog {
                background-color: #0f172a;
            }
            QWidget {
                color: #e5e7eb;
                font-family: "Segoe UI";
                font-size: 13px;
            }
            QGroupBox {
                font-weight: bold;
                border: 1px solid #334155;
                border-radius: 8px;
                margin-top: 16px;
                padding-top: 16px;
                background-color: #1e293b;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                subcontrol-position: top left;
                left: 12px;
                padding: 0 6px;
                color: #38bdf8;
            }
            QLineEdit, QTextEdit, QSpinBox {
                background-color: #0b1220;
                color: #f8fafc;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 8px;
            }
            QLineEdit:focus, QTextEdit:focus, QSpinBox:focus {
                border: 1px solid #3b82f6;
            }
            QTabWidget::pane {
                border: 1px solid #334155;
                background-color: #0f172a;
                border-radius: 6px;
            }
            QTabBar::tab {
                background-color: #1e293b;
                color: #94a3b8;
                padding: 10px 20px;
                border: 1px solid #334155;
                border-bottom: none;
                border-top-left-radius: 6px;
                border-top-right-radius: 6px;
                margin-right: 4px;
            }
            QTabBar::tab:selected {
                background-color: #3b82f6;
                color: #ffffff;
                font-weight: bold;
            }
            QTabBar::tab:hover:!selected {
                background-color: #334155;
            }
            QPushButton {
                background-color: #2563eb;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 10px 20px;
                font-weight: bold;
            }
            QPushButton:hover {
                background-color: #3b82f6;
            }
            QPushButton:pressed {
                background-color: #1d4ed8;
            }
            QCheckBox {
                spacing: 8px;
            }
            QCheckBox::indicator {
                width: 18px;
                height: 18px;
                border-radius: 4px;
                border: 1px solid #475569;
                background-color: #0b1220;
            }
            QCheckBox::indicator:checked {
                background-color: #3b82f6;
                border: 1px solid #3b82f6;
            }
        """)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)

        # Tab 1: Tương Tác
        tab1 = QWidget()
        l1 = QVBoxLayout(tab1)
        l1.setSpacing(15)
        
        group_feed = QGroupBox("Tương tác chung (Newsfeed & Video)")
        g_feed_layout = QGridLayout(group_feed)
        g_feed_layout.setHorizontalSpacing(8)
        g_feed_layout.setVerticalSpacing(8)

        self.chk_newsfeed = QCheckBox("Lướt Newsfeed")
        self.spin_newsfeed_time_min = QSpinBox()
        self.spin_newsfeed_time_min.setRange(1, 720)
        self.spin_newsfeed_time_min.setValue(3)
        self.spin_newsfeed_time_max = QSpinBox()
        self.spin_newsfeed_time_max.setRange(1, 720)
        self.spin_newsfeed_time_max.setValue(5)
        self.spin_newsfeed_interval_min = QSpinBox()
        self.spin_newsfeed_interval_min.setRange(1, 3600)
        self.spin_newsfeed_interval_min.setValue(5)
        self.spin_newsfeed_interval_max = QSpinBox()
        self.spin_newsfeed_interval_max.setRange(1, 3600)
        self.spin_newsfeed_interval_max.setValue(10)

        self.chk_video = QCheckBox("Xem Video / Reels")
        self.spin_video_time_min = QSpinBox()
        self.spin_video_time_min.setRange(1, 720)
        self.spin_video_time_min.setValue(5)
        self.spin_video_time_max = QSpinBox()
        self.spin_video_time_max.setRange(1, 720)
        self.spin_video_time_max.setValue(10)
        self.spin_video_interval_min = QSpinBox()
        self.spin_video_interval_min.setRange(1, 3600)
        self.spin_video_interval_min.setValue(8)
        self.spin_video_interval_max = QSpinBox()
        self.spin_video_interval_max.setRange(1, 3600)
        self.spin_video_interval_max.setValue(25)

        self.chk_like = QCheckBox("Like dạo ngẫu nhiên")
        self.chk_like.setEnabled(False)
        self.chk_like.setChecked(False)

        self.spin_like_count_min = QSpinBox()
        self.spin_like_count_min.setRange(1, 999)
        self.spin_like_count_min.setValue(1)
        self.spin_like_count_min.setEnabled(False)

        self.spin_like_count_max = QSpinBox()
        self.spin_like_count_max.setRange(1, 999)
        self.spin_like_count_max.setValue(3)
        self.spin_like_count_max.setEnabled(False)

        def update_like_enabled():
            enabled = self.chk_newsfeed.isChecked() or self.chk_video.isChecked()

            self.chk_like.setEnabled(enabled)

            if not enabled:
                self.chk_like.setChecked(False)
                self.spin_like_count_min.setEnabled(False)
                self.spin_like_count_max.setEnabled(False)

        self.chk_newsfeed.toggled.connect(update_like_enabled)
        self.chk_video.toggled.connect(update_like_enabled)

        self.chk_like.toggled.connect(self.spin_like_count_min.setEnabled)
        self.chk_like.toggled.connect(self.spin_like_count_max.setEnabled)

        update_like_enabled()

        def add_option_row(row: int, label: str, min_spin: QSpinBox, max_spin: QSpinBox, unit: str, checkbox: QCheckBox) -> None:
            g_feed_layout.addWidget(QLabel(label), row, 0)
            g_feed_layout.addWidget(min_spin, row, 1)
            g_feed_layout.addWidget(QLabel("-"), row, 2)
            g_feed_layout.addWidget(max_spin, row, 3)
            g_feed_layout.addWidget(QLabel(unit), row, 4)
            for widget in (min_spin, max_spin):
                widget.setEnabled(False)
                checkbox.toggled.connect(widget.setEnabled)

        g_feed_layout.addWidget(self.chk_newsfeed, 0, 0, 1, 5)
        add_option_row(1, "Thời gian:", self.spin_newsfeed_time_min, self.spin_newsfeed_time_max, "phút", self.chk_newsfeed)
        add_option_row(2, "Giữa các lần lướt:", self.spin_newsfeed_interval_min, self.spin_newsfeed_interval_max, "giây", self.chk_newsfeed)
        g_feed_layout.addWidget(self.chk_video, 3, 0, 1, 5)
        add_option_row(4, "Thời gian:", self.spin_video_time_min, self.spin_video_time_max, "phút", self.chk_video)
        add_option_row(5, "Giữa các lần lướt:", self.spin_video_interval_min, self.spin_video_interval_max, "giây", self.chk_video)
        g_feed_layout.addWidget(self.chk_like, 6, 0, 1, 5)
        add_option_row(7, "Số lượng:", self.spin_like_count_min, self.spin_like_count_max, "lần", self.chk_like)
        l1.addWidget(group_feed)



        group_follow = QGroupBox("Theo dõi / Tham gia")
        g_follow_layout = QVBoxLayout(group_follow)

        self.chk_follow = QCheckBox("Theo dõi Page (hàng loạt)")
        g_follow_layout.addWidget(self.chk_follow)

        self.inp_follow_links = QTextEdit()
        self.inp_follow_links.setPlaceholderText("Nhập danh sách link Page (mỗi dòng 1 URL)...\nhttps://www.facebook.com/page1\nhttps://www.facebook.com/page2")
        self.inp_follow_links.setMaximumHeight(90)
        self.inp_follow_links.setEnabled(False)
        g_follow_layout.addWidget(self.inp_follow_links)

        follow_count_row = QHBoxLayout()
        follow_count_row.addWidget(QLabel("Mỗi nick follow:"))
        follow_count_row.addWidget(QLabel("Tối thiểu:"))
        self.spin_follow_min = QSpinBox()
        self.spin_follow_min.setRange(1, 50)
        self.spin_follow_min.setValue(3)
        self.spin_follow_min.setEnabled(False)
        follow_count_row.addWidget(self.spin_follow_min)
        follow_count_row.addWidget(QLabel("Tối đa:"))
        self.spin_follow_max = QSpinBox()
        self.spin_follow_max.setRange(1, 50)
        self.spin_follow_max.setValue(5)
        self.spin_follow_max.setEnabled(False)
        follow_count_row.addWidget(self.spin_follow_max)
        follow_count_row.addWidget(QLabel("page"))
        follow_count_row.addStretch()
        g_follow_layout.addLayout(follow_count_row)

        follow_delay_row = QHBoxLayout()
        follow_delay_row.addWidget(QLabel("Delay giữa mỗi page:"))
        self.spin_follow_delay_min = QSpinBox()
        self.spin_follow_delay_min.setRange(5, 300)
        self.spin_follow_delay_min.setValue(15)
        self.spin_follow_delay_min.setEnabled(False)
        follow_delay_row.addWidget(self.spin_follow_delay_min)
        follow_delay_row.addWidget(QLabel("đến"))
        self.spin_follow_delay_max = QSpinBox()
        self.spin_follow_delay_max.setRange(5, 300)
        self.spin_follow_delay_max.setValue(45)
        self.spin_follow_delay_max.setEnabled(False)
        follow_delay_row.addWidget(self.spin_follow_delay_max)
        follow_delay_row.addWidget(QLabel("giây"))
        follow_delay_row.addStretch()
        g_follow_layout.addLayout(follow_delay_row)

        self.chk_follow.toggled.connect(self.inp_follow_links.setEnabled)
        self.chk_follow.toggled.connect(self.spin_follow_min.setEnabled)
        self.chk_follow.toggled.connect(self.spin_follow_max.setEnabled)
        self.chk_follow.toggled.connect(self.spin_follow_delay_min.setEnabled)
        self.chk_follow.toggled.connect(self.spin_follow_delay_max.setEnabled)
        self.spin_follow_min.valueChanged.connect(
            lambda value: self.spin_follow_max.setValue(max(self.spin_follow_max.value(), value))
        )
        self.spin_follow_max.valueChanged.connect(
            lambda value: self.spin_follow_min.setValue(min(self.spin_follow_min.value(), value))
        )
        self.spin_follow_delay_min.valueChanged.connect(
            lambda value: self.spin_follow_delay_max.setValue(max(self.spin_follow_delay_max.value(), value))
        )
        self.spin_follow_delay_max.valueChanged.connect(
            lambda value: self.spin_follow_delay_min.setValue(min(self.spin_follow_delay_min.value(), value))
        )

        self.chk_join = QCheckBox("Tham gia Nhóm")
        self.inp_join_link = QLineEdit(placeholderText="Link Group...")
        join_row = QHBoxLayout()
        join_row.addWidget(self.chk_join)
        join_row.addWidget(self.inp_join_link)
        g_follow_layout.addLayout(join_row)

        l1.addWidget(group_follow)

        l1.addStretch()
        if self.mode != "mail":
            self.tabs.addTab(tab1, "Tương Tác FB")

        # Tab 2: Nuôi Nick
        tab2 = QWidget()
        l2 = QVBoxLayout(tab2)
        l2.setSpacing(15)
        
        group_friends = QGroupBox("Bạn bè & Story")
        g_friends_layout = QVBoxLayout(group_friends)
        self.chk_friend_suggest = QCheckBox("Gợi ý kết bạn")
        self.spin_friend_suggest_min = QSpinBox()
        self.spin_friend_suggest_min.setRange(1, 999)
        self.spin_friend_suggest_min.setValue(1)
        self.spin_friend_suggest_min.setEnabled(False)
        self.spin_friend_suggest_max = QSpinBox()
        self.spin_friend_suggest_max.setRange(1, 999)
        self.spin_friend_suggest_max.setValue(3)
        self.spin_friend_suggest_max.setEnabled(False)
        
        self.chk_friend_confirm = QCheckBox("Xác nhận lời mời kết bạn")
        self.spin_friend_confirm_min = QSpinBox()
        self.spin_friend_confirm_min.setRange(1, 999)
        self.spin_friend_confirm_min.setValue(1)
        self.spin_friend_confirm_min.setEnabled(False)
        self.spin_friend_confirm_max = QSpinBox()
        self.spin_friend_confirm_max.setRange(1, 999)
        self.spin_friend_confirm_max.setValue(5)
        self.spin_friend_confirm_max.setEnabled(False)

        friend_suggest_row = QHBoxLayout()
        friend_suggest_row.addWidget(self.chk_friend_suggest)
        friend_suggest_row.addStretch()
        friend_suggest_row.addWidget(QLabel("Tối thiểu:"))
        friend_suggest_row.addWidget(self.spin_friend_suggest_min)
        friend_suggest_row.addWidget(QLabel("Tối đa:"))
        friend_suggest_row.addWidget(self.spin_friend_suggest_max)
        friend_suggest_row.addWidget(QLabel("người"))
        self.chk_friend_suggest.toggled.connect(self.spin_friend_suggest_min.setEnabled)
        self.chk_friend_suggest.toggled.connect(self.spin_friend_suggest_max.setEnabled)
        self.spin_friend_suggest_min.valueChanged.connect(
            lambda value: self.spin_friend_suggest_max.setValue(max(self.spin_friend_suggest_max.value(), value))
        )
        self.spin_friend_suggest_max.valueChanged.connect(
            lambda value: self.spin_friend_suggest_min.setValue(min(self.spin_friend_suggest_min.value(), value))
        )

        friend_confirm_row = QHBoxLayout()
        friend_confirm_row.addWidget(self.chk_friend_confirm)
        friend_confirm_row.addStretch()
        friend_confirm_row.addWidget(QLabel("Tối thiểu:"))
        friend_confirm_row.addWidget(self.spin_friend_confirm_min)
        friend_confirm_row.addWidget(QLabel("Tối đa:"))
        friend_confirm_row.addWidget(self.spin_friend_confirm_max)
        friend_confirm_row.addWidget(QLabel("người"))
        self.chk_friend_confirm.toggled.connect(self.spin_friend_confirm_min.setEnabled)
        self.chk_friend_confirm.toggled.connect(self.spin_friend_confirm_max.setEnabled)
        self.spin_friend_confirm_min.valueChanged.connect(
            lambda value: self.spin_friend_confirm_max.setValue(max(self.spin_friend_confirm_max.value(), value))
        )
        self.spin_friend_confirm_max.valueChanged.connect(
            lambda value: self.spin_friend_confirm_min.setValue(min(self.spin_friend_confirm_min.value(), value))
        )

        g_friends_layout.addLayout(friend_suggest_row)
        g_friends_layout.addLayout(friend_confirm_row)
        l2.addWidget(group_friends)

        group_story = QGroupBox("Story")
        g_story_layout = QVBoxLayout(group_story)
        self.chk_post_story = QCheckBox("Đăng Story")
        self.inp_story_text = QTextEdit()
        self.inp_story_text.setPlaceholderText("Nội dung story...")
        self.inp_story_text.setMaximumHeight(60)
        g_story_layout.addWidget(self.chk_post_story)
        g_story_layout.addWidget(self.inp_story_text)
        
        self.chk_view_story = QCheckBox("Xem Story")
        self.spin_view_story_min = QSpinBox()
        self.spin_view_story_min.setRange(1, 999)
        self.spin_view_story_min.setValue(5)
        self.spin_view_story_min.setEnabled(False)
        self.spin_view_story_max = QSpinBox()
        self.spin_view_story_max.setRange(1, 999)
        self.spin_view_story_max.setValue(10)
        self.spin_view_story_max.setEnabled(False)
        self.spin_view_story_interval_min = QSpinBox()
        self.spin_view_story_interval_min.setRange(1, 3600)
        self.spin_view_story_interval_min.setValue(5)
        self.spin_view_story_interval_min.setEnabled(False)

        self.spin_view_story_interval_max = QSpinBox()
        self.spin_view_story_interval_max.setRange(1, 3600)
        self.spin_view_story_interval_max.setValue(10)
        self.spin_view_story_interval_max.setEnabled(False)

        self.chk_view_story.toggled.connect(self.spin_view_story_interval_min.setEnabled)
        self.chk_view_story.toggled.connect(self.spin_view_story_interval_max.setEnabled)
        
        view_story_row = QHBoxLayout()
        view_story_row.addWidget(self.chk_view_story)
        view_story_row.addStretch()
        view_story_row.addWidget(QLabel("Xem:"))
        view_story_row.addWidget(self.spin_view_story_min)
        view_story_row.addWidget(QLabel("đến"))
        view_story_row.addWidget(self.spin_view_story_max)
        view_story_row.addWidget(QLabel("story"))
        view_story_interval_row = QHBoxLayout()
        view_story_interval_row.addWidget(QLabel("Giữa các lần xem:"))
        view_story_interval_row.addWidget(self.spin_view_story_interval_min)
        view_story_interval_row.addWidget(QLabel("đến"))
        view_story_interval_row.addWidget(self.spin_view_story_interval_max)
        view_story_interval_row.addWidget(QLabel("giây"))

        g_story_layout.addLayout(view_story_interval_row)
        self.chk_view_story.toggled.connect(self.spin_view_story_min.setEnabled)
        self.chk_view_story.toggled.connect(self.spin_view_story_max.setEnabled)
        self.spin_view_story_min.valueChanged.connect(
            lambda value: self.spin_view_story_max.setValue(max(self.spin_view_story_max.value(), value))
        )
        self.spin_view_story_max.valueChanged.connect(
            lambda value: self.spin_view_story_min.setValue(min(self.spin_view_story_min.value(), value))
        )
        
        g_story_layout.addLayout(view_story_row)
        
        self.chk_farm_story = QCheckBox("Xem Story (Pattern Matching)")
        self.spin_farm_story_count = QSpinBox()
        self.spin_farm_story_count.setRange(5, 120)
        self.spin_farm_story_count.setValue(30)
        self.spin_farm_story_count.setEnabled(False)
        
        farm_story_row = QHBoxLayout()
        farm_story_row.addWidget(self.chk_farm_story)
        farm_story_row.addStretch()
        farm_story_row.addWidget(QLabel("Số lượng:"))
        farm_story_row.addWidget(self.spin_farm_story_count)
        farm_story_row.addWidget(QLabel("story"))
        self.chk_farm_story.toggled.connect(self.spin_farm_story_count.setEnabled)
        
        g_story_layout.addLayout(farm_story_row)
        l2.addWidget(group_story)

        group_post = QGroupBox("Đăng bài viết mới")
        g_post_layout = QVBoxLayout(group_post)
        self.chk_post = QCheckBox("Kích hoạt đăng Status")
        self.inp_post_text = QTextEdit()
        self.inp_post_text.setPlaceholderText("Nội dung status...")
        self.inp_post_text.setMaximumHeight(70)
        g_post_layout.addWidget(self.chk_post)
        g_post_layout.addWidget(self.inp_post_text)
        l2.addWidget(group_post)

        group_share = QGroupBox("Chia sẻ bài viết")
        g_share_layout = QVBoxLayout(group_share)
        self.chk_share = QCheckBox("Chia sẻ bài viết lên trang cá nhân")
        self.inp_share_link = QLineEdit(placeholderText="Link bài cần chia sẻ...")
        g_share_layout.addWidget(self.chk_share)
        g_share_layout.addWidget(self.inp_share_link)
        l2.addWidget(group_share)

        l2.addStretch()
        if self.mode != "mail":
            self.tabs.addTab(tab2, "Nuôi Nick FB")

        # Tab 3: Seeding Bài Viết
        tab3 = QWidget()
        l3 = QVBoxLayout(tab3)
        l3.setSpacing(15)

        # Seeding chính
        group_seeding_main = QGroupBox("Seeding Bài Viết Chính")
        g_seeding_main_layout = QVBoxLayout(group_seeding_main)
        self.chk_seeding = QCheckBox("Kích hoạt Seeding")
        self.inp_seed_link = QLineEdit(placeholderText="Link bài viết cần seeding...")
        self.inp_seed_cmt = QTextEdit()
        self.inp_seed_cmt.setPlaceholderText("Nội dung bình luận (hỗ trợ nhiều bình luận, mỗi dòng 1 bình luận)...")
        self.inp_seed_cmt.setMaximumHeight(80)
        
        g_seeding_main_layout.addWidget(self.chk_seeding)
        g_seeding_main_layout.addWidget(QLabel("Link bài viết:"))
        g_seeding_main_layout.addWidget(self.inp_seed_link)
        g_seeding_main_layout.addWidget(QLabel("Nội dung bình luận:"))
        g_seeding_main_layout.addWidget(self.inp_seed_cmt)
        l3.addWidget(group_seeding_main)

        # Số lượng & thời gian
        group_seeding_config = QGroupBox("Cấu hình Seeding")
        g_seeding_config_layout = QVBoxLayout(group_seeding_config)
        
        self.spin_seeding_count_min = QSpinBox()
        self.spin_seeding_count_min.setRange(1, 999)
        self.spin_seeding_count_min.setValue(1)
        self.spin_seeding_count_min.setEnabled(False)
        self.spin_seeding_count_max = QSpinBox()
        self.spin_seeding_count_max.setRange(1, 999)
        self.spin_seeding_count_max.setValue(3)
        self.spin_seeding_count_max.setEnabled(False)
        
        seeding_count_row = QHBoxLayout()
        seeding_count_row.addWidget(QLabel("Số lượng bình luận:"))
        seeding_count_row.addWidget(self.spin_seeding_count_min)
        seeding_count_row.addWidget(QLabel("đến"))
        seeding_count_row.addWidget(self.spin_seeding_count_max)
        seeding_count_row.addStretch()
        self.chk_seeding.toggled.connect(self.spin_seeding_count_min.setEnabled)
        self.chk_seeding.toggled.connect(self.spin_seeding_count_max.setEnabled)
        self.spin_seeding_count_min.valueChanged.connect(
            lambda value: self.spin_seeding_count_max.setValue(max(self.spin_seeding_count_max.value(), value))
        )
        self.spin_seeding_count_max.valueChanged.connect(
            lambda value: self.spin_seeding_count_min.setValue(min(self.spin_seeding_count_min.value(), value))
        )
        g_seeding_config_layout.addLayout(seeding_count_row)

        # Khoảng thời gian giữa các lần seeding
        self.spin_seeding_interval_min = QSpinBox()
        self.spin_seeding_interval_min.setRange(1, 3600)
        self.spin_seeding_interval_min.setValue(30)
        self.spin_seeding_interval_min.setEnabled(False)
        self.spin_seeding_interval_max = QSpinBox()
        self.spin_seeding_interval_max.setRange(1, 3600)
        self.spin_seeding_interval_max.setValue(60)
        self.spin_seeding_interval_max.setEnabled(False)
        
        seeding_interval_row = QHBoxLayout()
        seeding_interval_row.addWidget(QLabel("Thòi gian giữa các lần:"))
        seeding_interval_row.addWidget(self.spin_seeding_interval_min)
        seeding_interval_row.addWidget(QLabel("đến"))
        seeding_interval_row.addWidget(self.spin_seeding_interval_max)
        seeding_interval_row.addWidget(QLabel("giây"))
        seeding_interval_row.addStretch()
        self.chk_seeding.toggled.connect(self.spin_seeding_interval_min.setEnabled)
        self.chk_seeding.toggled.connect(self.spin_seeding_interval_max.setEnabled)
        self.spin_seeding_interval_min.valueChanged.connect(
            lambda value: self.spin_seeding_interval_max.setValue(max(self.spin_seeding_interval_max.value(), value))
        )
        self.spin_seeding_interval_max.valueChanged.connect(
            lambda value: self.spin_seeding_interval_min.setValue(min(self.spin_seeding_interval_min.value(), value))
        )
        g_seeding_config_layout.addLayout(seeding_interval_row)
        l3.addWidget(group_seeding_config)

        # Seeding bài viết khác (tùy chọn)
        group_seeding_extra = QGroupBox("Seeding Bài Viết Khác (Tùy chọn)")
        g_seeding_extra_layout = QVBoxLayout(group_seeding_extra)
        self.chk_seeding_extra = QCheckBox("Kích hoạt Seeding thêm")
        self.inp_seed_extra_links = QTextEdit()
        self.inp_seed_extra_links.setPlaceholderText("Link các bài viết khác (mỗi dòng 1 link)...")
        self.inp_seed_extra_links.setMaximumHeight(60)
        
        g_seeding_extra_layout.addWidget(self.chk_seeding_extra)
        g_seeding_extra_layout.addWidget(QLabel("Links:"))
        g_seeding_extra_layout.addWidget(self.inp_seed_extra_links)
        l3.addWidget(group_seeding_extra)

        l3.addStretch()
        if self.mode != "mail":
            self.tabs.addTab(tab3, "Seeding Tương Tác")

        # Tab 4: Nghiệp vụ Mail
        tab4 = QWidget()
        l4 = QVBoxLayout(tab4)
        l4.setSpacing(15)

        self.chk_mail_read_news = QCheckBox("Đọc báo")
        self.spin_mail_read_time_min = QSpinBox()
        self.spin_mail_read_time_min.setRange(1, 720)
        self.spin_mail_read_time_min.setValue(3)
        self.spin_mail_read_time_min.setEnabled(False)
        self.spin_mail_read_time_max = QSpinBox()
        self.spin_mail_read_time_max.setRange(1, 720)
        self.spin_mail_read_time_max.setValue(5)
        self.spin_mail_read_time_max.setEnabled(False)

        mail_read_time_row = QHBoxLayout()
        mail_read_time_row.addWidget(self.chk_mail_read_news)
        mail_read_time_row.addStretch()
        mail_read_time_row.addWidget(QLabel("Thời gian:"))
        mail_read_time_row.addWidget(self.spin_mail_read_time_min)
        mail_read_time_row.addWidget(QLabel("đến"))
        mail_read_time_row.addWidget(self.spin_mail_read_time_max)
        mail_read_time_row.addWidget(QLabel("phút"))

        self.chk_mail_read_news.toggled.connect(self.spin_mail_read_time_min.setEnabled)
        self.chk_mail_read_news.toggled.connect(self.spin_mail_read_time_max.setEnabled)
        self.spin_mail_read_time_min.valueChanged.connect(
            lambda value: self.spin_mail_read_time_max.setValue(max(self.spin_mail_read_time_max.value(), value))
        )
        self.spin_mail_read_time_max.valueChanged.connect(
            lambda value: self.spin_mail_read_time_min.setValue(min(self.spin_mail_read_time_min.value(), value))
        )

        l4.addLayout(mail_read_time_row)

        self.chk_mail_google_doc = QCheckBox("Nhập Google Doc")
        self.inp_mail_google_doc = QLineEdit()
        self.inp_mail_google_doc.setPlaceholderText("Link Google Doc...")
        self.inp_mail_google_doc.setEnabled(False)

        mail_google_doc_row = QHBoxLayout()
        mail_google_doc_row.addWidget(self.chk_mail_google_doc)
        mail_google_doc_row.addWidget(self.inp_mail_google_doc)

        self.chk_mail_google_doc.toggled.connect(self.inp_mail_google_doc.setEnabled)

        l4.addLayout(mail_google_doc_row)
        l4.addStretch()
        self.tabs.addTab(tab4, "Nghiệp vụ Mail")

        if self.mode == "mail":
            self.tabs.setCurrentWidget(tab4)

        # Nút xác nhận
        btn_box = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btn_box.button(QDialogButtonBox.StandardButton.Ok).setText("Lưu & Chạy")
        btn_box.button(QDialogButtonBox.StandardButton.Cancel).setText("Hủy")
        btn_box.accepted.connect(self.accept)
        btn_box.rejected.connect(self.reject)
        layout.addWidget(btn_box)

        self._config_snapshot: dict = {}
        self._config_snapshot = self._collect_config()

    def accept(self) -> None:  # noqa: N802
        self._config_snapshot = self._collect_config()
        super().accept()

    def _collect_config(self) -> dict:
        def range_value(min_spin: QSpinBox | None, max_spin: QSpinBox | None) -> tuple[int, int]:
            if min_spin is None or max_spin is None:
                return 0, 0
            try:
                return min(min_spin.value(), max_spin.value()), max(min_spin.value(), max_spin.value())
            except RuntimeError:
                return 0, 0

        def bool_value(widget: QCheckBox | None) -> bool:
            if widget is None:
                return False
            try:
                return widget.isChecked()
            except RuntimeError:
                return False

        def text_value(widget: QLineEdit | QTextEdit | None) -> str:
            if widget is None:
                return ""
            try:
                if isinstance(widget, QLineEdit):
                    return widget.text().strip()
                return widget.toPlainText().strip()
            except RuntimeError:
                return ""

        def int_value(widget: QSpinBox | None, default: int = 0) -> int:
            if widget is None:
                return default
            try:
                return widget.value()
            except RuntimeError:
                return default

        newsfeed_time = range_value(getattr(self, "spin_newsfeed_time_min", None), getattr(self, "spin_newsfeed_time_max", None))
        newsfeed_interval = range_value(getattr(self, "spin_newsfeed_interval_min", None), getattr(self, "spin_newsfeed_interval_max", None))
        video_time = range_value(getattr(self, "spin_video_time_min", None), getattr(self, "spin_video_time_max", None))
        video_interval = range_value(getattr(self, "spin_video_interval_min", None), getattr(self, "spin_video_interval_max", None))
        like_count = range_value(getattr(self, "spin_like_count_min", None), getattr(self, "spin_like_count_max", None))
        friend_suggest_count = range_value(getattr(self, "spin_friend_suggest_min", None), getattr(self, "spin_friend_suggest_max", None))
        friend_confirm_count = range_value(getattr(self, "spin_friend_confirm_min", None), getattr(self, "spin_friend_confirm_max", None))
        seeding_count = range_value(getattr(self, "spin_seeding_count_min", None), getattr(self, "spin_seeding_count_max", None))
        seeding_interval = range_value(getattr(self, "spin_seeding_interval_min", None), getattr(self, "spin_seeding_interval_max", None))
        mail_read_time = range_value(getattr(self, "spin_mail_read_time_min", None), getattr(self, "spin_mail_read_time_max", None))

        return {
            "newsfeed": bool_value(getattr(self, "chk_newsfeed", None)),
            "newsfeed_time_min": newsfeed_time[0],
            "newsfeed_time_max": newsfeed_time[1],
            "newsfeed_interval_min": newsfeed_interval[0],
            "newsfeed_interval_max": newsfeed_interval[1],
            "video": bool_value(getattr(self, "chk_video", None)),
            "video_time_min": video_time[0],
            "video_time_max": video_time[1],
            "video_interval_min": video_interval[0],
            "video_interval_max": video_interval[1],
            "like": bool_value(getattr(self, "chk_like", None)) and (
                bool_value(getattr(self, "chk_newsfeed", None)) or bool_value(getattr(self, "chk_video", None))
            ),
            "like_count_min": like_count[0],
            "like_count_max": like_count[1],
            "seeding": bool_value(getattr(self, "chk_seeding", None)),
            "seed_link": text_value(getattr(self, "inp_seed_link", None)),
            "seed_cmt": text_value(getattr(self, "inp_seed_cmt", None)),
            "seeding_count_min": seeding_count[0],
            "seeding_count_max": seeding_count[1],
            "seeding_interval_min": seeding_interval[0],
            "seeding_interval_max": seeding_interval[1],
            "seeding_extra": bool_value(getattr(self, "chk_seeding_extra", None)),
            "seed_extra_links": text_value(getattr(self, "inp_seed_extra_links", None)),
            "follow": bool_value(getattr(self, "chk_follow", None)),
            "follow_links": text_value(getattr(self, "inp_follow_links", None)),
            "follow_count_min": int_value(getattr(self, "spin_follow_min", None), 3),
            "follow_count_max": int_value(getattr(self, "spin_follow_max", None), 5),
            "follow_delay_min": int_value(getattr(self, "spin_follow_delay_min", None), 15),
            "follow_delay_max": int_value(getattr(self, "spin_follow_delay_max", None), 45),
            "join": bool_value(getattr(self, "chk_join", None)),
            "join_link": text_value(getattr(self, "inp_join_link", None)),
            "friend_suggest": bool_value(getattr(self, "chk_friend_suggest", None)),
            "friend_suggest_count_min": friend_suggest_count[0],
            "friend_suggest_count_max": friend_suggest_count[1],
            "friend_confirm": bool_value(getattr(self, "chk_friend_confirm", None)),
            "friend_confirm_count_min": friend_confirm_count[0],
            "friend_confirm_count_max": friend_confirm_count[1],
            "post": bool_value(getattr(self, "chk_post", None)),
            "post_text": text_value(getattr(self, "inp_post_text", None)),
            "post_story": bool_value(getattr(self, "chk_post_story", None)),
            "story_text": text_value(getattr(self, "inp_story_text", None)),
            "view_story": bool_value(getattr(self, "chk_view_story", None)),
            "view_story_min": int_value(getattr(self, "spin_view_story_min", None), 1),
            "view_story_max": int_value(getattr(self, "spin_view_story_max", None), 3),
            "view_story_interval_min": int_value(getattr(self, "spin_view_story_interval_min", None), 5),
            "view_story_interval_max": int_value(getattr(self, "spin_view_story_interval_max", None), 10),
            "farm_story": bool_value(getattr(self, "chk_farm_story", None)),
            "farm_story_count": int_value(getattr(self, "spin_farm_story_count", None), 5),
            "share": bool_value(getattr(self, "chk_share", None)),
            "share_link": text_value(getattr(self, "inp_share_link", None)),
            "mail_read_news": bool_value(getattr(self, "chk_mail_read_news", None)),
            "mail_read_time_min": mail_read_time[0],
            "mail_read_time_max": mail_read_time[1],
            "mail_google_doc": bool_value(getattr(self, "chk_mail_google_doc", None)),
            "mail_google_doc_link": text_value(getattr(self, "inp_mail_google_doc", None)),
        }

    def get_config(self) -> dict:
        return dict(self._config_snapshot)

class MainWindow(QMainWindow):
    status_update_requested = Signal(str, str)
    _embed_window_signal = Signal(str, int)
    def _maybe_like_during_browse(self, serial: str, config: dict, source: str = "") -> None:
        if not config.get("like"):
            return

        remaining = int(config.get("_like_remaining", 0))

        if remaining <= 0:
            return

        # Tỷ lệ random, không phải lần nào lướt cũng like
        if random.random() > 0.35:
            return

        try:
            liked = like_random_post_or_reel(serial, count=1)

            if liked:
                config["_like_remaining"] = remaining - 1
                self.status_update_requested.emit(
                    serial,
                    f"Đã like {source}, còn {config['_like_remaining']} lần"
                )

        except Exception as exc:
            self.status_update_requested.emit(serial, f"Lỗi like {source}: {exc}")
    
    def _run_farm_view_story(self, serial: str, config: dict) -> None:
        min_count = int(config.get("view_story_min", 1))
        max_count = int(config.get("view_story_max", 3))
        count = random.randint(min(min_count, max_count), max(min_count, max_count))

        interval_min = int(config.get("view_story_interval_min", 5))
        interval_max = int(config.get("view_story_interval_max", 10))

        self.status_update_requested.emit(serial, f"Xem Story {count} story")

        viewed = farm_story(
            serial,
            count=count,
            interval_min=min(interval_min, interval_max),
            interval_max=max(interval_min, interval_max),
            load_wait=3,
        )

        self.status_update_requested.emit(serial, f"Đã xem Story {viewed}/{count}")

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Auto Social PhoneFarm")
        self.resize(1400, 860)

        self.profile_store = ProfileStore()
        self.follow_store = FollowHistoryStore()
        self.devices: list[DeviceInfo] = []
        self.all_devices: list[DeviceInfo] = []
        self._threads: list[TaskThread] = []
        self._selected_serial: str | None = None
        self._screen_slots: dict[str, ScreenSlot] = {}
        self._screen_hwnds: dict[str, int] = {}
        self._screen_pids: dict[str, int] = {}
        self._screen_procs: dict[str, object] = {}
        self._screen_quality: dict[str, str] = {}
        self._embed_retry: dict[str, int] = {}
        self._relaunch_attempts: dict[str, int] = {}
        self._scale_percent = 55
        self._embed_retry_max = 160
        self._duplicate_uid_groups: dict[str, list[str]] = {}
        self._active_filtered_serials: set[str] | None = None
        self._sort_column: int | None = None
        self._sort_order: Qt.SortOrder = Qt.SortOrder.AscendingOrder

        self._sync_timer = QTimer(self)
        self._sync_timer.setInterval(500)
        self._sync_timer.timeout.connect(self._sync_embedded_windows)
        self._sync_timer.start()
        
        self.status_update_requested.connect(self._update_table_status)
        self._embed_window_signal.connect(self._embed_window_ui)

        self._build_ui()
        self._apply_style()
        self.refresh_devices()

    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)

        outer_layout = QVBoxLayout(root)
        outer_layout.setContentsMargins(16, 16, 16, 16)
        outer_layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("Auto Social PhoneFarm")
        title.setObjectName("titleLabel")
        subtitle = QLabel("Quản lý thiết bị Android, nuôi nick mạng xã hội & tự động hóa tương tác qua ADB.")
        subtitle.setObjectName("subtitleLabel")
        title_box = QVBoxLayout()
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch(1)

        self.btn_refresh = QPushButton("Quét thiết bị")

        self.btn_select_ops = QPushButton("Quản lý chọn")
        select_menu = QMenu(self)
        self.act_select_current = select_menu.addAction("Chọn máy bôi đen")
        self.act_select_all = select_menu.addAction("Chọn tất cả")
        self.act_clear_selection = select_menu.addAction("Bỏ chọn")
        self.btn_select_ops.setMenu(select_menu)

        self.btn_screen_ops = QPushButton("Quản lý màn hình")
        screen_menu = QMenu(self)
        self.act_open_screen = screen_menu.addAction("Mở màn hình máy đã chọn")
        self.act_show_screen_win = screen_menu.addAction("Hiển thị cửa sổ màn hình")
        self.act_clear_screens = screen_menu.addAction("Đóng toàn bộ màn hình")
        self.btn_screen_ops.setMenu(screen_menu)

        self.btn_network_ops = QPushButton("Mạng & Proxy")
        network_menu = QMenu(self)
        self.act_wifi_on = network_menu.addAction("Kết nối Wi-Fi (SSID|Pass)")
        self.act_wifi_off = network_menu.addAction("Tắt Wi-Fi")
        self.act_apply_proxy = network_menu.addAction("Áp proxy & Fake (College Proxy)")
        self.act_connect_proxy_app = network_menu.addAction("Kết nối proxy trong app")
        self.act_check_proxy_status = network_menu.addAction("Kiểm tra kết nối Proxy thiết bị")
        self.act_clear_proxy = network_menu.addAction("Xóa proxy")
        network_menu.addSeparator()
        self.act_clear_college_proxy_data = network_menu.addAction("Xóa dữ liệu app College Proxy")
        self.btn_network_ops.setMenu(network_menu)

        self.btn_fb_ops = QPushButton("Nghiệp vụ FB")
        fb_menu = QMenu(self)
        self.act_set_fb_accounts = fb_menu.addAction("Thêm tài khoản hàng loạt (uid|pass|2fa)")
        self.act_login_fb = fb_menu.addAction("Login Facebook theo tài khoản đã lưu")
        fb_menu.addSeparator()
        self.act_get_fb = fb_menu.addAction("Lấy info FB")
        self.act_check_duplicate_uids = fb_menu.addAction("Check trùng UID trên các máy")
        self.act_copy_uids = fb_menu.addAction("Copy toàn bộ UID đang hiển thị")
        fb_menu.addSeparator()
        self.act_check_live_die = fb_menu.addAction("Check Live/Die UID thiết bị đã chọn")
        self.act_check_live_die_manual = fb_menu.addAction("Check Live/Die danh sách UID thủ công")
        fb_menu.addSeparator()
        self.act_farm_run = fb_menu.addAction("Chạy kịch bản Tương tác / Nuôi nick")
        fb_menu.addSeparator()
        self.act_update_avatar_bio = fb_menu.addAction("Check & Tự động Đổi Avatar + Viết Tiểu sử (Nếu chưa có)")
        self.act_download_avatars = fb_menu.addAction("Tải tự động hàng loạt ảnh Avatar từ Internet")
        self.act_push_photos = fb_menu.addAction("Nạp ảnh từ máy tính vào Thư viện điện thoại")
        self.btn_fb_ops.setMenu(fb_menu)

        self.act_check_duplicate_uids.triggered.connect(self.check_duplicate_fb_uids)
        self.act_copy_uids.triggered.connect(self.copy_visible_uids)

        self.btn_mail_ops = QPushButton("Nghiệp vụ Mail")
        self.btn_mail_ops.clicked.connect(self.open_mail_tab)

        self.btn_nav_ops = QPushButton("Điều hướng hàng loạt")
        nav_menu = QMenu(self)
        self.act_open_fb = nav_menu.addAction("1. Mở app Facebook")
        self.act_open_link = nav_menu.addAction("2. Mở Link (Bài viết/Page/Video/Group)")
        self.act_swipe_up = nav_menu.addAction("3. Vuốt lên (Cuộn xuống xem nội dung)")
        self.act_input_text = nav_menu.addAction("4. Nhập chữ (Gõ đoạn comment/bài viết)")
        self.act_clear_fb_data = nav_menu.addAction("5. Xóa dữ liệu app Facebook")
        self.act_enable_adb_keyboard = nav_menu.addAction("6. Kích hoạt Bàn phím ADB (Sửa lỗi gõ chữ)")
        self.act_install_apk = nav_menu.addAction("7. Cài đặt ứng dụng từ file APK")
        nav_menu.addSeparator()
        self.act_close_all_apps = nav_menu.addAction("8. Đóng tất cả ứng dụng đang chạy (Đa nhiệm -> Close all)")
        self.act_nav_clear_college_proxy = nav_menu.addAction("9. Xóa dữ liệu app College Proxy")
        self.btn_nav_ops.setMenu(nav_menu)

        self.chk_filter_empty_fb = QCheckBox("Lọc máy trống")
        self.chk_filter_empty_fb.stateChanged.connect(self._on_filter_changed)
        self.chk_filter_live = QCheckBox("Lọc máy LIVE")
        self.chk_filter_live.stateChanged.connect(self._on_filter_changed)
        self.chk_filter_die = QCheckBox("Lọc máy DIE")
        self.chk_filter_die.stateChanged.connect(self._on_filter_changed)
        self.btn_stop_tasks = QPushButton("🛑 Dừng tiến trình")
        self.btn_stop_tasks.setCursor(Qt.PointingHandCursor)
        self.btn_stop_tasks.setToolTip("Dừng ngay lập tức toàn bộ các tác vụ/tiến trình đang chạy")
        self.btn_stop_tasks.setStyleSheet("""
            QPushButton {
                background-color: #dc2626;
                color: #ffffff;
                font-weight: bold;
                border: 1px solid #ef4444;
                border-radius: 6px;
                padding: 6px 14px;
            }
            QPushButton:hover {
                background-color: #b91c1c;
                border: 1px solid #f87171;
            }
            QPushButton:pressed {
                background-color: #991b1b;
            }
        """)

        actions = QHBoxLayout()
        actions.setSpacing(8)
        
        for button in (
            self.btn_refresh,
            self.btn_select_ops,
            self.btn_screen_ops,
            self.btn_network_ops,
            self.btn_fb_ops,
            self.btn_mail_ops,
            self.btn_nav_ops,
            self.btn_stop_tasks,
        ):
            button.setCursor(Qt.PointingHandCursor)
            actions.addWidget(button)
            
        actions.addSpacing(10)
        actions.addWidget(self.chk_filter_empty_fb)
        actions.addWidget(self.chk_filter_live)
        actions.addWidget(self.chk_filter_die)
        actions.addSpacing(10)

        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("Tìm: ID, FB, Ghi chú, Trạng thái...")
        self.search_input.setFixedWidth(240)
        self.search_input.textChanged.connect(self._on_filter_changed)
        actions.addWidget(self.search_input)
        actions.addStretch(1)

        self.table = ClipboardTableWidget(0, 10)
        self.table.setHorizontalHeaderLabels(
            ["", "STT", "ID thiết bị", "Model", "Android", "FB UID", "FB Tên", "Proxy", "Ghi chú", "Trạng thái"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectItems)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setColumnWidth(0, 36)
        self.table.setColumnWidth(1, 48)
        self.table.setColumnWidth(2, 145)
        self.table.setColumnWidth(3, 110)
        self.table.setColumnWidth(4, 70)
        self.table.setColumnWidth(5, 140)
        self.table.setColumnWidth(6, 150)
        self.table.setColumnWidth(7, 220)
        self.table.setColumnWidth(8, 140)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.table.horizontalHeader().setSectionsClickable(True)
        self.table.horizontalHeader().setSortIndicatorShown(True)
        self.table.horizontalHeader().sectionClicked.connect(self._on_header_clicked)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.currentCellChanged.connect(self._on_current_changed)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)

        self.detail_status = QLabel("Chưa chọn thiết bị")
        self.detail_status.setWordWrap(True)
        self.proxy_input = QLineEdit()
        self.proxy_input.setPlaceholderText("host:port hoặc ip:port")
        self.note_input = QLineEdit()
        self.note_input.setPlaceholderText("Ghi chú riêng cho thiết bị")

        self.btn_save_profile = QPushButton("Lưu ghi chú / proxy")
        self.btn_save_profile.setCursor(Qt.PointingHandCursor)

        detail_panel = QWidget()
        detail_panel.setFixedWidth(270)
        detail_layout = QVBoxLayout(detail_panel)
        detail_layout.setContentsMargins(12, 12, 12, 12)
        detail_layout.setSpacing(10)
        detail_title = QLabel("Cấu hình máy đang chọn")
        detail_title.setObjectName("panelTitle")
        detail_layout.addWidget(detail_title)
        detail_layout.addWidget(QLabel("Trạng thái"))
        detail_layout.addWidget(self.detail_status)
        detail_layout.addWidget(QLabel("Proxy"))
        detail_layout.addWidget(self.proxy_input)
        detail_layout.addWidget(QLabel("Ghi chú"))
        detail_layout.addWidget(self.note_input)
        detail_layout.addWidget(self.btn_save_profile)
        detail_layout.addStretch(1)

        main_content = QHBoxLayout()
        main_content.setSpacing(12)
        main_content.setContentsMargins(0, 0, 0, 0)
        main_content.addWidget(self.table, 1)
        main_content.addWidget(detail_panel, 0)

        self.screen_window = ScreenWallWindow(self)
        self.scale_label = self.screen_window.scale_label
        self.scale_slider = self.screen_window.scale_slider
        self.screen_wall = self.screen_window.screen_wall
        self.screen_grid = self.screen_window.screen_grid
        self.screen_scroll = self.screen_window.screen_scroll
        self.screen_window.set_reflow_callback(self._reflow_screen_grid)
        self.screen_window.btn_clear_all.clicked.connect(self.clear_screen_wall)

        outer_layout.addLayout(header)
        outer_layout.addLayout(actions)
        outer_layout.addLayout(main_content, 1)

        self.setStatusBar(QStatusBar())

        self.btn_refresh.clicked.connect(self.refresh_devices)
        self.act_select_current.triggered.connect(self.select_current_row)
        self.act_select_all.triggered.connect(self.select_all_devices)
        self.act_clear_selection.triggered.connect(self.clear_selection)
        self.act_open_screen.triggered.connect(self.open_selected_screens)
        self.act_show_screen_win.triggered.connect(self.show_screen_window)
        self.act_clear_screens.triggered.connect(self.clear_screen_wall)
        self.act_wifi_on.triggered.connect(partial(self.set_wifi_for_selected, True))
        self.act_wifi_off.triggered.connect(partial(self.set_wifi_for_selected, False))
        self.act_apply_proxy.triggered.connect(self.apply_proxy_to_selected)
        self.act_connect_proxy_app.triggered.connect(self.connect_proxy_in_app_for_selected)
        self.act_check_proxy_status.triggered.connect(self.check_proxy_status_for_selected)
        self.act_clear_proxy.triggered.connect(self.clear_proxy_for_selected)
        self.act_clear_college_proxy_data.triggered.connect(self.clear_college_proxy_data_for_selected)
        self.act_nav_clear_college_proxy.triggered.connect(self.clear_college_proxy_data_for_selected)
        self.btn_save_profile.clicked.connect(self.save_current_profile)
        self.act_set_fb_accounts.triggered.connect(self.set_fb_accounts_for_selected)
        self.act_login_fb.triggered.connect(self.login_fb_for_selected)
        self.act_get_fb.triggered.connect(self.get_fb_info_for_selected)
        self.act_check_live_die.triggered.connect(self.check_live_die_for_selected)
        self.act_check_live_die_manual.triggered.connect(self.check_live_die_manual)
        self.act_farm_run.triggered.connect(self.open_farm_dialog)
        self.act_open_fb.triggered.connect(self.nav_open_fb)
        self.act_open_link.triggered.connect(self.nav_open_link)
        self.act_swipe_up.triggered.connect(self.nav_swipe_up)
        self.act_input_text.triggered.connect(self.nav_input_text)
        self.act_clear_fb_data.triggered.connect(self.nav_clear_fb_data)
        self.act_enable_adb_keyboard.triggered.connect(self.nav_enable_adb_keyboard)
        self.act_install_apk.triggered.connect(self.install_apk_for_selected)
        self.act_close_all_apps.triggered.connect(self.nav_close_all_apps)
        self.act_update_avatar_bio.triggered.connect(self.update_avatar_bio_for_selected)
        self.act_download_avatars.triggered.connect(self.download_avatars_action)
        self.btn_stop_tasks.clicked.connect(self.stop_all_tasks)
        if hasattr(self, "screen_window") and hasattr(self.screen_window, "btn_stop_all"):
            self.screen_window.btn_stop_all.clicked.connect(self.stop_all_tasks)
        self.act_push_photos.triggered.connect(self.push_photos_for_selected)
        self.scale_slider.valueChanged.connect(self._on_scale_changed)

        self._update_action_state(False)

    def _apply_style(self) -> None:
        app_font = QFont("Segoe UI", 10)
        self.setFont(app_font)
        self.setStyleSheet(
            """
            QMainWindow, QDialog, QMessageBox { background: #0f172a; }
            QWidget { color: #e5e7eb; }
            QLabel#titleLabel { font-size: 28px; font-weight: 700; color: #f8fafc; }
            QLabel#subtitleLabel { color: #94a3b8; }
            QLabel#panelTitle { font-size: 16px; font-weight: 600; color: #f8fafc; }
            QPushButton {
                background-color: #2563eb;
                color: white;
                border: none;
                border-radius: 6px;
                padding: 10px 14px;
                font-weight: bold;
            }
            QPushButton:hover { background-color: #3b82f6; }
            QPushButton:pressed { background-color: #1d4ed8; }
            QPushButton:disabled { color: #64748b; background-color: #1e293b; }
            QMenu {
                background-color: #1e293b;
                color: #f8fafc;
                border: 1px solid #334155;
            }
            QMenu::item:selected {
                background-color: #3b82f6;
            }
            QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox {
                background-color: #0b1220;
                color: #f8fafc;
                border: 1px solid #334155;
                border-radius: 6px;
                padding: 8px;
            }
            QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus {
                border: 1px solid #3b82f6;
            }
            QTableWidget {
                background: #0b1220;
                gridline-color: #1f2937;
                alternate-background-color: #0f172a;
                selection-background-color: #2563eb;
                selection-color: #ffffff;
                border: 1px solid #1f2937;
                border-radius: 12px;
            }
            QHeaderView::section {
                background: #111827;
                color: #cbd5e1;
                padding: 10px;
                border: none;
                border-bottom: 1px solid #1f2937;
            }
            QSplitter::handle { background: #1f2937; }
            QScrollArea {
                background: #0b1220;
                border: 1px solid #1f2937;
                border-radius: 12px;
            }
            QFrame#screenCard {
                background: #0f172a;
                border: 1px solid #334155;
                border-radius: 10px;
            }
            QFrame#screenHost {
                background: #020617;
                border: 1px solid #1f2937;
                border-radius: 8px;
            }
            QLabel#slotStatus {
                color: #93c5fd;
                font-size: 11px;
            }
            """
        )

    def _set_busy(self, busy: bool, message: str = "") -> None:
        # Không disable các button để cho phép sử dụng song song các chức năng khác
            
        if message:
            self.statusBar().showMessage(message)

    def _update_action_state(self, has_selection: bool) -> None:
        self.act_select_current.setEnabled(self.table.currentRow() >= 0)
        self.act_open_screen.setEnabled(has_selection)
        self.act_wifi_on.setEnabled(has_selection)
        self.act_wifi_off.setEnabled(has_selection)
        self.act_apply_proxy.setEnabled(has_selection)
        self.act_connect_proxy_app.setEnabled(has_selection)
        self.act_check_proxy_status.setEnabled(has_selection)
        self.act_clear_proxy.setEnabled(has_selection)
        self.act_clear_college_proxy_data.setEnabled(has_selection)
        self.act_set_fb_accounts.setEnabled(has_selection)
        self.act_login_fb.setEnabled(has_selection)
        self.act_get_fb.setEnabled(has_selection)
        self.act_check_live_die.setEnabled(has_selection)
        self.act_farm_run.setEnabled(has_selection)
        self.act_update_avatar_bio.setEnabled(has_selection)
        self.act_push_photos.setEnabled(has_selection)
        self.act_open_fb.setEnabled(has_selection)
        self.act_open_link.setEnabled(has_selection)
        self.act_swipe_up.setEnabled(has_selection)
        self.act_input_text.setEnabled(has_selection)
        self.act_clear_fb_data.setEnabled(has_selection)
        self.act_enable_adb_keyboard.setEnabled(has_selection)
        self.act_install_apk.setEnabled(has_selection)
        self.act_close_all_apps.setEnabled(has_selection)
        self.act_nav_clear_college_proxy.setEnabled(has_selection)
        self.btn_save_profile.setEnabled(has_selection)

    def stop_all_tasks(self) -> None:
        selected = self.selected_serials()
        if selected:
            request_stop_serials(selected)
            self.statusBar().showMessage(f"🛑 Đã gửi lệnh DỪNG tiến trình cho {len(selected)} máy đã chọn!", 5000)
            for s in selected:
                self.status_update_requested.emit(s, "⏹ Đã dừng")
        else:
            request_stop_all()
            for thread in list(self._threads):
                try:
                    thread.requestInterruption()
                except Exception:
                    pass
            self._set_busy(False)
            self.statusBar().showMessage("🛑 Đã gửi lệnh DỪNG toàn bộ tiến trình đang chạy!", 6000)
            for row in range(self.table.rowCount()):
                item = self.table.item(row, 9)
                if item and item.text() not in ("LIVE", "DIE", "Offline", "Exited"):
                    item.setText("⏹ Đã dừng")
                    item.setForeground(QBrush(QColor("#facc15")))

    def _start_task(self, func, success_callback=None, busy_message: str = "Đang xử lý...") -> None:
        reset_stop_event()
        self._set_busy(True, busy_message)
        thread = TaskThread(func)
        thread.succeeded.connect(lambda result: self._finish_task(result, success_callback))
        thread.failed.connect(self._task_failed)
        thread.finished.connect(lambda: self._cleanup_thread(thread))
        self._threads.append(thread)
        thread.start()

    def _cleanup_thread(self, thread: TaskThread) -> None:
        if thread in self._threads:
            self._threads.remove(thread)

    def _finish_task(self, result, success_callback) -> None:
        self._set_busy(False)
        if success_callback:
            success_callback(result)

    def _update_table_status(self, serial: str, status: str) -> None:
        device = self._device_by_serial(serial)
        if device:
            device.state = status
            if status in ("LIVE", "DIE"):
                device.last_check_status = status
        
        # Tìm dòng và cập nhật text
        for row in range(self.table.rowCount()):
            if self._serial_at_row(row) == serial:
                item = self.table.item(row, 9)
                if item:
                    item.setText(status)
                    st_low = status.lower()
                    if "live" in st_low:
                        item.setForeground(QBrush(QColor("#4ade80")))
                    elif "die" in st_low:
                        item.setForeground(QBrush(QColor("#f87171")))
                    elif "đã kết nối" in st_low or "stop proxy" in st_low:
                        item.setForeground(QBrush(QColor("#38bdf8")))
                    elif "chưa kết nối" in st_low or "start proxy" in st_low:
                        item.setForeground(QBrush(QColor("#f87171")))
                break

    def _task_failed(self, message: str) -> None:
        self._set_busy(False)
        QMessageBox.critical(self, "Lỗi", message)
        self.statusBar().showMessage(message, 7000)

    def refresh_devices(self) -> None:
        def work() -> list[DeviceInfo]:
            devices = list_devices()
            for device in devices:
                profile = self.profile_store.get(device.serial)
                device.proxy = profile.proxy
                device.note = profile.note
                device.fb_uid = profile.fb_uid
                device.fb_name = profile.fb_name
                note_lower = (device.note or "").lower()
                if "live" in note_lower:
                    device.last_check_status = "LIVE"
                elif "die" in note_lower:
                    device.last_check_status = "DIE"
            return devices

        self._start_task(work, self._populate_table, "Đang quét thiết bị ADB...")

    def _populate_table(self, devices: list[DeviceInfo]) -> None:
        self.all_devices = devices
        self._on_filter_changed()
        
    def _on_filter_changed(self) -> None:
        self._active_filtered_serials = None
        self._render_table(rebuild_filter=True)

    def _on_header_clicked(self, column: int) -> None:
        if column == 0:
            # Checkbox column: Chọn tất cả / Bỏ chọn tất cả các máy đang hiển thị
            checked_count = sum(
                1 for r in range(self.table.rowCount())
                if self.table.item(r, 0) and self.table.item(r, 0).checkState() == Qt.CheckState.Checked
            )
            new_state = Qt.CheckState.Unchecked if (checked_count == self.table.rowCount() and checked_count > 0) else Qt.CheckState.Checked
            for r in range(self.table.rowCount()):
                item = self.table.item(r, 0)
                if item:
                    item.setCheckState(new_state)
            self._update_action_state(bool(self.selected_serials()))
            return

        # Đảo chiều tăng/giảm nếu bấm lại cùng cột, hoặc đặt mặc định tăng dần cho cột mới
        if getattr(self, "_sort_column", None) == column:
            self._sort_order = (
                Qt.SortOrder.DescendingOrder
                if self._sort_order == Qt.SortOrder.AscendingOrder
                else Qt.SortOrder.AscendingOrder
            )
        else:
            self._sort_column = column
            self._sort_order = Qt.SortOrder.AscendingOrder

        self.table.horizontalHeader().setSortIndicator(self._sort_column, self._sort_order)
        self._render_table(rebuild_filter=False)

    def _render_table(self, rebuild_filter: bool = False) -> None:
        raw_devices = self.all_devices

        has_active_filter = (
            self.chk_filter_empty_fb.isChecked() or
            self.chk_filter_live.isChecked() or
            self.chk_filter_die.isChecked() or
            bool(self.search_input.text().strip())
        )

        if not has_active_filter:
            self._active_filtered_serials = None

        if self._active_filtered_serials is not None and not rebuild_filter and has_active_filter:
            raw_devices = [d for d in raw_devices if d.serial in self._active_filtered_serials]
        else:
            # 1. Filter máy trống
            if self.chk_filter_empty_fb.isChecked():
                def is_empty(val: str | None) -> bool:
                    if not val:
                        return True
                    return val.strip().lower() in ("", "trống", "none", "null")
                raw_devices = [d for d in raw_devices if is_empty(d.fb_uid) or is_empty(d.fb_name)]

            # 1.1. Filter theo Live/Die
            show_live = self.chk_filter_live.isChecked()
            show_die = self.chk_filter_die.isChecked()

            def is_live(d: DeviceInfo) -> bool:
                note_lower = (d.note or "").lower()
                return d.last_check_status == "LIVE" or d.state == "LIVE" or "live" in note_lower

            def is_die(d: DeviceInfo) -> bool:
                note_lower = (d.note or "").lower()
                return d.last_check_status == "DIE" or d.state == "DIE" or "die" in note_lower

            if show_live and show_die:
                raw_devices = [d for d in raw_devices if is_live(d) or is_die(d)]
            elif show_live:
                raw_devices = [d for d in raw_devices if is_live(d)]
            elif show_die:
                raw_devices = [d for d in raw_devices if is_die(d)]

            # 2. Filter theo Search Input
            q = self.search_input.text().strip().lower()
            if q:
                filtered = []
                for d in raw_devices:
                    match = (
                        (q in d.serial.lower()) or 
                        (d.fb_uid and q in d.fb_uid.lower()) or 
                        (d.fb_name and q in d.fb_name.lower()) or 
                        (d.note and q in d.note.lower()) or
                        (d.state and q in d.state.lower()) or
                        (d.last_check_status and q in d.last_check_status.lower())
                    )
                    if match:
                        filtered.append(d)
                raw_devices = filtered

            if has_active_filter:
                self._active_filtered_serials = {d.serial for d in raw_devices}

        # 3. Sắp xếp danh sách thiết bị theo cột được chọn (nếu có)
        sort_col = getattr(self, "_sort_column", None)
        if sort_col is not None and sort_col >= 1:
            reverse = (getattr(self, "_sort_order", Qt.SortOrder.AscendingOrder) == Qt.SortOrder.DescendingOrder)

            def get_sort_key(d: DeviceInfo):
                if sort_col == 1:  # STT (theo thứ tự ban đầu)
                    try:
                        return (0, self.all_devices.index(d))
                    except Exception:
                        return (0, 0)
                elif sort_col == 2:  # ID thiết bị
                    val = (d.serial or "").strip().lower()
                    return (0 if val else 1, val)
                elif sort_col == 3:  # Model
                    val = (d.display_name or "").strip().lower()
                    return (0 if val else 1, val)
                elif sort_col == 4:  # Android
                    val = (d.android_version or "").strip()
                    try:
                        return (0 if val else 1, float(val))
                    except ValueError:
                        return (0 if val else 1, val.lower())
                elif sort_col == 5:  # FB UID
                    val = (d.fb_uid or "").strip()
                    try:
                        return (0 if val else 1, int(val))
                    except ValueError:
                        return (0 if val else 1, val.lower())
                elif sort_col == 6:  # FB Tên
                    val = (d.fb_name or "").strip().lower()
                    return (0 if val else 1, val)
                elif sort_col == 7:  # Proxy
                    val = (d.proxy or "").strip().lower()
                    return (0 if val else 1, val)
                elif sort_col == 8:  # Ghi chú
                    val = (d.note or "").strip().lower()
                    return (0 if val else 1, val)
                elif sort_col == 9:  # Trạng thái (LIVE / DIE / Đã kết nối / Chưa kết nối / Trống)
                    st = (d.state or d.last_check_status or "").strip().lower()
                    return (0 if st else 1, st)
                return (0, "")

            raw_devices = list(raw_devices)
            raw_devices.sort(key=get_sort_key, reverse=reverse)

        # Lưu lại danh sách máy đang được tick chọn trước khi vẽ lại
        checked_serials = set()
        for r in range(self.table.rowCount()):
            cb = self.table.item(r, 0)
            if cb and cb.checkState() == Qt.CheckState.Checked:
                s = self._serial_at_row(r)
                if s:
                    checked_serials.add(s)

        self.devices = list(raw_devices)

        self.table.setRowCount(0)

        for index, device in enumerate(self.devices, start=1):
            row = self.table.rowCount()
            self.table.insertRow(row)

            checkbox_item = QTableWidgetItem()
            checkbox_item.setFlags(
                Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
            )
            if device.serial in checked_serials:
                checkbox_item.setCheckState(Qt.CheckState.Checked)
            else:
                checkbox_item.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row, 0, checkbox_item)

            values = [
                str(index),
                device.serial,
                device.display_name,
                device.android_version or "",
                device.fb_uid or "",
                device.fb_name or "",
                device.proxy or "",
                device.note or "",
                device.state,
            ]
            for column, value in enumerate(values, start=1):
                item = QTableWidgetItem(value)
                item.setFlags(Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
                if column == 9:
                    st_low = value.lower()
                    if "live" in st_low:
                        item.setForeground(QBrush(QColor("#4ade80")))
                    elif "die" in st_low:
                        item.setForeground(QBrush(QColor("#f87171")))
                    elif "đã kết nối" in st_low or "stop proxy" in st_low:
                        item.setForeground(QBrush(QColor("#38bdf8")))
                    elif "chưa kết nối" in st_low or "start proxy" in st_low:
                        item.setForeground(QBrush(QColor("#f87171")))
                self.table.setItem(row, column, item)

        self._apply_duplicate_uid_highlight()

        self.statusBar().showMessage(f"Đã quét {len(self.all_devices)} thiết bị. (Đang hiển thị {len(self.devices)})", 5000)
        has_uid = any((device.fb_uid or "").strip() for device in self.devices)
        self.act_copy_uids.setEnabled(has_uid)
        self.act_check_duplicate_uids.setEnabled(has_uid)
        self._update_detail_panel()
        self._update_action_state(bool(self.selected_serials()))

    def _apply_duplicate_uid_highlight(self) -> None:
        if not self._duplicate_uid_groups:
            return

        highlight_bg = QBrush(QColor("#7c2d12"))
        highlight_fg = QBrush(QColor("#fff7ed"))
        bold_font = QFont(self.font())
        bold_font.setBold(True)

        uid_to_rows: dict[str, list[int]] = {}
        for row, device in enumerate(self.devices):
            uid = (device.fb_uid or "").strip()
            if uid in self._duplicate_uid_groups:
                uid_to_rows.setdefault(uid, []).append(row)

        for uid, rows in uid_to_rows.items():
            if len(rows) < 2:
                continue
            serials = self._duplicate_uid_groups.get(uid, [])
            stt_values = [str(row + 1) for row in rows]
            tooltip = f"Trùng UID: {uid}\nSTT: {', '.join(stt_values)}\nMáy: {', '.join(serials)}"
            for row in rows:
                for column in range(self.table.columnCount()):
                    item = self.table.item(row, column)
                    if not item:
                        continue
                    item.setBackground(highlight_bg)
                    item.setForeground(highlight_fg)
                    item.setFont(bold_font)
                    item.setToolTip(tooltip)
                status_item = self.table.item(row, 9)
                if status_item:
                    status_item.setText(f"Trùng UID x{len(rows)}")

    def check_duplicate_fb_uids(self) -> None:
        groups: dict[str, list[str]] = {}
        for device in self.devices:
            uid = (device.fb_uid or "").strip()
            if not uid:
                continue
            groups.setdefault(uid, []).append(device.serial)

        self._duplicate_uid_groups = {uid: serials for uid, serials in groups.items() if len(serials) > 1}
        if not self._duplicate_uid_groups:
            self._render_table()
            self.statusBar().showMessage("Không tìm thấy UID nào bị trùng.", 5000)
            QMessageBox.information(self, "Check trùng UID", "Không tìm thấy máy nào trùng tài khoản Facebook theo UID.")
            return

        self._render_table()
        self.statusBar().showMessage(f"Phát hiện {len(self._duplicate_uid_groups)} UID bị trùng.", 7000)

    def copy_visible_uids(self) -> None:
        uids: list[str] = []
        seen: set[str] = set()
        for device in self.devices:
            uid = (device.fb_uid or "").strip()
            if not uid:
                continue
            if uid in seen:
                continue
            seen.add(uid)
            uids.append(uid)

        if not uids:
            self.statusBar().showMessage("Không có UID nào để copy.", 4000)
            QMessageBox.information(self, "Copy UID", "Không có UID nào trong danh sách đang hiển thị.")
            return

        QApplication.clipboard().setText("\n".join(uids))
        self.statusBar().showMessage(f"Đã copy {len(uids)} UID.", 4000)

    def _on_cell_clicked(self, row: int, column: int) -> None:
        if column == 0:
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(
                    Qt.CheckState.Unchecked if item.checkState() == Qt.CheckState.Checked else Qt.CheckState.Checked
                )
        self._update_detail_panel()
        self._update_action_state(bool(self.selected_serials()))

    def _on_selection_changed(self) -> None:
        self._selected_serial = self._primary_selected_serial()
        self._update_detail_panel()
        self._update_action_state(bool(self.selected_serials()))

    def _on_current_changed(self, current_row: int, _current_column: int, _previous_row: int, _previous_column: int) -> None:
        self._selected_serial = self._serial_at_row(current_row)
        self.act_select_current.setEnabled(current_row >= 0)
        self._update_detail_panel()

    def _serial_at_row(self, row: int) -> str | None:
        if row < 0:
            return None
        item = self.table.item(row, 2)
        return item.text() if item else None

    def _selected_rows(self) -> list[int]:
        rows = sorted({index.row() for index in self.table.selectionModel().selectedIndexes()})
        return rows

    def _primary_selected_serial(self) -> str | None:
        rows = self._selected_rows()
        if rows:
            return self._serial_at_row(rows[0])
        if self.table.currentRow() >= 0:
            return self._serial_at_row(self.table.currentRow())
        return None

    def selected_serials(self) -> list[str]:
        # 1. Nếu người dùng bôi đen (highlight) các dòng, CHỈ lấy các dòng được bôi đen
        selected_rows = self._selected_rows()
        if selected_rows:
            serials: list[str] = []
            for row in selected_rows:
                serial = self._serial_at_row(row)
                if serial:
                    serials.append(serial)
            return serials

        # 2. Nếu KHÔNG CÓ máy nào được bôi đen, lúc này mới lấy các máy được tick checkbox
        serials: list[str] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                serial = self._serial_at_row(row)
                if serial:
                    serials.append(serial)
                    
        return serials

    def _device_by_serial(self, serial: str) -> DeviceInfo | None:
        for device in self.all_devices:
            if device.serial == serial:
                return device
        return None

    def _current_device(self) -> DeviceInfo | None:
        if not self._selected_serial:
            return None
        return self._device_by_serial(self._selected_serial)

    def _update_detail_panel(self) -> None:
        device = self._current_device()
        if not device:
            self.detail_status.setText("Chưa chọn thiết bị")
            self.proxy_input.clear()
            self.note_input.clear()
            return

        self.detail_status.setText(
            f"Serial: {device.serial}\nModel: {device.display_name}\nAndroid: {device.android_version or 'Không rõ'}\nTrạng thái: {device.state}"
        )
        self.proxy_input.setText(device.proxy)
        self.note_input.setText(device.note)

    def select_current_row(self) -> None:
        row = self.table.currentRow()
        if row < 0:
            return
        self.table.selectRow(row)
        item = self.table.item(row, 0)
        if item is not None:
            item.setCheckState(Qt.CheckState.Checked)
        self._update_action_state(bool(self.selected_serials()))

    def select_all_devices(self) -> None:
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            index = self.table.model().index(row, 0)
            self.table.selectionModel().select(
                index,
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Checked)
        self._update_action_state(bool(self.selected_serials()))

    def clear_selection(self) -> None:
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item is not None:
                item.setCheckState(Qt.CheckState.Unchecked)
        self._selected_serial = None
        self._update_detail_panel()
        self._update_action_state(False)

    def _require_selection(self) -> list[str]:
        serials = self.selected_serials()
        if not serials:
            QMessageBox.information(self, "Thông báo", "Hãy bôi đen hoặc tick ít nhất một thiết bị.")
        return serials

    def show_screen_window(self) -> None:
        if hasattr(self, "screen_window") and self.screen_window:
            self.screen_window.showNormal()
            self.screen_window.show()
            self.screen_window.raise_()
            self.screen_window.activateWindow()

    def _embed_window_ui(self, serial: str, hwnd: int) -> None:
        slot = self._screen_slots.get(serial)
        if not slot:
            return
        self._screen_hwnds[serial] = hwnd
        if hasattr(slot.host, "set_hwnd"):
            slot.host.set_hwnd(hwnd)

        slot.host.setAttribute(Qt.WidgetAttribute.WA_NativeWindow, True)
        parent_hwnd = int(slot.host.winId())

        _reparent_window(hwnd, parent_hwnd)
        _move_window(hwnd, slot.host.width(), slot.host.height())
        _focus_embedded_window(hwnd)
        self._set_slot_status(serial, "Running")

    def open_selected_screens(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        self.show_screen_window()

        # Dọn dẹp tiến trình rác/màn hình mồ côi từ lần chạy bị ngắt đột ngột trước đó
        ready_serials = []
        for s in serials:
            proc = self._screen_procs.get(s)
            hwnd = self._screen_hwnds.get(s)
            is_valid = False
            if proc is not None and hwnd:
                try:
                    is_valid = (proc.poll() is None) and USER32.IsWindow(hwnd)
                except Exception:
                    is_valid = False

            if not is_valid:
                self._screen_hwnds.pop(s, None)
                self._embed_retry.pop(s, None)
                self._kill_screen_process(s)
                ready_serials.append(s)

        if not ready_serials:
            return

        quality = "low" if len(ready_serials) >= 12 else "balanced"

        for serial in ready_serials:
            if serial not in self._screen_slots:
                slot = self._create_screen_slot(serial, 540, 960)
                self.screen_grid.addWidget(slot.root)
            self._set_slot_status(serial, "Đang chờ...")

        self._reflow_screen_grid()

        def work_sequential() -> None:
            total = len(ready_serials)
            for idx, serial in enumerate(ready_serials, 1):
                self.status_update_requested.emit(
                    serial, f"Đang mở ({idx}/{total})..."
                )
                try:
                    self._screen_hwnds.pop(serial, None)
                    self._kill_screen_process(serial)

                    proc, win_w, win_h = launch_scrcpy(serial, quality=quality)
                    if proc is not None:
                        self._screen_procs[serial] = proc
                        self._screen_pids[serial] = proc.pid
                        self._screen_quality[serial] = quality
                        self._embed_retry[serial] = 0

                    embedded = False
                    for _ in range(60):
                        if serial in self._screen_hwnds:
                            embedded = True
                            break
                        pid = proc.pid if proc else 0
                        used_hwnds = set(self._screen_hwnds.values())
                        hwnd = _find_scrcpy_window(serial, pid, exclude_hwnds=used_hwnds)
                        if hwnd is not None:
                            self._embed_window_signal.emit(serial, hwnd)
                            embedded = True
                            time.sleep(0.4)
                            break
                        time.sleep(0.15)

                    if not embedded:
                        self.status_update_requested.emit(serial, "Đang chờ hình...")
                except Exception as exc:
                    self.status_update_requested.emit(serial, f"Lỗi: {exc}")

                time.sleep(0.2)

        self._start_task(work_sequential, busy_message="Đang mở màn hình từng máy 1...")

    def _create_screen_slot(self, serial: str, base_w: int, base_h: int) -> ScreenSlot:
        root = QFrame()
        root.setObjectName("screenCard")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel(serial)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)

        sync_chk = QCheckBox("Sync")
        sync_chk.setChecked(True)
        sync_chk.setToolTip("Nhận lệnh đồng bộ từ máy Master")
        self.screen_window._keyboard_hook.set_sync_allowed(serial, True)
        sync_chk.toggled.connect(lambda checked, s=serial: self.screen_window._keyboard_hook.set_sync_allowed(s, checked))

        refresh_btn = QPushButton("R")
        refresh_btn.setFixedSize(26, 24)
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.setToolTip("Mở lại máy này")
        refresh_btn.clicked.connect(lambda: self._relaunch_screen(serial, force=True))

        close_btn = QPushButton("X")
        close_btn.setFixedSize(26, 24)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(lambda: self.close_screen_slot(serial))

        header.addWidget(title, 1)
        header.addWidget(sync_chk, 0)
        header.addWidget(refresh_btn, 0)
        header.addWidget(close_btn, 0)

        status = QLabel("Connecting")
        status.setObjectName("slotStatus")
        status.setAlignment(Qt.AlignmentFlag.AlignCenter)

        host = ScreenHostWidget(serial)
        host.setObjectName("screenHost")

        layout.addLayout(header)
        layout.addWidget(status)
        layout.addWidget(host)

        slot = ScreenSlot(
            serial=serial,
            root=root,
            host=host,
            title=title,
            status=status,
            refresh_btn=refresh_btn,
            close_btn=close_btn,
            sync_chk=sync_chk,
            base_w=base_w,
            base_h=base_h,
        )
        self._screen_slots[serial] = slot
        return slot

    def _set_slot_status(self, serial: str, text: str) -> None:
        slot = self._screen_slots.get(serial)
        if slot:
            slot.status.setText(text)

    def _on_screens_opened(self, launched: list[tuple[str, object, int, int, int, str]]) -> None:
        if not launched:
            return

        self.show_screen_window()

        for serial, proc, pid, base_w, base_h, quality in launched:
            if serial not in self._screen_slots:
                slot = self._create_screen_slot(serial, base_w or 540, base_h or 960)
                self.screen_grid.addWidget(slot.root)
            else:
                slot = self._screen_slots[serial]
                if base_w > 0 and base_h > 0:
                    slot.base_w = base_w
                    slot.base_h = base_h

            if pid:
                self._screen_pids[serial] = pid
            if proc is not None:
                self._screen_procs[serial] = proc
                self._screen_quality[serial] = quality
            self._embed_retry[serial] = 0
            self._relaunch_attempts.setdefault(serial, 0)
            self._set_slot_status(serial, "Connecting")
            QTimer.singleShot(400, partial(self._try_embed_screen, serial))

        self._reflow_screen_grid()
        self._sync_embedded_windows()

    def _kill_screen_process(self, serial: str) -> None:
        proc = self._screen_procs.pop(serial, None)
        self._screen_quality.pop(serial, None)
        if proc is not None:
            try:
                poll = proc.poll() if hasattr(proc, "poll") else None
                if poll is None:
                    if hasattr(proc, "kill"):
                        proc.kill()
                    elif hasattr(proc, "terminate"):
                        proc.terminate()
            except Exception:
                pass

        self._screen_pids.pop(serial, 0)

    def close_screen_slot(self, serial: str) -> None:
        slot = self._screen_slots.pop(serial, None)
        self._screen_hwnds.pop(serial, None)
        self._embed_retry.pop(serial, None)
        self._relaunch_attempts.pop(serial, None)
        self._kill_screen_process(serial)
        if slot:
            slot.root.deleteLater()
        self._reflow_screen_grid()

    def _relaunch_screen(self, serial: str, force: bool = False) -> None:
        slot = self._screen_slots.get(serial)
        if not slot:
            return

        attempts = self._relaunch_attempts.get(serial, 0)
        if attempts >= 3 and not force:
            self._set_slot_status(serial, "Failed")
            return

        self._relaunch_attempts[serial] = 0 if force else attempts + 1
        shown_attempt = (attempts + 1) if not force else 1
        self._set_slot_status(serial, "Retrying")
        self.statusBar().showMessage(
            f"Đang thử mở lại máy {serial} (lần {shown_attempt})...",
            5000,
        )

        quality = self._screen_quality.get(serial, "low")
        self._screen_hwnds.pop(serial, None)
        self._embed_retry[serial] = 0
        self._kill_screen_process(serial)
        self._screen_quality[serial] = quality

        def _start() -> None:
            try:
                proc, win_w, win_h = launch_scrcpy(serial, quality=quality)
                if proc is not None:
                    self._screen_procs[serial] = proc
                    self._screen_pids[serial] = proc.pid
                    self._screen_quality[serial] = quality
                if win_w > 0 and win_h > 0:
                    slot.base_w = win_w
                    slot.base_h = win_h
                self._set_slot_status(serial, "Connecting")
                QTimer.singleShot(600, lambda s=serial: self._try_embed_screen(s))
            except Exception as exc:  # noqa: BLE001
                self._set_slot_status(serial, "Failed")
                self.statusBar().showMessage(f"Mở lại {serial} thất bại: {exc}", 7000)

        QTimer.singleShot(650, _start)

    def _scale_size(self, base_w: int, base_h: int) -> tuple[int, int]:
        scale = self._scale_percent / 100.0
        w = max(180, int(base_w * scale))
        h = max(320, int(base_h * scale))
        return w, h

    def _reflow_screen_grid(self) -> None:
        slots = list(self._screen_slots.values())
        if not slots:
            return

        for i in reversed(range(self.screen_grid.count())):
            item = self.screen_grid.itemAt(i)
            widget = item.widget() if item else None
            if widget:
                self.screen_grid.removeWidget(widget)

        sample_w, _ = self._scale_size(slots[0].base_w, slots[0].base_h)
        available = max(300, self.screen_scroll.viewport().width() - 24)
        cols = max(1, available // max(220, sample_w + 20))

        for index, slot in enumerate(slots):
            w, h = self._scale_size(slot.base_w, slot.base_h)
            slot.host.setMinimumSize(w, h)
            slot.host.setMaximumSize(w, h)
            slot.root.setMinimumSize(w + 18, h + 62)
            slot.root.setMaximumSize(w + 18, h + 62)
            row = index // cols
            col = index % cols
            self.screen_grid.addWidget(slot.root, row, col)

        self._sync_embedded_windows()

    def _try_embed_screen(self, serial: str) -> None:
        slot = self._screen_slots.get(serial)
        if not slot or serial in self._screen_hwnds:
            return

        pid = self._screen_pids.get(serial, 0)
        used_hwnds = set(self._screen_hwnds.values())
        hwnd = _find_scrcpy_window(serial, pid, exclude_hwnds=used_hwnds)

        if hwnd is None:
            proc = self._screen_procs.get(serial)
            if proc is not None and hasattr(proc, "poll") and proc.poll() is not None:
                device = self._device_by_serial(serial)
                if device and device.state == "offline":
                    self._set_slot_status(serial, "Offline (Lỗi cáp/USB)")
                else:
                    self._set_slot_status(serial, "Exited")
                return

            retries = self._embed_retry.get(serial, 0) + 1
            self._embed_retry[serial] = retries
            if retries > 35:
                self._set_slot_status(serial, "Timeout")
                return

            self._set_slot_status(serial, "Connecting...")
            QTimer.singleShot(400, partial(self._try_embed_screen, serial))
            return

        self._screen_hwnds[serial] = hwnd
        if hasattr(slot.host, "set_hwnd"):
            slot.host.set_hwnd(hwnd)
        _reparent_window(hwnd, int(slot.host.winId()))
        _move_window(hwnd, slot.host.width(), slot.host.height())
        self._set_slot_status(serial, "Running")

    def _sync_embedded_windows(self) -> None:
        # Maintain existing embedded windows.
        for serial, hwnd in list(self._screen_hwnds.items()):
            slot = self._screen_slots.get(serial)
            if not slot:
                continue
            _move_window(hwnd, slot.host.width(), slot.host.height())

        # For any slot not embedded yet, trigger another attach attempt.
        for serial in list(self._screen_slots.keys()):
            if serial not in self._screen_hwnds:
                self._try_embed_screen(serial)

        # Update keyboard hook with latest HWNDs
        if hasattr(self, "screen_window") and hasattr(self.screen_window, "_keyboard_hook"):
            self.screen_window._keyboard_hook.update_scrcpy_hwnds(self._screen_hwnds)
            self.screen_window.update_master_list(list(self._screen_slots.keys()))

    def _on_scale_changed(self, value: int) -> None:
        self._scale_percent = value
        self.scale_label.setText(f"Kích thước màn: {value}%")
        self._reflow_screen_grid()

    def clear_screen_wall(self) -> None:
        for serial in list(self._screen_slots.keys()):
            self.close_screen_slot(serial)

    def set_wifi_for_selected(self, enabled: bool) -> None:
        if not enabled:
            serials = self._require_selection()
            if not serials: return
            def work() -> None:
                for serial in serials:
                    set_wifi(serial, False)
                    self.status_update_requested.emit(serial, "Đã tắt Wi-Fi")
            self._start_task(work, busy_message="Đang tắt Wi-Fi cho thiết bị...")
            return

        serials = self._require_selection()
        if not serials:
            return
            
        text, ok = QInputDialog.getText(
            self,
            "Kết nối Wi-Fi",
            "Nhập Wi-Fi theo định dạng: Tên_Wifi|Mật_khẩu",
        )
        if not ok or not text.strip(): return
        
        parts = text.strip().split("|")
        if len(parts) != 2:
            QMessageBox.critical(self, "Lỗi", "Sai định dạng. Vui lòng nhập: Tên_Wifi|Mật_khẩu")
            return
            
        ssid, password = parts[0].strip(), parts[1].strip()

        def work() -> None:
            import concurrent.futures
            
            def process_wifi(serial: str) -> None:
                self.status_update_requested.emit(serial, f"Đang kết nối: {ssid}")
                ok_connect = connect_wifi(serial, ssid, password)
                if ok_connect:
                    self.status_update_requested.emit(serial, f"Wi-Fi OK")
                else:
                    self.status_update_requested.emit(serial, f"Lỗi Wi-Fi")
                    
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                for _ in executor.map(process_wifi, serials):
                    pass

        self._start_task(work, busy_message="Đang kiểm tra và kết nối Wi-Fi hàng loạt...")

    def _proxy_value(self) -> str:
        proxy = self.proxy_input.text().strip()
        if not proxy:
            raise AdbError("Proxy trống.")
        return proxy

    def apply_proxy_to_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        text, ok = QInputDialog.getMultiLineText(
            self,
            "Gán Proxy Hàng Loạt",
            f"Nhập danh sách proxy (mỗi dòng 1 proxy). Đã chọn {len(serials)} máy:\nVD: ip:port hoặc ip:port:user:pass",
        )
        if not ok or not text.strip():
            return

        proxies = [p.strip() for p in text.strip().split("\n") if p.strip()]
        if not proxies:
            return

        def work() -> None:
            import concurrent.futures
            
            def process_proxy(index_serial_tuple: tuple[int, str]) -> None:
                index, serial = index_serial_tuple
                proxy = proxies[index % len(proxies)]
                
                self.status_update_requested.emit(serial, "Đang gán Proxy...")
                set_proxy(serial, proxy)
                self.profile_store.update(serial, proxy=proxy)
                
                self.status_update_requested.emit(serial, "Khởi động Fake IP...")
                ok_proxy = connect_college_proxy(serial)
                if ok_proxy:
                    self.status_update_requested.emit(serial, "Proxy + Fake OK")
                else:
                    self.status_update_requested.emit(serial, "Lỗi Fake Proxy")
                    
            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                list(executor.map(process_proxy, enumerate(serials)))

        def on_success(_: object = None) -> None:
            self._render_table()
            self.statusBar().showMessage("Đã áp dụng proxy xong.", 5000)

        self._start_task(work, on_success, busy_message="Đang áp proxy cho thiết bị...")

    def connect_proxy_in_app_for_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        def work() -> None:
            import concurrent.futures

            def process_proxy(serial: str) -> None:
                profile = self.profile_store.get(serial)
                device = self._device_by_serial(serial)
                proxy = profile.proxy.strip() or (device.proxy if device and device.proxy else "").strip()
                if not proxy:
                    self.status_update_requested.emit(serial, "Thiếu proxy trong dữ liệu máy")
                    return

                self.status_update_requested.emit(serial, "Bắt đầu kết nối proxy...")
                ok_proxy = connect_college_proxy(
                    serial,
                    proxy=proxy,
                    progress=lambda message, s=serial: self.status_update_requested.emit(s, message),
                )
                if ok_proxy:
                    self.status_update_requested.emit(serial, "Proxy đã kết nối OK")
                else:
                    self.status_update_requested.emit(serial, "Kết nối proxy thất bại")

            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                list(executor.map(process_proxy, serials))

        def on_success(_: object = None) -> None:
            self._render_table()
            self.statusBar().showMessage(f"Đã hoàn tất kết nối proxy trong app cho {len(serials)} máy (20 luồng).", 5000)

        self._start_task(work, on_success, busy_message=f"Đang kết nối proxy 20 luồng cho {len(serials)} thiết bị...")

    def check_proxy_status_for_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        def work() -> dict[str, dict[str, object]]:
            import concurrent.futures

            results = {}

            def process_check(serial: str) -> None:
                self.status_update_requested.emit(serial, "Đang kiểm tra Proxy...")
                profile = self.profile_store.get(serial)
                device = self._device_by_serial(serial)
                saved_proxy = (profile.proxy or "").strip() if profile else ""
                if not saved_proxy and device and device.proxy:
                    saved_proxy = device.proxy.strip()
                status = check_proxy_status(serial, saved_proxy=saved_proxy)
                results[serial] = status
                if status.get("connected"):
                    msg = f"✓ {status.get('message')}"
                else:
                    msg = f"✗ {status.get('message')}"
                self.status_update_requested.emit(serial, msg)


            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                list(executor.map(process_check, serials))
            return results

        def on_success(results: dict[str, dict[str, object]]) -> None:
            connected_count = sum(1 for s in results.values() if s.get("connected"))
            summary = f"Check proxy xong {len(serials)} máy ({connected_count}/{len(serials)} máy đang kết nối Proxy)."
            self.statusBar().showMessage(summary, 7000)



        self._start_task(work, on_success, busy_message="Đang kiểm tra kết nối Proxy trên các thiết bị...")


    def clear_proxy_for_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        def work() -> None:
            for serial in serials:
                clear_proxy(serial)
                self.profile_store.update(serial, proxy="")

        self._start_task(work, busy_message="Đang xóa proxy khỏi thiết bị...")

    def clear_college_proxy_data_for_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        reply = QMessageBox.question(
            self,
            "Xóa dữ liệu College Proxy",
            f"Xóa dữ liệu ứng dụng College Proxy trên {len(serials)} máy đã chọn? Thao tác này sẽ reset lại cấu hình app như mới.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        def work() -> None:
            import concurrent.futures

            def process_clear(serial: str) -> None:
                if is_stop_requested(serial):
                    return
                try:
                    self.status_update_requested.emit(serial, "Đang xóa dữ liệu College Proxy...")
                    ok = clear_college_proxy_data(serial)
                    if ok:
                        self.status_update_requested.emit(serial, "✓ Đã xóa dữ liệu College Proxy")
                    else:
                        self.status_update_requested.emit(serial, "Đã đóng app College Proxy")
                except Exception as exc:
                    self.status_update_requested.emit(serial, f"Lỗi xóa dữ liệu proxy: {exc}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(serials), 20)) as executor:
                list(executor.map(process_clear, serials))

        def on_success(_: object = None) -> None:
            self.statusBar().showMessage(f"Đã xóa dữ liệu College Proxy trên {len(serials)} thiết bị.", 5000)

        self._start_task(work, on_success, busy_message=f"Đang xóa dữ liệu College Proxy trên {len(serials)} thiết bị...")

    def save_current_profile(self) -> None:
        device = self._current_device()
        if not device:
            QMessageBox.information(self, "Thông báo", "Hãy chọn một thiết bị để lưu cấu hình.")
            return

        proxy = self.proxy_input.text().strip()
        note = self.note_input.text().strip()

        def work() -> None:
            self.profile_store.update(device.serial, proxy=proxy, note=note)

        self._start_task(work, busy_message="Đang lưu cấu hình thiết bị...")

    def get_fb_info_for_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        def work() -> dict[str, tuple[str, str]]:
            import concurrent.futures

            results = {}

            def fetch_info(serial: str) -> tuple[str, str, str]:
                self.status_update_requested.emit(serial, "Đang lấy info...")
                try:
                    uid, name = get_fb_info(serial)
                except Exception as exc:
                    uid, name = "Trống", "Trống"
                    self.status_update_requested.emit(serial, f"Lỗi lấy info: {exc}")
                else:
                    self.status_update_requested.emit(serial, "Đã lấy xong info")
                return serial, uid, name

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(serials), 20)) as executor:
                # Dùng map để xử lý song song
                for serial, uid, name in executor.map(fetch_info, serials):
                    results[serial] = (uid, name)

            return results

        def on_success(results: dict[str, tuple[str, str]]) -> None:
            import re
            for serial, (uid, name) in results.items():
                device = self._device_by_serial(serial)
                if uid and uid != "Trống":
                    new_note = None
                    if device:
                        device.fb_uid = uid
                        device.fb_name = name
                        device.last_check_status = "LIVE"
                        if device.state == "DIE":
                            device.state = "LIVE"
                        if device.note and "die" in device.note.lower():
                            clean_note = re.sub(r'(?i)\bdie\b', '', device.note).strip()
                            device.note = clean_note
                            new_note = clean_note
                    if new_note is not None:
                        self.profile_store.update(serial, fb_uid=uid, fb_name=name, last_check_status="LIVE", state="LIVE", note=new_note)
                    else:
                        self.profile_store.update(serial, fb_uid=uid, fb_name=name, last_check_status="LIVE", state="LIVE")
                else:
                    if device:
                        device.fb_uid = uid
                        device.fb_name = name
                    self.profile_store.update(serial, fb_uid=uid, fb_name=name)
            
            self._render_table()
            self.statusBar().showMessage("Đã lấy xong thông tin Facebook và tự động bỏ khỏi danh sách lọc DIE.", 5000)

        self._start_task(work, on_success, busy_message="Đang cào thông tin FB, vui lòng chờ (có thể mất một lúc)...")

    def check_live_die_for_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        # Lấy danh sách UID của các máy đã chọn
        device_uids = []
        for serial in serials:
            profile = self.profile_store.get(serial)
            uid = profile.fb_uid.strip()
            if uid:
                device_uids.append((serial, uid))
            else:
                self._update_table_status(serial, "Không có UID")

        if not device_uids:
            QMessageBox.warning(self, "Thông báo", "Các thiết bị được chọn không có UID Facebook để kiểm tra.")
            return

        def work() -> dict[str, str]:
            import concurrent.futures
            
            results = {}
            
            def check_one(serial_uid: tuple[str, str]) -> tuple[str, str]:
                serial, uid = serial_uid
                self.status_update_requested.emit(serial, f"Đang check UID {uid}...")
                try:
                    status = check_fb_uid_live_die(uid)
                except Exception as exc:
                    status = f"LỖI: {exc}"
                self.status_update_requested.emit(serial, status)
                return serial, status

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(device_uids), 20)) as executor:
                for serial, status in executor.map(check_one, device_uids):
                    results[serial] = status
            return results

        def on_success(results: dict[str, str]) -> None:
            live_count = sum(1 for s in results.values() if s == "LIVE")
            die_count = sum(1 for s in results.values() if s == "DIE")
            self.statusBar().showMessage(f"Đã check xong: {live_count} LIVE, {die_count} DIE.", 6000)
            
            # Cập nhật trạng thái hiển thị trong bảng
            for row in range(self.table.rowCount()):
                serial = self._serial_at_row(row)
                if serial in results:
                    status = results[serial]
                    device = self._device_by_serial(serial)
                    if device:
                        device.state = status
                        if status in ("LIVE", "DIE"):
                            device.last_check_status = status
                    status_item = self.table.item(row, 9)
                    if status_item:
                        status_item.setText(status)
                        if status == "LIVE":
                            status_item.setForeground(QBrush(QColor("#4ade80"))) # Xanh lá sáng
                        elif status == "DIE":
                            status_item.setForeground(QBrush(QColor("#f87171"))) # Đỏ sáng

        self._start_task(work, on_success, busy_message="Đang kiểm tra Live/Die UID Facebook của thiết bị...")

    def check_live_die_manual(self) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self,
            "Check Live/Die UID Facebook thủ công",
            "Nhập danh sách UID Facebook (mỗi dòng một UID):"
        )
        if not ok or not text.strip():
            return

        uids = [line.strip() for line in text.splitlines() if line.strip()]
        if not uids:
            return

        def work() -> list[tuple[str, str]]:
            import concurrent.futures
            
            results = []
            
            def check_one(uid: str) -> tuple[str, str]:
                try:
                    status = check_fb_uid_live_die(uid)
                except Exception as exc:
                    status = f"LỖI: {exc}"
                return uid, status

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(uids), 20)) as executor:
                results = list(executor.map(check_one, uids))
            return results

        def on_success(results: list[tuple[str, str]]) -> None:
            live_uids = [uid for uid, status in results if status == "LIVE"]
            die_uids = [uid for uid, status in results if status == "DIE"]
            error_uids = [uid for uid, status in results if status not in ("LIVE", "DIE")]
            
            msg = (
                f"Kết quả kiểm tra {len(results)} UID:\n\n"
                f" - LIVE: {len(live_uids)} UID\n"
                f" - DIE: {len(die_uids)} UID\n"
            )
            if error_uids:
                msg += f" - LỖI: {len(error_uids)} UID\n"
                
            dialog = QMessageBox(self)
            dialog.setWindowTitle("Kết quả Check Live/Die")
            dialog.setText(msg)
            
            copy_live_btn = dialog.addButton("Copy UID LIVE", QMessageBox.ButtonRole.ActionRole)
            copy_die_btn = dialog.addButton("Copy UID DIE", QMessageBox.ButtonRole.ActionRole)
            close_btn = dialog.addButton("Đóng", QMessageBox.ButtonRole.RejectRole)
            
            if not live_uids:
                copy_live_btn.setEnabled(False)
            if not die_uids:
                copy_die_btn.setEnabled(False)
                
            dialog.exec()
            
            if dialog.clickedButton() == copy_live_btn:
                QApplication.clipboard().setText("\n".join(live_uids))
                self.statusBar().showMessage(f"Đã copy {len(live_uids)} UID LIVE vào Clipboard.", 5000)
            elif dialog.clickedButton() == copy_die_btn:
                QApplication.clipboard().setText("\n".join(die_uids))
                self.statusBar().showMessage(f"Đã copy {len(die_uids)} UID DIE vào Clipboard.", 5000)

        self._start_task(work, on_success, busy_message="Đang kiểm tra danh sách UID Facebook...")

    def set_fb_accounts_for_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        text, ok = QInputDialog.getMultiLineText(
            self,
            "Thêm tài khoản Facebook hàng loạt (9 Trường)",
            (
                f"Nhập mỗi dòng định dạng:\nuid|pass|2fa|cookie|token|mailr|pass mail|mail recover|session_token\n"
                f"(Các trường phía sau có thể bỏ trống, ví dụ chỉ nhập uid|pass|2fa).\n"
                f"Đã chọn {len(serials)} máy; dữ liệu sẽ gán tuần tự theo thứ tự đang chọn."
            ),
        )
        if not ok or not text.strip():
            return

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if len(lines) < len(serials):
            QMessageBox.warning(
                self,
                "Thiếu dữ liệu",
                f"Bạn chọn {len(serials)} máy nhưng chỉ nhập {len(lines)} dòng tài khoản.",
            )
            return

        def work() -> None:
            for index, serial in enumerate(serials):
                parts = [part.strip() for part in lines[index].split("|")]
                if len(parts) < 2:
                    self.status_update_requested.emit(serial, "Sai định dạng uid|pass|...")
                    continue
                uid = parts[0]
                fb_pass = parts[1]
                fb_2fa = parts[2] if len(parts) >= 3 else ""
                cookie = parts[3] if len(parts) >= 4 else ""
                token = parts[4] if len(parts) >= 5 else ""
                mailr = parts[5] if len(parts) >= 6 else ""
                pass_mail = parts[6] if len(parts) >= 7 else ""
                mail_recover = parts[7] if len(parts) >= 8 else ""
                session_token = parts[8] if len(parts) >= 9 else ""

                if not uid or not fb_pass:
                    self.status_update_requested.emit(serial, "Thiếu UID hoặc mật khẩu")
                    continue

                self.profile_store.update(
                    serial,
                    fb_uid=uid,
                    fb_pass=fb_pass,
                    fb_2fa=fb_2fa,
                    cookie=cookie,
                    token=token,
                    mailr=mailr,
                    pass_mail=pass_mail,
                    mail_recover=mail_recover,
                    session_token=session_token,
                )
                self.status_update_requested.emit(serial, "Đã lưu tài khoản FB (9 trường)")

        def on_success(_: object = None) -> None:
            self._render_table()
            self.statusBar().showMessage("Đã lưu thông tin tài khoản Facebook hàng loạt (9 trường).", 5000)
            
            # Hỏi người dùng có muốn chạy Đăng nhập ngay lập tức không
            reply = QMessageBox.question(
                self,
                "Tự động Đăng nhập ngay",
                f"Đã gán thành công tài khoản cho {len(serials)} máy.\n\nBạn có muốn TỰ ĐỘNG ĐĂNG NHẬP ngay cho các máy này không?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self.login_fb_for_selected()

        self._start_task(work, on_success, busy_message="Đang lưu tài khoản Facebook vào hồ sơ máy...")

    def login_fb_for_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        def work() -> None:
            import queue
            import threading

            device_queue: queue.Queue[str] = queue.Queue()
            for serial in serials:
                device_queue.put(serial)

            def worker_thread() -> None:
                while not device_queue.empty():
                    if is_stop_requested():
                        break
                    try:
                        serial = device_queue.get_nowait()
                    except queue.Empty:
                        break

                    if is_stop_requested(serial):
                        self.status_update_requested.emit(serial, "⏹ Đã dừng tiến trình")
                        device_queue.task_done()
                        continue

                    profile = self.profile_store.get(serial)
                    uid = profile.fb_uid.strip()
                    fb_pass = profile.fb_pass.strip()
                    fb_2fa = profile.fb_2fa.strip()

                    if not uid or not fb_pass:
                        self.status_update_requested.emit(serial, "Thiếu uid/pass trong hồ sơ")
                        device_queue.task_done()
                        continue

                    try:
                        self.status_update_requested.emit(serial, "Đang login Facebook...")
                        login_facebook(serial, uid, fb_pass, fb_2fa)
                        self.status_update_requested.emit(serial, "Đã đăng nhập xong")
                    except Exception as exc:
                        self.status_update_requested.emit(serial, f"Lỗi login: {exc}")
                    finally:
                        device_queue.task_done()

            # Tạo 10 luồng độc lập riêng biệt chạy song song cho từng máy
            max_workers = min(len(serials), 10)
            threads = []
            for i in range(max_workers):
                t = threading.Thread(target=worker_thread, daemon=True)
                t.start()
                threads.append(t)

            for t in threads:
                t.join()

        self._start_task(work, busy_message="Đang login Facebook (10 luồng riêng biệt song song)...")

    def open_mail_tab(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        dialog = FarmConfigDialog(self, mode="mail")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            config = dialog.get_config()
            self._start_mail_read_news_task(serials, config)

    def open_farm_dialog(self) -> None:
        serials = self._require_selection()
        if not serials: return

        dialog = FarmConfigDialog(self, mode="farm")
        if dialog.exec() == QDialog.DialogCode.Accepted:
            config = dialog.get_config()
            self._start_farm_task(serials, config)

    @staticmethod
    def _random_config_value(config: dict, min_key: str, max_key: str, default_min: float, default_max: float, multiplier: float = 1.0) -> float:
        min_val = float(config.get(min_key, default_min))
        max_val = float(config.get(max_key, default_max))
        low, high = min(min_val, max_val), max(min_val, max_val)
        return random.uniform(low, high) * multiplier

    def _run_farm_newsfeed(self, serial: str, config: dict) -> None:
        total_seconds = self._random_config_value(config, "newsfeed_time_min", "newsfeed_time_max", 3, 5, 60)
        end_time = time.time() + total_seconds
        self.status_update_requested.emit(serial, f"Lướt Newsfeed {total_seconds / 60:.1f} phút")

        while time.time() < end_time:
            if is_stop_requested(serial):
                break
            swipe(serial, "up")
            interval = self._random_config_value(config, "newsfeed_interval_min", "newsfeed_interval_max", 5, 10)
            remaining = end_time - time.time()
            if remaining <= 0 or is_stop_requested(serial):
                break
            time.sleep(min(interval, remaining))
            if (
                config.get("like")
                and config.get("_like_remaining", 0) > 0
            ):
                if random.randint(1, 100) <= 30:
                    try:
                        liked = like_random_post_or_reel(serial, count=1)

                        if liked:
                            config["_like_remaining"] -= liked
                            self.status_update_requested.emit(
                                serial,
                                f"Like Newsfeed, còn {config['_like_remaining']} lần"
                            )

                    except Exception as exc:
                        print(f"[{serial}] Like Newsfeed lỗi: {exc}")
    
    def _run_farm_video_reels(self, serial: str, config: dict) -> None:
        total_seconds = self._random_config_value(config, "video_time_min", "video_time_max", 5, 10, 60)
        end_time = time.time() + total_seconds
        self.status_update_requested.emit(serial, "Đang mở tab Reels...")
        try:
            open_facebook_reels(serial)
        except Exception as exc:
            self.status_update_requested.emit(serial, f"Lỗi mở Reels: {exc}")
            return
        self.status_update_requested.emit(serial, f"Xem Video/Reels {total_seconds / 60:.1f} phút")

        while time.time() < end_time:
            if is_stop_requested(serial):
                break
            watch_seconds = self._random_config_value(config, "video_interval_min", "video_interval_max", 8, 25)
            remaining = end_time - time.time()
            if remaining <= 0 or is_stop_requested(serial):
                break
            self.status_update_requested.emit(serial, f"Đang xem Video/Reels {min(watch_seconds, remaining):.1f}s")
            time.sleep(min(watch_seconds, remaining))
            if (
                config.get("like")
                and config.get("_like_remaining", 0) > 0
            ):
                if random.randint(1, 100) <= 35:
                    try:
                        liked = like_random_post_or_reel(serial, count=1)

                        if liked:
                            config["_like_remaining"] -= liked
                            self.status_update_requested.emit(
                                serial,
                                f"Like Reels, còn {config['_like_remaining']} lần"
                            )

                    except Exception as exc:
                        print(f"[{serial}] Like Reels lỗi: {exc}")

            if time.time() >= end_time:
                break

            swipe(serial, "up")
            time.sleep(1.5)

    def _run_farm_random_like(self, serial: str, config: dict) -> None:
        min_count = int(config.get("like_count_min", 1))
        max_count = int(config.get("like_count_max", 3))
        count = random.randint(min(min_count, max_count), max(min_count, max_count))
        self.status_update_requested.emit(serial, f"Like dạo {count} lần")
        like_random_post_or_reel(serial, count=count)

        for index in range(count):
            tap(serial, 540, 800)
            time.sleep(0.1)
            tap(serial, 540, 800)
            if index < count - 1:
                time.sleep(random.uniform(1.5, 3.0))
                swipe(serial, "up")
                time.sleep(random.uniform(1.0, 2.0))
    def _share_post_to_profile(self, serial: str, config: dict) -> None:
        if not config.get("share_link"):
            self.status_update_requested.emit(serial, "Bỏ qua Chia sẻ bài viết: thiếu link")
            return

        self.status_update_requested.emit(serial, "Đang chia sẻ bài viết...")
        try:
            import uiautomator2 as ui2
            open_link(serial, config["share_link"])
            time.sleep(5)
            
            d = ui2.connect(serial)
            
            # Find and click Share button
            share_btn = d(description="Share")
            if share_btn.exists():
                share_btn.click()
                time.sleep(2)
                self.status_update_requested.emit(serial, "Đã bấm Share")
                
                # Find and click Share now button
                share_now_btn = d(description="Share now")
                if share_now_btn.exists():
                    share_now_btn.click()
                    time.sleep(1)
                    self.status_update_requested.emit(serial, "Đã chia sẻ bài viết")
                else:
                    self.status_update_requested.emit(serial, "Không tìm thấy nút Share now")
            else:
                self.status_update_requested.emit(serial, "Không tìm thấy nút Share")
        except Exception as exc:
            self.status_update_requested.emit(serial, f"Lỗi chia sẻ: {exc}")

    def _run_farm_seeding(self, serial: str, config: dict) -> None:
        if not config.get("seed_link"):
            self.status_update_requested.emit(serial, "Bỏ qua Seeding: thiếu link")
            return
        
        self.status_update_requested.emit(serial, "Đang Seeding...")
        open_link(serial, config["seed_link"])
        time.sleep(6)
        swipe(serial, "up")
        
        # Find and click like button
        try:
            import uiautomator2 as ui2
            d = ui2.connect(serial)
            like_btn = d(description="Like. Double tap and hold to react.")
            if like_btn.exists():
                like_btn.click()
                time.sleep(1)
                self.status_update_requested.emit(serial, "Đã like bài viết")
        except Exception as exc:
            self.status_update_requested.emit(serial, f"Lỗi like: {exc}")
        
        # Find and click comment button
        try:
            cmt_btn = d(description="Comment")
            if cmt_btn.exists():
                cmt_btn.click()
                time.sleep(1)
                self.status_update_requested.emit(serial, "Đã mở comment")
        except Exception as exc:
            self.status_update_requested.emit(serial, f"Lỗi mở comment: {exc}")
        
        # Input comment text
        if config.get("seed_cmt"):
            try:
                input_text(serial, config["seed_cmt"])
                time.sleep(3)
                d(description="Send").click()
                self.status_update_requested.emit(serial, "Đã bình luận")
            except Exception as exc:
                self.status_update_requested.emit(serial, f"Lỗi bình luận: {exc}")

    def _run_farm_follow_page(self, serial: str, config: dict) -> None:
        # Parse danh sách page URL từ config (nhiều dòng)
        raw_links = config.get("follow_links", "") or ""
        all_pages = [url.strip() for url in raw_links.splitlines() if url.strip()]

        if not all_pages:
            self.status_update_requested.emit(serial, "Bỏ qua Follow Page: chưa nhập link nào")
            return

        # Lọc page chưa follow dựa trên lịch sử
        remaining = self.follow_store.get_remaining_pages(serial, all_pages)

        if not remaining:
            done_count = len(self.follow_store.get_followed_pages(serial))
            self.status_update_requested.emit(
                serial, f"Đã follow hết {done_count}/{len(all_pages)} page!"
            )
            return

        # Chọn ngẫu nhiên số page theo cấu hình
        min_count = int(config.get("follow_count_min", 3))
        max_count = int(config.get("follow_count_max", 5))
        count = random.randint(min(min_count, max_count), max(min_count, max_count))
        count = min(count, len(remaining))
        targets = random.sample(remaining, count)

        delay_min = int(config.get("follow_delay_min", 15))
        delay_max = int(config.get("follow_delay_max", 45))

        self.status_update_requested.emit(
            serial,
            f"Follow Page: {count} page được chọn (còn lại {len(remaining)}/{len(all_pages)})"
        )

        success_count = 0
        for i, page_url in enumerate(targets):
            # Lấy tên ngắn của page từ URL để hiển thị log rõ ràng
            short_page = page_url.strip().rstrip("/").split("/")[-1] or page_url
            self.status_update_requested.emit(
                serial, f"[{i + 1}/{len(targets)}] Đang mở {short_page}..."
            )
            print(f"[{serial}] Đang follow page {i + 1}/{len(targets)}: {page_url}")
            try:
                result = follow_facebook_page(serial, page_url)
                if result in ("action_executed", "already_done", "followed", "liked", "followed+liked"):
                    self.follow_store.mark_followed(serial, page_url)
                    success_count += 1
                    self.status_update_requested.emit(
                        serial, f"✓ [{i + 1}/{len(targets)}] Đã follow: {short_page}"
                    )
                    print(f"[{serial}] ✓ Đã follow thành công: {page_url}")
                else:
                    self.status_update_requested.emit(
                        serial, f"✗ [{i + 1}/{len(targets)}] Thất bại: {short_page}"
                    )
                    print(f"[{serial}] ✗ Thất bại follow: {page_url} (kết quả: {result})")
            except Exception as exc:
                self.status_update_requested.emit(
                    serial, f"✗ [{i + 1}/{len(targets)}] Lỗi {short_page}: {exc}"
                )
                print(f"[{serial}] ✗ Lỗi follow {page_url}: {exc}")

            # Delay ngẫu nhiên giữa mỗi page (chống ban)
            if i < len(targets) - 1:
                delay = random.uniform(
                    min(delay_min, delay_max), max(delay_min, delay_max)
                )
                self.status_update_requested.emit(
                    serial, f"Chờ {delay:.0f}s trước page tiếp..."
                )
                time.sleep(delay)

        done_total = len(self.follow_store.get_followed_pages(serial))
        self.status_update_requested.emit(
            serial,
            f"Xong! Follow {success_count}/{len(targets)} page. Đã xong {done_total}/{len(all_pages)} page"
        )


    def _run_farm_join_group(self, serial: str, config: dict) -> None:
        if not config.get("join_link"):
            self.status_update_requested.emit(serial, "Bỏ qua Tham gia Nhóm: thiếu link")
            return

        self.status_update_requested.emit(serial, "Đang mở Group...")
        try:
            ok = join_facebook_group(serial, config["join_link"])
            self.status_update_requested.emit(serial, "Đã tham gia" if ok else "Không tham gia được")
        except Exception as exc:
            self.status_update_requested.emit(serial, f"Lỗi tham gia: {exc}")

    def _run_farm_friend_suggest(self, serial: str, config: dict) -> None:
        min_count = int(config.get("friend_suggest_count_min", 1))
        max_count = int(config.get("friend_suggest_count_max", 3))
        count = random.randint(min(min_count, max_count), max(min_count, max_count))
        self.status_update_requested.emit(serial, f"Gợi ý kết bạn {count} người: đang quét UI")
        suggest_add_friends(serial, count=count)
        self.status_update_requested.emit(serial, f"Gợi ý kết bạn {count} người: đã in UI dump")

    def _run_farm_friend_confirm(self, serial: str, config: dict) -> None:
        min_count = int(config.get("friend_confirm_count_min", 1))
        max_count = int(config.get("friend_confirm_count_max", 5))
        count = random.randint(min(min_count, max_count), max(min_count, max_count))
        self.status_update_requested.emit(serial, f"Xác nhận lời mời {count} người: đang quét UI")
        accept_friend_requests(serial, count=count)
        self.status_update_requested.emit(serial, f"Xác nhận lời mời {count} người: hoàn thành")

    def _run_farm_post_story(self, serial: str, config: dict) -> None:
        if not config.get("story_text"):
            self.status_update_requested.emit(serial, "Bỏ qua Đăng Story: thiếu nội dung")
            return

        self.status_update_requested.emit(serial, "Đang đăng Story...")
        try:
            input_text(serial, config["story_text"])
            time.sleep(1)
            keyevent(serial, 66)  # Press Enter to post
            self.status_update_requested.emit(serial, "Đã đăng Story xong")
        except Exception as exc:
            self.status_update_requested.emit(serial, f"Lỗi đăng Story: {exc}")

    def _run_farm_view_story(self, serial: str, config: dict) -> None:
        min_count = int(config.get("view_story_min", 5))
        max_count = int(config.get("view_story_max", 10))
        story_count = random.randint(min(min_count, max_count), max(min_count, max_count))
        
        self.status_update_requested.emit(serial, f"Xem {story_count} story")
        
        for i in range(story_count):
            watch_seconds = random.uniform(2.0, 4.0)
            time.sleep(watch_seconds)
            self.status_update_requested.emit(serial, f"Xem story {i+1}/{story_count}")
        
        self.status_update_requested.emit(serial, "Đã xem xong Story")

    def _run_farm_farm_story(self, serial: str, config: dict) -> None:
        count = int(config.get("farm_story_count", 5))
        self.status_update_requested.emit(serial, f"Đang tìm story chưa xem ({count} story)...")
        try:
            viewed_count = farm_story(serial, count=count)
            if viewed_count > 0:
                self.status_update_requested.emit(serial, f"Đã xem {viewed_count} story")
            else:
                self.status_update_requested.emit(serial, "Không tìm thấy story chưa xem")
        except Exception as exc:
            self.status_update_requested.emit(serial, f"Lỗi khi xem story: {exc}")

    def _start_mail_read_news_task(self, serials: list[str], config: dict) -> None:
        if not config.get("mail_read_news") and not config.get("mail_google_doc"):
            return

        def work() -> None:
            for serial in serials:
                try:
                    opened_google_doc = False
                    if config.get("mail_google_doc"):
                        google_doc_link = str(config.get("mail_google_doc_link", "")).strip()
                        if google_doc_link:
                            self.status_update_requested.emit(serial, "Đang mở Google Doc...")
                            open_link(serial, google_doc_link)
                            time.sleep(3)
                            opened_google_doc = True
                        else:
                            self.status_update_requested.emit(serial, "Bỏ qua Google Doc: thiếu link")

                    if config.get("mail_read_news"):
                        self.status_update_requested.emit(serial, "Đang mở Google...")
                        open_google_app(serial)
                        time.sleep(2)
                        self.status_update_requested.emit(serial, "Đang nhập tìm kiếm báo...")
                        _read_news_in_google(serial, "")
                        wait_minutes = random.uniform(
                            float(config.get("mail_read_time_min", 3)),
                            float(config.get("mail_read_time_max", 5)),
                        )
                        time.sleep(wait_minutes * 60)
                        self.status_update_requested.emit(serial, "Đã mở Google và đọc báo")
                    elif opened_google_doc:
                        self.status_update_requested.emit(serial, "Đã mở Google Doc")
                    else:
                        self.status_update_requested.emit(serial, "Bỏ qua tác vụ Mail: không có lựa chọn hợp lệ")
                except Exception as exc:
                    self.status_update_requested.emit(serial, f"Lỗi mở Google: {exc}")

        self._start_task(work, busy_message="Đang chạy tác vụ Mail: mở Google...")

    def _start_farm_task(self, serials: list[str], config: dict) -> None:
        selected_steps = [
            (config.get("view_story"), self._run_farm_view_story),
            (config.get("newsfeed"), self._run_farm_newsfeed),
            (config.get("video"), self._run_farm_video_reels),
            (config.get("seeding"), self._run_farm_seeding),
            (config.get("follow"), self._run_farm_follow_page),
            (config.get("join"), self._run_farm_join_group),
            (config.get("friend_suggest"), self._run_farm_friend_suggest),
            (config.get("friend_confirm"), self._run_farm_friend_confirm),
            (config.get("post_story"), self._run_farm_post_story),
            (config.get("view_story"), self._run_farm_view_story),
            (config.get("farm_story"), self._run_farm_farm_story),
            (config.get("share"), self._share_post_to_profile),
        ]

        def work():
            import concurrent.futures

            def process_farm(serial: str) -> None:
                try:
                    if is_stop_requested(serial):
                        self.status_update_requested.emit(serial, "⏹ Đã dừng tiến trình")
                        return
                    self.status_update_requested.emit(serial, "Đang đóng Facebook...")
                    close_facebook(serial)
                    time.sleep(1)

                    if is_stop_requested(serial):
                        self.status_update_requested.emit(serial, "⏹ Đã dừng tiến trình")
                        return

                    self.status_update_requested.emit(serial, "Đang mở Facebook...")
                    open_facebook(serial)
                    time.sleep(6)
                    local_config = dict(config)
                    if local_config.get("like"):
                        min_like = int(local_config.get("like_count_min", 1))
                        max_like = int(local_config.get("like_count_max", 3))
                        local_config["_like_remaining"] = random.randint(
                            min(min_like, max_like),
                            max(min_like, max_like)
                        )
                    else:
                        local_config["_like_remaining"] = 0
                    for enabled, handler in selected_steps:
                        if is_stop_requested(serial):
                            self.status_update_requested.emit(serial, "⏹ Đã dừng tiến trình")
                            return
                        if enabled:
                            handler(serial, local_config)
                            time.sleep(random.uniform(1.0, 2.5))

                    self.status_update_requested.emit(serial, "Đã chạy xong kịch bản")
                finally:
                    go_facebook_home(serial, max_try=5)

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(serials), 20)) as executor:
                list(executor.map(process_farm, serials))

        self._start_task(work, busy_message="Đang thực thi bộ máy Nuôi nick tự động hàng loạt...")

    def _prompt_delay(self) -> tuple[float, float] | None:
        text, ok = QInputDialog.getText(
            self, "Khoảng nghỉ ngẫu nhiên", "Nhập thời gian chờ ngẫu nhiên giữa mỗi máy và mỗi tác vụ (ví dụ: 1-3 giây):",
            text="1-3"
        )
        if not ok or not text.strip(): return None
        try:
            parts = text.split("-")
            min_val = float(parts[0])
            max_val = float(parts[1]) if len(parts) > 1 else min_val
            return min(min_val, max_val), max(min_val, max_val)
        except Exception:
            QMessageBox.warning(self, "Lỗi", "Vui lòng nhập định dạng số, ví dụ 1-3")
            return None

    def nav_open_fb(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        def work():
            import concurrent.futures

            def process_open(serial: str) -> None:
                if is_stop_requested():
                    return
                self.status_update_requested.emit(serial, "Đang mở Facebook...")
                open_facebook(serial)
                self.status_update_requested.emit(serial, "Đã mở Facebook")

            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                list(executor.map(process_open, serials))

        def on_success(_: object = None) -> None:
            self.statusBar().showMessage(f"Đã mở app Facebook cho {len(serials)} máy (20 luồng).", 5000)

        self._start_task(work, on_success, busy_message=f"Đang mở app Facebook 20 luồng cho {len(serials)} thiết bị...")

    def nav_open_link(self) -> None:
        serials = self._require_selection()
        if not serials: return
        
        url, ok = QInputDialog.getText(self, "Mở Link Facebook", "Dán Link Bài viết, Group, Page hoặc Video Reels cần tương tác/seeding:")
        if not ok or not url.strip(): return
        
        delay_range = self._prompt_delay()
        if not delay_range: return

        def work():
            for serial in serials:
                if is_stop_requested():
                    break
                self.status_update_requested.emit(serial, "Mở Link...")
                open_link(serial, url.strip())
                time.sleep(random.uniform(delay_range[0], delay_range[1]))
                self.status_update_requested.emit(serial, "")

        self._start_task(work, busy_message="Đang bung Link đồng loạt...")

    def nav_swipe_up(self) -> None:
        serials = self._require_selection()
        if not serials: return
        delay_range = self._prompt_delay()
        if not delay_range: return

        def work():
            for serial in serials:
                if is_stop_requested():
                    break
                self.status_update_requested.emit(serial, "Đang lướt feed...")
                swipe(serial, "up")
                time.sleep(random.uniform(delay_range[0], delay_range[1]))
                self.status_update_requested.emit(serial, "")

        self._start_task(work, busy_message="Đơn lệnh vuốt cuộn màn hình trên máy...")

    def nav_input_text(self) -> None:
        serials = self._require_selection()
        if not serials: return
        
        text, ok = QInputDialog.getMultiLineText(self, "Gõ nội dung (Comment/Seeding/Đăng bài)", "Dán văn bản cần gõ:\n(Bạn cần chạm trỏ chuột vào đúng ô nhập trên màn hình điều khiển trước khi thao tác này chạy)")
        if not ok or not text.strip(): return
        
        delay_range = self._prompt_delay()
        if not delay_range: return

        def work():
            for serial in serials:
                if is_stop_requested():
                    break
                self.status_update_requested.emit(serial, "Đang gõ chữ...")
                input_text(serial, text.strip())
                time.sleep(random.uniform(delay_range[0], delay_range[1]))
                self.status_update_requested.emit(serial, "")

        self._start_task(work, busy_message="Đang đẩy chữ vào bàn phím ảo đồng loạt...")

    def nav_clear_fb_data(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        reply = QMessageBox.question(
            self,
            "Xóa dữ liệu Facebook",
            f"Xóa dữ liệu ứng dụng Facebook trên {len(serials)} máy đã chọn? Hành động này sẽ đăng xuất và xóa session trên thiết bị.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        def work() -> None:
            for serial in serials:
                if is_stop_requested():
                    break
                try:
                    self.status_update_requested.emit(serial, "Đang xóa dữ liệu Facebook...")
                    clear_facebook_data(serial)
                    self.status_update_requested.emit(serial, "Đã xóa dữ liệu Facebook")
                except Exception as exc:
                    self.status_update_requested.emit(serial, f"Lỗi xóa dữ liệu: {exc}")

        self._start_task(work, busy_message="Đang xóa dữ liệu app Facebook trên máy đã chọn...")

    def nav_enable_adb_keyboard(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        def work() -> None:
            for serial in serials:
                try:
                    self.status_update_requested.emit(serial, "Đang kích hoạt Bàn phím ADB...")
                    ok = ensure_adb_keyboard(serial)
                    if ok:
                        self.status_update_requested.emit(serial, "✓ Đã bật Bàn phím ADB")
                    else:
                        self.status_update_requested.emit(serial, "✗ Không thấy ADB Keyboard")
                except Exception as exc:
                    self.status_update_requested.emit(serial, f"Lỗi kích hoạt bàn phím: {exc}")

        self._start_task(work, busy_message="Đang kích hoạt Bàn phím ADB hàng loạt...")

    def install_apk_for_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Chọn file APK cần cài đặt lên thiết bị",
            "",
            "APK Files (*.apk);;All Files (*)",
        )
        if not file_path:
            return

        file_name = os.path.basename(file_path)

        def work() -> None:
            import concurrent.futures

            def process_install(serial: str) -> None:
                self.status_update_requested.emit(serial, f"Đang cài đặt {file_name}...")
                try:
                    install_apk(serial, file_path)
                    self.status_update_requested.emit(serial, f"✓ Đã cài xong {file_name}")
                except Exception as exc:
                    self.status_update_requested.emit(serial, f"✗ Lỗi cài APK: {exc}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
                list(executor.map(process_install, serials))

        def on_success(_: object = None) -> None:
            self.statusBar().showMessage(f"Đã hoàn tất cài đặt {file_name} cho {len(serials)} máy (20 luồng).", 5000)

        self._start_task(work, on_success, busy_message=f"Đang cài đặt {file_name} cho {len(serials)} thiết bị...")

    def nav_close_all_apps(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        def work() -> None:
            import concurrent.futures

            def process_close_apps(serial: str) -> None:
                if is_stop_requested(serial):
                    return
                try:
                    self.status_update_requested.emit(serial, "Đang mở Đa nhiệm & Đóng tất cả app...")
                    ok = close_all_recent_apps(serial)
                    if ok:
                        self.status_update_requested.emit(serial, "✓ Đã đóng tất cả app")
                    else:
                        self.status_update_requested.emit(serial, "Đã mở đa nhiệm")
                except Exception as exc:
                    self.status_update_requested.emit(serial, f"Lỗi đóng app: {exc}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(serials), 20)) as executor:
                list(executor.map(process_close_apps, serials))

        def on_success(_: object = None) -> None:
            self.statusBar().showMessage(f"Đã đóng tất cả ứng dụng đang chạy trên {len(serials)} thiết bị.", 5000)

        self._start_task(work, on_success, busy_message=f"Đang đóng tất cả ứng dụng trên {len(serials)} thiết bị...")

    def push_photos_for_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self, "Chọn file ảnh cần nạp vào Thư viện điện thoại", "", "Image Files (*.jpg *.jpeg *.png *.webp);;All Files (*)"
        )
        if not file_path:
            return

        file_name = os.path.basename(file_path)

        def work() -> None:
            import concurrent.futures

            def process_push(serial: str) -> None:
                self.status_update_requested.emit(serial, f"Đang nạp ảnh {file_name}...")
                try:
                    push_image_to_device(serial, file_path)
                    self.status_update_requested.emit(serial, "✓ Đã nạp ảnh vào Thư viện")
                except Exception as exc:
                    self.status_update_requested.emit(serial, f"✗ Lỗi nạp ảnh: {exc}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(serials), 20)) as executor:
                list(executor.map(process_push, serials))

        def on_success(_: object = None) -> None:
            self.statusBar().showMessage(f"Đã nạp ảnh {file_name} vào Thư viện cho {len(serials)} máy.", 5000)

        self._start_task(work, on_success, busy_message=f"Đang đẩy ảnh vào Thư viện điện thoại cho {len(serials)} máy...")

    def update_avatar_bio_for_selected(self) -> None:
        serials = self._require_selection()
        if not serials:
            return

        avatars_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "avatars")
        os.makedirs(avatars_dir, exist_ok=True)

        exts = {".jpg", ".jpeg", ".png", ".webp"}
        image_files = [
            os.path.join(avatars_dir, f)
            for f in os.listdir(avatars_dir)
            if os.path.splitext(f)[1].lower() in exts
        ]

        # Nếu số lượng ảnh trong data/avatars ít hơn số máy, tự động tải thêm ảnh người thật từ Internet về data/avatars
        if len(image_files) < len(serials):
            needed = len(serials) - len(image_files)
            self.statusBar().showMessage(f"Đang tự động tải thêm {needed} ảnh người thật mới vào data/avatars...")
            downloaded = download_bulk_avatars(needed, output_dir=avatars_dir)
            image_files.extend(downloaded)

        if not image_files:
            QMessageBox.warning(self, "Lỗi", "Không tìm thấy file ảnh nào trong thư mục data/avatars!")
            return

        # Tráo ngẫu nhiên danh sách ảnh để mỗi máy nhận 1 ảnh ngẫu nhiên
        random_image_pool = list(image_files)
        random.shuffle(random_image_pool)

        def work() -> None:
            import concurrent.futures

            def process_check_update(item: tuple[int, str]) -> None:
                idx, serial = item
                img_path = random_image_pool[idx % len(random_image_pool)]
                self.status_update_requested.emit(serial, "Đang kiểm tra Avatar & Tiểu sử FB...")
                try:
                    res = update_fb_avatar_and_bio(
                        serial,
                        image_path=img_path,
                        progress_callback=lambda msg, s=serial: self.status_update_requested.emit(s, msg),
                    )
                    status_text = f"Avatar: {res['avatar']} | Bio: {res['bio']}"
                    self.status_update_requested.emit(serial, status_text)
                except Exception as exc:
                    self.status_update_requested.emit(serial, f"✗ Lỗi update: {exc}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(serials), 20)) as executor:
                list(executor.map(process_check_update, enumerate(serials)))

        def on_success(_: object = None) -> None:
            self.statusBar().showMessage(f"Đã hoàn tất kiểm tra & cập nhật Avatar/Tiểu sử cho {len(serials)} máy (20 luồng).", 6000)

        self._start_task(work, on_success, busy_message=f"Đang kiểm tra & cập nhật Avatar/Tiểu sử cho {len(serials)} máy...")

    def download_avatars_action(self) -> None:
        count, ok = QInputDialog.getInt(
            self,
            "Tải tự động ảnh Avatar từ Internet",
            "Nhập số lượng ảnh gương mặt người chân thực cần tải về (ví dụ: 20):",
            value=20,
            minValue=1,
            maxValue=500,
        )
        if not ok or count <= 0:
            return

        avatars_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "avatars")

        def work() -> list[str]:
            def progress(current: int, total: int, fname: str):
                self.statusBar().showMessage(f"Đang tải {current}/{total} ảnh Avatar: {fname}...")

            return download_bulk_avatars(count, output_dir=avatars_dir, progress_callback=progress)

        def on_success(downloaded: list[str]) -> None:
            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Tải ảnh Avatar hoàn tất")
            msg_box.setText(f"✓ Đã tải thành công {len(downloaded)} ảnh gương mặt chân thực vào thư mục:\n{avatars_dir}")
            btn_open = msg_box.addButton("Mở thư mục ảnh", QMessageBox.ButtonRole.ActionRole)
            btn_ok = msg_box.addButton("Đóng", QMessageBox.ButtonRole.AcceptRole)
            msg_box.exec()

            if msg_box.clickedButton() == btn_open:
                try:
                    os.startfile(avatars_dir)
                except Exception:
                    pass

        self._start_task(work, on_success, busy_message=f"Đang tải tự động {count} ảnh Avatar người chân thực từ Internet...")

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        QTimer.singleShot(0, self._reflow_screen_grid)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.clear_screen_wall()
        if hasattr(self, "screen_window") and self.screen_window:
            self.screen_window.close_permanently()
        super().closeEvent(event)


def run_app() -> None:
    app = QApplication([])
    window = MainWindow()
    window.showMaximized()
    app.exec()

