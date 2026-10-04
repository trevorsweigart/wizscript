# WizScript

Wizard101 automation with a Tkinter interface.

## Run the executable

Double-click `dist\WizScript.exe`. It is a single Windows executable; Python
does not need to be installed on the computer running it. You can copy the EXE
to another folder or another 64-bit Windows computer.

The EXE stores `wizscript.log` and its learned `zone_map.json` in
`%LOCALAPPDATA%\WizScript`. On first launch it copies the bundled zone map there;
later launches preserve your learned zones. Source runs store these files beside
the Python sources. Set `WIZSCRIPT_DATA_DIR` to use a different writable folder.

## Build the executable

Install 64-bit Python on Windows with **Tcl/Tk and IDLE** enabled, and Git
(Wizwalker is installed from GitHub's current default branch). Python 3.14 was used
to verify this build. Then double-click `build.bat`, or run:

```powershell
.\build.ps1
```

The script creates a local `.venv-build`, installs `requirements-build.txt`,
and builds `dist\WizScript.exe` from `WizScript.spec`. Each build checks for
Wizwalker updates; its repository commit is not pinned. To select Python when
creating the environment, use `build.ps1 -Python 'C:\path\to\python.exe'`.

PyInstaller bundles `_tkinter`, the matching Tcl/Tk DLLs, and Tcl/Tk scripts.
Its hooks set `TCL_LIBRARY` and `TK_LIBRARY` to bundled directories for ordinary
Tcl/Tk installs. Tcl/Tk 9 installs with DLL-embedded ZIP libraries are also
supported by the pinned PyInstaller version. No paths to the build computer's
Python installation are hard-coded into the EXE. The zone graph includes the
matplotlib TkAgg backend.

To verify the packaged GUI and graph without connecting to the game:

```powershell
$env:WIZSCRIPT_DATA_DIR = "$PWD\build\smoke-data"
$process = Start-Process .\dist\WizScript.exe -ArgumentList '--smoke-test', 'build\smoke-report.json' -WindowStyle Hidden -PassThru -Wait
Get-Content .\build\smoke-report.json
Remove-Item Env:\WIZSCRIPT_DATA_DIR
```

The report should show `ok`, `frozen`, `graph_rendered`, and
`persistence_verified` as `true`. This checks startup and bundled dependencies;
game connection and automation require a running Wizard101 client.
