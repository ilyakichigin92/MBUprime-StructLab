"""Prepare frozen Tcl/Tk before application imports run.

This import-time PyInstaller hook replaces ``sys.excepthook`` with an
uncaught-exception reporter, prepends the frozen bundle root to ``sys.path``,
points Tcl/Tk at bundled data, and eagerly imports tkinter modules so startup
failures are attributable. Boot logging is best-effort because an unavailable
diagnostic path must neither block startup nor mask the original exception.
"""

import os
import sys
import traceback


def _write_boot_log(message: str):
    boot_log = os.environ.get("MBUPRIME_BOOT_LOG")
    if not boot_log:
        return
    try:
        with open(boot_log, "a", encoding="utf-8") as fh:
            fh.write(message)
    except Exception:
        pass


def _capture_uncaught_exception(exc_type, exc_value, exc_traceback):
    _write_boot_log(
        "uncaught_exception:\n"
        + "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    )
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


sys.excepthook = _capture_uncaught_exception


if getattr(sys, "frozen", False):
    base = getattr(sys, "_MEIPASS", "")
    if base and base not in sys.path:
        sys.path.insert(0, base)
    tcl_library = os.path.join(base, "_tcl_data")
    tk_library = os.path.join(base, "_tk_data")
    if os.path.isdir(tcl_library):
        os.environ["TCL_LIBRARY"] = tcl_library
    if os.path.isdir(tk_library):
        os.environ["TK_LIBRARY"] = tk_library
    import tkinter  # noqa: F401
    import tkinter.filedialog  # noqa: F401
    import tkinter.font  # noqa: F401
    import tkinter.messagebox  # noqa: F401
    import tkinter.simpledialog  # noqa: F401
    import tkinter.ttk  # noqa: F401
    boot_log = os.environ.get("MBUPRIME_BOOT_LOG")
    if boot_log:
        try:
            with open(boot_log, "a", encoding="utf-8") as fh:
                fh.write(f"base={base}\n")
                fh.write(f"sys.path={sys.path!r}\n")
                fh.write(f"tcl_library={tcl_library} exists={os.path.isdir(tcl_library)}\n")
                fh.write(f"tk_library={tk_library} exists={os.path.isdir(tk_library)}\n")
                fh.write(
                    f"tkinter_dir={os.path.join(base, 'tkinter')} "
                    f"exists={os.path.isdir(os.path.join(base, 'tkinter'))}\n")
                fh.write("import_tkinter=ok\n")
        except Exception:
            pass
