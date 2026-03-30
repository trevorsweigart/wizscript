"""
WizTeleport — entry point.

Launch the tkinter GUI for Wizard101 client management and teleportation.
"""

import logging
from app import App


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler("wizscript.log")
        ]
    )
    application = App()
    application.run()


if __name__ == "__main__":
    main()
