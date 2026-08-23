import sys

with open('src/app.py', 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Update ScrcpyInputHook
old_hook_init = '''        self.is_sync_enabled = False
        self.master_serial = ""
        self._drag_start = None
        self._executor = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=20)'''
new_hook_init = '''        self.is_sync_enabled = False
        self.master_serial = ""
        self._drag_start = None
        self._executor = __import__("concurrent.futures").futures.ThreadPoolExecutor(max_workers=20)
        self.sync_allowed_serials = set()'''
content = content.replace(old_hook_init, new_hook_init)

old_get_active = '''    def _get_active_slaves(self, exclude: str = "") -> list[str]:
        return [s for s in self.hwnd_to_serial.values() if s != exclude]'''
new_get_active = '''    def _get_active_slaves(self, exclude: str = "") -> list[str]:
        return [s for s in self.hwnd_to_serial.values() if s != exclude and s in self.sync_allowed_serials]

    def set_sync_allowed(self, serial: str, allowed: bool) -> None:
        if allowed:
            self.sync_allowed_serials.add(serial)
        else:
            self.sync_allowed_serials.discard(serial)'''
content = content.replace(old_get_active, new_get_active)

# 2. Update ScreenSlot dataclass
old_dataclass = '''class ScreenSlot:
    serial: str
    root: QFrame
    host: QFrame
    title: QLabel
    status: QLabel
    refresh_btn: QPushButton
    close_btn: QPushButton
    base_w: int
    base_h: int'''
new_dataclass = '''class ScreenSlot:
    serial: str
    root: QFrame
    host: QFrame
    title: QLabel
    status: QLabel
    refresh_btn: QPushButton
    close_btn: QPushButton
    sync_chk: __import__("PySide6.QtWidgets").QtWidgets.QCheckBox
    base_w: int
    base_h: int'''
content = content.replace(old_dataclass, new_dataclass)

# 3. Update _create_screen_slot
old_create_header = '''        refresh_btn = QPushButton("R")
        refresh_btn.setFixedSize(26, 24)
        refresh_btn.setCursor(Qt.PointingHandCursor)
        refresh_btn.setToolTip("Mở lại máy này")
        refresh_btn.clicked.connect(lambda: self._relaunch_screen(serial, force=True))

        close_btn = QPushButton("X")
        close_btn.setFixedSize(26, 24)
        close_btn.setCursor(Qt.PointingHandCursor)
        close_btn.clicked.connect(lambda: self.close_screen_slot(serial))

        header.addWidget(title, 1)
        header.addWidget(refresh_btn, 0)
        header.addWidget(close_btn, 0)'''
new_create_header = '''        sync_chk = QCheckBox("Sync")
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
        header.addWidget(close_btn, 0)'''
content = content.replace(old_create_header, new_create_header)

old_create_return = '''        slot = ScreenSlot(
            serial=serial,
            root=root,
            host=host,
            title=title,
            status=status,
            refresh_btn=refresh_btn,
            close_btn=close_btn,
            base_w=base_w,
            base_h=base_h,
        )'''
new_create_return = '''        slot = ScreenSlot(
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
        )'''
content = content.replace(old_create_return, new_create_return)

with open('src/app.py', 'w', encoding='utf-8') as f:
    f.write(content)

print("Patch 2 applied.")
