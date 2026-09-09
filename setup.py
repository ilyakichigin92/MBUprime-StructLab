"""Setuptools bridge for the pinned native extension and wheel provenance."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

from setuptools import setup
from setuptools.command.build import build
from setuptools.command.build_py import build_py

from native.setup_native import ManifestBuildExt, native_extension
from native_target import current_target


ROOT = Path(__file__).resolve().parent
PROVENANCE_SENTINEL = '_BUNDLED_SOURCE_PROVENANCE_JSON = "{}"'


def _source_provenance(build_lib: Path) -> dict[str, str]:
    path = ROOT / "packaging" / "generate_provenance.py"
    spec = importlib.util.spec_from_file_location(
        "mbuprime_build_provenance", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load scientific provenance generator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    native_relative = (
        f"rnastructure_native/native_manifests/{current_target()}.json")
    built_native_manifest = build_lib / Path(native_relative)
    overrides = (
        {native_relative: built_native_manifest}
        if built_native_manifest.is_file() else None)
    return module.build_source_provenance(ROOT, overrides=overrides)


class BuildExtFromPinnedArchive(ManifestBuildExt):
    """Distribute the pinned archive instead of its disposable extraction."""

    def get_source_files(self) -> list[str]:
        # setuptools adds extension inputs after processing MANIFEST.in on
        # some paths; compilation still uses the complete Extension.sources.
        return [path for path in super().get_source_files()
                if Path(path).parts[0] != "build"]


class BuildNativeBeforePython(build):
    """Build the target-qualified native identity before embedding provenance."""

    sub_commands = list(build.sub_commands)
    build_ext_entry = next(
        entry for entry in sub_commands if entry[0] == "build_ext")
    sub_commands.remove(build_ext_entry)
    build_py_index = next(
        index for index, entry in enumerate(sub_commands)
        if entry[0] == "build_py")
    sub_commands.insert(build_py_index, build_ext_entry)


class BuildPyWithScientificProvenance(build_py):
    """Embed build-time identity in the installed scientific metadata module."""

    def run(self) -> None:
        super().run()
        target = Path(self.build_lib) / "scientific_metadata.py"
        source = target.read_text(encoding="utf-8")
        if source.count(PROVENANCE_SENTINEL) != 1:
            raise RuntimeError("scientific provenance sentinel is missing or ambiguous")
        encoded = json.dumps(
            _source_provenance(Path(self.build_lib)),
            sort_keys=True, separators=(",", ":"),
            ensure_ascii=True)
        target.write_text(
            source.replace(
                PROVENANCE_SENTINEL,
                f"_BUNDLED_SOURCE_PROVENANCE_JSON = {encoded!r}"),
            encoding="utf-8", newline="\n")


setup(
    ext_modules=[native_extension()],
    cmdclass={
        "build_ext": BuildExtFromPinnedArchive,
        "build_py": BuildPyWithScientificProvenance,
        "build": BuildNativeBeforePython,
    },
)
