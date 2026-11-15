# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_submodules

ROOT_DIR = SPECPATH
ICON_PATH = os.path.join(ROOT_DIR, "Assets", "AppIcon.ico")

main_hiddenimports = collect_submodules("google.genai")
gui_excludes = [
    "tkinter",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineQuick",
    "PySide6.QtPdf",
    "PySide6.QtPdfWidgets",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.Qt3DCore",
    "PySide6.QtMultimedia",
    "PySide6.QtNetwork",
]

a_main = Analysis(
    [os.path.join(ROOT_DIR, "main.py")],
    pathex=[ROOT_DIR],
    binaries=[],
    datas=[(os.path.join(ROOT_DIR, "Assets"), "Assets")],
    hiddenimports=main_hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=gui_excludes,
    noarchive=False,
    optimize=0,
)
pyz_main = PYZ(a_main.pure)

exe_main = EXE(
    pyz_main,
    a_main.scripts,
    [],
    exclude_binaries=True,
    name="GeminiDesktopPet",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON_PATH],
)

# CleanConfig.exe 仅包含无 GUI 的清理入口与服务模块，与主程序共用同一份 _internal 依赖
a_clean = Analysis(
    [os.path.join(ROOT_DIR, "clean_config.py")],
    pathex=[ROOT_DIR],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "PySide6",
        "shiboken6",
        "google",
        "PIL",
        "unittest",
    ],
    noarchive=False,
    optimize=0,
)
pyz_clean = PYZ(a_clean.pure)

exe_clean = EXE(
    pyz_clean,
    a_clean.scripts,
    [],
    exclude_binaries=True,
    name="CleanConfig",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=[ICON_PATH],
)

coll = COLLECT(
    exe_main,
    exe_clean,
    a_main.binaries,
    a_main.datas,
    a_clean.binaries,
    a_clean.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="GeminiDesktopPet",
)
