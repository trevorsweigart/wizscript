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

## Jev combat

Auto Combat now sends fresh battle facts to Jev using TypeSafe's
[System One API](https://docs.typesafe.ai/api) and a
[Choice question](https://docs.typesafe.ai/primitives/choice). The old combat
strategy tree has been removed. Start **Jev Combat** after connecting to a
Wizard101 client. Each question asks Jev to play toward winning like an expert
Wizard101 player, using the offered actions and the available combat facts.

Enter a key using **Jev API key → Save key**, or set `TYPESAFE_API_KEY` in the
environment before launching. The saved key is encrypted with Windows DPAPI
for the current Windows user at `%LOCALAPPDATA%\WizScript\jev_credentials.json`.
It is excluded from Git and the EXE; another computer/user needs its own saved
key. Restart Jev Combat after changing the key. The default model is
`jev-latest`; `TYPESAFE_MODEL` can override it.

The state collector reads hand spell names, descriptions, accuracy, all pip
costs, enchantments, and nested/conditional effects; player, ally, and enemy
health/mana, pips by type and school, combat stats, masteries, archmastery,
shadow progress, blades/traps/shields, auras, overtime, globals, battlefield
effects, status conditions, and turn order. It also reads deck/graveyard
contents, trained spells, loaded spell templates, and treasure card data when
available. Optional reads are bounded so a loading screen or unsupported field
cannot stall the decision indefinitely. Missing values are `null`, with an
`unavailable` list; they are never invented or assumed to be zero.

Choices distinguish duplicate cards and combatants by IDs. They include
`pass`, `flee`, casting a specific card on a specific enemy/ally/self or team,
enchanting a hand card, discarding, and drawing treasure cards. After a verified
enchantment, discard, or draw, Jev receives the updated state and makes another
choice in the same turn. A normal spell/pass/flee submission is allowed once per
round. State and turn identity are checked again before input; stale API answers
are rejected. API failures send no new combat input and there is no strategy
fallback. Repeated errors stop Jev Combat so you can take over.

Use **Export combat snapshot** to inspect `combat_snapshot.json` in the app's
data directory. Decisions are also saved in `combat_decision.json`, with the
model, confidence, probability distribution, and token usage. These reports
do not contain the API key. Battle state is sent to TypeSafe when Jev Combat is
enabled; exporting a snapshot alone does not call the API.

Wizwalker does **not** expose a reliable full cast-event log. The fight history
records submitted actions, verified hand changes, and observed graveyard
additions separately. Graveyards may include discarded cards and cannot prove
that a spell was cast successfully. Histories start when tracking starts and
reset for each fight; the JSON explicitly marks confirmed cast lists as
unavailable. Enemy hidden hands/decks and complete metadata for every undrawn
card also depend on what the client has loaded/exposed. Enchantment compatibility
and unusual multi-stage targeting still require live-game verification.

Developer checks:

```powershell
.\.venv-build\Scripts\python.exe -m unittest discover -s tests -v
.\.venv-build\Scripts\python.exe check_jev.py
```

The first command runs offline behavior tests. The second authenticates with
TypeSafe and makes one synthetic combat Choice; it does not control the game.
