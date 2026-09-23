"""Qualification failures must still cancel workers and release their widgets."""
from types import SimpleNamespace

import pytest

from frozen_gui_qualification import run_small_panel


@pytest.mark.parametrize("cancel_fails", [False, True])
def test_small_panel_callback_failure_always_cleans_up(tmp_path, cancel_fails):
    events = []
    noop = lambda *args, **kwargs: None
    dialogs = SimpleNamespace(showinfo=noop, showerror=noop)
    exports = SimpleNamespace(load_condition_presets=noop)
    root = SimpleNamespace(report_callback_exception=noop)

    class App:
        _language_code = "en"
        input_nb = SimpleNamespace(select=noop)
        bulk_text = SimpleNamespace(delete=noop, insert=noop)
        running = True

        def _set_language(self, language):
            pass

        def analyze(self):
            dialogs.showerror("injected callback failure")

        def _analysis_running(self):
            return self.running

        def cancel_analysis(self):
            events.append("cancel")
            if cancel_fails:
                raise RuntimeError("injected cancellation failure")

        def _cancel_render_jobs(self):
            events.append("cancel-render")

        def destroy(self):
            events.append("destroy")

    app = App()
    updates = 0

    def update():
        nonlocal updates
        updates += 1
        if updates == 3:
            app.running = False

    root.update = update
    gui = SimpleNamespace(gui_exports=exports, messagebox=dialogs,
                          MBUprimeStructLabApp=lambda root: app)
    with pytest.raises(RuntimeError, match="callback/dialog error|cancellation failure"):
        run_small_panel(root, gui, tmp_path, noop)
    assert events == ["cancel", "cancel-render", "destroy"]
    assert dialogs.showinfo is noop and dialogs.showerror is noop
    assert exports.load_condition_presets is noop
    assert root.report_callback_exception is noop
    if not cancel_fails:
        assert updates == 3
        assert not app.running
