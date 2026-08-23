from pathlib import Path
import sys
import os
# Prefer any bundled adb.exe under tools/scrcpy by adding its folder to PATH
root = str(Path(__file__).resolve().parents[1])
for p in Path(root).rglob('adb.exe'):
    adb_candidate = str(p)
    adb_dir = str(Path(adb_candidate).parent)
    os.environ['PATH'] = adb_dir + os.pathsep + os.environ.get('PATH', '')
    os.environ['ADB'] = adb_candidate
    break
if root not in sys.path:
    sys.path.insert(0, root)
from src import adb_client
import time


def run():
    try:
        devices = adb_client.list_devices()
    except Exception as e:
        print('Failed to list devices:', e)
        return
    if not devices:
        print('No devices connected.')
        return
    print(f'Found {len(devices)} device(s)')
    for dev in devices:
        serial = dev.serial
        print('\n-- Device', serial)
        res = {'model': dev.display_name, 'state': dev.state}
        try:
            out = adb_client.adb_shell(serial, 'pm', 'list', 'packages', 'com.facebook.katana', check=False, timeout=8)
            res['facebook_package'] = 'com.facebook.katana' if 'com.facebook.katana' in out else ''
        except Exception as e:
            res['facebook_package_error'] = str(e)
        try:
            adb_client.open_facebook(serial)
            res['launch_attempt'] = 'ok'
            time.sleep(2)
            try:
                pid = adb_client.adb_shell(serial, 'pidof', 'com.facebook.katana', check=False, timeout=6)
                res['facebook_pid'] = pid.strip() if pid else ''
            except Exception as e:
                res['facebook_pid_error'] = str(e)
        except Exception as e:
            res['launch_attempt'] = f'error: {e}'
        try:
            adb_client.open_link(serial, 'https://m.facebook.com')
            res['open_link'] = 'ok'
        except Exception as e:
            res['open_link'] = f'error: {e}'
        try:
            adb_client.swipe(serial, 'up')
            res['swipe'] = 'ok'
        except Exception as e:
            res['swipe'] = f'error: {e}'
        try:
            proxy = adb_client.adb_shell(serial, 'settings', 'get', 'global', 'http_proxy', check=False, timeout=6)
            res['current_proxy'] = proxy.strip()
        except Exception as e:
            res['current_proxy_error'] = str(e)
        try:
            wifi = adb_client.adb_shell(serial, 'dumpsys', 'wifi', check=False, timeout=8)
            res['wifi_snippet'] = wifi[:1000]
        except Exception as e:
            res['wifi_error'] = str(e)
        try:
            p = adb_client.adb_shell(serial, 'ping', '-c', '1', '8.8.8.8', check=False, timeout=8)
            res['ping'] = 'ok' if ('1 packets transmitted' in p or '1 packets received' in p or '1 received' in p) else p.strip()
        except Exception as e:
            res['ping'] = f'error: {e}'
        for k,v in res.items():
            if k == 'wifi_snippet':
                print(' ', k, ': [snippet length]', len(v) if v else 0)
            else:
                print(' ', k, ':', v)

if __name__ == '__main__':
    run()
