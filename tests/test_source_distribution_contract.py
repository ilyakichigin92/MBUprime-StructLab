"""Exercise archive rejection without compiling a native extension."""

import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
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
