"""Read-only detail navigation without global input or clipboard changes."""
import ctypes
import os
import json
import sys
from types import SimpleNamespace
from unittest.mock import patch

import primer_tool_gui as gui
import pytest


@pytest.fixture
def prevent_test_window_activation():
    """Block native activation only on this test thread, including Tk startup.

    Tk 8.6.15 UpdateWrapper unconditionally calls SetActiveWindow for its first
    window. A style applied after wrapper creation cannot stop that request.
    WH_CBT can veto it before it affects the user's foreground/input target.
    """
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(
        ctypes.c_ssize_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t)
    user32.SetWindowsHookExW.argtypes = [
        ctypes.c_int, callback_type, ctypes.c_void_p, ctypes.c_ulong]
    user32.SetWindowsHookExW.restype = ctypes.c_void_p
    user32.CallNextHookEx.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t, ctypes.c_ssize_t]
    user32.CallNextHookEx.restype = ctypes.c_ssize_t
    user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
    user32.UnhookWindowsHookEx.restype = ctypes.c_int
    kernel32.GetCurrentThreadId.restype = ctypes.c_ulong
    blocked = []

    @callback_type
    def prevent_activation(code, window, detail):
        if code in (5, 9):  # HCBT_ACTIVATE, HCBT_SETFOCUS
            blocked.append(code)
            return 1
        return user32.CallNextHookEx(None, code, window, detail)

    hook = user32.SetWindowsHookExW(
        5, prevent_activation, None, kernel32.GetCurrentThreadId())  # WH_CBT
    if not hook:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield blocked
    finally:
        if not user32.UnhookWindowsHookEx(hook):
            raise ctypes.WinError(ctypes.get_last_error())


@pytest.mark.skipif(sys.platform != "win32", reason="Windows non-activating window isolation")
def test_readonly_detail_participates_in_both_tab_directions_and_can_copy(
        prevent_test_window_activation):
    foreground = ctypes.windll.user32.GetForegroundWindow
    foreground.restype = ctypes.c_void_p
    before = foreground()
    owner = ctypes.windll.user32.GetWindowThreadProcessId
    owner.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    def foreground_process():
        pid = ctypes.c_ulong()
        owner(foreground(), ctypes.byref(pid))
        return pid.value
    before_pid = foreground_process()
    assert before_pid != os.getpid()
    root = gui.tk.Tk()
    root.withdraw()
    root.attributes("-disabled", True)
    root.attributes("-alpha", 0.0)
    app = gui.MBUprimeStructLabApp(root)
    try:
        # Keep the actual widgets viewable for Tk focus traversal without
        # activating this test window on the user's desktop.
        root.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        user32.GetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        wrapper = user32.GetParent(root.winfo_id())
        assert wrapper, "Tk wrapper window must exist before mapping"
        extended_style = user32.GetWindowLongPtrW(wrapper, -20)
        user32.SetWindowLongPtrW(wrapper, -20, extended_style | 0x08000000)
        # Tk 8.6.15's wm deiconify explicitly raises and forces focus, even with
        # WS_EX_NOACTIVATE. wm state normal uses SW_SHOWNOACTIVATE without that
        # focus request (win/tkWinWm.c: WmStateCmd vs TkpWinToplevelDeiconify).
        root.state("normal")
        root.update()
        mapped_pid = foreground_process()
        print(json.dumps({"stage": "mapped", "test_pid": os.getpid(),
            "foreground_before": before, "foreground_pid_before": before_pid,
            "foreground_after_mapping": foreground(),
            "foreground_pid_after_mapping": mapped_pid}), flush=True)
        assert mapped_pid != os.getpid(), "mapping activated the isolated test window"
        text = app._detail_text
        content = "Selected structure\nΔG = -7.25 kcal/mol"
        text.configure(state="normal")
        text.insert("1.0", content)
        text.configure(state="disabled")
        previous = text.tk_focusPrev()
        following = text.tk_focusNext()
        assert previous.tk_focusNext() is text
        assert following.tk_focusPrev() is text
        assert root.tk.getboolean(root.tk.call("tk::FocusOK", text._w))
        # Execute the actual registered widget binding, replacing only focus
        # transfer so the test cannot change the user's foreground/input target.
        binding = text.bind("<Shift-Tab>")
        command = binding.split("[", 1)[1].split()[0]
        focused = []
        with patch.object(gui.tk.Misc, "focus_set", lambda widget: focused.append(widget)):
            assert root.tk.call(command, "unused_event") == "break"
        assert focused == [previous]
        clipboard = []
        root.tk.call("rename", "clipboard", "_real_clipboard")
        root.tk.createcommand("clipboard", lambda *args: clipboard.append(args) or "")
        try:
            event = SimpleNamespace(widget=text, state=gui._CTRL_MASK, keycode=65)
            assert app._handle_layout_independent_shortcut(event) == "break"
            assert text.get("sel.first", "sel.last").rstrip("\n") == content
            event.keycode = 67
            assert app._handle_layout_independent_shortcut(event) == "break"
            assert any(args[0] == "append" and content in args[-1] for args in clipboard)
            event.keycode = 86
            assert app._handle_layout_independent_shortcut(event) == "break"
            text.insert("1.0", "forbidden edit")
            assert text.get("1.0", "end-1c") == content
        finally:
            root.tk.deletecommand("clipboard")
            root.tk.call("rename", "_real_clipboard", "clipboard")
        assert str(text.cget("state")) == "disabled"
        # A user may switch their own foreground window during the test; the
        # invariant is that this disabled test window cannot acquire it.
        after_pid = foreground_process()
        print(json.dumps({"test_pid": os.getpid(), "foreground_before": before,
            "foreground_after": foreground(), "foreground_pid_before": before_pid,
            "foreground_pid_after": after_pid, "system_clipboard_modified": False,
            "blocked_native_activation_events": prevent_test_window_activation}),
            flush=True)
        assert after_pid != os.getpid()
    finally:
        root.destroy()
