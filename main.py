"""
WizTeleport — entry point.

Launch the tkinter GUI for Wizard101 client management and teleportation.
"""

import logging
import argparse
import sys

from runtime_paths import data_directory


def main():
    parser = argparse.ArgumentParser(description="WizScript")
    parser.add_argument("--smoke-test", metavar="REPORT", help="Check the packaged GUI and write a JSON report, then exit")
    args = parser.parse_args()
    handlers = [logging.FileHandler(data_directory() / "wizscript.log", encoding="utf-8")]
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
        handlers=handlers,
    )
    # Import after logging is configured so startup failures can be diagnosed.
    if args.smoke_test:
        from smoke_test import run
        return run(args.smoke_test)
    from app import App
    application = App()
    application.run()


if __name__ == "__main__":
    main()
