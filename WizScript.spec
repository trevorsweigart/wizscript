# Build with: python -m PyInstaller --clean --noconfirm WizScript.spec
import struct
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files
from PyInstaller.utils.hooks.tcl_tk import tcltk_info

project = Path(SPECPATH)
if sys.platform != "win32" or struct.calcsize("P") != 8:
    raise SystemExit("Build WizScript with 64-bit Python on Windows.")
if not tcltk_info.available or tcltk_info.tcl_data_missing or tcltk_info.tk_data_missing:
    raise SystemExit("Tcl/Tk libraries are missing. Repair Python with Tcl/Tk support enabled.")

# PyInstaller's _tkinter hook collects the matching DLLs and script directories.
# Tcl/Tk 9 can embed the scripts inside its DLLs (//zipfs:/); PyInstaller 6.22.3
# supports this layout without hard-coded paths to the build machine's Python.
a = Analysis(
    [str(project / "main.py")],
    pathex=[str(project)],
    binaries=[],
    datas=[(str(project / "zone_map.json"), ".")] + collect_data_files("wizwalker"),
    hiddenimports=[],
    hookspath=[],
    hooksconfig={"matplotlib": {"backends": ["TkAgg"]}},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="WizScript",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
)
