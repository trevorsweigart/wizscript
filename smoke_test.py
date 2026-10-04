"""Exercise the real GUI, Tcl/Tk, and matplotlib inside a frozen build."""

import json
import logging
import os
import sys
from pathlib import Path


def run(report_path: str):
    report = {"ok": False, "frozen": bool(getattr(sys, "frozen", False))}
    application = None
    try:
        from app import App
        from gui.zone_graph_window import ZoneGraphWindow
        from runtime_paths import data_directory

        application = App()
        application._root.withdraw()
        graph = ZoneGraphWindow(application._root, application._zone_mapper)
        graph._win.withdraw()
        graph._canvas.draw()
        application._root.update()
        report.update(
            tcl_library=application._root.tk.eval("info library"),
            tk_library=application._root.tk.eval("set tk_library"),
            tcl_version=application._root.tk.eval("info patchlevel"),
            tk_version=application._root.tk.eval("package require Tk"),
            bundle_directory=getattr(sys, "_MEIPASS", None),
            data_directory=str(data_directory()),
            zone_map=application._zone_mapper.summary(),
            graph_rendered=True,
        )
        graph._on_close()
        # Exercise persistence in the smoke-test data directory without changing
        # the user's map. The build verification sets WIZSCRIPT_DATA_DIR.
        if os.environ.get("WIZSCRIPT_DATA_DIR"):
            application._zone_mapper.record_visit("WizScript/SmokeTest")
            from zone_mapper import ZoneMapper
            assert "WizScript/SmokeTest" in ZoneMapper().dump()["zones"]
            report["persistence_verified"] = True
        report["ok"] = True
    except Exception:
        import traceback
        report["error"] = traceback.format_exc()
        logging.exception("Packaged GUI smoke test failed")
    finally:
        if application is not None:
            application._on_close()
        Path(report_path).write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["ok"]:
        raise SystemExit(1)
