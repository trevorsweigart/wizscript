"""Locate bundled resources and persistent, writable application files."""

import os
import sys
from pathlib import Path


def data_directory() -> Path:
    """Keep EXE data outside PyInstaller's temporary extraction directory."""
    override = os.environ.get("WIZSCRIPT_DATA_DIR")
    if override:
        directory = Path(override).expanduser().resolve()
    elif getattr(sys, "frozen", False):
        directory = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "WizScript"
    else:
        directory = Path(__file__).resolve().parent
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def zone_map_path() -> str:
    """Seed the persistent map once; subsequent launches keep learned zones."""
    target = data_directory() / "zone_map.json"
    seed = Path(__file__).resolve().parent / "zone_map.json"
    if not target.exists() and seed.is_file() and seed != target:
        try:
            with target.open("xb") as output:
                output.write(seed.read_bytes())
        except FileExistsError:
            pass  # Another instance already seeded the map.
    return str(target)
