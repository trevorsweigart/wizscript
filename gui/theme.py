"""
Theme — clean dark theme for ttk widgets.
"""

import tkinter as tk
from tkinter import ttk


# ------------------------------------------------------------------
# Color palette
# ------------------------------------------------------------------
COLORS = {
    "bg":           "#2b2b2b",
    "bg_light":     "#3c3f41",
    "bg_input":     "#45494a",
    "fg":           "#bbbbbb",
    "fg_bright":    "#e8e8e8",
    "fg_dim":       "#787878",
    "accent":       "#4a7ebf",
    "accent_hover": "#5a93d4",
    "green":        "#589e58",
    "green_hover":  "#6aad6a",
    "red":          "#c75450",
    "red_hover":    "#d46966",
    "yellow":       "#d4a84a",
    "border":       "#3c3f41",
    "separator":    "#505050",
}


def apply_theme(root: tk.Tk):
    """Apply a clean dark theme to the application."""
    root.configure(bg=COLORS["bg"])

    style = ttk.Style(root)
    style.theme_use("clam")

    # -- Base --
    style.configure(".",
        background=COLORS["bg_light"],
        foreground=COLORS["fg"],
        fieldbackground=COLORS["bg_input"],
        borderwidth=0,
        focuscolor=COLORS["accent"],
        font=("Segoe UI", 10),
    )

    # -- Frames --
    # Outer container uses the darker bg; everything inside panels uses bg_light
    style.configure("TFrame",        background=COLORS["bg_light"])
    style.configure("Outer.TFrame",  background=COLORS["bg"])

    # -- Labels --
    style.configure("TLabel",
        background=COLORS["bg_light"],
        foreground=COLORS["fg"],
        borderwidth=0,
    )
    style.configure("Status.TLabel",
        background=COLORS["bg_light"],
        foreground=COLORS["yellow"],
        font=("Segoe UI", 9),
    )
    style.configure("Value.TLabel",
        background=COLORS["bg_light"],
        foreground=COLORS["fg_bright"],
        font=("Consolas", 10),
    )

    # -- Buttons --
    style.configure("TButton",
        background=COLORS["accent"],
        foreground=COLORS["fg_bright"],
        padding=(10, 4),
        font=("Segoe UI", 9, "bold"),
        borderwidth=0,
    )
    style.map("TButton",
        background=[
            ("active",   COLORS["accent_hover"]),
            ("disabled", COLORS["bg_input"]),
        ],
        foreground=[("disabled", COLORS["fg_dim"])],
    )

    style.configure("Danger.TButton",  background=COLORS["red"])
    style.map("Danger.TButton",  background=[("active", COLORS["red_hover"])])

    style.configure("Success.TButton", background=COLORS["green"])
    style.map("Success.TButton", background=[("active", COLORS["green_hover"])])

    style.configure("Small.TButton",
        background=COLORS["bg_input"],
        foreground=COLORS["fg"],
        padding=(6, 2),
        font=("Segoe UI", 8),
    )
    style.map("Small.TButton",
        background=[("active", COLORS["accent_hover"])],
    )

    # -- Combobox --
    style.configure("TCombobox",
        fieldbackground=COLORS["bg_input"],
        background=COLORS["bg_light"],
        foreground=COLORS["fg_bright"],
        selectbackground=COLORS["accent"],
        selectforeground=COLORS["fg_bright"],
        arrowcolor=COLORS["fg"],
        borderwidth=0,
        padding=3,
    )
    style.map("TCombobox",
        fieldbackground=[("readonly", COLORS["bg_input"])],
        selectbackground=[("readonly", COLORS["bg_input"])],
        bordercolor=[("focus", COLORS["accent"])],
    )
    root.option_add("*TCombobox*Listbox.background",       COLORS["bg_input"])
    root.option_add("*TCombobox*Listbox.foreground",       COLORS["fg_bright"])
    root.option_add("*TCombobox*Listbox.selectBackground", COLORS["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", COLORS["fg_bright"])

    # -- Entry --
    style.configure("TEntry",
        fieldbackground=COLORS["bg_input"],
        foreground=COLORS["fg_bright"],
        insertcolor=COLORS["fg_bright"],
        borderwidth=0,
        padding=3,
    )
    style.map("TEntry",
        bordercolor=[("focus", COLORS["accent"])],
        lightcolor=[("focus",  COLORS["bg_input"])],
        darkcolor=[("focus",   COLORS["bg_input"])],
    )

    # -- Radiobutton --
    style.configure("TRadiobutton",
        background=COLORS["bg_light"],
        foreground=COLORS["fg"],
        indicatorcolor=COLORS["bg_input"],
        indicatormargin=4,
        borderwidth=0,
    )
    style.map("TRadiobutton",
        background=[("active", COLORS["bg_light"])],
        indicatorcolor=[("selected", COLORS["accent"])],
    )

    # -- LabelFrame --
    style.configure("TLabelframe",
        background=COLORS["bg_light"],
        bordercolor=COLORS["border"],
        borderwidth=1,
        relief="flat",
    )
    style.configure("TLabelframe.Label",
        background=COLORS["bg_light"],
        foreground=COLORS["fg_bright"],
        font=("Segoe UI", 10, "bold"),
    )

    # -- Separator --
    style.configure("TSeparator", background=COLORS["separator"])
