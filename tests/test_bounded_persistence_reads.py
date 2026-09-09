"""Acquisition limits hold even when file contents outgrow their stat result."""

from __future__ import annotations

import io
from types import SimpleNamespace

import pytest

import analyzed_run_archive as archive
import gui_exports


class _ObservedStream(io.BytesIO):
    def __init__(self, payload):
        super().__init__(payload)
        self.read_sizes = []
        self.returned_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        result = super().read(size)
        self.returned_sizes.append(len(result))
        return result


class _ChangingFile:
    """Model a file that grew after stat, without allocating a large fixture."""

    def __init__(self, payload, reported_size=0):
        self.stream = _ObservedStream(payload)
        self.reported_size = reported_size
        self.opens = 0

    def stat(self):
        return SimpleNamespace(st_size=self.reported_size)

    def open(self, mode):
        assert mode == "rb"
        self.opens += 1
        return self.stream

    def read_bytes(self):
        raise AssertionError("persistence acquisition must use a bounded read")


def test_growing_preset_rejected_before_unbounded_allocation(monkeypatch):
    source = _ChangingFile(b"x" * 100)
    monkeypatch.setattr(gui_exports, "Path", lambda _path: source)

    with pytest.raises(gui_exports.PersistenceError, match="size limit"):
        gui_exports._read_bounded("presets.json", 16, "preset file")

    assert source.stream.read_sizes == [17]
    assert source.stream.returned_sizes == [17]
    assert source.stream.closed


@pytest.mark.parametrize("length", [0, 15, 16])
def test_preset_at_or_below_limit_preserves_all_bytes(monkeypatch, length):
    payload = b"x" * length
    source = _ChangingFile(payload, reported_size=length)
    monkeypatch.setattr(gui_exports, "Path", lambda _path: source)

    assert gui_exports._read_bounded("presets.json", 16, "preset file") == payload
    assert source.stream.closed


def test_known_oversized_preset_rejected_without_open(monkeypatch):
    source = _ChangingFile(b"", reported_size=17)
    monkeypatch.setattr(gui_exports, "Path", lambda _path: source)

    with pytest.raises(gui_exports.PersistenceError, match="size limit"):
        gui_exports._read_bounded("presets.json", 16, "preset file")

    assert source.opens == 0


def test_growing_legacy_archive_rejected_before_unbounded_allocation(monkeypatch):
    source = _ChangingFile(b"{" + b" " * 100)
    monkeypatch.setattr(archive, "Path", lambda _path: source)
    monkeypatch.setattr(archive, "MAX_LEGACY_ANALYZED_RUN_BYTES", 16)

    with pytest.raises(archive.PersistenceError, match="exceeds.*limit"):
        archive.load_analyzed_run_archive("run.json")

    assert source.stream.read_sizes == [4, 17]
    assert source.stream.returned_sizes == [4, 17]
    assert source.opens == 1
    assert source.stream.closed


def test_legacy_limit_boundary_preserves_magic_bytes_for_decoder(monkeypatch):
    payload = b'{"key":"value"} '
    source = _ChangingFile(payload, reported_size=len(payload))
    monkeypatch.setattr(archive, "Path", lambda _path: source)
    monkeypatch.setattr(archive, "MAX_LEGACY_ANALYZED_RUN_BYTES", len(payload))
    received = []
    expected = object()

    def decode(raw):
        received.append(raw)
        return expected

    monkeypatch.setattr(archive, "_decode_legacy_analyzed_run", decode)

    assert archive.load_analyzed_run_archive("run.json") is expected
    assert received == [payload]
    assert source.stream.closed


def test_known_oversized_legacy_archive_only_reads_magic(monkeypatch):
    source = _ChangingFile(b"{}", reported_size=17)
    monkeypatch.setattr(archive, "Path", lambda _path: source)
    monkeypatch.setattr(archive, "MAX_LEGACY_ANALYZED_RUN_BYTES", 16)

    with pytest.raises(archive.PersistenceError, match="legacy.*64 MiB"):
        archive.load_analyzed_run_archive("run.json")

    assert source.stream.read_sizes == [4]
    assert source.stream.closed


def test_schema2_decoder_uses_same_open_stream_as_magic_detection(monkeypatch):
    source = _ChangingFile(b"PK\x03\x04contents", reported_size=12)
    monkeypatch.setattr(archive, "Path", lambda _path: source)
    expected = object()

    def decode(handle, compressed_size):
        assert handle is source.stream
        assert handle.tell() == 0
        assert not handle.closed
        assert compressed_size == 12
        return expected

    monkeypatch.setattr(archive, "_decode_schema2_archive", decode)

    assert archive.load_analyzed_run_archive("run.json") is expected
    assert source.opens == 1
    assert source.stream.read_sizes == [4]
    assert source.stream.closed


@pytest.mark.parametrize("loader", ["preset", "archive"])
def test_read_io_error_is_wrapped_and_handle_closed(monkeypatch, loader):
    source = _ChangingFile(b"{}")

    def fail_read(_size=-1):
        raise OSError("injected read failure")

    monkeypatch.setattr(source.stream, "read", fail_read)
    if loader == "preset":
        monkeypatch.setattr(gui_exports, "Path", lambda _path: source)
        load = lambda: gui_exports._read_bounded("presets.json", 16, "preset file")
    else:
        monkeypatch.setattr(archive, "Path", lambda _path: source)
        load = lambda: archive.load_analyzed_run_archive("run.json")

    with pytest.raises(archive.PersistenceError, match="cannot read.*injected"):
        load()

    assert source.stream.closed
