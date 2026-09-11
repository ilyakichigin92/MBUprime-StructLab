"""Original composition with usable overflowing panes; no desktop activation."""
import ctypes
import os
import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import primer_tool_gui as gui
from test_detail_keyboard_access_contract import prevent_test_window_activation

pytestmark = [pytest.mark.gui, pytest.mark.skipif(
    sys.platform != 'win32', reason='Windows non-activating Tk isolation')]


def assert_revealed(widget, pane):
    pane.reveal(widget)
    pane.update_idletasks()
    canvas = pane.canvas
    assert widget.winfo_viewable(), repr((str(widget), widget.winfo_width(),
        widget.winfo_height(), pane.content.winfo_width(), pane.content.winfo_height(),
        pane.content.winfo_reqheight(), canvas.cget('scrollregion'), canvas.yview()))
    for origin, extent, viewport, size in (
        (widget.winfo_rootx(), widget.winfo_width(), canvas.winfo_rootx(), canvas.winfo_width()),
        (widget.winfo_rooty(), widget.winfo_height(), canvas.winfo_rooty(), canvas.winfo_height()),
    ):
        visible = min(origin + extent, viewport + size) - max(origin, viewport)
        assert visible >= min(extent, size) - 2, repr((
            str(widget), origin, extent, viewport, size, canvas.cget('scrollregion'),
            canvas.xview(), canvas.yview(), pane.content.winfo_height(),
            pane.content.winfo_reqheight()))
    if isinstance(widget, (gui.ttk.Button, gui.ttk.Menubutton)):
        center = widget.winfo_rootx() + widget.winfo_width() / 2
        assert canvas.winfo_rootx() <= center <= canvas.winfo_rootx() + canvas.winfo_width()


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@pytest.mark.parametrize('scaling', [1.333, 2.0])
@pytest.mark.parametrize('size', ['1280x720', '1024x640', '800x600', '1920x1080'])
def test_original_panes_scroll_without_collapsing_content(
        scaling, size, prevent_test_window_activation):
    root = gui.tk.Tk()
    callback_errors = []
    root.report_callback_exception = lambda *error: callback_errors.append(error)
    root.withdraw()
    root.attributes('-disabled', True)
    root.attributes('-alpha', 0.0)
    root.tk.call('tk', 'scaling', scaling)
    root.geometry(size)
    try:
        with patch.object(gui.gui_exports, 'load_condition_presets', return_value={}):
            app = gui.MBUprimeStructLabApp(root)
        root.state('normal')
        root.update()
        assert not callback_errors, callback_errors
        expected = tuple(map(int, size.split('x')))
        actual = (root.winfo_width(), root.winfo_height())
        if size == '1920x1080':
            # Windows may cap decorated windows at the desktop work area.
            assert actual[0] == 1920 and 900 <= actual[1] <= 1080
        else:
            assert actual == expected
        assert app._language_code == 'en'
        assert isinstance(app._workbench, gui.ttk.PanedWindow)
        assert not hasattr(app, 'workbench_nb')
        assert len(app.input_nb.tabs()) == 2
        assert len(app.results_nb.tabs()) == 4
        left, right = app._setup_scroll, app._workspace_scroll
        assert .30 < left.winfo_width() / app._workbench.winfo_width() < .46
        assert left.canvas.canvasy(0) == 0
        editor_font = gui.tkfont.Font(root=root, font=app.bulk_text.cget('font'))
        assert app.bulk_text.winfo_height() >= 6 * editor_font.metrics('linespace') + 12
        assert app.bulk_text.winfo_rooty() < left.canvas.winfo_rooty() + left.canvas.winfo_height()
        assert app.bulk_text.winfo_rooty() < app._bulk_preview_tree.winfo_rooty()
        assert app._bulk_preview_tree.winfo_rooty() < app._condition_preset_combo.winfo_rooty()
        assert app._condition_preset_combo.winfo_rooty() < app.analyze_btn.winfo_rooty()
        assert app.summary.winfo_rooty() < app._language_combo.winfo_rooty()
        assert app._language_combo.winfo_rooty() < app.tm_tree.winfo_rooty()
        assert app.tm_tree.winfo_rooty() < app._detail_text.winfo_rooty()
        for language in ('ru', 'en', 'ru'):
            app._set_language(language)
            root.update()
            app.bulk_text.delete('1.0', 'end')
            app.bulk_text.insert('1.0', 'forward ATGCGTACGTACGTACGTAC\nreverse CGTACGTACGTACGTACGTA')
            app._update_bulk_count()
            assert len(app._bulk_preview_tree.get_children()) == 2
            assert_revealed(app.bulk_text, left)
            with patch.object(app, '_start_analysis_worker') as start:
                app.analyze_btn.invoke()
                assert len(start.call_args.args[0]) == 2
            if not app._conditions_expanded:
                app._toggle_conditions()
            if not app._additional_analysis_expanded:
                app._toggle_additional_analysis()
            root.update()
            for pane in (left, right):
                for widget in descendants(pane.content):
                    if widget.winfo_viewable() and isinstance(widget, (
                            gui.ttk.Button, gui.ttk.Menubutton, gui.ttk.Combobox,
                            gui.ttk.Entry, gui.ttk.Checkbutton)):
                        assert_revealed(widget, pane)
                        assert pane._tag in widget.bindtags()
                assert pane.bind_class(pane._tag, '<FocusIn>')
            assert_revealed(app._detail_text, right)
            assert app.tm_tree.winfo_height() >= 4 * int(gui.ttk.Style(root).lookup('Lab.Treeview', 'rowheight'))
            # Wheel routing is local; editing widgets retain their native wheel.
            left.canvas.yview_moveto(0)
            right_y = right.canvas.yview()
            overflows = left.content.winfo_height() > left.canvas.winfo_height()
            left._wheel(SimpleNamespace(widget=app.analyze_btn, delta=-120, num=0, state=0))
            assert (left.canvas.yview()[0] > 0) == overflows
            assert right.canvas.yview() == right_y
            before = left.canvas.yview()
            assert left._wheel(SimpleNamespace(widget=app.bulk_text, delta=-120, num=0, state=0)) is None
            assert left.canvas.yview() == before
            app._set_busy(True)
            assert_revealed(app.cancel_btn, left)
            app._set_busy(False)
            app.input_nb.select(0)
            root.update()
            for entry in app._entries.values():
                assert_revealed(entry, left)
            app.input_nb.select(1)
            root.update()
        # A deliberate divider position survives language and content changes.
        app._workbench.sashpos(0, expected[0] // 2)
        root.update()
        position = app._workbench.sashpos(0)
        app._set_language('en')
        root.update()
        assert app._workbench.sashpos(0) == position
        # Resizing must settle, clamp old offsets, and not change client dimensions.
        root.geometry('1024x640')
        root.update()
        for pane in (left, right):
            assert 0 <= pane.canvas.xview()[0] < 1
            assert 0 <= pane.canvas.yview()[0] < 1
            settled = []
            for _ in range(6):
                pane.refresh()
                root.update()
                settled.append((pane.content.winfo_width(), pane.content.winfo_height()))
            assert settled[-3:] == [settled[-1]] * 3
            dimensions = settled[-1]
            assert bool(pane.xbar.winfo_manager()) == (dimensions[0] > pane.canvas.winfo_width())
            assert bool(pane.ybar.winfo_manager()) == (dimensions[1] > pane.canvas.winfo_height())
        assert (root.winfo_width(), root.winfo_height()) == (1024, 640)
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), ctypes.byref(pid))
        assert pid.value != os.getpid()
        assert not callback_errors, callback_errors
    finally:
        root.destroy()
