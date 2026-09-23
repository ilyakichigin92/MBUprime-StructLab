"""Exercise archive rejection without compiling a native extension."""

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import tomllib

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "packaging/verify_source_distribution.py"
SPEC = importlib.util.spec_from_file_location("source_distribution_gate", SCRIPT)
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def test_pep517_metadata_without_checkout_on_import_path(tmp_path):
    """Pip invokes hooks with the backend runner, not the checkout, on sys.path."""
    probe = (
        "from setuptools.build_meta import prepare_metadata_for_build_wheel; "
        "import sys; prepare_metadata_for_build_wheel(sys.argv[1])")
    result = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(tmp_path)],
        cwd=SCRIPT.parents[1], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    metadata, = tmp_path.glob("*.dist-info/METADATA")
    version = tomllib.loads((SCRIPT.parents[1] / "pyproject.toml").read_text("utf-8"))[
        "project"]["version"]
    assert f"Version: {version}\n" in metadata.read_text("utf-8")


def test_cli_version_matches_distribution_metadata():
    from mbuprime_structlab import __version__
    from scientific_metadata import APPLICATION_VERSION

    metadata = tomllib.loads((SCRIPT.parents[1] / "pyproject.toml").read_text("utf-8"))
    assert __version__ == metadata["project"]["version"] == APPLICATION_VERSION


@pytest.mark.parametrize("failures,error", [
    (2, PermissionError), (10, PermissionError), (1, OSError),
])
def test_release_verifier_cleanup_retries_only_transient_permission_errors(
        monkeypatch, tmp_path, failures, error):
    spec = importlib.util.spec_from_file_location(
        "release_cleanup_gate", SCRIPT.with_name("verify_release_identity.py"))
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    temporary = tempfile.TemporaryDirectory(dir=tmp_path)
    root = Path(temporary.name)
    (root / "disposable.exe").write_bytes(b"synthetic executable")
    cleanup = temporary.cleanup
    attempts = []
    sleeps = []

    def locked_cleanup():
        attempts.append(1)
        if len(attempts) <= failures:
            raise error("synthetic cleanup failure")
        cleanup()

    monkeypatch.setattr(temporary, "cleanup", locked_cleanup)
    monkeypatch.setattr(verifier.tempfile, "TemporaryDirectory", lambda **kwargs: temporary)
    monkeypatch.setattr(verifier.time, "sleep", sleeps.append)
    try:
        if failures == 2:
            with verifier._self_test_directory(tmp_path) as directory:
                assert directory == root
            assert not root.exists()
            assert len(attempts) == 3
            assert sleeps == [0.5, 0.5]
        else:
            with pytest.raises(error, match="synthetic cleanup failure"):
                with verifier._self_test_directory(tmp_path):
                    pass
            assert root.exists()
            assert len(attempts) == failures
            assert sleeps == ([0.5] * 9 if error is PermissionError else [])
    finally:
        cleanup()


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "source"
    inventory = root / "packaging/release_inventory.json"
    inventory.parent.mkdir(parents=True)
    inventory.write_text(json.dumps({"source_groups": {
        "inventory": {"files": ["packaging/release_inventory.json"]},
    }}))
    for relative in gate.required_files(root) - {"packaging/release_inventory.json"}:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic source input\n")
    return root


def archive(tmp_path, source, *, omit=None, extra=None, mutate=None):
    path = tmp_path / "sample.tar.gz"
    with tarfile.open(path, "w:gz") as package:
        for relative in sorted(gate.required_files(source)):
            if relative == omit:
                continue
            data = (source / relative).read_bytes()
            if relative == mutate:
                data += b"changed"
            entry = tarfile.TarInfo("sample/" + relative)
            entry.size = len(data)
            package.addfile(entry, io.BytesIO(data))
        if extra:
            entry = tarfile.TarInfo(extra)
            package.addfile(entry, io.BytesIO())
    return path


def test_complete_archive_preserves_source_bytes(tmp_path, source):
    extracted = gate.inspect_and_extract(
        archive(tmp_path, source), source, tmp_path / "unpacked")
    assert (extracted / "frozen_gui_qualification.py").read_bytes() == (
        source / "frozen_gui_qualification.py").read_bytes()


@pytest.mark.parametrize("missing", [
    "frozen_gui_qualification.py", "packaging/cpython_windows_runtime.json",
])
def test_missing_build_input_fails_before_extraction(tmp_path, source, missing):
    with pytest.raises(ValueError, match="Missing sdist inputs"):
        gate.inspect_and_extract(archive(tmp_path, source, omit=missing),
                                 source, tmp_path / "unpacked")
    assert not (tmp_path / "unpacked").exists()


@pytest.mark.parametrize("extra", ["sample/../outside", "sample/native.pyd"])
def test_unsafe_or_generated_member_fails_before_extraction(tmp_path, source, extra):
    with pytest.raises(ValueError, match="sdist member"):
        gate.inspect_and_extract(archive(tmp_path, source, extra=extra),
                                 source, tmp_path / "unpacked")
    assert not (tmp_path / "unpacked").exists()


def test_modified_source_bytes_fail_validation(tmp_path, source):
    with pytest.raises(ValueError, match="changed source bytes"):
        gate.inspect_and_extract(
            archive(tmp_path, source, mutate="frozen_gui_qualification.py"),
            source, tmp_path / "unpacked")
