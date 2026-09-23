"""Fixed opt-in archive lifecycle checks inside the shipped GUI executable.

This exercises normal App methods with deterministic file/confirmation dialogs.
It is programmatic frozen-GUI qualification, not mouse or accessibility coverage.
No supplied code is executed; the input is parsed by the normal archive loader.
"""

from pathlib import Path
import time


def run_small_panel(root, gui, output_dir, log):
    """Analyze the public synthetic example in both locales and export/reopen it.

    Only file/confirmation dialogs are automated. Engines, analysis workers,
    rendering, export writers and archive readers follow the normal GUI paths.
    The explicitly selected output directory must be empty to preserve user data.
    """
    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Small-panel qualification requires an empty output directory")
    saved = []
    errors = []
    app = None

    def replace(owner, name, value):
        saved.append((owner, name, getattr(owner, name)))
        setattr(owner, name, value)

    def require(condition, message):
        if not condition:
            raise RuntimeError(f"Small-panel GUI qualification: {message}")

    def pump(done, stage, timeout=120, check_errors=True):
        deadline = time.monotonic() + timeout
        while True:
            require(time.monotonic() < deadline, f"{stage} timed out")
            root.update()
            if check_errors:
                require(not errors, f"{stage} callback/dialog error: {errors[:1]}")
            if done():
                log(f"stage=small-panel-{stage} status=ok")
                return
            time.sleep(0.002)

    try:
        replace(gui.gui_exports, "load_condition_presets", lambda *args, **kwargs: {})
        replace(gui.messagebox, "showinfo", lambda *args, **kwargs: None)
        replace(gui.messagebox, "showerror", lambda *args, **kwargs: errors.append(str(args)))
        replace(root, "report_callback_exception", lambda *args: errors.append(str(args)))
        app = gui.MBUprimeStructLabApp(root)
        require(app._language_code == "en", "public startup language is not English")
        app._confirm_analyzed_run_import = lambda loaded: True
        app.input_nb.select(1)
        app.bulk_text.delete("1.0", "end")
        app.bulk_text.insert("1.0", "F_demo = GCGCAAAAGCGC\nR_demo = GCGCTTTTGCGC")
        for language in ("en", "ru"):
            app._set_language(language)
            app.analyze()
            pump(lambda: not app._analysis_running() and not app._analysis_pending
                 and not app._render_jobs, f"{language}-analysis")
            report = app._last_report
            require(report is not None and report.complete, "analysis did not complete")
            groups = (report.hairpins, report.self_dimers, report.hetero_dimers)
            counts = [sum(len(item.structures) for item in group) for group in groups]
            require(counts == [5, 7, 4], f"unexpected synthetic structure counts: {counts}")
            app._show_structure_detail(report.hairpins[0].structures[0])
            require(bool(app._detail_text.get("1.0", "end-1c").strip()), "empty result detail")
            log(f"stage=small-panel-{language}-inspection status=ok oligos=2 "
                f"hairpins={counts[0]} self_dimers={counts[1]} heterodimers={counts[2]}")
            for kind, suffix in (("tsv", ".tsv"), ("run_archive", ".mbusl-run")):
                destination = output / (language + suffix)
                replace(gui.gui_exports.filedialog, "asksaveasfilename",
                        lambda _path=destination, **kwargs: str(_path))
                app._export(kind)
                require(not errors, f"{language} {kind} export failed: {errors[:1]}")
                require(destination.is_file() and destination.stat().st_size > 0,
                        f"{language} {kind} export missing")
            original_export = gui.gui_exports.format_export(
                "tsv", app._last_oligos, report, app._last_cond)
            archive = output / (language + ".mbusl-run")
            replace(gui.gui_exports, "choose_analyzed_run_path", lambda *args, **kwargs: str(archive))
            app._import_analyzed_run()
            pump(lambda: app._import_thread is None and not app._render_jobs,
                 f"{language}-archive-reopen")
            restored_export = gui.gui_exports.format_export(
                "tsv", app._last_oligos, app._last_report, app._last_cond)
            require(restored_export == original_export, "archive changed exported scientific results")
            log(f"stage=small-panel-{language}-export-roundtrip status=ok")
        log("stage=small-panel-qualification status=ok "
            "example=examples/small-panel/bulk.txt conditions=default-qPCR-TaqMan "
            "automation=programmatic-not-human-walkthrough")
    finally:
        try:
            if app is not None:
                try:
                    if app._analysis_running():
                        app.cancel_analysis()
                        pump(lambda: not app._analysis_running(), "cleanup", timeout=15,
                             check_errors=False)
                finally:
                    try:
                        app._cancel_render_jobs()
                    finally:
                        app.destroy()
        finally:
            for owner, name, value in reversed(saved):
                setattr(owner, name, value)


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
