"""Tk-first GUI bootstrap for Ubuntu 24.04 x86-64 source and onedir installs."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import platform
import sys
import traceback


SUPPORTED_MACHINES = frozenset({"X86_64", "AMD64"})
EXPECTED_FONT_SHA256 = (
    "0e141cb99609f6f10ad05313fd1807d5cc9e28658dcbb35ab162e52ff67dc718")


def _bundle_root() -> Path:
    bundle = getattr(sys, "_MEIPASS", None)
    return Path(bundle) if bundle else Path(__file__).resolve().parents[1]


def _configure_bundled_font() -> None:
    font_dir = _bundle_root() / "assets" / "fonts"
    config = font_dir / "linux-fonts.conf"
    font = font_dir / "CascadiaMono.ttf"
    if not config.is_file():
        raise RuntimeError(f"bundled font configuration is missing: {config}")
    if (not font.is_file()
            or hashlib.sha256(font.read_bytes()).hexdigest()
            != EXPECTED_FONT_SHA256):
        raise RuntimeError("bundled Cascadia Mono font hash mismatch")
    os.environ["FONTCONFIG_PATH"] = str(font_dir)
    os.environ["FONTCONFIG_FILE"] = str(config)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="MBUprime StructLab")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-test-log")
    return parser.parse_args(argv)


def _write_log(path: Path | None, message: str, *, reset: bool = False) -> None:
    if path is None:
        return
    mode = "w" if reset else "a"
    with path.open(mode, encoding="utf-8", newline="\n") as handle:
        handle.write(message.rstrip("\n") + "\n")


def _supported_host() -> bool:
    return (
        platform.system() == "Linux"
        and platform.machine().upper() in SUPPORTED_MACHINES
    )


_STAGE_HINTS = {
    "platform-check": "Use Ubuntu/Linux x86-64 with 64-bit CPython 3.12.",
    "font-config": "Reinstall the application to restore its bundled font files.",
    "tk-import": "Install Python Tk 8.6 support (on Ubuntu: python3-tk).",
    "app-assets-import": "Reinstall the application; its GUI modules are incomplete.",
    "tk-root": "Check DISPLAY/Wayland access and the desktop Tk installation.",
    "font-register": "Reinstall the application or repair the system fontconfig setup.",
    "gui-import": "Reinstall the application; its GUI payload is incomplete.",
    "self-test": "Review the self-test log for the failing engine or export stage.",
    "gui-run": "Review the startup log and retry from a supported desktop session.",
}


def _failure_message(stage: str, error: BaseException) -> str:
    hint = _STAGE_HINTS.get(stage, "Review the installation and startup environment.")
    return (
        f"MBUprime StructLab could not start at stage '{stage}'. {hint} "
        f"Technical detail: {type(error).__name__}: {error}")


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    log_path = Path(args.self_test_log) if args.self_test_log else None
    stage = "bootstrap-log"
    root = None
    try:
        _write_log(log_path, "MBUprime StructLab Linux bootstrap starting", reset=True)
        stage = "platform-check"
        if not _supported_host():
            _write_log(
                log_path,
                f"stage=platform-check status=failed system={platform.system()} "
                f"machine={platform.machine()}",
            )
            return 2
        _write_log(log_path, "stage=platform-check status=ok linux-x86_64")
        stage = "font-config"
        _configure_bundled_font()
        _write_log(log_path, "stage=font-config status=ok")
        stage = "tk-import"
        import tkinter as tk
        _write_log(log_path, "stage=tk-import status=ok")
        stage = "app-assets-import"
        import app_assets
        _write_log(log_path, "stage=app-assets-import status=ok")
        stage = "tk-root"
        root = tk.Tk()
        root.withdraw()
        _write_log(log_path, "stage=tk-root-ready status=ok")
        stage = "font-register"
        app_assets.validate_cascadia_mono(root)
        _write_log(log_path, "stage=font-register status=ok family=Cascadia Mono")
        stage = "gui-import"
        import primer_tool_gui

        _write_log(log_path, "stage=gui-import status=ok")
        if args.self_test:
            stage = "self-test"
            result = primer_tool_gui.frozen_self_test(
                root, lambda stage: _write_log(log_path, f"stage={stage} status=ok"))
            _write_log(
                log_path,
                "Self-test OK: "
                + ", ".join(f"{key}={value}" for key, value in result.items()),
            )
            root.destroy()
            root = None
            return 0
        stage = "gui-run"
        primer_tool_gui.main(root)
        return 0
    except Exception as error:
        failure = _failure_message(stage, error)
        diagnostic = (
            f"stage={stage} status=failed\n{failure}\n"
            + traceback.format_exc())
        try:
            _write_log(log_path, diagnostic)
        except OSError:
            pass
        print(failure, file=sys.stderr)
        return 1
    finally:
        if root is not None:
            try:
                root.destroy()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
