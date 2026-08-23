from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import time
import tempfile
import random
import threading
import unicodedata
import xml.etree.ElementTree as ET
import uiautomator2 as ui2
import re
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from .models import DeviceInfo


def print(*args, **kwargs):
    msg = " ".join(map(str, args))
    end = kwargs.get("end", "\n")
    try:
        sys.stdout.buffer.write((msg + end).encode("utf-8", errors="ignore"))
        sys.stdout.flush()
    except Exception:
        pass


class AdbError(RuntimeError):
    pass


_STOPPED_SERIALS: set[str] = set()
_GLOBAL_STOP_ALL = False
_STOP_LOCK = threading.Lock()


def request_stop_serials(serials: list[str]) -> None:
    with _STOP_LOCK:
        _STOPPED_SERIALS.update(serials)


def request_stop_all() -> None:
    global _GLOBAL_STOP_ALL
    _GLOBAL_STOP_ALL = True


def reset_stop_event(serials: list[str] | None = None) -> None:
    global _GLOBAL_STOP_ALL
    with _STOP_LOCK:
        if serials is None:
            _STOPPED_SERIALS.clear()
            _GLOBAL_STOP_ALL = False
        else:
            for s in serials:
                _STOPPED_SERIALS.discard(s)


def is_stop_requested(serial: str | None = None) -> bool:
    if _GLOBAL_STOP_ALL:
        return True
    if serial is not None:
        with _STOP_LOCK:
            return serial in _STOPPED_SERIALS
    return False


class FbInfoStatus(RuntimeError):
    def __init__(self, status: str) -> None:
        super().__init__(status)
        self.status = status


if getattr(sys, "frozen", False):
    ROOT_DIR = Path(sys.executable).resolve().parent
    MEI_DIR = Path(getattr(sys, "_MEIPASS", ROOT_DIR))
else:
    ROOT_DIR = Path(__file__).resolve().parents[1]
    MEI_DIR = ROOT_DIR

SCRCPY_DIR = ROOT_DIR / "tools" / "scrcpy"
if not SCRCPY_DIR.exists() and (MEI_DIR / "tools" / "scrcpy").exists():
    SCRCPY_DIR = MEI_DIR / "tools" / "scrcpy"

# Tự động nạp thư mục platform-tools chứa adb.exe vào PATH
for _pdir in (ROOT_DIR / "tools" / "platform-tools", MEI_DIR / "tools" / "platform-tools"):
    if _pdir.exists() and str(_pdir) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{_pdir};{os.environ.get('PATH', '')}"


def _tool_path(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise AdbError(f"Không tìm thấy '{name}' trong PATH.")
    return path


def _common_scrcpy_paths() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LocalAppData"):
        env_value = os.environ.get(env_name)
        if not env_value:
            continue
        base = Path(env_value)
        candidates.extend(
            [
                base / "scrcpy" / "scrcpy.exe",
                base / "scrcpy" / "scrcpy-win64-v2.5" / "scrcpy.exe",
                base / "scrcpy.exe",
            ]
        )
    return candidates


def _sync_scrcpy_adb(scrcpy_file: Path) -> None:
    """Synchronize system ADB binaries into scrcpy folder to prevent version conflicts."""
    sys_adb = shutil.which("adb") or shutil.which("adb.exe")
    if not sys_adb:
        return
    sys_adb_path = Path(sys_adb).resolve()
    scrcpy_dir = scrcpy_file.parent.resolve()
    sys_dir = sys_adb_path.parent
    if scrcpy_dir == sys_dir:
        return

    for name in ("adb.exe", "AdbWinApi.dll", "AdbWinUsbApi.dll"):
        src = sys_dir / name
        dst = scrcpy_dir / name
        if src.exists():
            try:
                if not dst.exists() or dst.stat().st_size != src.stat().st_size:
                    shutil.copy2(src, dst)
            except Exception:
                pass


def resolve_scrcpy(explicit_path: str = "") -> str | None:
    res_path: Path | None = None
    if explicit_path:
        path = Path(explicit_path).expanduser()
        if path.is_dir():
            path = path / "scrcpy.exe"
        if path.exists():
            res_path = path

    if not res_path:
        local_scrcpy = SCRCPY_DIR / "scrcpy.exe"
        if local_scrcpy.exists():
            res_path = local_scrcpy

    if not res_path and SCRCPY_DIR.exists():
        for candidate in SCRCPY_DIR.rglob("scrcpy.exe"):
            res_path = candidate
            break

    if not res_path:
        env_path = os.environ.get("SCRCPY_PATH", "").strip()
        if env_path:
            path = Path(env_path).expanduser()
            if path.is_dir():
                path = path / "scrcpy.exe"
            if path.exists():
                res_path = path

    if not res_path:
        which_path = shutil.which("scrcpy") or shutil.which("scrcpy.exe")
        if which_path:
            res_path = Path(which_path)

    if not res_path:
        for candidate in _common_scrcpy_paths():
            if candidate.exists():
                res_path = candidate
                break

    if res_path:
        _sync_scrcpy_adb(res_path)
        return str(res_path)
    return None


def _run(command: list[str], timeout: int = 30, check: bool = True, _retried: bool = False) -> str:
    try:
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except subprocess.TimeoutExpired as exc:
        if not check:
            output = exc.stdout or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            return output.strip()
        raise AdbError(f"Lệnh ADB quá thời gian chờ sau {timeout} giây: {' '.join(command)}") from exc

    if check and completed.returncode != 0:
        stderr = completed.stderr.strip() or completed.stdout.strip()
        # Tự động khởi động lại ADB Server nếu gặp lỗi đơ server / protocol fault / connection reset
        if not _retried and any(k in stderr.lower() for k in ("protocol fault", "connection reset", "cannot connect to daemon", "server version", "failed to check server")):
            try:
                adb_bin = _tool_path("adb")
                subprocess.run([adb_bin, "kill-server"], capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                time.sleep(0.5)
                subprocess.run([adb_bin, "start-server"], capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                time.sleep(0.5)
                return _run(command, timeout=timeout, check=check, _retried=True)
            except Exception:
                pass
        raise AdbError(stderr or "Lệnh ADB thất bại.")
    return completed.stdout.strip()


def _adb(*args: str, timeout: int = 30, check: bool = True) -> str:
    return _run([_tool_path("adb"), *args], timeout=timeout, check=check)


def adb_shell(serial: str, *args: str, timeout: int = 30, check: bool = True) -> str:
    return _adb("-s", serial, "shell", *args, timeout=timeout, check=check)


def _safe_adb_shell(serial: str, *args: str, timeout: int = 10) -> bool:
    try:
        adb_shell(serial, *args, check=False, timeout=timeout)
        return True
    except Exception as exc:
        print(f"[{serial}] Bo qua lenh ADB phu bi loi: {' '.join(args)} ({exc})")
        return False


def _is_blank_fb_value(value: str) -> bool:
    return (value or "").strip() in {"", "Trống", "Trống", "Trống"}


def get_screen_size(serial: str) -> tuple[int, int]:
    try:
        output = adb_shell(serial, "wm", "size", timeout=10)
    except Exception:
        return 0, 0

    override_match = re.search(r"Override size:\s*(\d+)x(\d+)", output)
    if override_match:
        return int(override_match.group(1)), int(override_match.group(2))

    physical_match = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
    if physical_match:
        return int(physical_match.group(1)), int(physical_match.group(2))

    generic_match = re.search(r"(\d+)x(\d+)", output)
    if generic_match:
        return int(generic_match.group(1)), int(generic_match.group(2))

    return 0, 0


def list_devices() -> list[DeviceInfo]:
    output = ""
    try:
        output = _adb("devices", "-l", timeout=15)
    except Exception:
        try:
            adb_bin = _tool_path("adb")
            subprocess.run([adb_bin, "start-server"], capture_output=True, timeout=5, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            time.sleep(0.8)
            output = _adb("devices", "-l", timeout=15)
        except Exception:
            return []

    if "offline" in output:
        try:
            _adb("reconnect", "offline", timeout=3)
        except Exception:
            pass

    devices: list[DeviceInfo] = []
    for line in output.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 2:
            continue

        serial = parts[0]
        state = parts[1]
        model = ""
        android_version = ""
        for token in parts[2:]:
            if token.startswith("model:"):
                model = token.split(":", 1)[1].replace("_", " ")

        if state == "device":
            try:
                model = model or adb_shell(serial, "getprop", "ro.product.model", timeout=10)
                android_version = adb_shell(serial, "getprop", "ro.build.version.release", timeout=10)
            except AdbError:
                pass

        devices.append(
            DeviceInfo(
                serial=serial,
                state=state,
                model=model,
                android_version=android_version,
            )
        )
    return devices


def launch_scrcpy(
    serial: str,
    scrcpy_path: str = "",
    quality: str = "balanced",
    window_title: str | None = None,
) -> tuple[subprocess.Popen | None, int, int]:
    # Automatically switch device to ADB Keyboard
    try:
        ensure_adb_keyboard(serial)
    except Exception:
        pass

    scrcpy = resolve_scrcpy(scrcpy_path)
    if not scrcpy:
        raise AdbError(
            "Không tìm thấy scrcpy. Hãy chạy bootstrap.bat để tải scrcpy tự động hoặc cài scrcpy rồi thêm vào PATH."
        )

    window_args: list[str] = []
    try:
        out = _adb("-s", serial, "shell", "wm", "size", timeout=5)
        m = re.search(r"(\d+)x(\d+)", out)
        if m:
            width, height = int(m.group(1)), int(m.group(2))
            max_dim = 1080
            scale = min(1.0, max_dim / max(width, height)) if max(width, height) > 0 else 1.0
            win_w = max(1, int(width * scale))
            win_h = max(1, int(height * scale))
            window_args = ["--render-fit=letterbox", "--window-width", str(win_w), "--window-height", str(win_h)]
    except Exception:
        window_args = ["--render-fit=letterbox"]

    env = None
    adb_path = shutil.which("adb") or shutil.which("adb.exe")
    if adb_path:
        adb_dir = str(Path(adb_path).parent)
        env = os.environ.copy()
        env["PATH"] = adb_dir + os.pathsep + env.get("PATH", "")
        env["ADB"] = adb_path

    if quality == "low":
        # Mở nhiều máy: Nét vừa phải, nhưng vẫn giữ 30 fps theo yêu cầu
        perf_args = [
            "--no-audio", "--max-fps", "30", "--video-bit-rate", "4M", "--max-size", "720"
        ]
    else:
        # Mở ít máy: Nét hơn, fps 30
        perf_args = [
            "--no-audio", "--max-fps", "30", "--video-bit-rate", "5M", "--max-size", "960"
        ]
    control_args = [
        "--keyboard=sdk",
        "--prefer-text",
        "--shortcut-mod=lctrl,rctrl,lalt,lsuper",
        "--keep-active",
        "--stay-awake",
    ]
    title = window_title or f"scrcpy_{serial}"
    args = [scrcpy, "-s", serial, *window_args, *perf_args, *control_args, "--window-title", title]
    proc = subprocess.Popen(args, env=env)

    if window_args and "--window-width" in window_args:
        try:
            idx_w = window_args.index("--window-width") + 1
            idx_h = window_args.index("--window-height") + 1
            win_w = int(window_args[idx_w])
            win_h = int(window_args[idx_h])
        except Exception:
            win_w, win_h = 0, 0
    else:
        win_w, win_h = 0, 0
    return proc, win_w, win_h


def set_wifi(serial: str, enabled: bool) -> None:
    adb_shell(serial, "svc", "wifi", "enable" if enabled else "disable", timeout=20)


def connect_wifi(serial: str, ssid: str, password: str) -> bool:
    set_wifi(serial, True)
    time.sleep(2)
    e_ssid = ssid.replace("'", "'\\''")
    e_pass = password.replace("'", "'\\''")
    
    # Sử dụng adbjoinwifi theo yêu cầu (Cần cài sẵn app com.steinwurf.adbjoinwifi trên máy)
    adb_shell(
        serial, 
        "am", "start", "-n", "com.steinwurf.adbjoinwifi/.MainActivity", 
        "-e", "ssid", f"'{e_ssid}'", 
        "-e", "password_type", "WPA", 
        "-e", "password", f"'{e_pass}'", 
        timeout=30,
        check=False
    )
    
    time.sleep(3)
    try:
        ping = adb_shell(serial, "ping", "-c", "1", "-W", "3", "8.8.8.8", timeout=10)
        return "1 received" in ping or "1 packets received" in ping
    except Exception:
        return False


def disable_auto_rotate(serial: str) -> None:
    """Tắt auto-rotate màn hình trên thiết bị Android."""
    try:
        adb_shell(serial, "settings", "put", "system", "accelerometer_rotation", "0", check=False, timeout=5)
        adb_shell(serial, "settings", "put", "system", "user_rotation", "0", check=False, timeout=5)
    except Exception:
        pass


def open_facebook(serial: str) -> None:
    # 1. Bật sáng màn hình
    _safe_adb_shell(serial, "input", "keyevent", "224", timeout=4)

    package = "com.facebook.katana"
    try:
        package = _find_launchable_package(serial, ("com.facebook.katana", "com.facebook.lite", "katana", "facebook"))
    except Exception:
        package = "com.facebook.katana"

    if not package:
        package = "com.facebook.katana"

    # 2. Phương pháp 1: Mở bằng Deep link chuẩn của Facebook (Mở trực tiếp News Feed)
    adb_shell(serial, "am", "start", "-a", "android.intent.action.VIEW", "-d", "fb://feed", "-p", package, "-f", "0x10200000", check=False, timeout=5)

    # 3. Phương pháp 2: Mở bằng Launcher Component đã resolve
    try:
        component = _resolve_launcher_component(serial, package)
        if component and "/" in component:
            adb_shell(serial, "am", "start", "-n", component, "-f", "0x10200000", check=False, timeout=5)
        else:
            adb_shell(serial, "am", "start", "-a", "android.intent.action.MAIN", "-c", "android.intent.category.LAUNCHER", "-p", package, "-f", "0x10200000", check=False, timeout=5)
    except Exception:
        pass

    # 4. Phương pháp 3: Khởi chạy bằng uiautomator2
    try:
        import uiautomator2 as u2
        d = u2.connect(serial)
        d.app_start(package, stop=False)
    except Exception:
        pass

    # 5. Kiểm tra và bù lệnh nếu màn hình chưa lên Facebook
    time.sleep(1.0)
    try:
        top = adb_shell(serial, "dumpsys", "window", "windows", check=False, timeout=4).lower()
        if package not in top and "com.facebook" not in top:
            adb_shell(serial, "am", "start", "-a", "android.intent.action.VIEW", "-d", "fb://facewebmodal/f?href=https://m.facebook.com", "-p", package, "-f", "0x10200000", check=False, timeout=5)
            try:
                import uiautomator2 as u2
                d = u2.connect(serial)
                d.app_start(package, stop=False)
            except Exception:
                pass
    except Exception:
        pass

    disable_auto_rotate(serial)


def open_google_app(serial: str) -> None:
    _safe_adb_shell(serial, "input", "keyevent", "224", timeout=10)
    adb_shell(
        serial,
        "am",
        "start",
        "-n",
        "com.google.android.googlequicksearchbox/.SearchActivity",
        check=False,
        timeout=10,
    )
    adb_shell(
        serial,
        "monkey",
        "-p",
        "com.google.android.googlequicksearchbox",
        "-c",
        "android.intent.category.LAUNCHER",
        "1",
        check=False,
        timeout=10,
    )
    time.sleep(1)
    disable_auto_rotate(serial)


def use_google_doc(serial: str) -> None:
    """Mở app Google, tìm ô search bằng XPath và nhập 'google doc'."""
    open_google_app(serial)
    time.sleep(2)

    try:
        d = ui2.connect(serial)
        search_box = d.xpath('//*[@resource-id="googleapp_facade_search_box"]')
        if not search_box.exists(timeout=5.0):
            raise AdbError('Không tìm thấy ô tìm kiếm Google.')

        search_box.click()
        time.sleep(1)

        edit_text = d.xpath('//android.widget.EditText')
        if not edit_text.exists(timeout=5.0):
            raise AdbError('Không tìm thấy ô nhập tìm kiếm Google.')

        edit_text.set_text('google doc')
        d.press('enter')
        time.sleep(2)
    except Exception as exc:
        raise AdbError(f'Không thể dùng Google Doc trên {serial}: {exc}') from exc


def close_facebook(serial: str) -> None:
    adb_shell(serial, "am", "force-stop", "com.facebook.katana", check=False, timeout=10)


def close_all_recent_apps(serial: str) -> bool:
    """
    Bấm nút Đa nhiệm (Recent Apps / App Switch - Keycode 187),
    sau đó tìm và bấm nút 'Close all' (Đóng tất cả) theo resource-id:
    com.sec.android.app.launcher:id/clear_all_button (Samsung)
    hoặc fallback uiautomator2 / XML text.
    """
    # 1. Bật sáng màn hình và mở giao diện Đa nhiệm (KEYCODE_APP_SWITCH = 187)
    _safe_adb_shell(serial, "input", "keyevent", "224", timeout=5)
    adb_shell(serial, "input", "keyevent", "187", check=False, timeout=5)
    time.sleep(1.2)

    # 2. Bấm qua uiautomator2 theo đúng resource-id: com.sec.android.app.launcher:id/clear_all_button
    try:
        import uiautomator2 as u2
        d = u2.connect(serial)
        btn = d(resourceId="com.sec.android.app.launcher:id/clear_all_button")
        if not btn.exists:
            btn = d(resourceIdMatches=r".*clear_all_button.*")
        if not btn.exists:
            btn = d(textMatches=r"(?i)(close all|đóng tất cả|xóa tất cả|clear all)")
        if btn.exists(timeout=2.0):
            btn.click()
            print(f"[{serial}] ✓ Đã bấm nút 'Close all' qua uiautomator2 (resourceId: com.sec.android.app.launcher:id/clear_all_button)")
            time.sleep(1.0)
            return True
    except Exception:
        pass

    # 3. Quét XML dump để tìm node có resource-id="com.sec.android.app.launcher:id/clear_all_button"
    xml_text = get_xml_dump(serial) or ""
    if xml_text:
        try:
            root = ET.fromstring(xml_text)
            for node in root.iter("node"):
                res_id = (node.attrib.get("resource-id") or "").strip()
                text = (node.attrib.get("text") or "").strip().lower()
                desc = (node.attrib.get("content-desc") or "").strip().lower()
                
                if res_id == "com.sec.android.app.launcher:id/clear_all_button" or "clear_all_button" in res_id or \
                   text in ("close all", "đóng tất cả", "clear all", "xóa tất cả") or \
                   desc in ("close all", "đóng tất cả", "clear all", "xóa tất cả"):
                    if tap_node(serial, node):
                        print(f"[{serial}] ✓ Đã bấm nút 'Close all' qua XML (resource-id/text: {res_id or text or desc})")
                        time.sleep(1.0)
                        return True
        except Exception:
            pass

    # 4. Fallback tọa độ trung tâm phía dưới màn hình trên Samsung nếu không bắt được node
    width, height = get_screen_size(serial)
    if width and height:
        tap(serial, int(width * 0.5), int(height * 0.85))
        time.sleep(0.8)
        adb_shell(serial, "input", "keyevent", "3", check=False, timeout=5)
        print(f"[{serial}] ✓ Đã bấm tọa độ dự phòng nút Close all ({int(width * 0.5)}, {int(height * 0.85)})")
        return True

    return False


def clear_facebook_data(serial: str) -> None:
    packages = [
        "com.facebook.katana",
        "com.facebook.orca",
        "com.facebook.services",
        "com.facebook.system",
        "com.facebook.appmanager",
    ]
    for pkg in packages:
        adb_shell(serial, "am", "force-stop", pkg, check=False, timeout=10)
    time.sleep(0.5)
    for pkg in packages:
        adb_shell(serial, "pm", "clear", pkg, check=False, timeout=30)


def get_xml_dump(serial: str) -> str:
    """Đổ cấu trúc giao diện UI XML của màn hình hiện tại."""
    adb_shell(serial, "uiautomator", "dump", "/sdcard/window_dump.xml", check=False, timeout=15)
    time.sleep(0.8)
    return adb_shell(serial, "cat", "/sdcard/window_dump.xml", check=False, timeout=10)


def debug_print_ui_dump(serial: str, title: str = "UI Dump", max_nodes: int | None = 300, include_empty: bool = False) -> None:
    try:
        xml_text = get_xml_dump(serial)
        root = ET.fromstring(xml_text)
    except Exception as exc:
        print(f"[{serial}] [{title}] UI dump error: {exc}")
        return

    print(f"[{serial}] ===== {title} =====")
    printed = 0
    total = 0
    for index, node in enumerate(root.iter("node"), start=1):
        total += 1
        text = (node.attrib.get("text") or "").strip()
        desc = (node.attrib.get("content-desc") or "").strip()
        resource_id = (node.attrib.get("resource-id") or "").strip()
        class_name = (node.attrib.get("class") or "").strip()
        bounds = (node.attrib.get("bounds") or "").strip()
        clickable = (node.attrib.get("clickable") or "").strip()
        enabled = (node.attrib.get("enabled") or "").strip()

        if not include_empty and not any((text, desc, resource_id)):
            continue

        printed += 1
        print(
            f"[{serial}] UI#{index} "
            f"text='{text}' desc='{desc}' id='{resource_id}' "
            f"class='{class_name}' bounds='{bounds}' clickable={clickable} enabled={enabled}"
        )
        if max_nodes is not None and printed >= max_nodes:
            print(f"[{serial}] [{title}] Reached max_nodes={max_nodes}, stop printing.")
            break

    print(f"[{serial}] ===== End {title}: printed={printed}, total_nodes={total} =====")

def _look_like_empty_suggest_friends_screen(serial: str) -> bool:
    probes = (
        "No new requests",
        "upload contacts",
        "try uploading your phone contacts",
        "can find your friends on facebook."
    )

    def _match(text: str) -> bool:
        lower = text.lower()
        hits = sum(1 for token in probes if token in lower)
        return hits >= 2 or "try uploading your phone contacts" in lower

    xml_text = ""
    try:
        xml_text = get_xml_dump(serial)
    except Exception:
        xml_text = ""
    if xml_text and _match(xml_text):
        return True

    try:
        ocr_text = _read_profile_ocr_text(serial)
    except Exception:
        ocr_text = ""
    return bool(ocr_text and _match(ocr_text))

def add_friends(
    serial: str,
    count_default: int,
    max_swipes: int = 10,
    click_delay: float = 2.0,
) -> int:

    d = ui2.connect(serial)

    clicked = 0
    swipes = 0

    add_friend_patterns = [
        re.compile(r"^add friend$", re.I),
        re.compile(r"^add .+ as a friend$", re.I),
    ]

    while clicked < count_default and swipes < max_swipes:

        found = False

        try:
            xml_text = get_xml_dump(serial)
            root = ET.fromstring(xml_text)
        except Exception as exc:
            print(f"[{serial}] Dump XML lỗi: {exc}")
            break

        for node in root.iter("node"):

            desc = (node.attrib.get("content-desc") or "").strip()

            if not desc:
                continue

            is_add_friend = any(
                pattern.match(desc)
                for pattern in add_friend_patterns
            )

            if not is_add_friend:
                continue

            try:
                print(f"[{serial}] Click: {desc}")

                d(description=desc).click()
                time.sleep(5)
                clicked += 1
                found = True

                print(
                    f"[{serial}] Add friend "
                    f"{clicked}/{count_default}"
                )

                time.sleep(click_delay)

                break

            except Exception as exc:
                print(f"[{serial}] Click lỗi: {exc}")

        if clicked >= count_default:
            break

        # Click xong hay không click được đều lướt lên
        swipe(serial, "up")
        swipes += 1

        print(
            f"[{serial}] Swipe {swipes} | "
            f"Clicked {clicked}/{count_default}"
        )

        time.sleep(8)

    print(
        f"[{serial}] Hoàn tất "
        f"{clicked}/{count_default}"
    )

    return clicked

def suggest_add_friends(serial: str,count: int, load_wait: float = 0.0) -> bool:
    time.sleep(load_wait)
    xml_text = get_xml_dump(serial)
    target_desc = "Friends, tab 3 of 6"
    print(f"count: {count}")

    try:
        try:
            d = ui2.connect(serial)
        except Exception:
            d = None
        if d is not None:
            try:
                widget = d(description=target_desc)
                widget.click()
                print(f"[{serial}] clicked via uiautomator2:")
                if _look_like_empty_suggest_friends_screen(serial):
                    if xml_text:
                        try:
                            if d(description="Search"):
                                d(description="Search").click()
                                print(f"[{serial}] clicked empty-screen Search button via uiautomator2")
                                time.sleep(5)
                                if d(text="Search for friends"):
                                    list_name = ('a','b','c','d','e','f','g','h','i','j','k','l','m','n','o','p','q','r','s','t','u','v','w','x','y','z')
                                    name = random.choice(list_name)
                                    d(text="Search for friends").set_text(name)
                                    d.press("enter")    
                                    time.sleep(5)
                                    add_friends(serial, count_default=count)
                                    time.sleep(5)
                        except Exception:
                                    pass
                else:
                    add_friends(serial, count_default=count)
                    time.sleep(3)         
                return False
            except Exception:
                        pass
            except Exception:
                pass
    except Exception:
        pass

    # Fallback: parse XML dump and tap the matching node
    # try:
    #     if xml_text:
    #         root = ET.fromstring(xml_text)
    #         for node in root.iter("node"):
    #             desc = (node.attrib.get("content-desc") or "").strip()
    #             if desc == target_desc:
    #                 if tap_node(serial, node):
    #                     print(f"[{serial}] clicked via XML parse: {target_desc}")
    #                     time.sleep(5)
    #                     if _look_like_empty_suggest_friends_screen(serial):
    #                         try:
    #                             for node2 in root.iter("node"):
    #                                 desc2 = (node2.attrib.get("content-desc") or "").strip()
    #                                 if desc2 == "Search":
    #                                     if tap_node(serial, node2):
    #                                         print(f"[{serial}] clicked empty-screen Search button")
    #                                         time.sleep(5)
    #                                         break

    #                         except Exception:
    #                             pass
    #                     return False
    # except Exception:
    #     pass
   
    # return False


def accept_friend_requests(
    serial: str,
    count: int,
    load_wait: float = 0.0,
) -> bool:
    """Chấp nhận lời mời kết bạn từ tab Friends."""

    time.sleep(load_wait)

    target_desc = "Friends, tab 3 of 6"

    print(f"[{serial}] Accepting friend requests: {count}")

    try:
        d = ui2.connect(serial)
    except Exception as exc:
        print(f"[{serial}] Không connect được uiautomator2: {exc}")
        return False

    try:
        # Mở tab Friends
        try:
            widget = d(description=target_desc)

            if widget.exists(timeout=3):
                widget.click()
                print(f"[{serial}] clicked Friends tab")
                time.sleep(3)
                d(description="Friend requests").click()

        except Exception as exc:
            print(f"[{serial}] Click Friends tab lỗi: {exc}")
            return False

        time.sleep(5)

        # Quét XML xem có lời mời kết bạn không
        confirm_pattern = r"^Confirm .+'s friend request$"

        try:
            widget = d(descriptionMatches=confirm_pattern)

            if widget.exists(timeout=3):
                print(f"[{serial}] Tìm thấy lời mời kết bạn bằng content-desc")

                accepted = _accept_friend_request_items(
                    serial,
                    count,
                    max_swipes=10,
                )

                print(f"[{serial}] Accepted {accepted} friend requests")
                return True

            print(f"[{serial}] Không có lời mời kết bạn nào")
            return False

        except Exception as e:
            print(f"[{serial}] Lỗi khi quét content-desc bằng uiautomator2: {e}")
            return False

    except Exception as e:
        print(f"[{serial}] Error in accept_friend_requests: {e}")
        return False


def _accept_friend_request_items(
    serial: str,
    count: int,
    max_swipes: int = 50,
    click_delay: float = 4.0,
) -> int:
    time.sleep(3)
    print(f"[{serial}] Accepting friend requests: {count}")

    try:
        d = ui2.connect(serial)
    except Exception as exc:
        print(f"[{serial}] Không connect được uiautomator2: {exc}")
        return 0

    accepted = 0
    swipes = 0

    confirm_pattern = r"^Confirm .+'s friend request$"

    while accepted < count and swipes < max_swipes:
        found = False

        try:
            widget = d(descriptionMatches=confirm_pattern)

            if widget.exists(timeout=3):
                info = widget.info
                desc = (info.get("contentDescription") or "").strip()

                if not desc:
                    print(f"[{serial}] Tìm thấy confirm nhưng không lấy được desc")
                    swipe(serial, "up")
                    swipes += 1
                    time.sleep(3)
                    continue

                print(f"[{serial}] Click confirm: {desc}")

                d(description=desc).click()

                accepted += 1
                found = True

                print(f"[{serial}] Accepted {accepted}/{count}")

                time.sleep(click_delay)

            else:
                print(f"[{serial}] Không thấy Confirm friend request")

        except Exception as exc:
            print(f"[{serial}] Lỗi khi quét/click confirm: {exc}")

        if accepted >= count:
            break

        # Giống add_friends: click xong hoặc không thấy đều swipe lên rồi quét tiếp
        swipe(serial, "up")
        swipes += 1

        print(
            f"[{serial}] Swipe {swipes} | "
            f"Accepted {accepted}/{count}"
        )

        time.sleep(3)

    print(f"[{serial}] Hoàn tất accept: {accepted}/{count}")
    return accepted




def _looks_like_fb_welcome_screen(serial: str) -> bool:
    def _match(text: str) -> bool:
        lower = text.lower()
        return "welcome to facebook" in lower or "you can log in or create a new account" in lower

    xml_text = ""
    try:
        xml_text = get_xml_dump(serial)
    except Exception:
        xml_text = ""
    if xml_text and _match(xml_text):
        return True

    try:
        ocr_text = _read_profile_ocr_text(serial)
    except Exception:
        ocr_text = ""
    return bool(ocr_text and _match(ocr_text))

def go_facebook_home(serial: str, max_try: int = 5) -> bool:
    """
    Quy trình:
    1. Quét Home, tab 1 of 6 -> thấy thì click.
    2. Không thấy thì swipe up rồi quét lại.
    3. Vẫn không thấy thì click Back.
    4. Sau Back thì quét Home và click lại.
    """

    try:
        d = ui2.connect(serial)
    except Exception as exc:
        print(f"[{serial}] Không connect được uiautomator2: {exc}")
        return False

    home_desc = "Home, tab 1 of 6"
    back_desc = "Back"

    for i in range(max_try):
        try:
            home = d(description=home_desc)

            if home.exists(timeout=2):
                home.click()
                print(f"[{serial}] Đã click Home")
                time.sleep(2)
                return True

            print(f"[{serial}] Không thấy Home, swipe up lần {i + 1}")
            swipe(serial, "down")
            time.sleep(2)

            home = d(description=home_desc)

            if home.exists(timeout=2):
                home.click()
                print(f"[{serial}] Đã click Home sau khi swipe")
                time.sleep(2)
                return True

            back = d(description=back_desc)

            if back.exists(timeout=2):
                back.click()
                print(f"[{serial}] Đã click Back")
                time.sleep(2)

                home = d(description=home_desc)

                if home.exists(timeout=2):
                    home.click()
                    print(f"[{serial}] Đã click Home sau Back")
                    time.sleep(2)
                    return True

            else:
                keyevent(serial, 4)
                print(f"[{serial}] Không thấy Back desc, dùng keyevent BACK")
                time.sleep(2)

        except Exception as exc:
            print(f"[{serial}] Lỗi go_facebook_home: {exc}")
            try:
                keyevent(serial, 4)
                time.sleep(2)
            except Exception:
                pass

    print(f"[{serial}] Không quay được về Home")
    return False

def like_random_post_or_reel(serial: str, count: int = 1) -> int:
    try:
        d = ui2.connect(serial)
    except Exception as exc:
        print(f"[{serial}] Không connect được uiautomator2: {exc}")
        return 0

    liked = 0

    like_patterns = [
        r"^Like\. Double tap and hold to react\.$",
    ]

    for _ in range(count):
        for pattern in like_patterns:
            try:
                widget = d(descriptionMatches=pattern)

                if widget.exists(timeout=2):
                    widget.click()
                    liked += 1
                    print(f"[{serial}] Đã like {liked}/{count}")
                    time.sleep(random.uniform(1.0, 2.0))
                    break

            except Exception as exc:
                print(f"[{serial}] Lỗi khi click like: {exc}")

    return liked


def farm_story(
    serial: str,
    count: int,
    interval_min: int = 5,
    interval_max: int = 10,
    load_wait: float = 3.0,
) -> int:
    time.sleep(load_wait)

    try:
        d = ui2.connect(serial)
    except Exception as exc:
        print(f"[{serial}] Không connect được uiautomator2: {exc}")
        return 0

    viewed = 0
    story_pattern = r"^.+\'s story(?:,\s*Unseen)?$"

    try:
        widget = d(descriptionMatches=story_pattern)

        if not widget.exists(timeout=5):
            print(f"[{serial}] Không tìm thấy story chưa xem")
            return 0

        info = widget.info
        desc = (info.get("contentDescription") or "").strip()
        bounds = info.get("bounds") or {}

        print(f"[{serial}] Story đầu tiên: {desc}")
        print(f"[{serial}] Bounds: {bounds}")

        # Ưu tiên click bằng tọa độ center của bounds
        left = bounds.get("left", 0)
        top = bounds.get("top", 0)
        right = bounds.get("right", 0)
        bottom = bounds.get("bottom", 0)

        if right > left and bottom > top:
            x = (left + right) // 2
            y = (top + bottom) // 2

            print(f"[{serial}] Click story tại x={x}, y={y}")
            d.click(x, y)
        else:
            print(f"[{serial}] Không lấy được bounds, fallback widget.click()")
            widget.click()

        viewed = 1
        print(f"[{serial}] Đã mở story {viewed}/{count}")

        width, height = get_screen_size(serial)
        tap_x = int(width * 0.80)
        tap_y = int(height * 0.50)

        while viewed < count:
            wait_time = random.randint(
                min(interval_min, interval_max),
                max(interval_min, interval_max)
            )

            print(f"[{serial}] Đợi {wait_time}s rồi chuyển story")
            time.sleep(wait_time)

            tap(serial, tap_x, tap_y)

            viewed += 1
            print(f"[{serial}] Đã xem story {viewed}/{count}")

        keyevent(serial, 4)
        time.sleep(2)

    except Exception as exc:
        print(f"[{serial}] Lỗi xem story: {exc}")

    print(f"[{serial}] Hoàn tất xem story {viewed}/{count}")
    return viewed


def back_keyevent(serial: str) -> None:
    """Bấm nút back (keycode 4)"""
    keyevent(serial, 4)


def _generate_totp_code(secret: str, digits: int = 6, period: int = 30) -> str:
    normalized = re.sub(r"[^A-Z2-7]", "", secret.strip().upper())
    if not normalized:
        raise AdbError("2FA secret trống.")

    padding = "=" * ((8 - len(normalized) % 8) % 8)
    try:
        key = base64.b32decode(normalized + padding, casefold=True)
    except (binascii.Error, ValueError) as exc:
        raise AdbError("2FA secret không hợp lệ (định dạng Base32).") from exc

    counter = int(time.time() // period)
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    code = value % (10**digits)
    return f"{code:0{digits}d}"


def solve_facebook_captcha(serial: str, width: int = 1080, height: int = 2400) -> bool:
    """Tự động nhận diện, cắt ảnh và giải mã Captcha ký tự của Facebook."""
    print(f"[{serial}] 🧩 Đang phân tích và giải mã Captcha Facebook...")

    xml_dump = get_xml_dump(serial) or ""

    # 1. Xác định tọa độ ô ảnh Captcha, ô nhập text và nút Continue từ XML
    img_box = None
    input_pos = (int(width * 0.5), int(height * 0.44))
    continue_pos = (int(width * 0.5), int(height * 0.54))

    if xml_dump:
        try:
            root = ET.fromstring(xml_dump)
            for node in root.iter("node"):
                cls = (node.attrib.get("class") or "").lower()
                text = (node.attrib.get("text") or "").strip().lower()
                desc = (node.attrib.get("content-desc") or "").strip().lower()
                bounds = node.attrib.get("bounds") or ""

                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                if not m:
                    continue
                l, t, r, b = map(int, m.groups())

                if "edittext" in cls or "enter characters" in text or "enter characters" in desc:
                    input_pos = ((l + r) // 2, (t + b) // 2)
                elif text in ("continue", "tiếp tục") or desc in ("continue", "tiếp tục"):
                    continue_pos = ((l + r) // 2, (t + b) // 2)
                elif "imageview" in cls or ("view" in cls and (r - l) > width * 0.35 and (b - t) > height * 0.04 and t < height * 0.45 and t > height * 0.15):
                    if img_box is None or (r - l) * (b - t) > (img_box[2] - img_box[0]) * (img_box[3] - img_box[1]):
                        img_box = (l, t, r, b)
        except Exception as exc:
            print(f"[{serial}] Lỗi phân tích XML Captcha: {exc}")

    if not img_box:
        img_box = (int(width * 0.08), int(height * 0.23), int(width * 0.92), int(height * 0.38))

    # 2. Chụp màn hình để cắt ảnh Captcha
    screenshot_path = _capture_device_screenshot(serial)
    if not screenshot_path or not screenshot_path.exists():
        print(f"[{serial}] ❌ Không thể chụp ảnh màn hình Captcha!")
        return False

    captcha_text = ""
    try:
        from PIL import Image, ImageEnhance
        from rapidocr_onnxruntime import RapidOCR

        ocr = RapidOCR()

        with Image.open(screenshot_path) as full_img:
            img_w, img_h = full_img.size
            scale_x = img_w / float(width) if width > 0 else 1.0
            scale_y = img_h / float(height) if height > 0 else 1.0

            crop_l = max(0, int(img_box[0] * scale_x))
            crop_t = max(0, int(img_box[1] * scale_y))
            crop_r = min(img_w, int(img_box[2] * scale_x))
            crop_b = min(img_h, int(img_box[3] * scale_y))

            cropped = full_img.crop((crop_l, crop_t, crop_r, crop_b))

            # Cách 1: Tăng độ tương phản (High contrast)
            cropped_gray = cropped.convert("L")
            enhancer = ImageEnhance.Contrast(cropped_gray)
            enhanced = enhancer.enhance(2.5)

            crop_temp1 = screenshot_path.with_name(f"crop1_{screenshot_path.name}")
            enhanced.save(crop_temp1)
            res1, _ = ocr(str(crop_temp1))
            text1 = re.sub(r"[^A-Za-z0-9]", "", "".join(str(i[1]) for i in res1 or [] if len(i) >= 2 and i[1])).strip()

            # Cách 2: Phân ngưỡng nhị phân (Binary threshold) lọc sạch đường gạch ngang
            fn_thresh = lambda p: 255 if p > 135 else 0
            binary_img = cropped_gray.point(fn_thresh, mode='1')
            crop_temp2 = screenshot_path.with_name(f"crop2_{screenshot_path.name}")
            binary_img.save(crop_temp2)
            res2, _ = ocr(str(crop_temp2))
            text2 = re.sub(r"[^A-Za-z0-9]", "", "".join(str(i[1]) for i in res2 or [] if len(i) >= 2 and i[1])).strip()

            # Cách 3: Ảnh gốc crop
            crop_temp3 = screenshot_path.with_name(f"crop3_{screenshot_path.name}")
            cropped.save(crop_temp3)
            res3, _ = ocr(str(crop_temp3))
            text3 = re.sub(r"[^A-Za-z0-9]", "", "".join(str(i[1]) for i in res3 or [] if len(i) >= 2 and i[1])).strip()

            # Chọn kết quả có độ dài hợp lệ nhất (thường từ 4-8 ký tự)
            candidates = [t for t in (text1, text2, text3) if len(t) >= 4]
            if candidates:
                captcha_text = candidates[0]
            elif text1:
                captcha_text = text1
            elif text2:
                captcha_text = text2
            elif text3:
                captcha_text = text3

            for p in (crop_temp1, crop_temp2, crop_temp3):
                try:
                    p.unlink(missing_ok=True)
                except Exception:
                    pass
    except Exception as exc:
        print(f"[{serial}] ❌ Lỗi giải mã OCR Captcha: {exc}")
    finally:
        try:
            screenshot_path.unlink(missing_ok=True)
        except Exception:
            pass

    if not captcha_text or len(captcha_text) < 3:
        print(f"[{serial}] ⚠️ Không đọc được mã Captcha rõ ràng (Kết quả: '{captcha_text}'), bấm đổi mã mới...")
        tap(serial, int(width * 0.40), int(height * 0.40))
        time.sleep(2.5)
        return False

    print(f"[{serial}] ✓ ĐÃ GIẢI MÃ CAPTCHA THÀNH CÔNG: [{captcha_text}]")

    # 3. Điền mã Captcha vào ô
    print(f"[{serial}] Điền mã Captcha ({captcha_text}) vào ô...")
    filled = False
    try:
        import uiautomator2 as u2
        d = u2.connect(serial)
        cap_inputs = d(className="android.widget.EditText")
        if cap_inputs.exists and len(cap_inputs) > 0:
            cap_inputs[0].set_text(captcha_text)
            filled = True
            print(f"[{serial}] ✓ Đã điền mã Captcha qua UIAutomator2!")
    except Exception:
        pass

    if not filled:
        tap(serial, input_pos[0], input_pos[1])
        time.sleep(0.4)
        tap(serial, input_pos[0], input_pos[1])
        time.sleep(0.3)
        replace_focused_text(serial, captcha_text, clear_presses=30)
        time.sleep(0.5)

    # 4. Bấm nút Continue
    print(f"[{serial}] Bấm nút Continue để xác nhận Captcha...")
    clicked_continue = False
    try:
        import uiautomator2 as u2
        d = u2.connect(serial)
        btn_continue = d(text="Continue")
        if not btn_continue.exists:
            btn_continue = d(textContains="Continue")
        if btn_continue.exists:
            btn_continue.click()
            clicked_continue = True
    except Exception:
        pass

    if not clicked_continue:
        tap(serial, continue_pos[0], continue_pos[1])

    time.sleep(4.0)
    return True


def handle_switch_to_authenticator_app(serial: str, width: int, height: int, xml_text: str) -> str:
    """Nếu gặp màn hình 'Check your notifications on another device' (Ảnh 1), tự động bấm 'Try another way' -> chọn 'Authentication app' -> 'Continue' (Ảnh 2)."""
    xml_lower = xml_text.lower()

    # 1. Nếu đang ở màn hình 'Check your notifications on another device' (Ảnh 1)
    if any(k in xml_lower for k in ("check your notifications on another device", "waiting for approval", "approve from the other device", "notifications on another device")):
        print(f"[{serial}] Phát hiện Màn hình 'Check your notifications on another device' (Ảnh 1) -> Bấm 'Try another way'...")
        clicked_try = False
        try:
            import uiautomator2 as u2
            d = u2.connect(serial)
            btn = d(textContains="Try another way")
            if btn.exists:
                btn.click()
                clicked_try = True
        except Exception:
            pass

        if not clicked_try and xml_text:
            try:
                root = ET.fromstring(xml_text)
                for node in root.iter("node"):
                    text = (node.attrib.get("text") or "").strip().lower()
                    desc = (node.attrib.get("content-desc") or "").strip().lower()
                    if "try another way" in text or "try another way" in desc:
                        clicked_try = tap_node(serial, node)
                        break
            except Exception:
                pass

        if not clicked_try:
            tap(serial, int(width * 0.5), int(height * 0.64))

        time.sleep(3.0)
        xml_text = get_xml_dump(serial) or ""
        xml_lower = xml_text.lower()

    # 2. Nếu đang ở màn hình 'Choose a way to confirm it's you' (Ảnh 2)
    if any(k in xml_lower for k in ("choose a way to confirm", "available confirmation methods", "choose a way to confirm it's you")):
        print(f"[{serial}] Phát hiện Màn hình 'Choose a way to confirm it's you' (Ảnh 2) -> Chọn 'Authentication app' & Bấm 'Continue'...")
        selected_auth = False
        try:
            import uiautomator2 as u2
            d = u2.connect(serial)
            opt = d(textContains="Authentication app")
            if opt.exists:
                opt.click()
                selected_auth = True
        except Exception:
            pass

        if not selected_auth and xml_text:
            try:
                root = ET.fromstring(xml_text)
                for node in root.iter("node"):
                    text = (node.attrib.get("text") or "").strip().lower()
                    desc = (node.attrib.get("content-desc") or "").strip().lower()
                    if "authentication app" in text or "authentication app" in desc:
                        selected_auth = tap_node(serial, node)
                        break
            except Exception:
                pass

        if not selected_auth:
            tap(serial, int(width * 0.5), int(height * 0.33))

        time.sleep(1.0)

        # Bấm nút Continue
        clicked_continue = False
        try:
            import uiautomator2 as u2
            d = u2.connect(serial)
            btn = d(text="Continue")
            if not btn.exists:
                btn = d(textContains="Continue")
            if btn.exists:
                btn.click()
                clicked_continue = True
        except Exception:
            pass

        if not clicked_continue and xml_text:
            try:
                root = ET.fromstring(xml_text)
                for node in root.iter("node"):
                    text = (node.attrib.get("text") or "").strip().lower()
                    desc = (node.attrib.get("content-desc") or "").strip().lower()
                    if text in ("continue", "tiếp tục") or desc in ("continue", "tiếp tục"):
                        clicked_continue = tap_node(serial, node)
                        break
            except Exception:
                pass

        if not clicked_continue:
            tap(serial, int(width * 0.5), int(height * 0.91))

        time.sleep(3.5)
        xml_text = get_xml_dump(serial) or ""

    return xml_text


def handle_send_code_email_screen(serial: str, width: int, height: int, xml_text: str) -> str:
    """Xử lý màn hình 'We'll send you a code to your email' (Ảnh 1) -> Bấm 'Try another way' -> Chọn 'Password' (Ảnh 2) -> 'Continue'."""
    xml_lower = xml_text.lower()

    # 1. Màn hình 'We'll send you a code to your email' (Ảnh 1)
    if any(k in xml_lower for k in ("we'll send you a code", "send you a code to your email", "send you a code to your", "can't access this email")):
        print(f"[{serial}] Phát hiện Màn hình 'We'll send you a code to your email' (Ảnh 1) -> Bấm 'Try another way'...")
        clicked_try = False
        try:
            import uiautomator2 as u2
            d = u2.connect(serial)
            btn = d(textContains="Try another way")
            if btn.exists:
                btn.click()
                clicked_try = True
        except Exception:
            pass

        if not clicked_try and xml_text:
            try:
                root = ET.fromstring(xml_text)
                for node in root.iter("node"):
                    text = (node.attrib.get("text") or "").strip().lower()
                    desc = (node.attrib.get("content-desc") or "").strip().lower()
                    if "try another way" in text or "try another way" in desc:
                        clicked_try = tap_node(serial, node)
                        break
            except Exception:
                pass

        if not clicked_try:
            tap(serial, int(width * 0.5), int(height * 0.53))

        time.sleep(3.0)
        xml_text = get_xml_dump(serial) or ""
        xml_lower = xml_text.lower()

    # 2. Màn hình 'Choose a way to confirm your account' (Ảnh 2)
    if any(k in xml_lower for k in ("choose a way to confirm your account", "enter password to log in", "choose a way to confirm")):
        print(f"[{serial}] Phát hiện Màn hình 'Choose a way to confirm your account' (Ảnh 2) -> Chọn 'Password' & Bấm 'Continue'...")
        selected_pass = False
        try:
            import uiautomator2 as u2
            d = u2.connect(serial)
            opt = d(textContains="Password")
            if not opt.exists:
                opt = d(textContains="Enter password to log in")
            if opt.exists:
                opt.click()
                selected_pass = True
        except Exception:
            pass

        if not selected_pass and xml_text:
            try:
                root = ET.fromstring(xml_text)
                for node in root.iter("node"):
                    text = (node.attrib.get("text") or "").strip().lower()
                    desc = (node.attrib.get("content-desc") or "").strip().lower()
                    if "password" in text or "password" in desc or "enter password to log in" in text:
                        selected_pass = tap_node(serial, node)
                        break
            except Exception:
                pass

        if not selected_pass:
            tap(serial, int(width * 0.5), int(height * 0.48))

        time.sleep(1.0)

        # Bấm Continue
        clicked_continue = False
        try:
            import uiautomator2 as u2
            d = u2.connect(serial)
            btn = d(text="Continue")
            if not btn.exists:
                btn = d(textContains="Continue")
            if btn.exists:
                btn.click()
                clicked_continue = True
        except Exception:
            pass

        if not clicked_continue and xml_text:
            try:
                root = ET.fromstring(xml_text)
                for node in root.iter("node"):
                    text = (node.attrib.get("text") or "").strip().lower()
                    desc = (node.attrib.get("content-desc") or "").strip().lower()
                    if text in ("continue", "tiếp tục") or desc in ("continue", "tiếp tục"):
                        clicked_continue = tap_node(serial, node)
                        break
            except Exception:
                pass

        if not clicked_continue:
            tap(serial, int(width * 0.5), int(height * 0.89))

        time.sleep(3.5)
        xml_text = get_xml_dump(serial) or ""

    return xml_text


def login_facebook(serial: str, uid: str, password: str, otp_2fa: str = "") -> bool:
    uid = uid.strip()
    password = password.strip()
    otp_2fa = otp_2fa.strip()
    if not uid or not password or uid.lower() in ("trống", "trong", "none", "null", ""):
        raise AdbError("Thiết bị chưa có UID hoặc mật khẩu Facebook hợp lệ.")

    width, height = get_screen_size(serial)

    print(f"[{serial}] Mở Facebook và chờ 6s nạp giao diện...")
    open_facebook(serial)
    time.sleep(6.0)

    logged_in_form_submitted = False
    unrecognized_count = 0

    for step in range(35):
        if is_stop_requested(serial):
            print(f"[{serial}] 🛑 Nhận lệnh DỪNG TIẾN TRÌNH -> Dừng đăng nhập!")
            return False

        xml_dump = get_xml_dump(serial) or ""
        xml_lower = xml_dump.lower()

        # 1. KIỂM TRA ĐÃ VÀO TRANG CHỦ / NEWSFEED THÀNH CÔNG CHƯA
        if any(k in xml_lower for k in ("what's on your mind", "bạn đang nghĩ gì", "search facebook", "tìm kiếm trên facebook", "reels", "trang chủ")):
            print(f"[{serial}] ✓ ĐÃ VÀO TRANG CHỦ FACEBOOK -> HOÀN TẤT ĐĂNG NHẬP!")
            return True

        # 2. KIỂM TRA LỖI SAI TÀI KHOẢN / KHÓA TÀI KHOẢN (Wrong Credentials / Checkpoint)
        if any(k in xml_lower for k in (
            "wrong credentials", "invalid username or password", "incorrect password",
            "sai mật khẩu", "mật khẩu không chính xác", "tài khoản không chính xác",
            "account disabled", "account has been locked", "we suspended your account",
            "tài khoản bị vô hiệu hóa", "tài khoản đã bị khóa"
        )):
            print(f"[{serial}] ❌ Phát hiện 'Wrong Credentials' hoặc Khóa tài khoản -> DỪNG TOOL TRÊN MÁY NÀY!")
            return False

        has_edittext = "edittext" in xml_lower

        # 2.5. MÀN HÌNH ONBOARDING: ADD MOBILE NUMBER (Would you like to add a mobile number) -> BẤM SKIP GÓC TRÊN PHẢI
        if any(k in xml_lower for k in (
            "would you like to add a mobile number", "add a mobile number to your account",
            "add a current phone number", "add a mobile number", "add your phone number", "thêm số di động"
        )):
            print(f"[{serial}] Phát hiện Màn hình 'Would you like to add a mobile number?' -> Bấm 'Skip' góc trên phải...")
            skip_phone_clicked = False
            try:
                import uiautomator2 as u2
                d = u2.connect(serial)
                btn = d(text="Skip")
                if btn.exists:
                    btn.click()
                    skip_phone_clicked = True
            except Exception:
                pass

            if not skip_phone_clicked and xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if text == "skip" or desc == "skip":
                            skip_phone_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass

            if not skip_phone_clicked:
                tap(serial, int(width * 0.92), int(height * 0.055))

            unrecognized_count = 0
            time.sleep(2.5)
            continue

        # 3. MÀN HÌNH ĐĂNG NHẬP GIAO DIỆN MỚI (Có ô 'Mobile number or email' & 'Password')
        is_new_login_screen = has_edittext and ("password" in xml_lower or "mật khẩu" in xml_lower) and (
            "mobile number or email" in xml_lower or "số di động hoặc email" in xml_lower
            or (any(k in xml_lower for k in ("log in", "đăng nhập")) and not any(k in xml_lower for k in ("phone or email", "create new facebook account", "tạo tài khoản facebook mới", "go to your authentication app", "enter the 6-digit code", "add a mobile number", "add a current phone number")))
        )
        if is_new_login_screen and not logged_in_form_submitted:
            print(f"[{serial}] ✓ PHÁT HIỆN MÀN HÌNH ĐĂNG NHẬP [Có ô điền UID & Mật khẩu] -> Điền thông tin...")
            uid_filled = False
            pass_filled = False

            try:
                import uiautomator2 as u2
                d = u2.connect(serial)
                inputs = d(className="android.widget.EditText")
                if inputs.count >= 2:
                    print(f"[{serial}] ⚡ Dán siêu tốc UID và Password vào ô...")
                    inputs[0].set_text(uid)
                    time.sleep(0.2)
                    inputs[1].set_text(password)
                    time.sleep(0.3)
                    uid_filled = True
                    pass_filled = True
                    print(f"[{serial}] ✓ Đã dán siêu tốc UID ({uid}) và Password thành công!")
            except Exception:
                pass

            if not uid_filled or not pass_filled:
                uid_x, uid_y = int(width * 0.5), int(height * 0.43)
                pass_x, pass_y = int(width * 0.5), int(height * 0.51)
                if xml_dump:
                    try:
                        root = ET.fromstring(xml_dump)
                        edit_text_nodes = []
                        for node in root.iter("node"):
                            cls = (node.attrib.get("class") or "").lower()
                            bounds = node.attrib.get("bounds") or ""
                            if "edittext" in cls:
                                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                                if m:
                                    l, t, r, b = map(int, m.groups())
                                    edit_text_nodes.append(((l + r) // 2, (t + b) // 2))
                        if len(edit_text_nodes) >= 2:
                            uid_x, uid_y = edit_text_nodes[0]
                            pass_x, pass_y = edit_text_nodes[1]
                    except Exception:
                        pass

                print(f"[{serial}] Bấm vào ô Mobile number or email & điền UID...")
                tap(serial, uid_x, uid_y)
                time.sleep(0.4)
                tap(serial, uid_x, uid_y)
                time.sleep(0.4)
                replace_focused_text(serial, uid, clear_presses=180)
                time.sleep(0.8)
                tap(serial, uid_x, uid_y)
                time.sleep(0.4)
                keyevent(serial, 111)
                time.sleep(0.6)

                print(f"[{serial}] Bấm vào ô Password & điền Mật khẩu...")
                tap(serial, pass_x, pass_y)
                time.sleep(0.5)
                replace_focused_text(serial, password, clear_presses=120)
                time.sleep(1.0)

            print(f"[{serial}] Đang bấm nút Log in...")
            login_clicked = False
            xml_current = get_xml_dump(serial) or ""
            if xml_current:
                try:
                    root = ET.fromstring(xml_current)
                    for node in root.iter("node"):
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        text = (node.attrib.get("text") or "").strip().lower()
                        if desc in ("log in", "đăng nhập") or text in ("log in", "đăng nhập"):
                            login_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass

            if not login_clicked:
                tap(serial, int(width * 0.5), int(height * 0.58))

            logged_in_form_submitted = True
            unrecognized_count = 0
            time.sleep(5.0)
            continue

        # 4. MÀN HÌNH "Welcome to Facebook" HOẶC GIAO DIỆN ĐĂNG NHẬP CŨ -> ĐỢI 3S RỒI ĐÓNG HẮN FACEBOOK VÀ MỞ LẠI
        is_welcome_screen = (
            any(k in xml_lower for k in ("welcome to facebook", "you can log in or create a new account", "chào mừng bạn đến với facebook", "you can log in"))
            or ("create new account" in xml_lower and any(k in xml_lower for k in ("log in", "đăng nhập")) and not any(k in xml_lower for k in ("mobile number or email", "số di động hoặc email", "forgot password", "quên mật khẩu")))
            or (any(k in xml_lower for k in ("more...", "afrikaans", "tiếng việt", "español")) and any(k in xml_lower for k in ("log in", "đăng nhập", "create new account")) and not any(k in xml_lower for k in ("mobile number or email", "số di động hoặc email", "forgot password", "quên mật khẩu")))
        )
        is_old_ui_screen = ("create new facebook account" in xml_lower or "tạo tài khoản facebook mới" in xml_lower or ("phone or email" in xml_lower and "mobile number or email" not in xml_lower))
        if (is_welcome_screen or is_old_ui_screen) and not is_new_login_screen:
            print(f"[{serial}] ✓ PHÁT HIỆN MÀN HÌNH WELCOME TO FACEBOOK -> Đợi 3s rồi đóng hẳn Facebook và mở lại...")
            time.sleep(3.0)
            close_facebook_app(serial)
            time.sleep(2.5)
            print(f"[{serial}] Mở lại Facebook và chờ nạp màn hình Đăng nhập (10s)...")
            open_facebook(serial)
            time.sleep(10.0)
            unrecognized_count = 0
            continue

        # 5. MÀN HÌNH "Join Facebook" -> BẤM "I already have a profile"
        if any(k in xml_lower for k in ("i already have a profile", "already have a profile", "join facebook", "tôi đã có tài khoản", "tôi đã có trang cá nhân")):
            print(f"[{serial}] Phát hiện màn hình Join Facebook -> Bấm 'I already have a profile'...")
            clicked_already = False
            if xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        desc = (node.attrib.get("content-desc") or "").lower()
                        text = (node.attrib.get("text") or "").lower()
                        if any(k in desc or k in text for k in ("already have a profile", "tôi đã có tài khoản", "tôi đã có trang cá nhân")):
                            clicked_already = tap_node(serial, node)
                            break
                except Exception:
                    pass
            if not clicked_already:
                tap(serial, int(width * 0.5), int(height * 0.85))
            unrecognized_count = 0
            time.sleep(3.5)
            continue

        # 6. MÀN HÌNH "This Page Isn't Available Right Now" -> ĐÓNG APP MỞ LẠI
        if any(k in xml_lower for k in ("this page isn't available", "page isn't available right now", "try reloading this page", "trang này hiện không khả dụng", "không xem được nội dung này")):
            print(f"[{serial}] Phát hiện Màn hình 'This Page Isn't Available Right Now' -> ĐÓNG FACEBOOK VÀ MỞ LẠI...")
            close_facebook_app(serial)
            time.sleep(2.5)
            open_facebook(serial)
            time.sleep(8.0)
            unrecognized_count = 0
            continue

        # 7. MÀN HÌNH "Check your notifications on another device" -> CHUYỂN SANG AUTHENTICATION APP
        if any(k in xml_lower for k in ("check your notifications on another device", "waiting for approval", "choose a way to confirm it's you", "available confirmation methods")):
            xml_dump = handle_switch_to_authenticator_app(serial, width, height, xml_dump)
            unrecognized_count = 0
            time.sleep(2.5)
            continue

        # 8. MÀN HÌNH "We'll send you a code to your email" -> CHUYỂN SANG PASSWORD
        if any(k in xml_lower for k in ("we'll send you a code", "choose a way to confirm your account", "can't access this email", "enter password to log in")):
            xml_dump = handle_send_code_email_screen(serial, width, height, xml_dump)
            unrecognized_count = 0
            time.sleep(2.5)
            continue

        # 9. MÀN HÌNH CAPTCHA ("Enter the characters you see") -> TỰ ĐỘNG GIẢI MÃ
        if any(k in xml_lower for k in ("enter the characters you see", "characters you see", "make sure there's a real human", "can't read this?", "nhập các ký tự")):
            print(f"[{serial}] ✓ Phát hiện Màn hình Captcha -> Bắt đầu tự động giải mã...")
            solve_facebook_captcha(serial, width, height)
            unrecognized_count = 0
            time.sleep(3.0)
            continue

        # 10. MÀN HÌNH NHẬP MÃ 2FA ("Go to your authentication app" / "Two-factor")
        if any(k in xml_lower for k in (
            "go to your authentication app", "authentication app", "two-factor", "2-factor",
            "login code", "authentication code", "enter the 6-digit code",
            "xác thực 2 yếu tố", "mã xác nhận 6 chữ số", "mã đăng nhập"
        )):
            if not otp_2fa:
                print(f"[{serial}] ❌ Màn hình yêu cầu mã 2FA nhưng thiết bị KHÔNG CÓ MÃ 2FA -> DỪNG TOOL TRÊN MÁY NÀY!")
                return False

            print(f"[{serial}] ✓ PHÁT HIỆN CHÍNH XÁC MÀN HÌNH 2FA -> Tiến hành giải mã & nhập mã 2FA...")
            otp_code: str | None = None
            if re.fullmatch(r"\d{6,8}", otp_2fa):
                otp_code = otp_2fa
            else:
                try:
                    otp_code = _generate_totp_code(otp_2fa)
                    print(f"[{serial}] ✓ Đã sinh mã TOTP 2FA: {otp_code}")
                except Exception as exc:
                    print(f"[{serial}] ❌ Lỗi sinh mã 2FA TOTP: {exc} -> DỪNG TOOL TRÊN MÁY NÀY!")
                    return False

            if otp_code:
                # Nhập mã 2FA
                otp_filled = False
                try:
                    import uiautomator2 as u2
                    d = u2.connect(serial)
                    otp_inputs = d(className="android.widget.EditText")
                    if otp_inputs.exists and len(otp_inputs) > 0:
                        print(f"[{serial}] ⚡ Điền mã 2FA ({otp_code}) qua UIAutomator2...")
                        otp_inputs[0].set_text(otp_code)
                        otp_filled = True
                except Exception:
                    pass

                if not otp_filled:
                    otp_x, otp_y = int(width * 0.5), int(height * 0.55)
                    if xml_dump:
                        try:
                            root = ET.fromstring(xml_dump)
                            for node in root.iter("node"):
                                cls = (node.attrib.get("class") or "").lower()
                                bounds = node.attrib.get("bounds") or ""
                                if "edittext" in cls:
                                    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                                    if m:
                                        l, t, r, b = map(int, m.groups())
                                        otp_x, otp_y = (l + r) // 2, (t + b) // 2
                                        break
                        except Exception:
                            pass
                    print(f"[{serial}] Nhập mã 2FA ({otp_code}) bằng ADB Shell...")
                    tap(serial, otp_x, otp_y)
                    time.sleep(0.4)
                    tap(serial, otp_x, otp_y)
                    time.sleep(0.3)
                    replace_focused_text(serial, otp_code, clear_presses=40)

                time.sleep(1.0)

                # Bấm nút Continue
                print(f"[{serial}] Bấm nút Continue...")
                clicked_continue = False
                try:
                    import uiautomator2 as u2
                    d = u2.connect(serial)
                    btn = d(text="Continue")
                    if not btn.exists:
                        btn = d(textContains="Continue")
                    if btn.exists:
                        btn.click()
                        clicked_continue = True
                except Exception:
                    pass

                if not clicked_continue and xml_dump:
                    try:
                        root = ET.fromstring(xml_dump)
                        for node in root.iter("node"):
                            text = (node.attrib.get("text") or "").strip().lower()
                            desc = (node.attrib.get("content-desc") or "").strip().lower()
                            if text in ("continue", "tiếp tục") or desc in ("continue", "tiếp tục"):
                                clicked_continue = tap_node(serial, node)
                                break
                    except Exception:
                        pass

                if not clicked_continue:
                    tap(serial, int(width * 0.5), int(height * 0.65))

                print(f"[{serial}] Đang đợi Facebook hoàn tất xác thực...")
                time.sleep(6.0)

                # Kiểm tra xem có sai mã 2FA không
                xml_chk = get_xml_dump(serial) or ""
                if any(k in xml_chk.lower() for k in ("wrong code", "incorrect code", "mã không chính xác", "invalid code")):
                    print(f"[{serial}] ❌ Sai mã 2FA -> DỪNG TOOL TRÊN MÁY NÀY!")
                    return False

                # Đóng app và mở lại để vào thẳng feed
                print(f"[{serial}] Đóng hẳn ứng dụng Facebook...")
                close_facebook_app(serial)
                time.sleep(2.0)
                open_facebook(serial)
                time.sleep(4.0)
                unrecognized_count = 0
                continue

        # 11. POPUP "Save password to Google" -> BẤM NEVER
        if any(k in xml_lower for k in ("save password to google", "save password", "lưu mật khẩu", "google password manager")):
            print(f"[{serial}] Phát hiện popup 'Save password to Google?' -> Bấm 'Never'...")
            clicked_never = False
            if xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if text in ("never", "không bao giờ") or desc in ("never", "không bao giờ"):
                            clicked_never = tap_node(serial, node)
                            break
                except Exception:
                    pass
            if not clicked_never:
                tap(serial, int(width * 0.40), int(height * 0.89))
            unrecognized_count = 0
            time.sleep(1.5)
            continue

        # 12. MÀN HÌNH CHỈ ĐIỀN PASSWORD (Khi chỉ có 1 ô Password và có Avatar/Tên người dùng)
        is_single_password_screen = has_edittext and ("password" in xml_lower or "mật khẩu" in xml_lower) and any(k in xml_lower for k in ("log in", "đăng nhập")) and not any(k in xml_lower for k in ("welcome to facebook", "mobile number or email", "số di động hoặc email", "forgot password", "quên mật khẩu", "go to your authentication app", "enter the 6-digit code", "login code"))
        if is_single_password_screen:
            print(f"[{serial}] ✓ Phát hiện Màn hình chỉ điền Password -> Điền Mật khẩu & Bấm Log in...")
            pass_filled = False
            try:
                import uiautomator2 as u2
                d = u2.connect(serial)
                inputs = d(className="android.widget.EditText")
                if inputs.exists and len(inputs) > 0:
                    inputs[0].set_text(password)
                    pass_filled = True
            except Exception:
                pass

            if not pass_filled:
                pass_x, pass_y = int(width * 0.5), int(height * 0.35)
                if xml_dump:
                    try:
                        root = ET.fromstring(xml_dump)
                        for node in root.iter("node"):
                            cls = (node.attrib.get("class") or "").lower()
                            bounds = node.attrib.get("bounds") or ""
                            if "edittext" in cls:
                                m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds)
                                if m:
                                    l, t, r, b = map(int, m.groups())
                                    pass_x, pass_y = (l + r) // 2, (t + b) // 2
                                    break
                    except Exception:
                        pass
                tap(serial, pass_x, pass_y)
                time.sleep(0.4)
                tap(serial, pass_x, pass_y)
                time.sleep(0.3)
                replace_focused_text(serial, password, clear_presses=120)

            time.sleep(0.8)

            # Bấm nút Log in
            print(f"[{serial}] Bấm nút Log in...")
            clicked_login = False
            try:
                import uiautomator2 as u2
                d = u2.connect(serial)
                btn = d(text="Log in")
                if not btn.exists:
                    btn = d(textContains="Log in")
                if not btn.exists:
                    btn = d(textContains="Đăng nhập")
                if btn.exists:
                    btn.click()
                    clicked_login = True
            except Exception:
                pass

            if not clicked_login and xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        text = (node.attrib.get("text") or "").strip().lower()
                        if desc in ("log in", "đăng nhập") or text in ("log in", "đăng nhập"):
                            clicked_login = tap_node(serial, node)
                            break
                except Exception:
                    pass

            if not clicked_login:
                tap(serial, int(width * 0.5), int(height * 0.42))

            unrecognized_count = 0
            time.sleep(5.0)
            continue

        # 12.5. MÀN HÌNH ONBOARDING: KEEP YOUR PROFILE UPDATED (Add profile picture) -> BẤM SKIP GÓC TRÊN PHẢI
        if any(k in xml_lower for k in ("keep your profile updated", "add a profile picture", "add picture", "cập nhật trang cá nhân", "thêm ảnh đại diện")):
            print(f"[{serial}] Phát hiện Màn hình 'Keep your profile updated' -> Bấm 'Skip' góc trên phải...")
            skip_profile_clicked = False
            try:
                import uiautomator2 as u2
                d = u2.connect(serial)
                btn = d(text="Skip")
                if not btn.exists:
                    btn = d(textContains="Skip")
                if btn.exists:
                    btn.click()
                    skip_profile_clicked = True
            except Exception:
                pass

            if not skip_profile_clicked and xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if text == "skip" or desc == "skip":
                            skip_profile_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass

            if not skip_profile_clicked:
                tap(serial, int(width * 0.92), int(height * 0.055))

            unrecognized_count = 0
            time.sleep(2.5)
            continue

        # 13. MÀN HÌNH ONBOARDING: LOCATION (Access to location) -> BẤM SKIP
        if any(k in xml_lower for k in ("access to location", "location services", "allow facebook to access your location", "to use location services")):
            print(f"[{serial}] Phát hiện Màn hình Location (Access to location) -> Bấm 'Skip'...")
            loc_skip_clicked = False
            try:
                import uiautomator2 as u2
                d = u2.connect(serial)
                btn = d(text="Skip")
                if btn.exists:
                    btn.click()
                    loc_skip_clicked = True
            except Exception:
                pass
            if not loc_skip_clicked and xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if text == "skip" or desc == "skip":
                            loc_skip_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass
            if not loc_skip_clicked:
                tap(serial, int(width * 0.92), int(height * 0.05))
            unrecognized_count = 0
            time.sleep(2.5)
            continue

        # 13. MÀN HÌNH ONBOARDING: TEEN ACCOUNT -> BẤM SEE YOUR SETTINGS
        if any(k in xml_lower for k in ("teen account on facebook", "you now have a teen account", "built-in protections for teens", "see your settings")):
            print(f"[{serial}] Phát hiện Màn hình Teen Account -> Bấm 'See your settings'...")
            see_settings_clicked = False
            if xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if "see your settings" in text or "see your settings" in desc:
                            see_settings_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass
            if not see_settings_clicked:
                tap(serial, int(width * 0.5), int(height * 0.92))
            unrecognized_count = 0
            time.sleep(2.5)
            continue

        # 14. MÀN HÌNH ONBOARDING: TEEN SAFETY -> BẤM CLOSE
        if any(k in xml_lower for k in ("teen safety settings", "teen safety", "parent or guardian", "time management")):
            print(f"[{serial}] Phát hiện Màn hình Teen Safety Settings -> Bấm 'Close'...")
            close_clicked = False
            if xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if text == "close" or desc == "close" or "close" in text:
                            close_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass
            if not close_clicked:
                tap(serial, int(width * 0.5), int(height * 0.92))
            unrecognized_count = 0
            time.sleep(2.5)
            continue

        # 15. MÀN HÌNH ONBOARDING: SAVE LOGIN INFO -> BẤM NOT NOW
        if any(k in xml_lower for k in ("save your login info", "save login info", "lưu thông tin đăng nhập")):
            print(f"[{serial}] Phát hiện Màn hình Save your login info -> Bấm 'Not now'...")
            notnow_clicked = False
            if xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if text in ("not now", "lúc khác") or desc in ("not now", "lúc khác"):
                            notnow_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass
            if not notnow_clicked:
                tap(serial, int(width * 0.5), int(height * 0.92))
            unrecognized_count = 0
            time.sleep(2.0)
            continue

        # 16. MÀN HÌNH ONBOARDING: AUTOMATED BEHAVIOR -> BẤM DISMISS
        if any(k in xml_lower for k in ("automated behavior", "suspect automated behavior", "bảo vệ tài khoản")):
            print(f"[{serial}] Phát hiện Màn hình Automated behavior -> Bấm 'Dismiss'...")
            dismiss_clicked = False
            if xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if text == "dismiss" or desc == "dismiss":
                            dismiss_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass
            if not dismiss_clicked:
                tap(serial, int(width * 0.5), int(height * 0.22))
            unrecognized_count = 0
            time.sleep(2.0)
            continue

        # 17. MÀN HÌNH ONBOARDING: UPLOAD CONTACTS POPUP -> BẤM SKIP
        if "sure you don't want to upload" in xml_lower or "upload your contacts?" in xml_lower:
            print(f"[{serial}] Phát hiện Màn hình Popup Upload Contacts -> Bấm 'SKIP'...")
            skip_dialog_clicked = False
            if xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().upper()
                        desc = (node.attrib.get("content-desc") or "").strip().upper()
                        if text == "SKIP" or desc == "SKIP":
                            skip_dialog_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass
            if not skip_dialog_clicked:
                tap(serial, int(width * 0.48), int(height * 0.58))
            unrecognized_count = 0
            time.sleep(2.0)
            continue

        # 18. MÀN HÌNH ONBOARDING: ALLOW CONTACTS / MOBILE NUMBER -> BẤM SKIP
        if any(k in xml_lower for k in ("allow contacts access", "find people to follow", "add a mobile number", "mobile number to your account")):
            print(f"[{serial}] Phát hiện Màn hình Contacts / Phone number -> Bấm 'Skip' góc trên phải...")
            skip_top_clicked = False
            if xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if text == "skip" or desc == "skip":
                            skip_top_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass
            if not skip_top_clicked:
                tap(serial, int(width * 0.92), int(height * 0.05))
            unrecognized_count = 0
            time.sleep(2.0)
            continue

        # 19. MÀN HÌNH ONBOARDING: SELECT LANGUAGE -> BẤM CONTINUE IN ENGLISH (US)
        if any(k in xml_lower for k in ("continue in english", "english (us)", "choose your language", "试用中文", "facebook", "हिन्दी में facebook")):
            print(f"[{serial}] Phát hiện Màn hình Ngôn ngữ -> Bấm 'Continue in English (US)'...")
            lang_clicked = False
            try:
                import uiautomator2 as u2
                d = u2.connect(serial)
                btn = d(textContains="English")
                if not btn.exists:
                    btn = d(textContains="Continue in English")
                if btn.exists:
                    btn.click()
                    lang_clicked = True
            except Exception:
                pass

            if not lang_clicked and xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if "english" in text or "english" in desc or "continue" in text:
                            lang_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass

            if not lang_clicked:
                tap(serial, int(width * 0.5), int(height * 0.69))

            unrecognized_count = 0
            time.sleep(3.0)
            continue

        # 20. TỔNG QUÁT: BẤT KỲ MÀN HÌNH THIẾT LẬP NÀO CÓ NÚT 'SKIP'
        if "skip" in xml_lower or "bỏ qua" in xml_lower:
            skip_any_clicked = False
            try:
                import uiautomator2 as u2
                d = u2.connect(serial)
                btn = d(text="Skip")
                if not btn.exists:
                    btn = d(textContains="Skip")
                if btn.exists:
                    btn.click()
                    skip_any_clicked = True
            except Exception:
                pass

            if not skip_any_clicked and xml_dump:
                try:
                    root = ET.fromstring(xml_dump)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if text == "skip" or desc == "skip":
                            skip_any_clicked = tap_node(serial, node)
                            break
                except Exception:
                    pass

            if skip_any_clicked:
                print(f"[{serial}] Phát hiện nút 'Skip' thiết lập -> Đã bấm Skip...")
                unrecognized_count = 0
                time.sleep(2.5)
                continue

        # KHÔNG KHỚP MÀN HÌNH NÀO TRONG BƯỚC NÀY
        unrecognized_count += 1
        if unrecognized_count >= 15:
            print(f"[{serial}] ⚠️ Không phát hiện màn hình hợp lệ nào tiếp theo -> DỪNG TIẾN TRÌNH TRÊN MÁY NÀY!")
            break
        time.sleep(2.0)

    print(f"[{serial}] ✓ Hoàn tất quy trình Đăng nhập Facebook!")
    return True


def open_link(serial: str, url: str) -> None:
    escaped = url.replace("'", "'\\''")
    adb_shell(serial, "am", "start", "-a", "android.intent.action.VIEW", "-d", f"'{escaped}'", check=False, timeout=10)


def open_facebook_reels(serial: str) -> None:
    open_facebook(serial)
    time.sleep(2)
    swipe(serial, "down")
    time.sleep(1.5)
    tap(serial, 270, 235)
    time.sleep(3)
    tap(serial, 367, 288)
    time.sleep(2)


def ensure_adb_keyboard(serial: str) -> bool:
    """Ensure ADB Keyboard is automatically enabled and set as active on Android."""
    try:
        output = adb_shell(serial, "ime", "list", "-a", check=False, timeout=5)
        for ime_component in (
            "com.android.adbkeyboard/.AdbIME",
            "com.genfarmer.uiautomator/.AdbKeyboard",
            "com.github.uiautomator/.FastInputIME",
        ):
            pkg = ime_component.split("/")[0]
            if pkg in output:
                adb_shell(serial, "ime", "enable", ime_component, check=False, timeout=5)
                adb_shell(serial, "ime", "set", ime_component, check=False, timeout=5)
                return True
    except Exception as exc:
        print(f"[{serial}] Lỗi tự động bật bàn phím ADB: {exc}")
    return False


def input_text(serial: str, text: str) -> None:
    if not text:
        return

    import unicodedata
    nfkd_form = unicodedata.normalize('NFKD', text)
    ascii_text = "".join([c for c in nfkd_form if not unicodedata.combining(c)])
    # Cho phép giữ ký tự @ và các ký tự đặc biệt thông dụng trong mật khẩu/email
    ascii_text = re.sub(r'[^\w\s\.\!\?\-\,\@\#\$\%\^\&\*\(\)\_\+\=\/\<\>\[\]\{\}]', '', ascii_text).strip()

    if ascii_text:
        # Chuyển các ký tự đặc biệt có ý nghĩa với shell thành escape hoặc URL encode nếu dùng ADB input
        # Ký tự @ trong ADB shell input text có thể truyền trực tiếp nếu nằm trong chuỗi được escape đúng
        safe_str = ascii_text.replace(" ", "%s").replace("@", "\\@").replace("&", "\\&").replace("#", "\\#").replace("'", "").replace('"', '')
        adb_shell(serial, "input", "text", safe_str, check=False, timeout=10)


def swipe(serial: str, direction: str) -> None:
    if direction == "up":
        adb_shell(serial, "input", "swipe", "500", "1500", "500", "500", "300", check=False, timeout=10)
    elif direction == "down":
        adb_shell(serial, "input", "swipe", "500", "500", "500", "1500", "300", check=False, timeout=10)


def tap(serial: str, x: int, y: int) -> None:
    adb_shell(serial, "input", "tap", str(x), str(y), check=False, timeout=10)


def keyevent(serial: str, keycode: int) -> None:
    adb_shell(serial, "input", "keyevent", str(keycode), check=False, timeout=10)


def install_apk(serial: str, apk_path: str) -> None:
    apk_file = Path(apk_path)
    if not apk_file.exists():
        raise AdbError(f"Không tìm thấy file APK: {apk_path}")

    # 1. Tắt tạm thời kiểm tra Play Protect / Package Verifier qua ADB
    try:
        adb_shell(serial, "settings", "put", "global", "package_verifier_enable", "0", check=False, timeout=5)
        adb_shell(serial, "settings", "put", "global", "verifier_verify_adb_installs", "0", check=False, timeout=5)
    except Exception:
        pass

    # 2. Chạy lệnh cài đặt tương thích tất cả phiên bản Android (dùng -r -g -t)
    install_cmd = [_tool_path("adb"), "-s", serial, "install", "-r", "-g", "-t", str(apk_file)]
    
    proc = subprocess.Popen(install_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    
    start_time = time.time()
    clicked_install_anyway = False
    
    while proc.poll() is None:
        if time.time() - start_time > 180:
            proc.kill()
            raise AdbError("Cài đặt APK quá thời gian (Timeout 180s)")
            
        # Kiểm tra màn hình xem có popup Google Play Protect không để tự bấm "Install anyway"
        if not clicked_install_anyway and (time.time() - start_time) > 1.5:
            try:
                xml_text = get_xml_dump(serial)
                if xml_text and ("unsafe app blocked" in xml_text.lower() or "play protect" in xml_text.lower() or "install anyway" in xml_text.lower() or "vẫn cài đặt" in xml_text.lower()):
                    root = ET.fromstring(xml_text)
                    for node in root.iter("node"):
                        text = (node.attrib.get("text") or "").strip().lower()
                        desc = (node.attrib.get("content-desc") or "").strip().lower()
                        if "install anyway" in text or "vẫn cài đặt" in text or "install anyway" in desc or "vẫn cài đặt" in desc:
                            if tap_node(serial, node):
                                clicked_install_anyway = True
                                print(f"[{serial}] Đã tự động bấm 'Install anyway' trên Play Protect.")
                                break
            except Exception:
                pass
        time.sleep(0.8)

    stdout, stderr = proc.communicate()
    output = (stdout or "") + (stderr or "")
    
    if "Success" not in output and "success" not in output.lower():
        clean_out = output.strip().replace("\r", "").replace("\n", " ")
        raise AdbError(f"Cài đặt APK thất bại: {clean_out or 'Lỗi không xác định'}")


def push_image_to_device(serial: str, local_image_path: str, remote_name: str = "") -> str:
    local_file = Path(local_image_path)
    if not local_file.exists():
        raise AdbError(f"Không tìm thấy file ảnh: {local_image_path}")

    ext = local_file.suffix or ".jpg"
    if not remote_name:
        remote_name = f"img_{int(time.time())}_{random.randint(100, 999)}{ext}"

    adb_shell(serial, "mkdir", "-p", "/sdcard/DCIM/Camera", check=False, timeout=5)
    remote_path = f"/sdcard/DCIM/Camera/{remote_name}"
    _adb("-s", serial, "push", str(local_file), remote_path, timeout=60, check=False)

    # Update Android MediaStore so Gallery & Facebook see the new picture immediately
    adb_shell(
        serial,
        "am", "broadcast",
        "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
        "-d", f"file://{remote_path}",
        check=False,
        timeout=10,
    )
    return remote_path


def change_facebook_avatar(serial: str, image_path: str) -> bool:
    # 1. Push image into DCIM gallery
    push_image_to_device(serial, image_path)
    time.sleep(1)

    # 2. Open Facebook and reset screen
    open_facebook_and_reset(serial)
    time.sleep(3)

    width, height = get_screen_size(serial)

    # 3. Open menu / profile
    xml_home = get_xml_dump(serial)
    profile_clicked = False
    if xml_home:
        try:
            root = ET.fromstring(xml_home)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").lower()
                text = (node.attrib.get("text") or "").lower()
                if any(k in desc or k in text for k in ("menu, tab", "profile", "trang cá nhân", "see your profile", "xem trang cá nhân")):
                    profile_clicked = tap_node(serial, node)
                    break
        except Exception:
            pass

    if not profile_clicked:
        tap(serial, int(width * 0.9), int(height * 0.1))
    time.sleep(3)

    # Tap on "See your profile" if in menu
    xml_menu = get_xml_dump(serial)
    if xml_menu:
        try:
            root = ET.fromstring(xml_menu)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").lower()
                text = (node.attrib.get("text") or "").lower()
                if any(k in desc or k in text for k in ("xem trang cá nhân", "see your profile")):
                    tap_node(serial, node)
                    break
        except Exception:
            pass
    time.sleep(3)

    # 4. Tap profile picture camera icon / avatar
    xml_profile = get_xml_dump(serial)
    avatar_clicked = False
    if xml_profile:
        try:
            root = ET.fromstring(xml_profile)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").lower()
                text = (node.attrib.get("text") or "").lower()
                if any(k in desc or k in text for k in ("profile picture", "camera", "chọn ảnh đại diện", "select profile picture", "choose profile picture", "chỉnh sửa ảnh đại diện", "add profile picture")):
                    avatar_clicked = tap_node(serial, node)
                    break
        except Exception:
            pass

    if not avatar_clicked:
        tap(serial, int(width * 0.25), int(height * 0.28))
    time.sleep(2.5)

    # 5. Tap "Choose profile picture" / "Select Profile Picture" / "Chọn ảnh đại diện" in popup bottom sheet
    xml_popup = get_xml_dump(serial)
    popup_clicked = False
    if xml_popup:
        try:
            root = ET.fromstring(xml_popup)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").lower()
                text = (node.attrib.get("text") or "").lower()
                if any(k in desc or k in text for k in (
                    "choose profile picture", "select profile picture", "chọn ảnh đại diện",
                    "chọn hình đại diện", "choose profile photo", "select profile photo",
                    "thêm ảnh đại diện", "tải ảnh lên"
                )):
                    popup_clicked = tap_node(serial, node)
                    break
        except Exception:
            pass

    if not popup_clicked:
        # Fallback: Tap bottom sheet "Choose profile picture" position (~y: 80%)
        tap(serial, int(width * 0.4), int(height * 0.8))
    time.sleep(3)

    # 5.5. Handle Android Permission Dialog if pops up ("Allow", "Cho phép", "Allow access to all photos")
    xml_perm = get_xml_dump(serial)
    if xml_perm:
        try:
            root = ET.fromstring(xml_perm)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").lower()
                text = (node.attrib.get("text") or "").lower()
                if any(k in desc or k in text for k in (
                    "allow", "cho phép", "while using the app", "trong khi dùng ứng dụng",
                    "allow access to all photos", "cho phép truy cập tất cả ảnh", "only this time"
                )):
                    tap_node(serial, node)
                    time.sleep(2)
                    break
        except Exception:
            pass

    # 5.6. Đảm bảo ở đúng tab CAMERA ROLL (tránh bị nhảy sang tab UPLOADS)
    xml_tab = get_xml_dump(serial)
    if xml_tab:
        try:
            root = ET.fromstring(xml_tab)
            for node in root.iter("node"):
                text = (node.attrib.get("text") or "").lower()
                desc = (node.attrib.get("content-desc") or "").lower()
                if "camera roll" in text or "camera roll" in desc:
                    tap_node(serial, node)
                    time.sleep(1.5)
                    break
        except Exception:
            pass

    # 6. Bấm thẳng vào ảnh đầu tiên trong lưới CAMERA ROLL (tọa độ x: 18%, y: 36%)
    tap(serial, int(width * 0.18), int(height * 0.36))
    time.sleep(3)

    # 7. Tap "Save" / "Lưu" / "Update" button
    xml_save = get_xml_dump(serial)
    save_clicked = False
    if xml_save:
        try:
            root = ET.fromstring(xml_save)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").lower()
                text = (node.attrib.get("text") or "").lower()
                if any(k in desc or k in text for k in ("save", "lưu", "done", "tải lên", "upload", "share", "chia sẻ", "update")):
                    save_clicked = tap_node(serial, node)
                    break
        except Exception:
            pass

    if not save_clicked:
        tap(serial, int(width * 0.9), int(height * 0.08))
    time.sleep(2)
    return True


REALISTIC_BIOS = [
    "Sống là cho, đâu chỉ nhận riêng mình. 🌿",
    "Bình yên là ở trong tâm ✨",
    "Work hard in silence, let success make the noise.",
    "Mọi chuyện rồi cũng sẽ qua 🍃",
    "Do what you love, love what you do. 💫",
    "Nỗ lực mỗi ngày 🎯",
    "Keep smiling and be happy 😊",
    "Cuộc sống là những chuyến đi ✈️",
    "Lạc quan giữa đám đông ☘️",
    "Học cách trân trọng những gì mình đang có.",
    "Tâm tự tại, đời an yên. 🕊️",
    "Mỗi ngày là một món quà 🎁",
    "Chân thành là đỉnh cao của sự thông minh.",
    "Thanh xuân như một tách trà ☕",
    "Luôn mỉm cười với cuộc sống 😊",
    "Sống đơn giản cho đời thanh thản ✨",
    "Cứ vui vẻ, cuộc đời sẽ mỉm cười! 🔥",
    "Vạn sự tùy duyên 🌸",
    "Yêu thương bản thân nhiều hơn 💖",
    "Bình tĩnh sống ☕",
]


def close_facebook_app(serial: str) -> None:
    """Force close Facebook application via ADB shell."""
    adb_shell(serial, "am", "force-stop", "com.facebook.katana", check=False, timeout=5)


def paste_text_to_device(serial: str, text: str, x: int, y: int) -> None:
    """
    Directly sets text into Android device clipboard via multiple ADB methods,
    then pastes into the targeted field coordinates (x, y) using KEYCODE_PASTE and native popup.
    """
    if not text:
        return

    clean_text = text.replace("'", "\\'").replace('"', '\\"')

    # 1. Set text into Android device clipboard via ADB cmd clipboard
    try:
        adb_shell(serial, "cmd", "clipboard", "set", "text", f"'{clean_text}'", check=False, timeout=5)
    except Exception:
        pass

    # 2. Set text via ADB Keyboard Broadcast intent
    try:
        b64 = base64.b64encode(text.encode("utf-8")).decode("utf-8")
        adb_shell(serial, "am", "broadcast", "-a", "ADB_SET_CLIPBOARD", "--es", "msg", b64, check=False, timeout=5)
        adb_shell(serial, "am", "broadcast", "-a", "ADB_INPUT_B64", "--es", "msg", b64, check=False, timeout=5)
    except Exception:
        pass

    # 3. Focus cursor into field at (x, y)
    tap(serial, x, y)
    time.sleep(0.8)

    # 4. Issue KEYCODE_PASTE (279)
    keyevent(serial, 279)
    time.sleep(1.0)

    # 5. Long press at (x, y) for 1.2s to pop up native 'Paste' / 'Dán' button
    adb_shell(serial, "input", "swipe", str(x), str(y), str(x), str(y), "1200", check=False, timeout=5)
    time.sleep(1.0)

    xml_popup = get_xml_dump(serial) or ""
    pasted = False
    if xml_popup:
        try:
            root = ET.fromstring(xml_popup)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").lower()
                text_val = (node.attrib.get("text") or "").lower()
                if any(k in desc or k in text_val for k in ("paste", "dán")):
                    pasted = tap_node(serial, node)
                    break
        except Exception:
            pass

    if not pasted:
        keyevent(serial, 279)

    time.sleep(1.0)
    # 6. Fallback input_text
    input_text(serial, text)


def update_fb_avatar_and_bio(serial: str, image_path: str = "", progress_callback=None) -> dict[str, str]:
    """
    Checks if Facebook profile already has Avatar and Bio.
    If Avatar is missing and image_path is provided -> updates Avatar.
    If Bio is missing -> writes a random realistic Bio.
    If both already exist -> skips.
    """
    def log(msg: str):
        if progress_callback:
            progress_callback(msg)

    status_result = {"avatar": "Đã có", "bio": "Đã có"}

    width, height = get_screen_size(serial)

    # 1. THOÁT KHỎI MÀN HÌNH CHỌN/XEM ẢNH (SELECT PHOTO / PREVIEW PROFILE PICTURE) NẾU ĐANG BỊ TREO
    for _ in range(3):
        xml_check = get_xml_dump(serial) or ""
        xml_check_lower = xml_check.lower()
        if any(k in xml_check_lower for k in ("select photo", "camera roll", "uploads", "photos of you", "preview profile picture", "make temporary", "add frame", "suggested photos")):
            log("Phát hiện đang ở màn hình xem/chọn ảnh -> Bấm Quay lại...")
            tap(serial, int(width * 0.07), int(height * 0.06))
            time.sleep(1)
            keyevent(serial, 4)
            time.sleep(2)
        else:
            break

    # 2. VÀO LẠI TRANG CÁ NHÂN SẠCH SẼ
    open_facebook_and_reset(serial)
    time.sleep(3)

    xml_home = get_xml_dump(serial) or ""
    profile_clicked = False
    if xml_home:
        try:
            root = ET.fromstring(xml_home)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").lower()
                text = (node.attrib.get("text") or "").lower()
                if any(k in desc or k in text for k in ("menu, tab", "profile", "trang cá nhân", "see your profile", "xem trang cá nhân")):
                    profile_clicked = tap_node(serial, node)
                    break
        except Exception:
            pass

    if not profile_clicked:
        tap(serial, int(width * 0.9), int(height * 0.1))
    time.sleep(3)

    xml_menu = get_xml_dump(serial) or ""
    if xml_menu:
        try:
            root = ET.fromstring(xml_menu)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").lower()
                text = (node.attrib.get("text") or "").lower()
                if any(k in desc or k in text for k in ("xem trang cá nhân", "see your profile")):
                    tap_node(serial, node)
                    break
        except Exception:
            pass
    time.sleep(3)

    xml_profile_main = get_xml_dump(serial) or ""
    if any(k in xml_profile_main.lower() for k in ("see profile picture", "choose profile picture", "import from instagram", "select photo", "preview profile picture", "camera roll")):
        log("Đang đóng menu chọn ảnh (Choose profile picture)...")
        keyevent(serial, 4)
        time.sleep(2)
        xml_profile_main = get_xml_dump(serial) or ""

    # 3. BẤM CHÍNH XÁC NÚT 'EDIT PROFILE' TRÊN MÀN HÌNH TRANG CÁ NHÂN
    log("Đang tìm & bấm nút 'Edit profile'...")
    edit_profile_clicked = False
    if xml_profile_main:
        try:
            root = ET.fromstring(xml_profile_main)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").lower().strip()
                text = (node.attrib.get("text") or "").lower().strip()
                if desc in ("edit profile", "chỉnh sửa trang cá nhân", "edit public details") or \
                   text in ("edit profile", "chỉnh sửa trang cá nhân", "edit public details") or \
                   ("edit profile" in text and len(text) < 30) or ("chỉnh sửa trang cá nhân" in text and len(text) < 30):
                    edit_profile_clicked = tap_node(serial, node)
                    log(f"✓ Đã tìm thấy & bấm nút Edit profile (Chữ: '{text or desc}')")
                    break
        except Exception:
            pass

    if not edit_profile_clicked:
        # Bấm dự phòng vào vị trí nút Edit profile (tọa độ y ~ 58% height)
        tap(serial, int(width * 0.66), int(height * 0.58))
    time.sleep(3)

    # 4. ĐỌC GIAO DIỆN 'EDIT PROFILE' ĐỂ KIỂM TRA AVATAR VÀ BIO
    xml_edit = get_xml_dump(serial) or ""
    xml_edit_lower = xml_edit.lower()

    if not any(k in xml_edit_lower for k in ("profile picture", "cover photo", "bio", "details", "chỉnh sửa", "tiểu sử", "ảnh đại diện")):
        tap(serial, int(width * 0.66), int(height * 0.58))
        time.sleep(3)
        xml_edit = get_xml_dump(serial) or ""
        xml_edit_lower = xml_edit.lower()

    # Kiểm tra Avatar xem có nút "Add" / "Thêm" riêng biệt không
    avatar_add_node = None
    if xml_edit:
        try:
            root = ET.fromstring(xml_edit)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").lower()
                text = (node.attrib.get("text") or "").lower()
                if desc in ("add profile picture", "thêm ảnh đại diện") or text in ("add profile picture", "thêm ảnh đại diện") or (
                    (text in ("add", "thêm") or desc in ("add", "thêm")) and any(p in desc or p in text for p in ("profile picture", "picture", "avatar", "ảnh đại diện"))
                ):
                    avatar_add_node = node
                    break
        except Exception:
            pass

    has_no_avatar = avatar_add_node is not None

    if has_no_avatar:
        log("Phát hiện nick CHƯA CÓ Avatar -> Đang nạp & đổi Avatar mới...")
        if image_path:
            try:
                change_facebook_avatar(serial, image_path)
                status_result["avatar"] = "✓ Đã thêm Avatar mới"
                tap(serial, int(width * 0.66), int(height * 0.58))
                time.sleep(3)
            except Exception as exc:
                status_result["avatar"] = f"Lỗi Avatar: {exc}"
        else:
            status_result["avatar"] = "Chưa có Avatar (Thiếu file ảnh)"
    else:
        log("✓ Nick ĐÃ CÓ Avatar -> Bỏ qua đổi Avatar!")
        status_result["avatar"] = "✓ Đã có Avatar (Bỏ qua)"

    # 5. TÌM VỊ TRÍ CHUẨN CỦA MỤC BIO TRÊN MÀN HÌNH EDIT PROFILE
    log("Đang dò tìm vị trí chuẩn của mục Bio...")
    bio_add_node = None
    xml_edit_scrolled = ""

    for attempt in range(3):
        xml_edit_scrolled = get_xml_dump(serial) or ""
        xml_scrolled_lower = xml_edit_scrolled.lower()

        if "cover photo" in xml_scrolled_lower:
            # Vuốt nhẹ màn hình lên 35% để đưa mục Bio lên trung tâm màn hình
            adb_shell(serial, "input", "swipe", str(int(width*0.5)), str(int(height*0.7)), str(int(width*0.5)), str(int(height*0.35)), "300", check=False)
            time.sleep(1.5)
            xml_edit_scrolled = get_xml_dump(serial) or ""
            xml_scrolled_lower = xml_edit_scrolled.lower()

        if xml_edit_scrolled:
            try:
                root = ET.fromstring(xml_edit_scrolled)
                for node in root.iter("node"):
                    desc = (node.attrib.get("content-desc") or "").lower()
                    text = (node.attrib.get("text") or "").lower()
                    if any(k in desc or k in text for k in ("add bio", "thêm tiểu sử", "describe yourself", "mô tả bản thân")) or \
                       ((text in ("add", "thêm") or desc in ("add", "thêm")) and "details" not in text and "details" not in desc):
                        bio_add_node = node
                        break
            except Exception:
                pass

        if bio_add_node:
            break

    in_edit_bio = ("0/101" in xml_scrolled_lower or "you can add a short bio" in xml_scrolled_lower) and "cover photo" not in xml_scrolled_lower

    if not in_edit_bio:
        log("Đang mở màn hình nhập Bio (nút Add / Describe yourself)...")
        add_clicked = False
        if bio_add_node:
            add_clicked = tap_node(serial, bio_add_node)

        if not add_clicked:
            tap(serial, int(width * 0.88), int(height * 0.35))
        time.sleep(3.5)

    # BƯỚC B: KHI ĐÃ Ở MÀN HÌNH 'Edit bio' -> BẤM CẢ HỆ THỐNG & TỌA ĐỘ VÀO KHUNG NHẬP ĐỂ BẬT CON TRỎ NHẤP NHÁY
    selected_bio = random.choice(REALISTIC_BIOS)
    log("Đã vào màn hình Edit bio -> Đang kích hoạt con trỏ nhấp nháy...")

    # 1. Thử bấm kích hoạt qua UIAutomator Accessibility Node Click (phương thức chuẩn nhất của Android)
    box_focused = False
    try:
        d = ui2.connect(serial)
        if d(className="android.widget.EditText").exists(timeout=2):
            d(className="android.widget.EditText").click()
            box_focused = True
            log("✓ Đã bấm kích hoạt con trỏ nhấp nháy qua UIAutomator Node Click!")
    except Exception:
        pass

    # 2. Bấm chạm vào đúng tâm ô Describe yourself (tọa độ y ~ 40% height)
    input_box_x = int(width * 0.5)
    input_box_y = int(height * 0.40)

    log(f"Đang bấm 2 lần vào chính giữa ô nhập Bio (Tọa độ: {input_box_x}, {input_box_y})...")
    tap(serial, input_box_x, input_box_y)
    time.sleep(0.4)
    tap(serial, input_box_x, input_box_y)

    # Chờ 3s cho bàn phím & con trỏ nhấp nháy xuất hiện
    log("Đang chờ 3 giây để con trỏ nhấp nháy xuất hiện...")
    time.sleep(3.0)

    # BƯỚC C: TIẾN HÀNH NHẬP BIO VÀO
    log(f"Đang nhập câu Bio: '{selected_bio}'...")
    input_text(serial, selected_bio)
    time.sleep(2.5)

    # BƯỚC D: BẤM NÚT SAVE (LƯU)
    log("Đang bấm nút Save (Lưu)...")
    save_clicked = False
    xml_save = get_xml_dump(serial) or ""
    if xml_save:
        try:
            root = ET.fromstring(xml_save)
            for node in root.iter("node"):
                desc = (node.attrib.get("content-desc") or "").strip().lower()
                text = (node.attrib.get("text") or "").strip().lower()
                if desc in ("save", "lưu") or text in ("save", "lưu"):
                    save_clicked = tap_node(serial, node)
                    break
        except Exception:
            pass

    if not save_clicked:
        # Tọa độ nút Save trên màn hình Edit bio (góc trên bên phải, x ~ 92%, y ~ 6%)
        tap(serial, int(width * 0.92), int(height * 0.06))
    time.sleep(3.5)

    status_result["bio"] = f"✓ Đã thêm Tiểu sử: '{selected_bio}'"
    log(f"✓ Đã lưu thành công Tiểu sử: {selected_bio}")

    # BƯỚC E: ĐÓNG ỨNG DỤNG FACEBOOK
    close_facebook_app(serial)
    log("✓ Đã đóng ứng dụng Facebook!")

    return status_result


def download_bulk_avatars(count: int = 20, output_dir: str = "", progress_callback=None) -> list[str]:
    """
    Downloads bulk 100% REAL human portrait photos (men & women real photography) from RandomUser real people repository.
    """
    import urllib.request
    import concurrent.futures

    if not output_dir:
        output_dir = str(ROOT_DIR / "data" / "avatars")

    os.makedirs(output_dir, exist_ok=True)

    real_photo_urls: list[str] = []
    
    # 1. Fetch real human portrait photo URLs from RandomUser real people dataset
    try:
        api_url = f"https://randomuser.me/api/?results={min(max(count * 2, 50), 500)}&inc=picture"
        req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            for item in data.get("results", []):
                pic = item.get("picture", {}).get("large")
                if pic:
                    real_photo_urls.append(pic)
    except Exception as exc:
        print(f"Lỗi gọi RandomUser API: {exc}")

    # 2. Supplement with direct real human portrait indices (women/men 1-99 real photos)
    for i in range(1, 100):
        real_photo_urls.append(f"https://randomuser.me/api/portraits/women/{i}.jpg")
        real_photo_urls.append(f"https://randomuser.me/api/portraits/men/{i}.jpg")

    random.shuffle(real_photo_urls)

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    def download_one(index: int) -> str | None:
        filename = f"nguoi_that_{int(time.time())}_{index + 1:03d}.jpg"
        file_path = os.path.join(output_dir, filename)
        url = real_photo_urls[index % len(real_photo_urls)]
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as response:
                content = response.read()
                if len(content) > 3000:
                    with open(file_path, "wb") as f:
                        f.write(content)
                    if progress_callback:
                        progress_callback(index + 1, count, filename)
                    return file_path
        except Exception as exc:
            print(f"Lỗi tải ảnh người thật ({url}): {exc}")
        return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(download_one, range(count)))

    return [r for r in results if r]


def set_clipboard_text(serial: str, text: str) -> None:
    candidates = [
        ("cmd", "clipboard", "set", text),
        ("cmd", "clipboard", "set", f"'{text}'"),
        ("cmd", "clipboard", "set", "text", text),
    ]
    last_error: Exception | None = None
    for args in candidates:
        try:
            adb_shell(serial, *args, check=False, timeout=10)
            return
        except Exception as exc:
            last_error = exc
    if last_error:
        raise AdbError(f"Không đặt được clipboard: {last_error}")


def paste_text(serial: str, text: str) -> None:
    try:
        set_clipboard_text(serial, text)
        time.sleep(0.2)
        keyevent(serial, 279)
    except Exception:
        input_text(serial, text)


def replace_focused_text(serial: str, text: str, clear_presses: int = 120) -> None:
    try:
        keyevent(serial, 123)
    except Exception:
        pass
    for _ in range(max(1, clear_presses)):
        try:
            keyevent(serial, 67)
        except Exception:
            break
    time.sleep(0.2)
    input_text(serial, text)


def parse_proxy_spec(proxy: str) -> tuple[str, str, str, str]:
    parts = [part.strip() for part in proxy.strip().split(":", 3)]
    while len(parts) < 4:
        parts.append("")
    host, port, username, password = parts[:4]
    if not host or not port:
        raise AdbError("Proxy phải có dạng host:port hoặc host:port:user:pass.")
    if username.lower() in {"random", "trống", "trong", "none", "null"}:
        username = ""
    if password.lower() in {"random", "trống", "trong", "none", "null"}:
        password = ""
    return host, port, username, password


def _center_from_bounds(bounds: str) -> tuple[int, int] | None:
    match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
    if not match:
        return None
    left, top, right, bottom = map(int, match.groups())
    return (left + right) // 2, (top + bottom) // 2


def tap_node(serial: str, node: ET.Element) -> bool:
    bounds = node.attrib.get("bounds", "")
    center = _center_from_bounds(bounds)
    if not center:
        return False
    tap(serial, center[0], center[1])
    return True


def _click_add_friend_items(serial: str, desired_count: int, max_swipes: int = 6) -> int:
    """Click 'Add friend' buttons until desired_count is reached, swiping up to load more.

    Returns the number of successful clicks performed.
    """
    clicked = 0
    seen_centers: set[tuple[int, int]] = set()
    swipes = 0

    while clicked < desired_count and swipes <= max_swipes:
        try:
            xml_text = get_xml_dump(serial)
            if not xml_text:
                break
            root = ET.fromstring(xml_text)
        except Exception:
            break

        centers: list[tuple[int, int]] = []
        for node in root.iter("node"):
            text = (node.attrib.get("text") or "").strip()
            desc = (node.attrib.get("content-desc") or "").strip()
            if text.lower() == "add friend" or desc.lower() == "add friend":
                center = _center_from_bounds(node.attrib.get("bounds", ""))
                if center and center not in seen_centers:
                    centers.append(center)

        if centers:
            for cx, cy in centers:
                if clicked >= desired_count:
                    break
                try:
                    tap(serial, cx, cy)
                    seen_centers.add((cx, cy))
                    clicked += 1
                    time.sleep(0.8)
                except Exception:
                    continue
        else:
            # nothing found on this screen
            pass

        if clicked >= desired_count:
            break

        # need more: swipe up and try again
        swipe(serial, "up")
        time.sleep(3)
        swipes += 1

    return clicked


def _find_launchable_package(serial: str, keywords: tuple[str, ...]) -> str:
    output = adb_shell(serial, "pm", "list", "packages", check=False, timeout=15)
    packages = [line.split(":", 1)[1].strip() for line in output.splitlines() if line.startswith("package:")]
    lowered_keywords = tuple(keyword.lower() for keyword in keywords)
    for package in packages:
        lower = package.lower()
        if any(keyword in lower for keyword in lowered_keywords):
            return package
    raise AdbError("Không tìm thấy package proxy phù hợp trên máy.")


def _resolve_launcher_component(serial: str, package: str) -> str:
    output = adb_shell(
        serial, "cmd", "package", "resolve-activity", "--brief", "-a", "android.intent.action.MAIN",
        "-c", "android.intent.category.LAUNCHER", package, check=False, timeout=15,
    )
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in reversed(lines):
        if "/" in line and not line.startswith("{") and not line.startswith("priority="):
            return line
    return f"{package}/.Splash_Activity"


def _launch_proxy_app(serial: str, package: str = "", force_restart: bool = True) -> str:
    if not package:
        package = _find_launchable_package(serial, ("college", "proxy"))
    if force_restart:
        adb_shell(serial, "am", "force-stop", package, check=False, timeout=10)
        time.sleep(0.5)
        try:
            adb_shell(serial, "input", "keyevent", "3", check=False, timeout=5)
        except Exception:
            pass
    component = _resolve_launcher_component(serial, package)
    try:
        adb_shell(serial, "am", "start", "-n", component, check=False, timeout=10)
    except Exception:
        adb_shell(serial, "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1", check=False, timeout=10)
    return package


def _fill_proxy_form(serial: str, host: str, port: str, username: str = "", password: str = "") -> None:
    """
    Kiểm tra và điền thông tin proxy vào College Proxy:
    - Nhận diện chính xác 4 ô nhập EditText: Host, Port, Username, Password.
    - So khớp ĐỦ cả 4 trường (Host, Port, Username, Password).
    - Nếu trùng khớp 100% -> Bấm Start (hoặc giữ nguyên nếu đã Connected).
    - Nếu khác hoặc thiếu User/Pass -> Dừng service cũ nếu có, xóa sạch và điền đủ 4 trường rồi bấm Start.
    """
    target_host = (host or "").strip()
    target_port = (port or "").strip()
    target_user = (username or "").strip()
    target_pass = (password or "").strip()

    # 1. SỬ DỤNG UIAUTOMATOR2 ĐỂ XỬ LÝ CHÍNH XÁC
    try:
        import uiautomator2 as u2
        d = u2.connect(serial)

        # Chờ app hết màn hình Loading... và hết popup "Please wait..." (tối đa 10s)
        count_edits = 0
        for _ in range(10):
            if d(textMatches=r"(?i).*(please wait|loading\.\.\.).*").exists:
                time.sleep(1.0)
                continue
            all_edits = d(className="android.widget.EditText")
            count_edits = all_edits.count
            if count_edits >= 2:
                break
            time.sleep(1.0)

        all_edits = d(className="android.widget.EditText")
        count_edits = all_edits.count

        if count_edits >= 2:
            addr_box = all_edits[0]
            port_box = all_edits[1]
            user_box = all_edits[2] if count_edits > 2 else None
            pass_box = all_edits[3] if count_edits > 3 else None

            # Đọc giá trị hiện có trên từng ô
            cur_host = (addr_box.get_text() or "").strip() if addr_box and addr_box.exists else ""
            cur_port = (port_box.get_text() or "").strip() if port_box and port_box.exists else ""
            cur_user = (user_box.get_text() or "").strip() if user_box and user_box.exists else ""
            cur_pass = (pass_box.get_text() or "").strip() if pass_box and pass_box.exists else ""

            # Lọc bỏ placeholder / hint text của Android
            if cur_user.lower() in ("optional", "tùy chọn", "username", "tên đăng nhập"): cur_user = ""
            if cur_pass.lower() in ("optional", "tùy chọn", "password", "mật khẩu"): cur_pass = ""
            if cur_host.lower() in ("optional", "tùy chọn", "proxy ip", "ip", "host"): cur_host = ""
            if cur_port.lower() in ("optional", "tùy chọn", "proxy port", "port", "cổng"): cur_port = ""

            # Kiểm tra xem app đã có đủ 4 thông số khớp với proxy được gán chưa
            matches_assigned = False
            if target_host and target_port:
                if cur_host == target_host and cur_port == target_port and cur_user == target_user and cur_pass == target_pass:
                    matches_assigned = True
            elif not target_host and not target_port:
                if cur_host and cur_port:
                    matches_assigned = True

            start_btn = d(resourceIdMatches=r".*id/proxy_start_button.*")
            if not start_btn.exists:
                start_btn = d(textMatches=r"(?i)(start proxy service|stop proxy service|start service|stop service)")

            btn_text = (start_btn.get_text() or "").strip().upper() if start_btn.exists else ""

            if matches_assigned:
                print(f"[{serial}] ✓ Proxy trong app ({cur_host}:{cur_port} | user='{cur_user}' | pass='{cur_pass}') ĐÃ KHỚP với proxy được gán.")
                if "STOP" in btn_text:
                    print(f"[{serial}] ✓ College Proxy đang ở trạng thái Connected -> Giữ nguyên kết nối.")
                elif start_btn.exists:
                    start_btn.click()
                else:
                    d.press("enter")
                return

            # NẾU KHÁC HOẶC THIẾU THÔNG TIN -> XỬ LÝ ĐIỀN ĐẦY ĐỦ
            print(f"[{serial}] 🔄 Proxy trong app ({cur_host}:{cur_port} | user='{cur_user}' | pass='{cur_pass}') KHÁC proxy được gán ({target_host}:{target_port} | user='{target_user}' | pass='{target_pass}') -> Đang điền...")

            # Nếu đang bật kết nối cũ (STOP PROXY SERVICE) -> bấm Stop trước để mở khóa ô nhập
            if "STOP" in btn_text and start_btn.exists:
                start_btn.click()
                time.sleep(1.0)

            # Điền Host
            if cur_host != target_host and addr_box and addr_box.exists:
                if target_host:
                    addr_box.set_text(target_host)
                else:
                    addr_box.clear_text()
                time.sleep(0.2)

            # Điền Port
            if cur_port != target_port and port_box and port_box.exists:
                if target_port:
                    port_box.set_text(target_port)
                else:
                    port_box.clear_text()
                time.sleep(0.2)

            # Điền Username
            if user_box and user_box.exists:
                if target_user:
                    user_box.set_text(target_user)
                else:
                    user_box.clear_text()
                time.sleep(0.2)

            # Điền Password
            if pass_box and pass_box.exists:
                if target_pass:
                    pass_box.set_text(target_pass)
                else:
                    pass_box.clear_text()
                time.sleep(0.2)

            time.sleep(0.5)
            # Bấm nút Start Proxy Service
            start_btn = d(resourceIdMatches=r".*id/proxy_start_button.*")
            if not start_btn.exists:
                start_btn = d(textMatches=r"(?i)(start proxy service|start service|start)")
            if start_btn.exists:
                start_btn.click()
                print(f"[{serial}] ✓ Đã điền đầy đủ Host:Port:User:Pass ({target_host}:{target_port}:{target_user}) và bấm Start Proxy Service.")
            else:
                d.press("enter")
            return
    except Exception as exc:
        print(f"[{serial}] Thao tác qua uiautomator2 gặp lỗi: {exc} -> Chuyển sang fallback XML/ADB...")

    # 2. FALLBACK QUÉT XML DUMP VÀ GỬI LỆNH ADB
    edit_boxes: list[tuple[int, int]] = []
    field_centers: dict[str, tuple[int, int]] = {}
    field_texts: dict[str, str] = {}
    start_button: tuple[int, int] | None = None

    for attempt in range(6):
        xml_text = get_xml_dump(serial) or ""
        if not xml_text:
            time.sleep(1.0)
            continue
        try:
            root = ET.fromstring(xml_text)
        except Exception:
            time.sleep(1.0)
            continue

        edit_boxes.clear()
        field_centers.clear()
        field_texts.clear()
        start_button = None

        for node in root.iter("node"):
            text = (node.attrib.get("text") or "").strip()
            ltext = text.lower()
            class_name = (node.attrib.get("class") or "").strip().lower()
            resource_id = (node.attrib.get("resource-id") or "").strip().lower()
            bounds = node.attrib.get("bounds") or ""
            center = _center_from_bounds(bounds)
            if not center:
                continue

            if resource_id.endswith("id/edittext_address"):
                field_centers["host"] = center
                field_texts["host"] = text
                continue
            if resource_id.endswith("id/edittext_port"):
                field_centers["port"] = center
                field_texts["port"] = text
                continue
            if resource_id.endswith("id/edittext_username"):
                field_centers["username"] = center
                field_texts["username"] = text
                continue
            if resource_id.endswith("id/edittext_password"):
                field_centers["password"] = center
                field_texts["password"] = text
                continue
            if resource_id.endswith("id/proxy_start_button"):
                start_button = center
                continue
            if "edittext" in class_name:
                edit_boxes.append(center)
                continue
            if not start_button and text and ("start proxy service" in ltext or ltext == "start" or "start service" in ltext or "stop proxy service" in ltext):
                start_button = center

        if field_centers or edit_boxes or start_button:
            break
        time.sleep(1.2)

    # Sắp xếp các ô EditText theo tọa độ Y từ trên xuống dưới
    edit_boxes.sort(key=lambda c: c[1])
    if len(edit_boxes) >= 4:
        if "host" not in field_centers: field_centers["host"] = edit_boxes[0]
        if "port" not in field_centers: field_centers["port"] = edit_boxes[1]
        if "username" not in field_centers: field_centers["username"] = edit_boxes[2]
        if "password" not in field_centers: field_centers["password"] = edit_boxes[3]

    current_host = field_texts.get("host", "").strip()
    current_port = field_texts.get("port", "").strip()
    current_user = field_texts.get("username", "").strip()
    current_pass = field_texts.get("password", "").strip()
    if current_user.lower() in ("optional", "tùy chọn", "username"): current_user = ""
    if current_pass.lower() in ("optional", "tùy chọn", "password"): current_pass = ""

    matches_assigned = False
    if target_host and target_port:
        if current_host == target_host and current_port == target_port and current_user == target_user and current_pass == target_pass:
            matches_assigned = True
    elif not target_host and not target_port:
        if current_host and current_port:
            matches_assigned = True

    if matches_assigned:
        print(f"[{serial}] College Proxy đã có sẵn thông tin ({current_host}:{current_port}:{current_user}), bấm Start luôn.")
        if start_button:
            tap(serial, start_button[0], start_button[1])
        else:
            adb_shell(serial, "input", "keyevent", "66", check=False, timeout=5)
        return

    # Nếu khác hoặc thiếu -> Xóa sạch bằng cách Move End + Backspace loop và điền lại toàn bộ
    print(f"[{serial}] 🔄 Proxy trong app ({current_host}:{current_port}) KHÁC proxy được gán ({target_host}:{target_port}:{target_user}:{target_pass}) -> Xóa proxy cũ và dán proxy mới...")
    values = {
        "host": target_host,
        "port": target_port,
        "username": target_user,
        "password": target_pass,
    }

    ordered_keys = ["host", "port", "username", "password"]
    for index, key in enumerate(ordered_keys):
        value = values[key]
        target = field_centers.get(key)
        if target is None and index < len(edit_boxes):
            target = edit_boxes[index]
        if target is None:
            continue
        x, y = target
        tap(serial, x, y)
        time.sleep(0.2)
        
        # Di chuyển con trỏ về cuối ô và xóa sạch ký tự cũ
        adb_shell(serial, "input", "keyevent", "123", check=False, timeout=2) # KEYCODE_MOVE_END
        for _ in range(40):
            adb_shell(serial, "input", "keyevent", "67", check=False, timeout=1) # KEYCODE_DEL
        time.sleep(0.1)

        # Dán nội dung proxy mới
        if value:
            input_text(serial, value)
            time.sleep(0.2)

    if start_button:
        tap(serial, start_button[0], start_button[1])
    else:
        adb_shell(serial, "input", "keyevent", "66", check=False, timeout=5)


def _proxy_connection_ok(serial: str) -> bool:
    try:
        ping = adb_shell(serial, "ping", "-c", "1", "8.8.8.8", check=False, timeout=10)
        if "1 packets transmitted" in ping and ("1 received" in ping or "1 packets received" in ping):
            return True
        if "1 packets transmitted" in ping and "0 received" not in ping:
            return True
    except Exception:
        pass
    try:
        dump = get_xml_dump(serial).lower()
        return any(token in dump for token in ("connected", "running", "started", "service"))
    except Exception:
        return False


def _find_and_tap_button(serial: str, keywords: list[str]) -> bool:
    try:
        xml_text = get_xml_dump(serial)
        root = ET.fromstring(xml_text)
    except Exception:
        return False

    lowered_keywords = [k.lower() for k in keywords]
    candidates: list[tuple[int, bool, tuple[int, int], str, str]] = []
    for node in root.iter("node"):
        text = (node.attrib.get("text") or "").strip()
        ltext = text.lower()
        resource_id = (node.attrib.get("resource-id") or "").strip()
        lrid = resource_id.lower()
        content_desc = (node.attrib.get("content-desc") or "").strip()
        lcdesc = content_desc.lower()
        bounds = node.attrib.get("bounds") or ""
        center = _center_from_bounds(bounds)
        if not center:
            continue
        clickable = (node.attrib.get("clickable") or "false").lower() == "true"
        score = 0
        for k in lowered_keywords:
            if k == ltext or k == lrid or lrid.endswith(k) or k == lcdesc:
                score += 20
            elif k in ltext or k in lrid or k in lcdesc:
                score += 8
        if score > 0:
            candidates.append((score + (5 if clickable else 0), clickable, center, text, resource_id))

    if not candidates:
        btns: list[tuple[tuple[int, int], bool]] = []
        for node in root.iter("node"):
            cls = (node.attrib.get("class") or "").lower()
            bounds = node.attrib.get("bounds") or ""
            center = _center_from_bounds(bounds)
            if not center:
                continue
            clickable = (node.attrib.get("clickable") or "false").lower() == "true"
            if "button" in cls or cls.endswith("button"):
                btns.append((center, clickable))
        if btns:
            btns.sort(key=lambda b: 1 if b[1] else 0, reverse=True)
            tap(serial, btns[0][0][0], btns[0][0][1])
            time.sleep(0.6)
            return True
        return False

    candidates.sort(key=lambda t: (t[0], 1 if t[1] else 0), reverse=True)
    best = candidates[0]
    if not best[1]:
        bx, by = best[2]
        nearby_clickables: list[tuple[int, tuple[int, int]]] = []
        for node in root.iter("node"):
            cbounds = node.attrib.get("bounds") or ""
            ccenter = _center_from_bounds(cbounds)
            if not ccenter:
                continue
            cclick = (node.attrib.get("clickable") or "false").lower() == "true"
            if not cclick:
                continue
            dx = abs(ccenter[0] - bx)
            dy = abs(ccenter[1] - by)
            if dx <= 120 and dy <= 120:
                nearby_clickables.append((dx + dy, ccenter))
        if nearby_clickables:
            nearby_clickables.sort(key=lambda t: t[0])
            tap(serial, nearby_clickables[0][1][0], nearby_clickables[0][1][1])
            time.sleep(0.6)
            return True

    tap(serial, best[2][0], best[2][1])
    time.sleep(0.6)
    return True


def _screen_text(serial: str) -> str:
    parts: list[str] = []
    try:
        xml_text = get_xml_dump(serial)
        if xml_text:
            parts.append(xml_text)
    except Exception:
        pass
    try:
        ocr_text = _read_profile_ocr_text(serial)
        if ocr_text:
            parts.append(ocr_text)
    except Exception:
        pass
    return "\n".join(parts).lower()


def _tap_first_edit_field(serial: str, xml_text: str, keywords: tuple[str, ...] = ()) -> bool:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return False

    lowered_keywords = tuple(keyword.lower() for keyword in keywords)
    candidates: list[tuple[int, int, tuple[int, int]]] = []
    fallback: list[tuple[int, int, tuple[int, int]]] = []

    for node in root.iter("node"):
        class_name = (node.attrib.get("class") or "").strip().lower()
        text = (node.attrib.get("text") or "").strip().lower()
        desc = (node.attrib.get("content-desc") or "").strip().lower()
        resource_id = (node.attrib.get("resource-id") or "").strip().lower()
        bounds = node.attrib.get("bounds") or ""
        center = _center_from_bounds(bounds)
        if not center:
            continue

        is_editable = "edittext" in class_name or "android.widget.edittext" in class_name or "textinput" in class_name
        if not is_editable:
            continue

        score = 0
        for keyword in lowered_keywords:
            if keyword and (keyword in text or keyword in desc or keyword in resource_id):
                score += 10

        if "password" in text or "password" in desc or "password" in resource_id:
            score += 8
        if "email" in text or "email" in desc or "email" in resource_id or "phone" in text or "phone" in desc:
            score += 8
        if score > 0:
            candidates.append((score, center[1], center))
        else:
            fallback.append((0, center[1], center))

    pool = candidates or fallback
    if not pool:
        return False

    pool.sort(key=lambda item: (item[0], -item[1]), reverse=True)
    tap(serial, pool[0][2][0], pool[0][2][1])
    time.sleep(0.4)
    return True


def _set_text_via_field(serial: str, xml_text: str, text: str, keywords: tuple[str, ...] = (), clear_presses: int = 120) -> bool:
    if _tap_first_edit_field(serial, xml_text, keywords=keywords):
        try:
            replace_focused_text(serial, text, clear_presses=clear_presses)
            return True
        except Exception:
            pass
    return False


def _login_state_snapshot(serial: str) -> str:
    return _screen_text(serial)


def _open_facebook_and_reset(serial: str, uid: str = "", password: str = "") -> None:
    """Mở Facebook, đánh thức màn hình và thử điền form đăng nhập tự động nếu cần."""
    _safe_adb_shell(serial, "input", "keyevent", "224", timeout=10)
    _safe_adb_shell(serial, "input", "keyevent", "3", timeout=10)
    time.sleep(0.8)
    width, height = get_screen_size(serial)

    def _open_and_prepare() -> None:
        open_facebook(serial)
        time.sleep(2.5)
        if _looks_like_fb_welcome_screen(serial):
            print(f"[{serial}] Phat hien man hinh Welcome to Facebook.")

    _open_and_prepare()


def _tap_profile_copy_link(serial: str, xml_text: str, width: int, height: int) -> bool:
    """Hàm bổ sung: Tìm nút sao chép liên kết dựa vào cấu trúc XML và ấn vào."""
    if not xml_text:
        return False
    try:
        root = ET.fromstring(xml_text)
        for node in root.iter("node"):
            text = (node.attrib.get("text") or "").lower()
            desc = (node.attrib.get("content-desc") or "").lower()
            if any(k in text or k in desc for k in ("copy link", "sao chép liên kết", "copy profile link")):
                return tap_node(serial, node)
    except Exception:
        pass
    return False


def _read_clipboard_text(serial: str) -> str:
    parts: list[str] = []
    for command in (
        ("cmd", "clipboard", "get"),
        ("am", "broadcast", "-a", "clipper.get"),
        ("service", "call", "clipboard", "1"),
        ("service", "call", "clipboard", "2"),
        ("service", "call", "clipboard", "3"),
        ("content", "query", "--uri", "content://clipboard/clip"),
    ):
        try:
            parts.append(adb_shell(serial, *command, timeout=10, check=False))
        except Exception:
            parts.append("")
    return " ".join(parts)


def _try_copy_link_sweep(serial: str, width: int, height: int) -> str:
    x_candidates = [int(width * ratio) for ratio in (0.35, 0.45, 0.50, 0.55, 0.65)]
    y_candidates = [int(height * ratio) for ratio in (0.60, 0.64, 0.68, 0.72, 0.76)]

    for y in y_candidates:
        for x in x_candidates:
            tap(serial, x, y)
            time.sleep(0.7)
            clipboard = _read_clipboard_text(serial)
            clip_match = re.search(r'(https?://[^\s\)]+)', clipboard)
            if clip_match:
                return clipboard
            if re.search(r'id=(\d{10,20})', clipboard) or re.search(r'(1000\d{11}|615\d{11,12}|\d{14,20})', clipboard):
                return clipboard
    return ""


def _capture_device_screenshot(serial: str) -> Path | None:
    local_path = Path(tempfile.gettempdir()) / f"fb_profile_{serial}_{int(time.time() * 1000)}.png"
    remote_path = f"/sdcard/{local_path.name}"
    try:
        adb_shell(serial, "screencap", "-p", remote_path, check=False, timeout=20)
        _adb("-s", serial, "pull", remote_path, str(local_path), check=False, timeout=30)
        try:
            adb_shell(serial, "rm", remote_path, check=False, timeout=10)
        except Exception:
            pass
        if local_path.exists() and local_path.stat().st_size > 0:
            return local_path
    except Exception:
        pass
    try:
        local_path.unlink(missing_ok=True)
    except Exception:
        pass
    return None


def _read_profile_ocr_text(serial: str) -> str:
    screenshot_path = _capture_device_screenshot(serial)
    if not screenshot_path:
        return ""

    try:
        from rapidocr_onnxruntime import RapidOCR

        ocr = RapidOCR()
        result, _ = ocr(str(screenshot_path))
        if not result:
            return ""
        lines: list[str] = []
        for item in result:
            if len(item) >= 2 and item[1]:
                lines.append(str(item[1]))
        return "\n".join(lines)
    except Exception:
        return ""
    finally:
        try:
            screenshot_path.unlink(missing_ok=True)
        except Exception:
            pass


def _extract_fb_name_from_ocr(serial: str) -> str:
    screenshot_path = _capture_device_screenshot(serial)
    if not screenshot_path:
        return ""

    stop_phrases = (
        "profile settings", "follow settings", "profile status", "archive", "view as",
        "lock profile", "activity log", "manage posts", "review posts and tags", "privacy center",
        "search", "turn on professional mode", "share profile", "your profile link",
        "your personalized link on facebook", "add cover photo", "thinking about", "add to story",
        "edit profile", "people you may know", "friends", "bạn bè", "người theo dõi", "tùy chọn",
        "chỉnh sửa", "profile", "story", "like", "comment", "share",
    )

    try:
        from rapidocr_onnxruntime import RapidOCR

        ocr = RapidOCR()
        result, _ = ocr(str(screenshot_path))
        if not result:
            return ""

        candidates: list[tuple[int, int, str]] = []
        for item in result:
            if len(item) < 2:
                continue
            text = str(item[1]).strip()
            if not text:
                continue
            box = item[0]
            try:
                y1 = int(box[0][1])
                y2 = int(box[2][1])
            except Exception:
                continue

            lower = text.lower().strip()
            if y1 < 650 or y1 > 1100:
                continue
            if len(text) < 2 or len(text) > 40:
                continue
            if not any(ch.isalpha() for ch in text):
                continue
            if any(phrase in lower for phrase in stop_phrases):
                continue
            if re.search(r"\d", text):
                continue
            if any(symbol in text for symbol in ("/", "http", "://", "@", "#", "|")):
                continue

            candidates.append((y1, y2, text))

        if not candidates:
            return ""

        candidates.sort(key=lambda item: (item[0], len(item[2])), reverse=True)
        return candidates[0][2].strip()
    except Exception:
        return ""
    finally:
        try:
            screenshot_path.unlink(missing_ok=True)
        except Exception:
            pass


def _looks_like_empty_facebook_screen(serial: str) -> bool:
    def _matches(text: str) -> bool:
        lower = text.lower()
        home_markers = (
            "what's on your mind",
            "what’s on your mind",
            "on your mind",
            "menu, tab",
            "home, tab",
            "friends, tab",
            "notifications, tab",
            "create story",
            "see translation",
            "see more",
            " follow",
        )
        if any(marker in lower for marker in home_markers):
            return False

        has_account_field = (
            "mobile number or email" in lower
            or "email address or mobile number" in lower
            or "phone number or email" in lower
        )
        has_password = "password" in lower
        has_login_action = "log in" in lower or "login" in lower
        has_create_account = "create new account" in lower or "join facebook" in lower
        has_recovery = (
            "forgot password" in lower
            or "forgotten password" in lower
            or "try another way" in lower
            or "this is my account" in lower
            or "we'll send you a code to your email" in lower
            or "send you a code to your email" in lower
        )

        return (has_account_field and (has_password or has_login_action or has_create_account)) or (
            has_recovery and (has_account_field or has_password or has_login_action)
        )

    try:
        xml_text = get_xml_dump(serial)
    except Exception:
        xml_text = ""

    if xml_text and _matches(xml_text):
        return True

    return False


def _facebook_human_confirm_message(serial: str) -> str:
    def _match(value: str) -> str:
        text = (value or "").strip()
        lower = text.lower()
        if "confirm you're human" in lower and "use your account" in lower:
            return re.sub(r"\s+", " ", text)
        return ""

    try:
        xml_text = get_xml_dump(serial)
    except Exception:
        xml_text = ""

    if xml_text:
        try:
            root = ET.fromstring(xml_text)
            for node in root.iter("node"):
                for attr in ("text", "content-desc"):
                    matched = _match(node.attrib.get(attr, ""))
                    if matched:
                        return matched
        except Exception:
            matched = _match(xml_text)
            if matched:
                return matched

    try:
        ocr_text = _read_profile_ocr_text(serial)
    except Exception:
        ocr_text = ""
    return _match(ocr_text)


def open_facebook_and_reset(serial: str) -> None:
    _open_facebook_and_reset(serial)

def _find_text_bounds_by_ocr(serial: str, keywords: tuple[str, ...]) -> tuple[int, int, int, int] | None:
    """Locate a text region on screen by OCR and return its bounding box."""
    screenshot_path = _capture_device_screenshot(serial)
    if not screenshot_path:
        return None

    try:
        from rapidocr_onnxruntime import RapidOCR

        ocr = RapidOCR()
        result, _ = ocr(str(screenshot_path))
        if not result:
            return None

        candidates: list[tuple[int, int, int, int]] = []
        for item in result:
            if len(item) < 2 or not item[1]:
                continue
            text = str(item[1]).strip()
            if not text:
                continue
            lower = text.lower()
            if not any(keyword in lower for keyword in keywords):
                continue

            box = item[0]
            try:
                xs = [int(point[0]) for point in box]
                ys = [int(point[1]) for point in box]
            except Exception:
                continue
            if not xs or not ys:
                continue
            candidates.append((min(xs), min(ys), max(xs), max(ys)))

        if not candidates:
            return None

        candidates.sort(key=lambda rect: (rect[2] - rect[0]) * (rect[3] - rect[1]), reverse=True)
        return candidates[0]
    except Exception:
        return None
    finally:
        try:
            screenshot_path.unlink(missing_ok=True)
        except Exception:
            pass
        
def _create_ellipsis_template(orientation: str, radius: int, spacing: int) -> Any:
    import cv2
    import numpy as np

    if orientation == "vertical":
        width = radius * 2 + 12
        height = radius * 6 + spacing * 2 + 12
        centers = [
            (width // 2, 6 + radius),
            (width // 2, 6 + radius + spacing + radius * 2),
            (width // 2, 6 + radius + 2 * (spacing + radius * 2)),
        ]
    else:
        width = radius * 6 + spacing * 2 + 12
        height = radius * 2 + 12
        centers = [
            (6 + radius, height // 2),
            (6 + radius + spacing + radius * 2, height // 2),
            (6 + radius + 2 * (spacing + radius * 2), height // 2),
        ]

    template = np.full((height, width), 255, dtype=np.uint8)
    for cx, cy in centers:
        cv2.circle(template, (cx, cy), radius, 0, -1)
    return template

def _find_ellipsis_icon_in_roi(roi_image: Image.Image) -> tuple[int, int] | None:
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None

    gray = np.array(roi_image.convert("L"))
    gray = cv2.GaussianBlur(gray, (5, 5), 0)

    best_score = 0.0
    best_center: tuple[int, int] | None = None
    for orientation in ("vertical", "horizontal"):
        for radius, spacing in ((3, 8), (4, 10), (5, 12)):
            template = _create_ellipsis_template(orientation, radius, spacing)
            if gray.shape[0] < template.shape[0] or gray.shape[1] < template.shape[1]:
                continue
            for inverted in (False, True):
                target = 255 - gray if inverted else gray
                result = cv2.matchTemplate(target, template, cv2.TM_CCOEFF_NORMED)
                _, max_val, _, max_loc = cv2.minMaxLoc(result)
                if max_val > best_score:
                    best_score = max_val
                    best_center = (max_loc[0] + template.shape[1] // 2, max_loc[1] + template.shape[0] // 2)

    if best_score >= 0.45:
        return best_center
    return None

def _tap_ellipsis_near_edit_profile(serial: str, ref_bounds: tuple[int, int, int, int]) -> bool:
    screenshot_path = _capture_device_screenshot(serial)
    if not screenshot_path:
        return False

    try:
        image = Image.open(str(screenshot_path))
        x1, y1, x2, y2 = ref_bounds
        crop_left = max(0, x2)
        crop_top = max(0, y1 - 20)
        crop_right = min(image.width, x2 + 150)
        crop_bottom = min(image.height, y2 + 20)
        if crop_left >= crop_right or crop_top >= crop_bottom:
            return False

        roi = image.crop((crop_left, crop_top, crop_right, crop_bottom))
        found = _find_ellipsis_icon_in_roi(roi)
        if not found:
            return False

        tap(serial, crop_left + found[0], crop_top + found[1])
        return True
    except Exception:
        return False
    finally:
        try:
            screenshot_path.unlink(missing_ok=True)
        except Exception:
            pass

#Goi ý thêm bạn bè
def get_fb_info(serial: str, extra_delay: float = 0.0, return_home: bool = True) -> tuple[str, str]:
    open_facebook_and_reset(serial)
    width, height = get_screen_size(serial)
    fb_uid = "Trống"
    fb_name = "Trống"

    human_confirm_status = _facebook_human_confirm_message(serial)
    if human_confirm_status:
        print(f"[{serial}] {human_confirm_status}")
        raise FbInfoStatus(human_confirm_status)

    if _looks_like_empty_facebook_screen(serial):
        print(f"[{serial}] Facebook dang o man login/recovery, coi nhu chua co tai khoan dang nhap.")
        return fb_uid, fb_name

    # --- BƯỚC 1: ẤN VÀO DẤU 3 GẠCH (MENU) ---
    print(f"[{serial}] Bước 1: Tìm và ấn icon 3 gạch (Menu)...")
    xml_home = get_xml_dump(serial)
    menu_clicked = False

    if xml_home:
        try:
            root = ET.fromstring(xml_home)
            for node in root.iter('node'):
                desc = node.attrib.get('content-desc', '').lower()
                text = node.attrib.get('text', '').lower()
                if "menu, tab" in desc or desc == "menu" or desc == "trình đơn":
                    menu_clicked = tap_node(serial, node)
                    break
        except Exception:
            pass

    if not menu_clicked:
        tap(serial, int(width * 0.9), int(height * 0.1))
    time.sleep(6 + extra_delay)

    # --- BƯỚC 2: BẤM VÀO TÊN TÀI KHOẢN ---
    print(f"[{serial}] Bước 2: Bấm vào tên để vào trang cá nhân...")
    xml_menu = get_xml_dump(serial)
    name_clicked = False

    if xml_menu:
        try:
            root_menu = ET.fromstring(xml_menu)
            for node in root_menu.iter('node'):
                desc = node.attrib.get('content-desc', '').lower()
                text = node.attrib.get('text', '').lower()
                if "xem trang cá nhân" in desc or "see your profile" in desc or "xem trang cá nhân" in text or "see your profile" in text:
                    name_clicked = tap_node(serial, node)
                    break
        except Exception:
            pass

    if not name_clicked:
        tap(serial, int(width * 0.5), int(height * 0.15))
    time.sleep(6 + extra_delay)

    # --- BƯỚC 3: LẤY TÊN FACEBOOK & BẤM 3 CHẤM ---
    print(f"[{serial}] Bước 3: Đọc Tên Facebook và bấm dấu 3 chấm (...) ...")
    time.sleep(3 + extra_delay)
    xml_profile = get_xml_dump(serial)
    dots_clicked = False

    if xml_profile:
        try:
            root_profile = ET.fromstring(xml_profile)
            texts_info = []
            ref_bounds = None

            for node in root_profile.iter('node'):
                text = node.attrib.get('text', '').strip()
                desc = node.attrib.get('content-desc', '').strip()
                bounds_str = node.attrib.get('bounds', '')
                val = text if text else desc
                val_lower = val.lower()

                m = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds_str)
                if m:
                    x1, y1, x2, y2 = map(int, m.groups())
                    if val:
                        texts_info.append({"text": val, "y1": y1, "y2": y2})

                    if val_lower in ["add to story", "thêm vào tin", "edit profile", "chỉnh sửa trang cá nhân"]:
                        if not ref_bounds:
                            ref_bounds = (x1, y1, x2, y2)
                        elif y1 < ref_bounds[1] + 10:
                            ref_bounds = (x1, y1, x2, y2)

            if ref_bounds:
                candidate_names = []
                for item in texts_info:
                    if item["y2"] <= ref_bounds[1] + 10:
                        candidate_names.append(item)

                candidate_names.sort(key=lambda x: x["y2"], reverse=True)
                for item in candidate_names:
                    t = item["text"]
                    tl = t.lower()
                    if t.isdigit() or len(t) < 2 or "..." in t:
                        continue
                    ignore_kws = ['friends', 'bạn bè', 'chỉnh sửa', 'người theo dõi', 'tùy chọn', 'cover', 'ảnh bìa', 'profile', 'story','see your about info', 'xem thông tin cá nhân', 'add to story', 'thêm vào tin', 'edit profile', 'chỉnh sửa trang cá nhân']
                    if any(kw in tl for kw in ignore_kws):
                        continue
                    fb_name = t
                    break

            if fb_name == "Trống":
                ocr_name = _extract_fb_name_from_ocr(serial)
                if ocr_name:
                    fb_name = ocr_name
                    print(f"[{serial}] OCR đọc được tên Facebook: {fb_name}")

            if not ref_bounds:
                ref_bounds = _find_text_bounds_by_ocr(
                    serial,
                    (
                        "edit profile",
                        "chỉnh sửa trang cá nhân",
                        "thêm vào tin",
                        "chỉnh sửa",
                    ),
                )

            if ref_bounds:
                if _tap_ellipsis_near_edit_profile(serial, ref_bounds):
                    dots_clicked = True
                    print(f"[{serial}] Đã click 3 chấm bằng template matching gần Edit Profile")
                else:
                    _, y1, _, y2 = ref_bounds
                    cy = (y1 + y2) // 2
                    tap(serial, int(width * 0.91), cy)
                    dots_clicked = True
                    print(f"[{serial}] Không tìm được icon template, đã fallback click tại ({int(width*0.91)}, {cy})")

        except Exception as e:
            print(f"[{serial}] Lỗi bắt tọa độ: {e}")

    if fb_name == "Trống":
        ocr_name = _extract_fb_name_from_ocr(serial)
        if ocr_name:
            fb_name = ocr_name
            print(f"[{serial}] OCR đọc được tên Facebook: {fb_name}")

    if not dots_clicked:
        tap(serial, int(width * 0.91), int(height * 0.55))
    time.sleep(5 + extra_delay)

    print(f"[{serial}] Bước 4: Trích xuất UID từ link...")
    _safe_adb_shell(serial, "input", "keyevent", "224", timeout=10)

    time.sleep(3 + extra_delay)
    xml_settings = get_xml_dump(serial)

    ocr_text = _read_profile_ocr_text(serial)
    if ocr_text:
        m_ocr_url = re.search(r'https?://[^\s\)\"]+', ocr_text)
        if m_ocr_url:
            ocr_url = m_ocr_url.group(0)
            m_ocr_uid = re.search(r'id=(\d{10,20})', ocr_url) or re.search(r'(1000\d{11}|615\d{11,12}|\d{14,20})', ocr_url)
            if m_ocr_uid:
                fb_uid = m_ocr_uid.group(1)
                if return_home:
                    _safe_adb_shell(serial, "input", "keyevent", "3", timeout=10)
                return fb_uid, fb_name

    m_id = re.search(r'id=*(\d{10,20})', xml_settings)
    if m_id:
        fb_uid = m_id.group(1)
    else:
        copy_btn_pattern = r'(?:text|content-desc)="[^"]*(copy link|sao chép liên kết|copy profile link)[^"]*".*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]'
        copy_match = re.search(copy_btn_pattern, xml_settings, re.IGNORECASE)
        time.sleep(2)

        if fb_uid == "Trống":
            sweep_clipboard = _try_copy_link_sweep(serial, width, height)
            if sweep_clipboard:
                full_clipboard = sweep_clipboard

        time.sleep(1)
        tap_x, tap_y = int(width * 0.5), int(height * 0.68)

        if copy_match:
            bx1, by1, bx2, by2 = map(int, copy_match.groups()[1:5])
            tap_x, tap_y = (bx1 + bx2) // 2, (by1 + by2) // 2
        else:
            link_block_pattern = r'(?:text|content-desc)="[^"]*(profile link|liên kết trang cá nhân)[^"]*".*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]'
            block_match = re.search(link_block_pattern, xml_settings, re.IGNORECASE)
            if block_match:
                bx1, by1, bx2, by2 = map(int, block_match.groups()[1:5])
                tap_x, tap_y = (bx1 + bx2) // 2, by2 - int((by2 - by1) * 0.15)

        if not _tap_profile_copy_link(serial, xml_settings, width, height):
            tap(serial, tap_x, tap_y)
        time.sleep(3)

        search_match = re.search(r'text="[^"]*?(search|tìm kiếm)[^"]*?".*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', xml_settings, re.IGNORECASE)
        clip_6 = ""
        if search_match:
            sx1, sy1, sx2, sy2 = map(int, search_match.groups()[1:5])
            tap(serial, (sx1 + sx2) // 2, (sy1 + sy2) // 2)
            time.sleep(2)
            tap(serial, int(width * 0.5), int(height * 0.1))
            time.sleep(1)
            keyevent(serial, 279)
            time.sleep(1.5)
            xml_pasted = get_xml_dump(serial)
            if xml_pasted:
                m_pasted = re.search(r'(https?://[^\s\("<]+|1000\d{11}|615\d{11,12})', xml_pasted)
                if m_pasted:
                    clip_6 = m_pasted.group(1)
            keyevent(serial, 4)
            time.sleep(1)

        clipboard_probe = _read_clipboard_text(serial)
        full_clipboard = clipboard_probe if clipboard_probe else clip_6
        clean_text = full_clipboard.replace(".", "").replace("'", "").replace("\x00", "")

    m_link = re.search(r'(https?://[^\s\)]+)', clip_6) if clip_6 and "http" in clip_6 else re.search(r'(https?://[^\s\)]+)', full_clipboard)

    if m_link and "facebook.com" in m_link.group(1):
        share_link = m_link.group(1)
        try:
            import importlib
            selenium = importlib.import_module("selenium")
            webdriver = selenium.webdriver
            Service = importlib.import_module("selenium.webdriver.chrome.service").Service
            ChromeDriverManager = importlib.import_module("webdriver_manager.chrome").ChromeDriverManager
            Options = importlib.import_module("selenium.webdriver.chrome.options").Options

            options = Options()
            options.add_argument('--headless')
            options.add_argument('--disable-gpu')
            options.add_argument('--no-sandbox')
            options.add_argument('--log-level=3')

            service = Service(ChromeDriverManager().install())
            driver = webdriver.Chrome(service=service, options=options)
            driver.set_page_load_timeout(30)
            driver.get(share_link)
            time.sleep(3)

            final_url = driver.current_url
            html_content = driver.page_source
            driver.quit()

            m_final = re.search(r'(?:id=|\/)(\d{10,20})', final_url)
            if m_final:
                fb_uid = m_final.group(1)
            else:
                m_html = re.search(r'"userID"\s*:\s*"(\d{10,20})"', html_content) or re.search(r'fb://profile/(\d{10,20})', html_content)
                if m_html:
                    fb_uid = m_html.group(1)
        except Exception:
            try:
                startupinfo = subprocess.STARTUPINFO() if os.name == "nt" else None
                if startupinfo:
                    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                curl_out = subprocess.check_output(
                    ["curl", "-s", "-L", "-A", "Mozilla/5.0", share_link], timeout=15, startupinfo=startupinfo
                ).decode('utf-8', errors='ignore')
                m_html = re.search(r'"userID"\s*:\s*"(\d{10,20})"', curl_out) or re.search(r'fb://profile/(\d{10,20})', curl_out)
                if m_html:
                    fb_uid = m_html.group(1)
            except Exception:
                pass

    if fb_uid == "Trống":
        m = re.search(r'id=(\d{10,20})', full_clipboard) or re.search(r'id=(\d{10,20})', clean_text)
        if m:
            fb_uid = m.group(1)
        else:
            nums = re.findall(r'(1000\d{11}|615\d{11,12}|\d{14,20})', full_clipboard) or re.findall(r'(1000\d{11}|615\d{11,12}|\d{14,20})', clean_text)
            if nums:
                fb_uid = nums[-1]
            else:
                m2 = re.search(r'facebook\.com/([A-Za-z0-9._\-]+)', full_clipboard)
                if m2 and not any(k in m2.group(1) for k in ("profile.php", "help", "share")):
                    fb_uid = m2.group(1)

    if fb_uid != "Trống":
        fb_uid = re.sub(r'[^A-Za-z0-9._\-]', '', fb_uid)

    if return_home and not (_is_blank_fb_value(fb_uid) and _is_blank_fb_value(fb_name)):
        _safe_adb_shell(serial, "input", "keyevent", "3", timeout=10)

    return fb_uid, fb_name

# import time
# import subprocess

# def follow_facebook_page(serial: str, url: str) -> str:
#     """
#     Mở trang Facebook và click thẳng vào tọa độ dịch xuống phía dưới
#     để né ảnh đại diện (Avatar) và trúng nút Follow/Like.
#     """
#     # 1. Mở liên kết Facebook Page
#     open_link(serial, url)
    
#     # 2. Chờ trang tải xong (Giữ nguyên màn hình cố định, KHÔNG VUỐT)
#     time.sleep(6) 

#     # 3. TỌA ĐỘ ĐÃ DỊCH XUỐNG (Né avatar, trúng tâm nút xanh)
#     X_COORD = 232
#     Y_COORD = 1140  # Đã hạ từ 575 xuống 690 để né rìa ảnh đại diện

#     try:
#         # 4. Thực hiện lệnh bấm giữ 150ms chống chặn
#         cmd = f"adb -s {serial} shell input swipe {X_COORD} {Y_COORD} {X_COORD} {Y_COORD} 150"
#         subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
#         time.sleep(3.0)
#         return "action_executed"
        
#     except Exception as e:
#         print(f"Lỗi khi bấm trên thiết bị {serial}: {e}")
#         return "not_found"


import xml.etree.ElementTree as ET
import time
import re
import subprocess

def follow_facebook_page(serial: str, url: str) -> str:
    """Mở page Facebook, cố gắng bấm Follow, và chỉ trả về trạng thái kết quả.

    Hàm này không đóng Facebook sớm để tránh trường hợp app bị thoát trước khi
    nút Follow được bấm. Caller có thể tự đóng app nếu cần.
    """

    close_facebook(serial)
    time.sleep(1)
    open_facebook(serial)
    time.sleep(3)
    open_link(serial, url)
    time.sleep(8)

    try:
        d = ui2.connect(serial)
    except Exception:
        d = None

    width, height = get_screen_size(serial)

    def _normalize(text: str) -> str:
        text = unicodedata.normalize("NFD", text.lower()).replace("đ", "d")
        return "".join(ch for ch in text if unicodedata.category(ch) != "Mn")

    def _bounds_rect(bounds: str) -> tuple[int, int, int, int] | None:
        match = re.search(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", bounds or "")
        if not match:
            return None
        return tuple(map(int, match.groups()))

    def _center_from_rect(rect: tuple[int, int, int, int]) -> tuple[int, int]:
        left, top, right, bottom = rect
        return ((left + right) // 2, (top + bottom) // 2)

    def _node_label(node: ET.Element) -> str:
        parts = [
            (node.attrib.get("text") or "").strip(),
            (node.attrib.get("content-desc") or "").strip(),
            (node.attrib.get("resource-id") or "").strip(),
        ]
        return _normalize(" ".join(part for part in parts if part))

    follow_tokens = ("follow",)
    already_labels = {"following", "followed", "unfollow"}

    def _collect_targets(root: ET.Element, tokens: tuple[str, ...]) -> list[tuple[int, int, tuple[int, int]]]:
        targets: list[tuple[int, int, tuple[int, int]]] = []
        seen: set[tuple[int, int]] = set()
        parent_map = {child: parent for parent in root.iter("node") for child in parent}

        def _clickable_target(node: ET.Element) -> ET.Element:
            target = node
            parent = parent_map.get(node)
            while parent is not None:
                target_clickable = (target.attrib.get("clickable") or "false").lower() == "true"
                target_rect = _bounds_rect(target.attrib.get("bounds") or "")
                if target_clickable and target_rect:
                    break

                parent_clickable = (parent.attrib.get("clickable") or "false").lower() == "true"
                parent_rect = _bounds_rect(parent.attrib.get("bounds") or "")
                if parent_clickable and parent_rect:
                    target = parent
                    break
                if not target_rect and parent_rect:
                    target = parent
                parent = parent_map.get(parent)
            return target

        for node in root.iter("node"):
            label = _node_label(node)
            if not label:
                continue
            if label in already_labels:
                continue
            if not any(token in label for token in tokens):
                continue

            target = _clickable_target(node)
            rect = _bounds_rect(target.attrib.get("bounds") or "")
            if not rect:
                continue
            center = _center_from_rect(rect)
            if center in seen:
                continue
            seen.add(center)

            clickable = (target.attrib.get("clickable") or "false").lower() == "true"
            class_name = (target.attrib.get("class") or "").lower()
            score = 10 + (6 if clickable else 0)
            if "button" in class_name or class_name.endswith("button"):
                score += 4
            if target.attrib.get("resource-id"):
                score += 2
            if rect[2] - rect[0] >= 180:
                score += 2
            targets.append((score, 1 if clickable else 0, center))

        targets.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return targets

    def _xml_root() -> ET.Element | None:
        try:
            xml_text = get_xml_dump(serial)
            if not xml_text:
                return None
            return ET.fromstring(xml_text)
        except Exception:
            return None

    def _ui_click(label: str) -> bool:
        if d is None:
            return False
        for selector_args in (
            {"description": label},
            {"text": label},
            {"descriptionMatches": rf"^{re.escape(label)}$"},
            {"textMatches": rf"^{re.escape(label)}$"},
        ):
            try:
                widget = d(**selector_args)
                if widget.exists(timeout=1):
                    widget.click()
                    time.sleep(2.5)
                    return True
            except Exception:
                continue
        return False

    def _has_strong_already_state(root: ET.Element) -> bool:
        for node in root.iter("node"):
            label = _node_label(node)
            if label in already_labels:
                return True
        return False

    def _tap_follow_from_xml(root: ET.Element) -> bool:
        candidates = _collect_targets(root, follow_tokens)
        if not candidates:
            return False
        x, y = candidates[0][2]
        tap(serial, x, y)
        time.sleep(2.5)
        return True

    def _finish(result: str) -> str:
        try:
            close_facebook(serial)
            time.sleep(1)
        except Exception:
            pass
        return result

    for _ in range(10):
        root = _xml_root()
        if root is not None and _has_strong_already_state(root):
            return _finish("already_done")

        if _ui_click("Follow"):
            time.sleep(2.5)
            return _finish("action_executed")

        if root is not None and _tap_follow_from_xml(root):
            return _finish("action_executed")

        if width and height:
            try:
                if _ui_click("See more"):
                    time.sleep(1)
                    continue
            except Exception:
                pass

        time.sleep(1)

    return _finish("not_found")

def _read_news_in_google(serial: str, url: str) -> None:
    """Mở trực tiếp trang báo để đọc, mặc định là VnExpress."""
    target_url = (url or "").strip() or "https://vnexpress.net/"
    try:
        open_link(serial, target_url)
        time.sleep(5)
        try:
            d = ui2.connect(serial)
            vnexpress_desc = "VnExpress - Bao tieng Viet nhieu nguoi xem nhat"
            if d(description=vnexpress_desc).exists(timeout=5.0):
                tap(serial, 555, 1270)
                time.sleep(2)
                print(f"[{serial}] Đã thấy content-desc VnExpress và click tọa độ 555,1270")
                return
        except Exception as exc:
            print(f"[{serial}] Không quét được content-desc VnExpress sau khi mở link: {exc}")
        print(f"[{serial}] Đã mở trực tiếp trang báo: {target_url}")
    except Exception as exc:
        print(f"[{serial}] Lỗi khi mở trang báo trực tiếp: {exc}")

def _force_click_default(serial: str, x: int, y: int) -> str:
    """Hàm phụ trợ thực hiện bấm giữ 150ms theo tọa độ cố định chỉ định"""
    try:
        cmd = f"adb -s {serial} shell input swipe {x} {y} {x} {y} 150"
        subprocess.run(cmd, shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(3.5)  # Chờ 3.5 giây để lệnh thực thi hoàn tất trên Facebook
        return "action_executed"
    except Exception as e:
        print(f"Lỗi khi bấm tọa độ mặc định trên {serial}: {e}")
        return "not_found"


def _search_google_without_uiautomator(serial: str, query: str) -> None:
    """Dự phòng khi uiautomator2/dump hierarchy lỗi trên một số máy."""
    search_query = (query.strip() or "bao moi").replace(" ", "%s")

    try:
        adb_shell(serial, "input", "keyevent", "84", check=False, timeout=5)
        time.sleep(0.8)
    except Exception:
        pass

    adb_shell(serial, "input", "text", search_query, check=False, timeout=10)
    time.sleep(0.5)
    adb_shell(serial, "input", "keyevent", "66", check=False, timeout=5)
    time.sleep(4)
    tap(serial, 531, 620)
    time.sleep(2)

def join_facebook_group(serial: str, url: str) -> bool:
    open_link(serial, url)
    time.sleep(4)
    keywords = ["join group", "join", "tham gia nhóm", "tham gia", "request to join"]
    if _find_and_tap_button(serial, keywords):
        time.sleep(1)
        return True
    try:
        tap(serial, 540, 200)
        time.sleep(0.6)
        return True
    except Exception:
        return False


def _emit_progress(progress: Callable[[str], None] | None, message: str) -> None:
    if not progress:
        return
    try:
        progress(message)
    except Exception:
        pass


def set_proxy(serial: str, proxy: str) -> None:
    proxy = proxy.strip()
    if not proxy:
        raise AdbError("Proxy trống.")
    adb_shell(serial, "settings", "put", "global", "http_proxy", proxy, timeout=20)


def connect_college_proxy(
    serial: str, proxy: str = "", package: str = "", progress: Callable[[str], None] | None = None
) -> bool:
    try:
        if not package:
            package = _find_launchable_package(serial, ("college", "proxy"))
        _emit_progress(progress, "Đang mở College Proxy...")
        _launch_proxy_app(serial, package)
        _emit_progress(progress, f"Đã mở {package}")
        time.sleep(2.0)

        # 1. KIỂM TRA XEM CÓ VÀO ĐƯỢC FORM CHÍNH KHÔNG (HAY BỊ TREO MÀN HÌNH LOADING QUAY TRÒN)
        def _check_form_active() -> bool:
            xml = (get_xml_dump(serial) or "").lower()
            return any(k in xml for k in (
                "edittext_address", "proxy_start_button", "start proxy service", 
                "stop proxy service", "id/edittext_port", "android.widget.edittext"
            ))

        form_ready = False
        for _ in range(3):
            if _check_form_active():
                form_ready = True
                break
            time.sleep(1.2)

        # NẾU SAU KHI CHỜ VẪN CHƯA VÀO ĐƯỢC FORM (TỨC LÀ BỊ TREO Ở SPLASH LOADING QUAY TRÒN LIÊN TỤC)
        if not form_ready:
            _emit_progress(progress, "Treo màn hình Loading -> Xóa dữ liệu app (pm clear) và mở lại...")
            print(f"[{serial}] ⚠️ PHÁT HIỆN TREO QUAY LOADING -> Xóa dữ liệu app (pm clear {package}) và mở lại...")
            adb_shell(serial, "am", "force-stop", package, check=False, timeout=5)
            adb_shell(serial, "pm", "clear", package, check=False, timeout=15)
            time.sleep(1.0)
            _launch_proxy_app(serial, package)
            # Chờ app nạp lại sau khi clear
            for _ in range(6):
                time.sleep(1.2)
                if _check_form_active():
                    print(f"[{serial}] ✓ College Proxy đã nạp form chính sau khi clear data!")
                    break

        # 2. Điền thông tin proxy được gán và bấm Start
        if proxy.strip():
            _emit_progress(progress, "Đang điền proxy...")
            host, port, username, password = parse_proxy_spec(proxy)
            _fill_proxy_form(serial, host, port, username, password)
            time.sleep(1.5)
        else:
            _emit_progress(progress, "Không có proxy trong hồ sơ, thử bấm Start...")
            try:
                _fill_proxy_form(serial, "", "", "", "")
            except Exception:
                pass

        # 3. Tự động chấp nhận Popup Yêu cầu kết nối VPN (nếu có sau khi clear data hoặc lần đầu start)
        time.sleep(1.5)
        for _ in range(3):
            xml_vpn = (get_xml_dump(serial) or "").lower()
            if any(k in xml_vpn for k in ("connection request", "yêu cầu kết nối", "wants to set up a vpn", "muốn thiết lập kết nối vpn", "vpn connection", "trust")):
                try:
                    root_diag = ET.fromstring(get_xml_dump(serial) or "")
                    for node in root_diag.iter("node"):
                        text_val = (node.attrib.get("text") or "").strip().lower()
                        desc_val = (node.attrib.get("content-desc") or "").strip().lower()
                        if text_val in ("ok", "đồng ý", "cho phép", "allow", "accept") or desc_val in ("ok", "đồng ý", "cho phép", "allow", "accept"):
                            tap_node(serial, node)
                            print(f"[{serial}] ✓ Đã tự động chấp nhận quyền kết nối VPN!")
                            time.sleep(1.5)
                            break
                except Exception:
                    pass

        _emit_progress(progress, "Đang bật Start Proxy Service...")
        time.sleep(2.5)
        ok = _proxy_connection_ok(serial)
        _emit_progress(progress, "Kết nối OK" if ok else "Kết nối chưa xác nhận được")
        return ok
    except Exception as exc:
        _emit_progress(progress, f"Kết nối proxy thất bại: {exc}")
        return False


def clear_proxy(serial: str) -> None:
    adb_shell(serial, "settings", "delete", "global", "http_proxy", timeout=20)


def clear_college_proxy_data(serial: str) -> bool:
    """
    Xóa sạch dữ liệu (pm clear) và force-stop ứng dụng College Proxy.
    """
    try:
        package = _find_launchable_package(serial, ("college", "proxy"))
    except Exception:
        package = "com.cell47.collegeproxy"

    adb_shell(serial, "am", "force-stop", package, check=False, timeout=5)
    time.sleep(0.5)
    out = adb_shell(serial, "pm", "clear", package, check=False, timeout=15)
    print(f"[{serial}] Đã xóa dữ liệu College Proxy ({package}): {out}")
    return "Success" in out or "success" in out.lower()


def check_proxy_status(serial: str, saved_proxy: str = "") -> dict[str, Any]:
    """
    Kiểm tra trạng thái kết nối Proxy bằng cách mở app College Proxy:
    - Chờ app load xong màn hình chính (tránh đọc nhầm khi app đang ở Splash/Loading).
    - Nếu hiện nút 'STOP PROXY SERVICE' hoặc trạng thái Connected -> ĐÃ KẾT NỐI (connected = True).
    - Nếu hiện nút 'START PROXY SERVICE' hoặc trạng thái Disconnected -> CHƯA KẾT NỐI (connected = False).
    """
    res: dict[str, Any] = {
        "connected": False,
        "button_text": "",
        "message": "Chưa kết nối (START PROXY SERVICE)",
    }

    try:
        try:
            package = _find_launchable_package(serial, ("college", "proxy"))
        except Exception:
            package = "com.cell47.College_Proxy"

        # 1. Bật sáng màn hình nếu đang tắt
        _safe_adb_shell(serial, "input", "keyevent", "224", timeout=5)

        # 2. Mở app College Proxy (KHÔNG force-stop để giữ nguyên dịch vụ đang chạy)
        _launch_proxy_app(serial, package, force_restart=False)

        # 3. Lặp quét kiểm tra trạng thái trong tối đa 6 giây
        saw_stop = False
        saw_start = False
        button_label = ""

        for attempt in range(1, 7):
            time.sleep(0.9)

            # Ưu tiên kiểm tra nhanh bằng uiautomator2
            try:
                import uiautomator2 as u2
                d = u2.connect(serial)

                start_btn = d(resourceIdMatches=r".*id/proxy_start_button.*")
                if not start_btn.exists:
                    start_btn = d(className="android.widget.Button", textMatches=r"(?i).*(stop|start).*proxy.*service.*")
                if not start_btn.exists:
                    start_btn = d(textMatches=r"(?i)^(stop proxy service|start proxy service)$")

                btn_txt = (start_btn.get_text() or "").strip().upper() if start_btn.exists else ""

                if "STOP" in btn_txt or d(text="Connected").exists or d(textMatches=r"(?i)^connected$").exists:
                    saw_stop = True
                    button_label = "STOP PROXY SERVICE"
                    break
                elif "START" in btn_txt or d(text="Disconnected").exists or d(textMatches=r"(?i)^disconnected$").exists:
                    saw_start = True
                    button_label = "START PROXY SERVICE"
            except Exception:
                pass

            # Fallback quét cấu trúc XML dump tìm cả nút bấm và chữ Connected/Disconnected
            xml = (get_xml_dump(serial) or "").lower()
            if xml:
                if "stop proxy service" in xml or 'text="connected"' in xml or ">connected<" in xml:
                    saw_stop = True
                    button_label = "STOP PROXY SERVICE"
                    break
                elif "start proxy service" in xml or 'text="disconnected"' in xml or ">disconnected<" in xml:
                    saw_start = True
                    button_label = "START PROXY SERVICE"

            # Nếu đã tìm thấy STOP -> Đã kết nối chắc chắn 100%, dừng vòng lặp ngay!
            if saw_stop:
                break

            # Nếu chỉ thấy START ở 1-2 giây đầu (có thể là giao diện mặc định chưa kịp nạp dữ liệu),
            # tiếp tục chờ thêm ít nhất 3.5s (attempt >= 4) để app nạp xong trạng thái thực tế
            if saw_start and attempt >= 4:
                break

        if saw_stop:
            res["connected"] = True
            res["button_text"] = "STOP PROXY SERVICE"
            res["message"] = "Đã kết nối (STOP PROXY SERVICE)"
        elif saw_start:
            res["connected"] = False
            res["button_text"] = "START PROXY SERVICE"
            res["message"] = "Chưa kết nối (START PROXY SERVICE)"
        else:
            res["connected"] = False
            res["button_text"] = ""
            res["message"] = "Chưa kết nối (Không nhận diện được app)"

    except Exception as exc:
        res["connected"] = False
        res["message"] = f"Lỗi kiểm tra app proxy: {exc}"

    return res






def set_location(serial: str, lat: float, lon: float, open_map: bool = True) -> None:
    try:
        adb_shell(serial, "svc", "location", "enable", timeout=10, check=False)
    except Exception:
        pass
    if open_map:
        uri = f"geo:{lat},{lon}"
        adb_shell(serial, "am", "start", "-a", "android.intent.action.VIEW", "-d", uri, check=False, timeout=10)


def batch_connect_proxy_with_location(
    serials: list[str], proxies: list[str], locations: list[tuple[float, float]] | None = None,
    delay_between: float = 3.0, verify: bool = True,
) -> dict:
    results: dict = {}
    if not serials:
        return results
    for idx, serial in enumerate(serials):
        res: dict = {"set_location": None, "set_proxy": None, "verify_proxy": None}
        proxy = proxies[idx % len(proxies)] if proxies else ""
        loc = locations[idx % len(locations)] if locations else None
        try:
            if loc:
                lat, lon = float(loc[0]), float(loc[1])
                set_location(serial, lat, lon)
                res["set_location"] = "ok"
            else:
                res["set_location"] = "skipped"
        except Exception as e:
            res["set_location"] = f"error: {e}"
        try:
            if not proxy:
                raise AdbError("No proxy string provided")
            set_proxy(serial, proxy)
            res["set_proxy"] = "ok"
        except Exception as e:
            res["set_proxy"] = f"error: {e}"
        if verify:
            try:
                cur = adb_shell(serial, "settings", "get", "global", "http_proxy", check=False, timeout=6)
                res["verify_proxy"] = cur.strip()
            except Exception as e:
                res["verify_proxy"] = f"error: {e}"
        results[serial] = res
        time.sleep(delay_between)
    return results


def check_fb_uid_live_die(uid: str) -> str:
    """
    Kiểm tra trạng thái Live/Die của một UID Facebook không cần token.
    """
    import urllib.request
    import urllib.error

    uid = str(uid).strip()
    if not uid.isdigit():
        return "DIE"

    # Cách 1: Graph API Picture Check (Rất nhanh và chính xác)
    url_graph = f"https://graph.facebook.com/{uid}/picture?type=normal"
    try:
        req = urllib.request.Request(
            url_graph,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            final_url = response.geturl()
            # Nếu chuyển hướng về trang tĩnh rsrc.php hoặc static.xx.fbcdn.net, tức là DIE
            if "rsrc.php" in final_url or "static.xx.fbcdn.net" in final_url:
                return "DIE"
            return "LIVE"
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):
            return "DIE"
        # Bị lỗi rate limit (429) hoặc lỗi khác, chuyển sang cách dự phòng
    except Exception:
        pass

    # Cách 2: Profile Check (Dự phòng khi Graph API bị chặn)
    url_profile = f"https://www.facebook.com/{uid}"
    try:
        req = urllib.request.Request(
            url_profile,
            headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            html = response.read().decode("utf-8", errors="ignore").lower()
            # Nếu trang chứa thông báo lỗi đặc trưng của Facebook khi tài khoản bị khóa/bị xóa
            die_indicators = [
                "trang này không hiển thị", 
                "this page isn't available", 
                "trang bạn yêu cầu không tìm thấy",
                "the link you followed may be broken",
                "tính năng này hiện không khả dụng",
                "hoặc trang đã bị gỡ",
                "không tìm thấy trang",
                "không khả dụng",
                "không tồn tại",
                "unavailable",
                "not available"
            ]
            if any(indicator in html for indicator in die_indicators):
                return "DIE"
            return "LIVE"
    except Exception:
        # Nếu cả 2 phương thức đều gặp lỗi mạng/mất kết nối
        return "LỖI KẾT NỐI"

