# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller specification for packaging TDrive desktop client."""

import os
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

datas = [
    ('web', 'web'),
]

# Bundle compressed vendor archives when present to keep package size compact,
# falling back to extracted vendor folders if archives were already pruned.
for name in ('LibreOffice', 'vlc'):
    archive_path = os.path.join('core_app', 'vendor', f'{name}.7z')
    folder_path = os.path.join('core_app', 'vendor', name)
    if os.path.exists(archive_path):
        datas.append((archive_path, os.path.join('core_app', 'vendor')))
    elif os.path.isdir(folder_path):
        datas.append((folder_path, os.path.join('core_app', 'vendor', name)))

hiddenimports = sorted(set(
    collect_submodules('core_app')
    + [
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebChannel',
        'qasync',
        'aiohttp',
        'telethon',
        'cryptography',
        'keyring',
        'keyring.backends.Windows',
        'machineid',
        'qrcode',
        'PIL',
        'py7zr',
        'fitz',
        'vlc',
        'chardet',
        'cv2',
        'pydub',
        'pypdf',
        'olefile',
        'docx',
        'openpyxl',
        'pptx',
    ]
))

a = Analysis(
    ['main.py'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'pytest'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='TDrive',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='web/icon.ico',
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='TDrive',
)
