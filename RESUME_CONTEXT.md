# WizScript work in progress: resume handoff

Updated 2026-10-04, America/New_York. Work is **paused at the user's request**.
This is a checkpoint of unfinished development, not a declaration that the
full automation goal is complete. The user explicitly requested committing
and pushing this checkpoint, superseding the earlier instruction to leave
these quest changes uncommitted.

## Goal and constraints

Make mainline questing work unattended from a fresh character through the
actual free-content boundary, with Jev choosing combat actions. Specifically:

- Fix the first post-tutorial Merle Ambrose conversation when the quest arrow
  incorrectly points at his exit door.
- Find unloaded collection objects, including Blad Raveneye's cogs, and
  actually collect them when standing nearby requires an X interaction.
- Use general rules from fresh game state, HUD text, quest metadata, NPC/object
  metadata, and walkable map data. Do not introduce quest-specific names,
  IDs, coordinates, or scripted quest walkthroughs.
- Remove the former Triton-only limit. Continue all mainline quests until an
  actual paywall is observed; do not assume the old Cyclops/Firecat boundary.
- Intercept moving street enemies ahead of their motion/facing, then walk
  into them instead of repeatedly teleporting behind them.
- Visit the player's school teacher, learn only own-school spells whose UI
  explicitly shows a cost of **0 training points**, and confirm points did not
  change. Never spend training points on another school.
- Build a small useful deck from learned spell effects and pip costs, including
  efficient attacks and support, especially before Harvest Lord. Jev continues
  to choose individual combat actions; deck management is not a combat tree.
- Repeat fresh-character tests, including complete Triton runs, with no manual
  quest movement, interactions, or combat assistance.
- Always finish a live session using WizScript's **Disconnect** button and
  verify the disconnected state. Preserve the running game session; do not
  restart the game or automate login.

Earlier completed baseline work added PyInstaller/Tcl support, removed the
Wizwalker pin, and replaced the old combat strategy with Jev choices. Relevant
baseline commits: `ad88e6d` (EXE/upstream Wizwalker) and `b62bdea` (Jev combat).
The current checkpoint adds general questing fixes and unfinished school/deck
management on top of that baseline.

## Where the live session stopped

- Character: **Anna Gravecrown**, Death school, most recently observed level 7.
- Latest automated progress: completed the Tempest Nexus follow-up after
  Harvest Lord, handed in to Sergeant Muldoon, accepted **Putting Out the
  Fire!**, entered **Firecat Alley**, and began approaching Private Quinn.
- Last source-app position display was approximately `(279, -290, 1)` in
  `WizardCity/WC_Streets/WC_Firecat`. Re-read all state on resume; these are
  historical observations, not navigation coordinates to put into code.
- There are six character slots occupied. Existing characters were preserved.
  Do not assume a new slot is available for another fresh test.
- The source app was reloaded with the latest diagnostics and school-search
  changes, but not connected again. On pause, its client list showed the game,
  both automation buttons showed **Start**, Connect was available, Disconnect
  was disabled, and game info was blank. The previous connected session had
  already been ended through Disconnect and verified.
- The game remained running. No quest movement or interaction should be sent
  while this task is paused. Re-discover native windows rather than retaining
  window handles or screenshot coordinates from this session.

## What has been implemented

### Main quest, dialog, NPCs, and transitions

`quest_state.py` reads bounded fresh quest/UI snapshots. `quest_navigation.py`
tracks accepted main quests through the journal and turns off Quest Finder
while following an accepted mainline quest.

`quest_dialog.py` handles dialog/services and verifies the mainline star before
accepting a new offer. It declines verified unstarred offers where supported,
prefers the current main quest in NPC services, and revisits the last NPC after
a hand-in if the next offer has not appeared yet. Purchase actions are not part
of quest dialog handling.

`quest_npc.py` and `AutoQuester._talk_target()` resolve the current Talk To
objective against real active NPCs, approach from bounded angles, avoid nearby
actors, walk into interaction range, and require the correct prompt before X.
Dormant stand-ins with no actor body are excluded from ordinary talk targets.
This fixes following the exit arrow instead of talking to the initial NPC.

`quest_objects.py` identifies usage/scavenge targets from goal tags and display
names and supplies generic sigil approach geometry. Entry interaction uses a
short X press and waits for the actual transition instead of cancelling a
countdown through repeated movement/input.

`zone_walker.py` observes each actual arrival and records transitions through
a callback. If a route unexpectedly lands directly in its final destination,
it succeeds immediately; other unexpected arrivals stop the route and require
a fresh path. This handles the intro Ravenwood variant becoming normal
Ravenwood. `zone_map.json` includes learned transitions; the latest source
reload reported **16 zones, 38 transitions**.

The last live-verified fix clears an old expected NPC when the HUD advances
from Talk To to another action. For unloaded usage objects with a nonzero
finder position, it lets normal arrow navigation run even when the goal's
destination is empty. This took Anna out of the school and through the
Tempest Nexus objective into Firecat Alley.

### Collection and moving enemies

`entities.py` can use static entity locations when a collection object has no
actor body. Collection handles bounded teleport offsets, walking through the
target, fresh progress observations, matching prompts, and brief X retries.
Previously collected positions are excluded after the HUD counter advances.

`quest_search.py` parses installed `zone.nav` walkable graph data and picks
uncovered connected points for out-of-render-range search. It validates binary
sizes, finite coordinates, indices, and duplicate labels; ambiguous vertices
are excluded. A bounded expanding search is the fallback when geometry is
unavailable. Cached data is static map geometry, not persistent game state.

`quest_mobs.py` predicts motion from two observations, rejects implausible
respawn velocity, falls back to game-facing yaw, stages ahead of the enemy,
then walks through its predicted position. It rechecks battle/loading before
further movement.

### School and deck management: unfinished

`spell_management.py` includes `SchoolTrainer`, `SchoolVisits`, `DeckManager`,
spell effect summaries, deck policy, and UI list-layout compatibility helpers.

The trainer requires own-school identity, an exact displayed numeric zero
cost, an enabled Train button, a newly learned ID, and unchanged displayed
training points. Unknown cost/points or unconfirmed changes stop training.
Teacher names are a school-role registry, not quest-specific navigation.
Visits use recorded routes, require a return route, and return to the original
quest position afterward. The latest patch searches walkable school grounds
when the teacher is not loaded at the entrance; **that patch is not yet live
validated**.

The deck policy selects learned spells by school, pip cost, accuracy, and
effects: efficient/cheap attacks, a finisher, positive damage buffs, and an
emergency heal. Nested/random effects are summarized so attacks are not
mistaken for cards with no damage. Each UI edit must produce the exact expected
deck-count change; otherwise editing stops. No live deck edit has succeeded yet.

The installed Wizwalker card-list widget offsets appear stale for this game
build. The wrapper checks both the old layout and a candidate +72-byte shift
against installed deck UI dimensions before dereferencing entries. Offline
tests verify layout selection and rejection, but **neither candidate has been
confirmed against the live widgets**. The last live attempt reported
`Deck checks paused: card data is unavailable`. Do not treat passing mock
tests as proof of the real layout.

`spell_ui_report()` currently includes temporary bounded, read-only layout
diagnostics (uint32 fields at offsets 700 through 916) for unverified list
widgets in the local quest snapshot. Export with the deck displayed, identify
the actual validated layout, then remove or narrow this temporary diagnostic
before declaring the feature finished. Do not blindly click guessed geometry
or write raw memory to modify decks.

### Combat correctness

The combat snapshot now marks participant health as the live health source;
raw GameStats HP can retain a creature's spawn value. The Jev prompt explicitly
uses `health.current` and `health.maximum`. Tests cover this distinction.
Jev is still the combat decision maker; no old strategy fallback was restored.

## Evidence and limits

Verified in the fresh Anna development run, without manual quest/combat help
after tutorial skipping:

- Initial Merle conversation and Unicorn Way progression, including Rattlebones.
- Triton street fights and collection of **all three cogs**, including X-based
  collection and progress detection.
- Blad's lever interaction and subsequent quest hand-ins.
- Entry into the Harvest Lord tower and victory over the minion and boss.
- Post-boss follow-ups, Tempest Nexus, and mainline continuation to Firecat Alley.

This was **one development run with pauses/reloads**, not repeated clean runs
of the final implementation. The earlier expanding collection search was slow;
the newer walkable-map search still needs live validation. No successful free
training or confirmed automatic deck edits have been demonstrated. No actual
paywall has been reached. Further zones/mainline branches need live coverage.

At this checkpoint, **72 offline tests pass** and `git diff --check` reports
no whitespace errors. This validates bounded policies and state-handling cases,
not completion of all live acceptance criteria.

## Next work, in order

1. Re-observe the game and connect the source app. Keep the current character's
   mainline quest intact. Inspect the displayed deck with questing paused and
   export **Print Game State** to diagnose actual list layout/entry geometry.
2. Fix the card reader using validated game/UI data. Verify learned spell IDs,
   effects, pip costs, current deck, page indices, slot geometry, and capacity.
   The current policy defaults to capacity 20, while the displayed Starter Deck
   appeared to have 16 slots; use actual capacity rather than that assumption.
3. Validate school-ground searching, locate the own-school teacher, train a
   displayed zero-cost spell, and confirm training points are unchanged.
   Earlier visits returned immediately because the teacher was not loaded.
   Test the new static-location fallback/search, not just the route itself.
4. Confirm automatic deck changes in game, including a useful two-pip attack
   and support when learned. Handle transient unavailable metadata without
   silently checking off the whole level forever. Check pagination and entry
   rectangle scaling before any card click.
5. Continue the current Firecat mainline, other Wizard City mainline content,
   and onward until an actual purchase/access gate is observed. Stop at the
   gate; do not buy access. Keep fixes general across zones and quest types.
6. Repeat fresh-character tests of the initial NPC, unloaded cogs, and complete
   Triton with final source, training, and deck management. Preserve the
   distinction between manual diagnostics and unattended quest assistance.
7. Rebuild the EXE, run its packaging smoke test, and live-test the final EXE.
   Always Disconnect and verify when ending a test. Update this document with
   verified outcomes and remaining failures.

## Methodology for continuing

- Observe the failure through the actual game screen, fresh quest/UI metadata,
  and local logs. Diagnose the general missing behavior before coding a fix.
- Use the installed Wizwalker implementation and game assets as evidence for
  field layouts and geometry; consult primary API documentation where needed.
  Some game files named XML are binary BINd data, not parseable text XML.
- Add focused tests for behavior that can fail meaningfully: stale targets,
  missing destinations, unloaded objects, transition mismatches, moving mobs,
  training cost checks, layout validation, and unexpected deck changes.
- Run the offline suite after changes, then reload source and verify the exact
  behavior live. Do not claim live success solely from mocks or source review.
- Keep Wizwalker calls awaited on the background asyncio loop. Update Tk via
  `root.after`. Check loading/battle, catch transient reads, bound waits, and
  reacquire state before input. Follow `AGENTS.md`.
- Native Windows UI inspection/input uses the computer-use skill's
  `@oai/sky` API through `node_repl`: observe, inspect, issue one input, refresh.
  Select exactly one returned window; do not guess handles or reuse screenshot
  coordinates after a reload. No custom native-input helper or PowerShell UIA.
- Drive questing through WizScript itself. Manual game movement/X/combat clicks
  must not be used to make an unattended acceptance test pass. Opening/closing
  P for read-only deck diagnostics is separate from quest assistance.
- Provide concise progress updates, clearly label partial verification, and
  stop/disconnect for pauses. Preserve other characters and unrelated apps.

## Environment, local artifacts, and build commands

Workspace used: `C:\Users\Trevor\Desktop\wizscript`; PowerShell on Windows.
Source runtime: `.venv-build\Scripts\python.exe` / `pythonw.exe`, Python 3.14.7.
Installed Wizwalker was upstream commit prefix `8b0474`; requirement is unpinned.

The source app logs to `wizscript.log` in this workspace. Local diagnostic
files `quest_snapshot.json`, `combat_snapshot.json`, and `combat_decision.json`
are ignored by Git. Scratch scripts/assets in ignored `build/` include
`inspect_spell_ui.py`, `inspect_nav.py`, `inspect_zone_assets.py`, and
`inspect_localization.py`; they are optional research aids, not committed
dependencies. The newest local quest snapshot predates the latest live state
and the new layout diagnostics, so export a new one rather than trusting it.

The saved Jev credential is Windows-DPAPI protected under
`%LOCALAPPDATA%\WizScript\jev_credentials.json`. Never put its contents or the
user's key into this file, source, logs, Git, or the EXE. The EXE's logs and
persistent zone map use that same application data directory; preserve them
when rebuilding.

**The current local `dist\WizScript.exe` is stale**: last built 2026-10-04
06:49:13, size 45,215,659 bytes. It does not include this checkpoint's latest
changes. `dist/` and build artifacts remain ignored. The previous Tcl/Tk 9.0.4
packaging smoke test passed, but that is not a test of the latest source.

```powershell
# Offline verification
.\.venv-build\Scripts\python.exe -m unittest discover -s tests -q

# Launch source after closing/disconnecting the prior source instance
Start-Process -FilePath '.\.venv-build\Scripts\pythonw.exe' -ArgumentList 'main.py' -WorkingDirectory (Get-Location).Path -WindowStyle Hidden

# Final rebuild: wait for successful completion before launching the EXE
.\.venv-build\Scripts\python.exe -m PyInstaller --clean --noconfirm WizScript.spec

# Isolated packaging smoke test
$env:WIZSCRIPT_DATA_DIR = Join-Path (Get-Location).Path 'build\smoke-data'
$smokeProcess = Start-Process -FilePath '.\dist\WizScript.exe' -ArgumentList '--smoke-test','build\smoke-final.json' -WindowStyle Hidden -PassThru -Wait
$smokeProcess.ExitCode
Get-Content .\build\smoke-final.json
Remove-Item Env:\WIZSCRIPT_DATA_DIR
```

## Research references

Earlier official research found Cyclops Lane, Firecat Alley, Dark Cave,
Nightside, and Haunted Cave listed as free, and Oasis listed as free for a
limited time. Availability is time-sensitive and region-specific: recheck
the current official pricing and confirm this account's actual gate in game.
Do not report a specific first paywall before observing it.

- Area pricing: https://eu.wizard101.com/game/areapricing
- Alternate official pricing: https://akamaieu.wizard101.com/game/areapricing
- School training information: https://akamaieu.wizard101.com/game/magic-schools
- Keyboard shortcuts (END = Commons, P = deck, X = interaction):
  https://www.wizard101.com/w101playersguide/keyboardshortcuts
- Wizwalker: https://starrfox.github.io/wizwalker/wizwalker.html
- Jev System One: https://docs.typesafe.ai/api
- Jev Choice: https://docs.typesafe.ai/primitives/choice
