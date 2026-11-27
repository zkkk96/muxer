# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：onedir + QtWebEngine + 可选内置 ffmpeg。

若存在 vendor/ffmpeg（其中 bin/ffmpeg.exe 与 bin/ffprobe.exe），
会一并打包到 _internal/ffmpeg/，运行期优先使用。
"""

from pathlib import Path

ROOT = Path(SPECPATH)

datas = [
    (str(ROOT / "muxer" / "assets"), "muxer/assets"),
    (str(ROOT / "muxer" / "ui" / "theme" / "qss"), "muxer/ui/theme/qss"),
]
vendor = ROOT / "vendor" / "ffmpeg"
if vendor.exists():
    datas.append((str(vendor), "ffmpeg"))

a = Analysis(
    [str(ROOT / "muxer" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebChannel",
        "PySide6.QtNetwork",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "rich", "PIL"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Muxer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ROOT / "muxer" / "assets" / "icons" / "muxer.ico"),
    version=str(ROOT / "packaging" / "version_info.txt"),
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Muxer",
)