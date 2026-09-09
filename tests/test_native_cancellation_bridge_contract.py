"""Cancellation lifecycle contracts, without native science or timing thresholds.

The caller contract is external_engines.CancelEvent.is_set(). Completion must
wake the monitor independently of that caller event, including on exceptions.
"""

import threading
from types import SimpleNamespace

import pytest

import rnastructure_native as native


class FakeNative:
    def __init__(self):
        self.token = object()
        self.cancelled = threading.Event()
        self.cancelled_tokens = []

    def make_cancel_token(self):
        return self.token

    def cancel(self, token):
        self.cancelled_tokens.append(token)
        self.cancelled.set()


def test_no_event_has_no_monitor_and_close_is_idempotent():
    api = FakeNative()
    bridge = native._CancellationBridge(None, api)
    bridge.close()
    bridge.close()
    assert bridge.token is api.token
    assert bridge._thread is None
    assert not api.cancelled_tokens


@pytest.mark.parametrize("initially_cancelled", [False, True])
def test_caller_cancellation_reaches_the_native_token(initially_cancelled):
    api = FakeNative()
    first_poll = threading.Event()

    class CallerEvent(threading.Event):
        def is_set(self):
            cancelled = super().is_set()
            first_poll.set()
            return cancelled

    event = CallerEvent()
    if initially_cancelled:
        event.set()
    bridge = native._CancellationBridge(event, api)
    try:
        assert first_poll.wait(2), "monitor did not poll caller cancellation"
        event.set()
        assert api.cancelled.wait(2), "native cancellation was not forwarded"
    finally:
        bridge.close()
    assert api.cancelled_tokens == [api.token]
    assert not bridge._thread.is_alive()


def test_cancellation_accepts_the_is_set_only_protocol():
    api = FakeNative()
    event = SimpleNamespace(is_set=lambda: True)
    bridge = native._CancellationBridge(event, api)
    try:
        assert api.cancelled.wait(2), "is_set-only cancellation was not forwarded"
    finally:
        bridge.close()
    assert api.cancelled_tokens == [api.token]
    assert not bridge._thread.is_alive()


@pytest.fixture
def stop_wait(monkeypatch):
    """Hold the monitor's real Event.wait until close explicitly wakes it.

    The five-second timeout only bounds a broken implementation's cleanup;
    it is not a performance assertion or a substitute for synchronization.
    Patch only this module's threading reference, leaving Thread internals real.
    """
    entered = threading.Event()
    released_by_stop = threading.Event()

    class StopEvent(threading.Event):
        def wait(self, timeout=None):
            entered.set()
            stopped = super().wait(5)
            if stopped:
                released_by_stop.set()
            return stopped

    monkeypatch.setattr(native, "threading", SimpleNamespace(
        Event=StopEvent, Thread=threading.Thread))
    return entered, released_by_stop


def test_close_wakes_monitor_without_setting_the_caller_event(stop_wait):
    entered, released = stop_wait
    api = FakeNative()
    event = threading.Event()
    bridge = native._CancellationBridge(event, api)
    try:
        assert entered.wait(2), "monitor did not enter its stoppable wait"
        bridge.close()
        assert released.wait(2), "close did not wake the monitor's wait"
        bridge._thread.join(2)
        assert not bridge._thread.is_alive()
        assert not event.is_set()
        assert not api.cancelled_tokens
    finally:
        bridge.close()
        bridge._thread.join(6)


@pytest.mark.parametrize("failure", [None, RuntimeError, InterruptedError])
def test_invoke_cleans_up_monitor_on_success_and_native_errors(stop_wait, failure):
    from external_engines import ExternalEngineCancelled

    entered, released = stop_wait
    api = FakeNative()
    backend = native.Backend.__new__(native.Backend)
    backend._native = api
    event = threading.Event()
    monitors = []

    def operation(value, *, cancel_token):
        monitors.extend(thread for thread in threading.enumerate()
                        if thread.name == "RNAstructure-cancel")
        assert entered.wait(2), "monitor did not enter its stoppable wait"
        assert cancel_token is api.token
        if failure is not None:
            raise failure("native failure")
        return value

    try:
        if failure is None:
            assert backend._invoke(operation, 42, cancel_event=event) == 42
        else:
            expected = (ExternalEngineCancelled
                        if failure is InterruptedError else failure)
            with pytest.raises(expected, match="native failure") as caught:
                backend._invoke(operation, 42, cancel_event=event)
            if failure is InterruptedError:
                assert isinstance(caught.value.__cause__, InterruptedError)
        assert released.wait(2), "native completion left its monitor waiting"
        assert monitors
        for monitor in monitors:
            monitor.join(2)
            assert not monitor.is_alive()
        assert not event.is_set()
        assert not api.cancelled_tokens
    finally:
        for monitor in monitors:
            monitor.join(6)
