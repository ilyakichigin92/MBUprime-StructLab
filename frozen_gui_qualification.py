"""Fixed opt-in archive lifecycle checks inside the shipped GUI executable.

This exercises normal App methods with deterministic file/confirmation dialogs.
It is programmatic frozen-GUI qualification, not mouse or accessibility coverage.
No supplied code is executed; the input is parsed by the normal archive loader.
"""

from pathlib import Path
import time


def run(root, gui, archive_path, log):
    """Import, cancel, redraw and reject stale imports using a supplied archive."""
    path = Path(archive_path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("Archive qualification requires a regular input file")
    callbacks = []
    errors = []
    heartbeat = []
    saved = []
    app = None

    def replace(owner, name, value):
        saved.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def require(condition, message):
        if not condition:
            raise RuntimeError(f"Frozen GUI qualification: {message}")

    def beat():
        heartbeat.append(time.monotonic())
        callbacks.append(root.after(20, beat))

    def pump(done, stage, timeout=120, action=None, check_errors=True):
        started = time.monotonic()
        first = max(0, len(heartbeat) - 1)
        deadline = started + timeout
        if action is not None:
            action()
        while True:
            require(time.monotonic() < deadline, f"{stage} timed out")
            root.update()
            if check_errors:
                require(not errors, f"{stage} callback/dialog error: {errors[:1]}")
            if done():
                break
            time.sleep(0.002)
        samples = heartbeat[first:]
        gaps = [b - a for a, b in zip(samples, samples[1:])]
        log(f"stage=frozen-gui-{stage} status=ok seconds={time.monotonic()-started:.6f} "
            f"heartbeats={len(samples)} max_heartbeat_gap_seconds={max(gaps, default=0):.6f}")

    try:
        replace(gui.gui_exports, "load_condition_presets", lambda *args, **kwargs: {})
        replace(gui.gui_exports, "choose_analyzed_run_path", lambda *args, **kwargs: str(path))
        replace(gui.messagebox, "showinfo", lambda *args, **kwargs: None)
        replace(gui.messagebox, "showerror", lambda *args, **kwargs: errors.append(str(args)))
        replace(root, "report_callback_exception", lambda *args: errors.append(str(args)))
        app = gui.MBUprimeStructLabApp(root)
        app._confirm_analyzed_run_import = lambda loaded: True
        root.update()
        beat()

        app._import_analyzed_run()
        require(app._import_thread is not None, "import did not start a worker")
        callbacks.append(root.after(50, app.cancel_analysis))
        pump(lambda: app._import_thread is None, "cancel")
        require(app._last_report is None, "cancel committed a report")

        pump(lambda: app._import_thread is None and not app._render_jobs, "import-render",
             action=app._import_analyzed_run)
        report = app._last_report
        require(report is not None and report.complete, "import did not commit a complete report")
        count = sum(len(item.structures) for group in
                    (report.hairpins, report.self_dimers, report.hetero_dimers) for item in group)
        require(count > 0, "supplied archive has no structures")
        rendered = sum(len(panel["tree"].get_children(group))
                       for panel in app._panels.values()
                       for group in panel["tree"].get_children())
        require(rendered == count, f"rendered {rendered} rows for {count} structures")
        log(f"stage=frozen-gui-counts status=ok oligos={len(app._last_oligos)} structures={count}")

        pump(lambda: not app._render_jobs, "language", action=lambda: app._set_language("en"))
        require(app._last_report is report, "language changed the imported report")
        app._prime_risks_only_var.set(False)
        for key in ("severity", "kind"):
            app._filter_vars[key].set("All")
        app._filter_vars["search"].set("")
        pump(lambda: not app._render_jobs, "show-all", action=app._refresh_problems_first)
        tree = app._problems_panel["tree"]
        require(len(tree.get_children()) == count, "show-all lost structure rows")
        pump(lambda: not app._render_jobs, "sort", action=lambda: app._sort_problem_column("tm"))
        require(len(tree.get_children()) == count, "sort lost structure rows")

        original_text = app.bulk_text.get("1.0", "end-1c")
        app._import_analyzed_run()
        app.bulk_text.insert("end", "\n")
        pump(lambda: app._import_thread is None, "stale-input")
        require(app._last_report is report, "stale import replaced the current report")
        app.bulk_text.delete("1.0", "end")
        app.bulk_text.insert("1.0", original_text)
        require(not errors, "callback or error dialog occurred")
        log("stage=frozen-gui-qualification status=ok")
    finally:
        try:
            if app is not None:
                if app._import_thread is not None:
                    app.cancel_analysis()
                    # The regular poller joins only after cooperative decoding stops.
                    pump(lambda: app._import_thread is None, "cleanup", timeout=15, check_errors=False)
                app._cancel_render_jobs()
        finally:
            for callback in callbacks:
                try:
                    root.after_cancel(callback)
                except gui.tk.TclError:
                    pass
            for owner, name, value in reversed(saved):
                setattr(owner, name, value)
            if app is not None:
                app.destroy()
