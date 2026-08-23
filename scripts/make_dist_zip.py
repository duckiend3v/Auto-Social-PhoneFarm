import zipfile
from pathlib import Path

root = Path(__file__).resolve().parents[1]
dist_exe = root / 'dist' / 'fb_tool.exe'
zip_path = root / 'dist_bundle_with_icon.zip'

with zipfile.ZipFile(zip_path, 'w', compression=zipfile.ZIP_DEFLATED) as z:
    if dist_exe.exists():
        z.write(dist_exe, 'dist/fb_tool.exe')
    # add tools/scrcpy
    scrcpy_dir = root / 'tools' / 'scrcpy'
    if scrcpy_dir.exists():
        for p in scrcpy_dir.rglob('*'):
            if p.is_file():
                z.write(p, str(p.relative_to(root)))
    # add bundled adb platform-tools if present
    platform_tools_dir = root / 'tools' / 'platform-tools'
    if platform_tools_dir.exists():
        for p in platform_tools_dir.rglob('*'):
            if p.is_file():
                z.write(p, str(p.relative_to(root)))
    # add data/
    data_dir = root / 'data'
    if data_dir.exists():
        for p in data_dir.rglob('*'):
            if p.is_file():
                z.write(p, str(p.relative_to(root)))
    # additional docs
    for doc in ['README.md', 'DIST-INSTRUCTIONS.txt']:
        f = root / doc
        if f.exists():
            z.write(f, doc)

print('Wrote', zip_path)
