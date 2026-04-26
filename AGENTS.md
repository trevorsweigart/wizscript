# WizScript - Agent Reference Guide

## Overview
This repository contains a Python-based automation tool for the game Wizard101, utilizing the [wizwalker](https://github.com/Deimos-Wizard101/wizwalker) library to interact with the game client. The application features a Tkinter graphical user interface (GUI) and performs automated tasks such as questing, teleporting, and combat.

Wizwalker Documentation: https://starrfox.github.io/wizwalker/wizwalker.html

## System Architecture

The application is built with a decoupled architecture separating the GUI, game polling, and automation loops.

- **`main.py`**: The entry point. Initializes logging and starts the `App`.
- **`app.py`**: The central orchestrator. It manages the `Tkinter` UI on the main thread while spinning up a dedicated background thread for the `asyncio` event loop. This is critical because `wizwalker` is fully asynchronous, and running it on the main thread would block the UI.
- **`client_manager.py`**: Wraps `wizwalker.ClientHandler`. Manages the discovery of Wizard101 processes, hooking into them (`activate_hooks`), and gracefully disconnecting.
- **`game_info.py`**: Asynchronous data-fetching layer. Exposes functions to read game state (XYZ position, zone, quest data) safely.
- **`teleporter.py`**: Contains discrete teleportation functions (absolute, relative, quest objective).
- **`auto_quester.py`**: An asynchronous loop that automatically navigates to quest objectives, skips dialogs, and attempts to circumvent "snap-back" issues during teleportation by using coordinate offsets.
- **`auto_combat.py`**: An asynchronous loop that polls for battle state. When a battle is detected, it delegates to `combat.py`.
- **`combat.py`**: The combat decision engine (selects spells, handles the planning phase, and drives mouse/keyboard events).
- **`gui/`**: Contains the Tkinter panels representing different components (Client, Info, Teleport, Auto Quest, Auto Combat).

## WizWalker API Interaction & Best Practices

When writing or modifying code that interacts with the `wizwalker` library, adhere to the following best practices:

### 1. Asynchronous Execution
`wizwalker` relies heavily on `asyncio`. 
- **Rule**: All interaction with the `Client` object must be `await`ed within an async function.
- **Rule**: Do not run async code on the Tkinter main thread. Use `app._run_async()` or `asyncio.run_coroutine_threadsafe()` to dispatch tasks to the background loop.
- **Rule**: When updating the GUI from an async callback, use `root.after(0, callback)` to ensure the update occurs on the main Tkinter thread.

### 2. Error Handling and Memory Reading
Reading memory from an active game client can occasionally fail or return transient data (e.g., during loading screens or area transitions).
- **Rule**: Wrap API calls in `try...except` blocks when polling game state (see `game_info.py`). Swallow benign exceptions or return `None` to prevent the polling loop from crashing.
- **Rule**: Check if the client is in a loading screen (`await client.is_loading()`) before attempting complex actions or reading critical data.

### 3. Teleportation Quirks ("Snap-Back")
Wizard101 has server-side checks that can reject a teleport if it's placed inside a blocked area or structure, causing the player to "snap back" to their original position.
- **Rule**: When building teleportation logic, check the player's position after a short delay (e.g., 0.6s) to ensure the teleport was successful.
- **Rule**: Implement fallback offsets (e.g., ±50 on X/Y/Z) if a direct teleport to an objective fails (see `auto_quester.py`).

### 4. Hook Lifecycle
- **Rule**: Always call `await client.activate_hooks()` before attempting to use features like teleportation or memory writing.
- **Rule**: Ensure hooks are deactivated gracefully (`await client.close()`) when disconnecting or shutting down the application to prevent game client instability.

### 5. Input Simulation
- **Rule**: For keyboard input, use `await client.send_key(Keycode.SPACEBAR, duration)`. 
- **Rule**: For mouse interactions, use `async with client.mouse_handler:` to ensure the mouse is properly captured and released. Add brief sleeps (`await asyncio.sleep(...)`) between inputs to ensure the game engine registers them.

## Future Development Guidelines

1. **Modularity**: Keep automation logic (like a new minigame bot) in its own class with a clear `start()`/`stop()` lifecycle, similar to `AutoQuester` and `AutoCombat`.
2. **State Management**: Do not store game state long-term. Always poll fresh state from `game_info.py` or the `wizwalker` client directly, as the game environment changes rapidly.
3. **UI Decoupling**: Automation modules should accept callbacks (e.g., `set_status_callback`) to communicate with the UI, rather than importing UI components directly.
