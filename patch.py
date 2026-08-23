import sys
import re

with open('src/app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Replace ScrcpyKeyboardHook with ScrcpyInputHook
hook_code = '''class ScrcpyInputHook:
    """Win32 low-level hooks: captures keystrokes and mouse clicks when Screen Wall is
    the foreground window and forwards them to the active device via ADB."""

    WH_KEYBOARD_LL = 13
    WH_MOUSE_LL = 14
    WM_KEYDOWN = 0x0100
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
        self._executor = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=20)

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
        return [s for s in self.hwnd_to_serial.values() if s != exclude]

    def _ll_mouse_proc(self, nCode, wParam, lParam):
        if nCode == self.HC_ACTION and (wParam == self.WM_LBUTTONDOWN or wParam == self.WM_LBUTTONUP):
            if lParam:
                pt = lParam.contents.pt
                curr_hwnd = USER32.WindowFromPoint(pt)
                found_serial = None
                
                check_hwnd = curr_hwnd
                while check_hwnd:
                    serial = self.hwnd_to_serial.get(check_hwnd)
                    if serial:
                        found_serial = serial
                        self._cached_serial = serial
                        break
                    check_hwnd = USER32.GetParent(check_hwnd)
                
                # Handling Sync
                if self.is_sync_enabled and found_serial and found_serial == self.master_serial:
                    # Get client rect
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

    def _ll_keyboard_proc(self, nCode, wParam, lParam):
        if nCode == self.HC_ACTION and wParam == self.WM_KEYDOWN:
            serial = self._cached_serial
            if serial and self._is_wall_foreground():
                raw = ctypes.cast(lParam, ctypes.POINTER(ctypes.c_uint32))
                vk = raw[0]
                
                targets = [serial]
                if self.is_sync_enabled and serial == self.master_serial:
                    targets.extend(self._get_active_slaves(exclude=serial))
                    
                for t in set(targets):
                    self._dispatch_key(t, vk)
                return 1
        return USER32.CallNextHookEx(self._kb_hook, nCode, wParam, lParam)

    def _is_wall_foreground(self) -> bool:
        fg = USER32.GetForegroundWindow()
        if not fg:
            return False
        if fg == self.wall_hwnd:
            return True
        parent = USER32.GetAncestor(fg, 2)
        return parent == self.wall_hwnd

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
            ch = "|" if (USER32.GetAsyncKeyState(0x10) & 0x8000) else "\\\\"
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif vk == 0xDD:
            ch = "}" if (USER32.GetAsyncKeyState(0x10) & 0x8000) else "]"
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()
        elif 0x60 <= vk <= 0x69:
            ch = chr(vk - 0x60 + 0x30)
            threading.Thread(target=lambda: input_text(serial, ch), daemon=True).start()'''

old_hook_regex = re.compile(r'class ScrcpyKeyboardHook:.*?# Shift keys for digits.*?\n.*?\n', re.DOTALL)
content = old_hook_regex.sub(hook_code + '\n\n', content)

# 2. Rename ScrcpyKeyboardHook usage in app.py
content = content.replace('ScrcpyKeyboardHook()', 'ScrcpyInputHook()')

# 3. Add methods to ScreenWallWindow
screen_wall_methods = '''    def closeEvent(self, event) -> None:  # noqa: N802
        if self._is_closing:
            super().closeEvent(event)
        else:
            event.ignore()
            self.hide()

    def _on_sync_toggled(self, checked: bool) -> None:
        self._keyboard_hook.set_sync_enabled(checked)

    def _on_master_changed(self, index: int) -> None:
        if index >= 0:
            self._keyboard_hook.set_sync_master(self.combo_master.itemText(index))

    def update_master_list(self, serials: list[str]) -> None:
        current = self.combo_master.currentText()
        self.combo_master.blockSignals(True)
        self.combo_master.clear()
        self.combo_master.addItems(serials)
        if current in serials:
            self.combo_master.setCurrentText(current)
        elif serials:
            self.combo_master.setCurrentIndex(0)
            self._on_master_changed(0)
        self.combo_master.blockSignals(False)
'''
content = content.replace('''    def closeEvent(self, event) -> None:  # noqa: N802
        if self._is_closing:
            super().closeEvent(event)
        else:
            event.ignore()
            self.hide()''', screen_wall_methods)

# 4. Update MainWindow to call update_master_list
open_screens_hook = '''        self._update_action_state(True)
        self.screen_window.update_master_list(list(self.active_screens.keys()))'''
content = content.replace('        self._update_action_state(True)', open_screens_hook)

close_screen_hook = '''        self.screen_window.screen_grid.removeWidget(slot.root)
        self.screen_window.update_master_list(list(self.active_screens.keys()))'''
content = content.replace('        self.screen_window.screen_grid.removeWidget(slot.root)', close_screen_hook)


with open('src/app.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Patch applied.")
