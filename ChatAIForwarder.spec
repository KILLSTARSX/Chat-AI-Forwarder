# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller onefile spec for 聊天软件 AI 转发助手.

Design rules:
  * No absolute paths - the entry script is resolved relative to this file.
  * Heavy optional runtimes (OCR / numpy / Pillow / OpenCV) are deliberately
    excluded: the app detects them at runtime and degrades gracefully.
  * disable_windowed_traceback keeps crash dialogs from printing local paths.
"""
import importlib.util

# Optional UI Automation support: include it only when it is actually installed.
hiddenimports = []
for _mod in ("uiautomation", "comtypes", "comtypes.client"):
    try:
        if importlib.util.find_spec(_mod) is not None:
            hiddenimports.append(_mod)
    except Exception:
        pass

# These are optional accelerators / OCR runtimes. Bundling them would turn a
# small single file into a multi-hundred-MB archive with a very slow cold start.
excludes = [
    "numpy", "PIL", "cv2", "onnxruntime", "rapidocr_onnxruntime",
    "shapely", "paddleocr", "pyclipper", "matplotlib", "pandas",
    "scipy", "torch", "sklearn", "PyQt5", "PySide2", "PySide6",
]

a = Analysis(
    ['src/chat_ai_forwarder.py'],
    pathex=[],
    binaries=[],
    # 运行时窗口图标：PE 资源里的图标只影响资源管理器/任务栏，
    # Tk 窗口标题栏还需要 iconbitmap() 从解包目录读取一份。
    datas=[('assets/app.ico', 'assets')],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=1,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='聊天软件AI转发助手',
    icon='assets/app.ico',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
