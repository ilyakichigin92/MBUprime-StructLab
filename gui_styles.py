"""Visual tokens and Tk/ttk style configuration for the desktop GUI."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from gui_tokens_generated import (
    COLORS as GENERATED_COLORS,
    COMFORTABLE,
    DIMENSIONS,
    FONT_FAMILIES,
    SPACING,
)

COLOR = dict(GENERATED_COLORS)
SPACE = dict(SPACING)
FONT: dict[str, tuple[str, int]] = {}
DIMEN = {
    "sidebar_width": DIMENSIONS["sidebar_width"],
    "matrix_label_w": DIMENSIONS["matrix_label_width"],
    "matrix_header_h": DIMENSIONS["matrix_header_height"],
}

_COMFORTABLE = dict(COMFORTABLE)
FONT.update({
    "body": (FONT_FAMILIES["body"], _COMFORTABLE["body_size"]),
    "body_bold": (f'{FONT_FAMILIES["body"]} Semibold',
                  _COMFORTABLE["body_size"]),
    "mono": (FONT_FAMILIES["mono"], _COMFORTABLE["mono_size"]),
})
DIMEN.update({
    "matrix_cell_w": _COMFORTABLE["matrix_cell_w"],
    "matrix_cell_h": _COMFORTABLE["matrix_cell_h"],
})

SEVERITY_COLOR = {
    "ok": COLOR["ok_bg"],
    "caution": COLOR["caution_bg"],
    "problem": COLOR["problem_bg"],
    "unclassified": COLOR["surface_muted"],
}
SEVERITY_FOREGROUND = {
    "ok": COLOR["ok_fg"],
    "caution": COLOR["caution_fg"],
    "problem": COLOR["problem_fg"],
    "unclassified": COLOR["text_muted"],
}
UI = {
    "background": COLOR["app_bg"],
    "surface": COLOR["surface"],
    "surface_subtle": COLOR["surface_subtle"],
    "surface_muted": COLOR["surface_muted"],
    "border": COLOR["border"],
    "border_strong": COLOR["border_strong"],
    "text": COLOR["text"],
    "muted": COLOR["text_muted"],
    "accent": COLOR["accent"],
    "accent_hover": COLOR["accent_hover"],
    "accent_soft": COLOR["accent_soft"],
    "selected": COLOR["selected"],
    "focus": COLOR["focus"],
    "detail_bg": COLOR["surface_subtle"],
    "summary_idle_bg": COLOR["surface_muted"],
    "summary_info_bg": COLOR["accent_soft"],
    "matrix_header_bg": COLOR["surface_muted"],
    "matrix_grid": COLOR["border"],
}
STYLE_CONTRACT = {
    "frames": ("Workbench.TFrame", "Rail.TFrame", "Workspace.TFrame", "Lab.TFrame"),
    "labels": ("Lab.TLabel", "Muted.TLabel", "MetricTitle.TLabel",
               "MetricValue.TLabel", "Status.TLabel"),
    "sections": ("Section.TLabelframe",),
    "controls": ("Accent.TButton", "Secondary.TButton", "Danger.TButton",
                 "Lab.TEntry", "Lab.TCombobox", "Lab.TCheckbutton",
                 "Secondary.TMenubutton"),
    "data": ("Lab.Treeview", "Lab.TNotebook", "Status.Horizontal.TProgressbar"),
}
SUMMARY_STATES = {
    "idle": (UI["summary_idle_bg"], UI["text"]),
    "info": (UI["summary_info_bg"], UI["accent"]),
    "ok": (SEVERITY_COLOR["ok"], SEVERITY_FOREGROUND["ok"]),
    "caution": (SEVERITY_COLOR["caution"], SEVERITY_FOREGROUND["caution"]),
    "problem": (SEVERITY_COLOR["problem"], SEVERITY_FOREGROUND["problem"]),
}


def configure_styles(master) -> ttk.Style:
    """Configure the named ttk styles used by the GUI and return the style."""

    profile = _COMFORTABLE
    master.configure(bg=UI["background"])
    style = ttk.Style(master)
    try:
        if "clam" in style.theme_names():
            style.theme_use("clam")
    except tk.TclError:
        pass

    def configure(name: str, **options) -> None:
        try:
            style.configure(name, **options)
        except tk.TclError:
            pass

    def map_style(name: str, **options) -> None:
        try:
            style.map(name, **options)
        except tk.TclError:
            pass

    style.configure(".", font=FONT["body"])

    for frame_style, background in (
        ("Workbench.TFrame", UI["background"]),
        ("Rail.TFrame", UI["background"]),
        ("Sidebar.TFrame", UI["background"]),
        ("Workspace.TFrame", UI["background"]),
        ("Lab.TFrame", UI["surface"]),
        ("Toolbar.TFrame", UI["surface"]),
        ("TFrame", UI["background"]),
    ):
        configure(frame_style, background=background)

    for label_style, foreground, background, font in (
        ("Lab.TLabel", UI["text"], UI["surface"], FONT["body"]),
        ("TLabel", UI["text"], UI["background"], FONT["body"]),
        ("Muted.TLabel", UI["muted"], UI["background"], FONT["body"]),
        ("MetricTitle.TLabel", UI["muted"], UI["surface"], FONT["body"]),
        ("MetricValue.TLabel", UI["text"], UI["surface"], FONT["body_bold"]),
        ("Status.TLabel", UI["muted"], UI["surface"], FONT["body"]),
    ):
        configure(label_style, background=background, foreground=foreground,
                  font=font)

    for frame_style in ("TLabelframe", "Section.TLabelframe"):
        configure(frame_style, background=UI["surface"],
                  bordercolor=UI["border"], relief="solid")
    for label_style in ("TLabelframe.Label", "Section.TLabelframe.Label"):
        configure(label_style, background=UI["surface"],
                  foreground=UI["text"], font=FONT["body_bold"])

    for notebook_style in ("TNotebook", "Lab.TNotebook"):
        configure(notebook_style, background=UI["background"], borderwidth=0,
                  tabmargins=(0, 0, 0, 0))
    for tab_style in ("TNotebook.Tab", "Lab.TNotebook.Tab"):
        configure(tab_style, padding=(SPACE["md"], profile["control_pad_y"]),
                  foreground=UI["muted"], background=UI["surface_muted"],
                  borderwidth=0)
        map_style(
            tab_style,
            background=[("selected", UI["surface"]),
                        ("active", UI["surface_subtle"])],
            foreground=[("selected", UI["text"]),
                        ("active", UI["text"])],
        )

    button_padding = (SPACE["sm"], profile["control_pad_y"])
    configure("TButton", padding=button_padding, relief="flat",
              borderwidth=1, background=UI["surface_muted"],
              foreground=UI["text"], focusthickness=1,
              focuscolor=UI["focus"])
    configure("Secondary.TButton", padding=button_padding, relief="flat",
              borderwidth=1, background=UI["surface_muted"],
              foreground=UI["text"], focusthickness=1,
              focuscolor=UI["focus"])
    configure("Accent.TButton", padding=(SPACE["md"],
                                           profile["control_pad_y"] + 1),
              relief="flat", borderwidth=1, background=UI["accent"],
              foreground=COLOR["on_accent"], font=FONT["body_bold"],
              focusthickness=1, focuscolor=UI["focus"])
    configure("Danger.TButton", padding=button_padding, relief="flat",
              borderwidth=1, background=COLOR["problem_bg"],
              foreground=COLOR["problem_fg"], focusthickness=1,
              focuscolor=UI["focus"])
    for button_style, active_background, active_foreground in (
        ("TButton", UI["selected"], UI["text"]),
        ("Secondary.TButton", UI["selected"], UI["text"]),
        ("Accent.TButton", UI["accent_hover"], COLOR["on_accent"]),
        ("Danger.TButton", COLOR["problem_active"], COLOR["problem_fg"]),
    ):
        map_style(
            button_style,
            background=[("pressed", active_background),
                        ("active", active_background),
                        ("disabled", UI["surface_muted"])],
            foreground=[("active", active_foreground),
                        ("disabled", UI["muted"])],
        )

    for input_style in ("Lab.TEntry", "TEntry"):
        configure(input_style, fieldbackground=UI["surface"],
                  foreground=UI["text"], insertcolor=UI["text"],
                  bordercolor=UI["border"], lightcolor=UI["border"],
                  darkcolor=UI["border"], padding=(SPACE["xs"], 1))
        map_style(input_style, bordercolor=[("focus", UI["focus"])])
    for combo_style in ("Lab.TCombobox", "TCombobox"):
        configure(combo_style, fieldbackground=UI["surface"],
                  foreground=UI["text"], background=UI["surface_muted"],
                  bordercolor=UI["border"], arrowcolor=UI["muted"],
                  padding=(SPACE["xs"], 1))
        map_style(combo_style, bordercolor=[("focus", UI["focus"])],
                  fieldbackground=[("readonly", UI["surface"])])
    for check_style in ("Lab.TCheckbutton", "TCheckbutton"):
        configure(check_style, background=UI["surface"], foreground=UI["text"])
        map_style(check_style, background=[("active", UI["surface"])])

    configure("Secondary.TMenubutton", padding=button_padding, relief="flat",
              borderwidth=1, background=UI["surface_muted"],
              foreground=UI["text"])
    map_style("Secondary.TMenubutton",
              background=[("active", UI["selected"]),
                          ("disabled", UI["surface_muted"])],
              foreground=[("disabled", UI["muted"])])

    for tree_style in ("Treeview", "Lab.Treeview"):
        configure(tree_style, background=UI["surface"],
                  fieldbackground=UI["surface"], foreground=UI["text"],
                  bordercolor=UI["border"], rowheight=profile["rowheight"])
        map_style(tree_style,
                  background=[("selected", UI["selected"])],
                  foreground=[("selected", UI["text"])])
    for heading_style in ("Treeview.Heading", "Lab.Treeview.Heading"):
        configure(heading_style, background=UI["surface_muted"],
                  foreground=UI["text"], font=FONT["body_bold"],
                  relief="flat", borderwidth=1)

    for scrollbar_style in (
        "Lab.Vertical.TScrollbar", "Lab.Horizontal.TScrollbar",
        "Vertical.TScrollbar", "Horizontal.TScrollbar",
    ):
        configure(scrollbar_style, background=UI["surface_muted"],
                  troughcolor=UI["surface"], bordercolor=UI["border"],
                  arrowcolor=UI["muted"], relief="flat")

    configure("Status.Horizontal.TProgressbar",
              troughcolor=UI["surface_muted"], background=UI["accent"],
              bordercolor=UI["border"], lightcolor=UI["accent"],
              darkcolor=UI["accent"])
    return style

def configure_menu(menu) -> None:
    """Apply the shared palette to a Tk menu."""

    menu.configure(
        bg=UI["surface"],
        fg=UI["text"],
        activebackground=UI["selected"],
        activeforeground=UI["text"],
        disabledforeground=UI["muted"],
        relief="solid",
        borderwidth=1,
    )
