"""
WizTeleport — entry point.

Launch the tkinter GUI for Wizard101 client management and teleportation.
"""

from app import App


def main():
    application = App()
    application.run()


if __name__ == "__main__":
    main()
