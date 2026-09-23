"""Source, installed-wheel and PyInstaller access to shared application assets."""

from __future__ import annotations

import ctypes
from dataclasses import dataclass
import hashlib
import os
import platform
from pathlib import Path
import sys
import tkinter as tk
from tkinter import font as tkfont


ICON_NAME = "mbu_sl_laboratory_tile.ico"
CASCADIA_MONO_NAME = "Cascadia Mono"
CASCADIA_MONO_ASSET = "fonts/CascadiaMono.ttf"
CASCADIA_MONO_SHA256 = (
    "0e141cb99609f6f10ad05313fd1807d5cc9e28658dcbb35ab162e52ff67dc718")
DIAGRAM_FONT_ALPHABET = "ACGTUNacgtun0123456789-_/\\|()[]{}<>.:+' "
FR_PRIVATE = 0x10


class FontRegistrationError(RuntimeError):
    """The required private diagram font could not be loaded or verified."""


@dataclass
class PrivateFontRegistration:
    """One process-private Windows font registration."""

    path: Path
    _gdi32: object
    active: bool = True

    def unregister(self) -> None:
        if not self.active:
            return
        removed = self._gdi32.RemoveFontResourceExW(
            str(self.path), FR_PRIVATE, 0)
        self.active = False
        if not removed:
            raise FontRegistrationError(
                f"Windows could not unregister the private font: {self.path}")


def asset_path(name: str) -> Path:
    """Resolve an asset from the active source, wheel or frozen layout."""

    if hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "assets" / name
    installed_root = os.environ.get("MBUPRIME_ASSET_ROOT")
    if installed_root:
        return Path(installed_root) / name
    module_root = Path(__file__).resolve().parent
    package_assets = module_root / "mbuprime_structlab" / "assets"
    if package_assets.is_dir():
        return package_assets / name
    return module_root / "assets" / name


def apply_window_icon(window: tk.Misc) -> bool:
    """Apply the shared Windows icon, returning whether Tk accepted it."""

    icon = asset_path(ICON_NAME)
    try:
        window.iconbitmap(default=str(icon))
    except (AttributeError, OSError, tk.TclError):
        return False
    return True


def _load_gdi32():
    return ctypes.WinDLL("gdi32", use_last_error=True)


def validate_cascadia_mono(root: tk.Misc) -> None:
    """Require Tk to resolve Cascadia Mono as a true fixed-width font."""

    try:
        font = tkfont.Font(root=root, family=CASCADIA_MONO_NAME, size=10)
        resolved_family = str(font.actual("family"))
        widths = {character: font.measure(character)
                  for character in DIAGRAM_FONT_ALPHABET}
    except (AttributeError, RuntimeError, tk.TclError) as exc:
        raise FontRegistrationError(
            "Tk could not inspect the required Cascadia Mono font") from exc
    if resolved_family.casefold() != CASCADIA_MONO_NAME.casefold():
        raise FontRegistrationError(
            "Tk did not resolve the required Cascadia Mono font; "
            f"resolved {resolved_family!r} instead")
    distinct_widths = set(widths.values())
    if not distinct_widths or 0 in distinct_widths or len(distinct_widths) != 1:
        raise FontRegistrationError(
            "Cascadia Mono failed the fixed-width diagram alphabet check")


def register_private_cascadia_mono(root: tk.Misc) -> PrivateFontRegistration:
    """Register the bundled font for this process only and validate it in Tk."""

    if platform.system() != "Windows":
        raise FontRegistrationError(
            "The bundled private-font runtime is supported only on Windows")
    path = asset_path(CASCADIA_MONO_ASSET).resolve()
    if not path.is_file():
        raise FontRegistrationError(f"Bundled Cascadia Mono is missing: {path}")
    actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_hash != CASCADIA_MONO_SHA256:
        raise FontRegistrationError(
            "Bundled Cascadia Mono failed its SHA-256 integrity check")
    try:
        gdi32 = _load_gdi32()
        added = gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0)
    except (AttributeError, OSError) as exc:
        raise FontRegistrationError(
            "Windows could not load the bundled Cascadia Mono font") from exc
    if not added:
        raise FontRegistrationError(
            f"Windows rejected the bundled Cascadia Mono font: {path}")
    registration = PrivateFontRegistration(path, gdi32)
    try:
        validate_cascadia_mono(root)
    except Exception:
        registration.unregister()
        raise
    return registration
