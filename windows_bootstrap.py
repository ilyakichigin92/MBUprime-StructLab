"""Tk-first Windows x64 bootstrap for the packaged application."""

from __future__ import annotations

import argparse
import importlib
import os
from pathlib import Path
import platform
import sys
import traceback
import tkinter as tk
from tkinter import messagebox

import app_assets


SUPPORTED_SYSTEM = "Windows"
SUPPORTED_MACHINES = frozenset({"AMD64", "X86_64"})
PLATFORM_LIMITATION = "MBUprime StructLab supports Windows x64 (AMD64) only."
PLATFORM_LIMITATION_RU = (
    "MBUprime StructLab \u043f\u043e\u0434\u0434\u0435\u0440\u0436\u0438\u0432\u0430\u0435\u0442 "
    "\u0442\u043e\u043b\u044c\u043a\u043e Windows x64 (AMD64).")
RU_LANGUAGE = "\u0420\u0443\u0441\u0441\u043a\u0438\u0439"
UNSUPPORTED_PLATFORM_TITLE = (
    "Unsupported platform / "
    "\u041d\u0435\u043f\u043e\u0434\u0434\u0435\u0440\u0436\u0438\u0432\u0430\u0435\u043c\u0430\u044f "
    "\u043f\u043b\u0430\u0442\u0444\u043e\u0440\u043c\u0430")
MOVE_APPLICATION_TITLE = (
    "Move application / "
    "\u041f\u0435\u0440\u0435\u043c\u0435\u0441\u0442\u0438\u0442\u0435 "
    "\u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435")
PATH_LABEL = "Path / \u041f\u0443\u0442\u044c"
FONT_UNAVAILABLE_TITLE = (
    "Font unavailable / "
    "\u0428\u0440\u0438\u0444\u0442 \u043d\u0435\u0434\u043e\u0441\u0442\u0443\u043f\u0435\u043d")
ERROR_TITLE_RU = "\u041e\u0448\u0438\u0431\u043a\u0430"
RELOCATION_MESSAGE = (
    "English\n"
    "This packaged build cannot run from a folder whose resolved path contains "
    "non-ASCII characters. Move the complete application folder to an ASCII-only "
    "path, for example C:\\MBUprimeStructLab, and start it again.\n\n"
    f"{RU_LANGUAGE}\n"
    "\u042d\u0442\u0430 \u0441\u0431\u043e\u0440\u043a\u0430 \u043d\u0435 \u0437\u0430\u043f\u0443\u0441\u043a\u0430\u0435\u0442\u0441\u044f \u0438\u0437 \u043f\u0430\u043f\u043a\u0438, \u0432 \u043f\u043e\u043b\u043d\u043e\u043c \u043f\u0443\u0442\u0438 \u043a\u043e\u0442\u043e\u0440\u043e\u0439 \u0435\u0441\u0442\u044c \u0441\u0438\u043c\u0432\u043e\u043b\u044b "
    "\u0432\u043d\u0435 ASCII. \u041f\u0435\u0440\u0435\u043c\u0435\u0441\u0442\u0438\u0442\u0435 \u0432\u0441\u044e \u043f\u0430\u043f\u043a\u0443 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u044f \u0432 \u043f\u0443\u0442\u044c \u0442\u043e\u043b\u044c\u043a\u043e \u0441 \u043b\u0430\u0442\u0438\u043d\u0441\u043a\u0438\u043c\u0438 "
    "\u0431\u0443\u043a\u0432\u0430\u043c\u0438 \u0438 \u0446\u0438\u0444\u0440\u0430\u043c\u0438, \u043d\u0430\u043f\u0440\u0438\u043c\u0435\u0440 C:\\MBUprimeStructLab, \u0438 \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u0435 \u0441\u043d\u043e\u0432\u0430.")
PLATFORM_MESSAGE = (
    f"English\n{PLATFORM_LIMITATION}\n\n"
    f"{RU_LANGUAGE}\n{PLATFORM_LIMITATION_RU}")
FONT_FAILURE_MESSAGE = (
    "English\nThe required bundled Cascadia Mono font could not be loaded. "
    "Reinstall the official application package.\n\n"
    f"{RU_LANGUAGE}\n\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u0433\u0440\u0443\u0437\u0438\u0442\u044c \u043e\u0431\u044f\u0437\u0430\u0442\u0435\u043b\u044c\u043d\u044b\u0439 \u0432\u0441\u0442\u0440\u043e\u0435\u043d\u043d\u044b\u0439 \u0448\u0440\u0438\u0444\u0442 Cascadia Mono. "
    "\u041f\u0435\u0440\u0435\u0443\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u0435 \u043e\u0444\u0438\u0446\u0438\u0430\u043b\u044c\u043d\u044b\u0439 \u043f\u0430\u043a\u0435\u0442 \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u044f.")
STARTUP_FAILURE_MESSAGE = (
    "English\nThe application could not start. Run the packaged self-test and "
    "reinstall the official package if the failure repeats.\n\n"
    f"{RU_LANGUAGE}\n\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u0437\u0430\u043f\u0443\u0441\u0442\u0438\u0442\u044c \u043f\u0440\u0438\u043b\u043e\u0436\u0435\u043d\u0438\u0435. \u0412\u044b\u043f\u043e\u043b\u043d\u0438\u0442\u0435 \u0441\u0430\u043c\u043e\u043f\u0440\u043e\u0432\u0435\u0440\u043a\u0443 \u043f\u0430\u043a\u0435\u0442\u0430 \u0438 "
    "\u043f\u0435\u0440\u0435\u0443\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u0435 \u043e\u0444\u0438\u0446\u0438\u0430\u043b\u044c\u043d\u044b\u0439 \u043f\u0430\u043a\u0435\u0442, \u0435\u0441\u043b\u0438 \u043e\u0448\u0438\u0431\u043a\u0430 \u043f\u043e\u0432\u0442\u043e\u0440\u044f\u0435\u0442\u0441\u044f.")


class _BootLogger:
    def __init__(self, path: str | None):
        self.path = Path(path).expanduser() if path else None

    def start(self) -> None:
        if self.path is not None:
            with self.path.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write("MBUprime StructLab bootstrap starting\n")

    def __call__(self, message: str) -> None:
        if self.path is not None:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(message.rstrip("\n") + "\n")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="MBUprime StructLab")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--self-test-log")
    parser.add_argument("--qualify-archive", type=Path,
                        help="Exercise packaged GUI import/export using an analyzed-run archive")
    args = parser.parse_args(argv)
    if args.qualify_archive is not None and not (args.self_test and args.self_test_log):
        parser.error("--qualify-archive requires --self-test and --self-test-log")
    return args


def unsupported_platform(system: str | None = None,
                         machine: str | None = None) -> bool:
    """Return whether the runtime lies outside the Windows x64 contract."""

    runtime_system = platform.system() if system is None else system
    runtime_machine = platform.machine() if machine is None else machine
    return (runtime_system != SUPPORTED_SYSTEM
            or runtime_machine.upper() not in SUPPORTED_MACHINES)


def frozen_runtime_paths() -> tuple[Path, ...]:
    """Return resolved packaged install/bundle paths relevant to native imports."""

    if not getattr(sys, "frozen", False):
        return ()
    candidates = [Path(sys.executable).resolve().parent]
    bundle = getattr(sys, "_MEIPASS", None)
    if bundle:
        resolved_bundle = Path(bundle).resolve()
        if resolved_bundle not in candidates:
            candidates.append(resolved_bundle)
    return tuple(candidates)


def first_non_ascii_path(paths: tuple[Path, ...]) -> Path | None:
    """Return the first path that cannot be represented as strict ASCII."""

    for path in paths:
        try:
            str(path).encode("ascii", errors="strict")
        except UnicodeEncodeError:
            return path
    return None


def _show_error(root: tk.Tk, title: str, message: str) -> None:
    messagebox.showerror(title, message, parent=root)


def _run_platform_preflight(root: tk.Tk, args: argparse.Namespace,
                            log: _BootLogger) -> int | None:
    if unsupported_platform():
        log(f"stage=platform-check status=failed system={platform.system()} "
            f"machine={platform.machine()}")
        if not args.self_test:
            _show_error(root, UNSUPPORTED_PLATFORM_TITLE, PLATFORM_MESSAGE)
        return 2
    log("stage=platform-check status=ok windows-x64")
    return None


def _run_frozen_path_preflight(root: tk.Tk, args: argparse.Namespace,
                               log: _BootLogger) -> int | None:
    invalid_path = first_non_ascii_path(frozen_runtime_paths())
    if invalid_path is not None:
        log(f"stage=install-path-check status=failed path={invalid_path}")
        if not args.self_test:
            _show_error(root, MOVE_APPLICATION_TITLE,
                        f"{RELOCATION_MESSAGE}\n\n{PATH_LABEL}:\n{invalid_path}")
        return 3
    log("stage=install-path-check status=ok")
    return None


def _run_frozen_self_test(gui, root: tk.Tk, log: _BootLogger) -> None:
    result = gui.frozen_self_test(root, log)
    message = "Self-test OK: " + ", ".join(
        f"{key}={value}" for key, value in result.items())
    log(message)
    print(message)


def _run_normal_startup(gui, root: tk.Tk) -> None:
    gui.main(root)


def _cleanup_application(root: tk.Tk, registration, log: _BootLogger,
                         exit_code: int) -> int:
    try:
        if root.winfo_exists():
            root.destroy()
    except (AttributeError, RuntimeError, tk.TclError):
        pass
    if registration is not None:
        try:
            registration.unregister()
            log("stage=font-unregister status=ok")
        except Exception:
            exit_code = 1
            log("stage=font-unregister status=failed\n" + traceback.format_exc())
    return exit_code


def _run_application(root: tk.Tk, args: argparse.Namespace,
                     log: _BootLogger) -> int:
    registration = None
    exit_code = 1
    try:
        log("stage=font-register status=starting")
        registration = app_assets.register_private_cascadia_mono(root)
        log("stage=font-register status=ok family=Cascadia Mono")
        log("stage=gui-import status=starting")
        gui = importlib.import_module("primer_tool_gui")
        log("stage=gui-import status=ok")
        if args.self_test:
            _run_frozen_self_test(gui, root, log)
            if getattr(args, "qualify_archive", None) is not None:
                qualification = importlib.import_module("frozen_gui_qualification")
                qualification.run(root, gui, args.qualify_archive, log)
        else:
            _run_normal_startup(gui, root)
        exit_code = 0
    except app_assets.FontRegistrationError:
        log("stage=font-register status=failed\n" + traceback.format_exc())
        if not args.self_test:
            _show_error(root, FONT_UNAVAILABLE_TITLE, FONT_FAILURE_MESSAGE)
    except Exception:
        label = "Self-test" if args.self_test else "Startup"
        failure = f"{label} FAILED:\n{traceback.format_exc()}"
        log(failure)
        print(failure, file=sys.stderr)
        if not args.self_test:
            _show_error(root, f"{label} failed / {ERROR_TITLE_RU}",
                        STARTUP_FAILURE_MESSAGE)
    finally:
        exit_code = _cleanup_application(root, registration, log, exit_code)
    return exit_code


def _run_after_tk(root: tk.Tk, args: argparse.Namespace,
                  log: _BootLogger) -> int:
    log("stage=tk-root-ready")
    platform_result = _run_platform_preflight(root, args, log)
    if platform_result is not None:
        return platform_result
    path_result = _run_frozen_path_preflight(root, args, log)
    if path_result is not None:
        return path_result
    return _run_application(root, args, log)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    log_path = args.self_test_log or os.environ.get("MBUPRIME_BOOT_LOG")
    log = _BootLogger(log_path)
    try:
        log.start()
    except OSError as exc:
        print(f"Cannot open bootstrap log {log_path!r}: {exc}", file=sys.stderr)
    try:
        root = tk.Tk()
        root.withdraw()
    except Exception:
        failure = "Tk root creation FAILED:\n" + traceback.format_exc()
        try:
            log(failure)
        except OSError:
            pass
        print(failure, file=sys.stderr)
        return 1
    try:
        try:
            return _run_after_tk(root, args, log)
        except OSError:
            print("Bootstrap logging failed", file=sys.stderr)
            return 1
    finally:
        try:
            if root.winfo_exists():
                root.destroy()
        except (AttributeError, RuntimeError, tk.TclError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
