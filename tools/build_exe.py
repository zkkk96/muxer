"""调用 PyInstaller 生成 onedir 安装包。

用法：python tools/build_exe.py
产物：dist/Muxer/Muxer.exe
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    vendor = ROOT / "vendor" / "ffmpeg" / "bin" / "ffmpeg.exe"
    if vendor.exists():
        print(f"内置 ffmpeg: {vendor}")
    else:
        print("未找到 vendor/ffmpeg/bin/ffmpeg.exe，打包后将回退到 PATH 中的 ffmpeg")

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "Muxer.spec",
    ]
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode == 0:
        print(f"打包完成: {ROOT / 'dist' / 'Muxer' / 'Muxer.exe'}")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
