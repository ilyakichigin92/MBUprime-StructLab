"""Build the pinned RNAstructure 6.6 extension on CPython 3.12 hosts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import shutil
import struct
import sys
import zipfile

from setuptools import Extension, setup
from setuptools.command.build_ext import build_ext


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ARCHIVE = ROOT / "vendor" / "rnastructure-6.6" / "RNAstructureSource.zip"
EXPECTED_ARCHIVE_SHA256 = (
    "4e30fa06f10a89556ad070c8d141fff6090165c330df786e19f9627df4407fd4")
EXTRACT_ROOT = ROOT / "build" / "rnastructure-6.6-source"

UPSTREAM_SOURCES = (
    "RNA_class/RNA.cpp", "RNA_class/thermodynamics.cpp",
    "RNA_class/design.cpp", "RNA_class/RsampleData.cpp",
    "src/algorithm.cpp", "src/alltrace.cpp", "src/DynProgArray.cpp",
    "src/dotarray.cpp", "src/draw.cpp", "src/extended_double.cpp",
    "src/forceclass.cpp", "src/MaxExpect.cpp", "src/MaxExpectStack.cpp",
    "src/outputconstraints.cpp", "src/pfunction.cpp", "src/probknot.cpp",
    "src/random.cpp", "src/rna_library.cpp", "src/stackclass.cpp",
    "src/stackstruct.cpp", "src/stochastic.cpp", "src/structure.cpp",
    "src/substructure.cpp", "src/TProgressDialog.cpp",
    "src/common_utils.cpp", "src/phmm/utils/xmath/log/xlog_math.cpp",
    "src/bimol.cpp",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _mapping_sha256(payload: dict[str, str]) -> str:
    import json

    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


def _archive_source_manifest() -> dict[str, str]:
    """Hash the entire upstream tree, including transitive and extensionless headers."""

    with zipfile.ZipFile(ARCHIVE) as archive:
        members = archive.infolist()
        anchor = "RNA_class/RNA.cpp"
        roots = [
            item.filename[:-len(anchor)] for item in members
            if item.filename == anchor or item.filename.endswith("/" + anchor)
        ]
        if len(roots) != 1:
            raise RuntimeError("pinned archive must contain one RNAstructure source root")
        prefix = roots[0]
        result = {
            item.filename[len(prefix):]: hashlib.sha256(archive.read(item)).hexdigest()
            for item in members
            if not item.is_dir() and item.filename.startswith(prefix)
        }
        if not all(relative in result for relative in UPSTREAM_SOURCES):
            raise RuntimeError("pinned archive lacks required compilation sources")
        return result


def _located_source_root() -> Path | None:
    for candidate in (EXTRACT_ROOT / "RNAstructure", EXTRACT_ROOT):
        if (candidate / "RNA_class" / "RNA.cpp").is_file():
            return candidate
    return None


def _extracted_source_manifest(source_root: Path) -> dict[str, str] | None:
    result: dict[str, str] = {}
    if source_root.is_symlink() or source_root.is_junction():
        return None

    def unreadable(error: OSError) -> None:
        raise error

    for directory, subdirs, files in os.walk(
            source_root, followlinks=False, onerror=unreadable):
        for name in (*subdirs, *files):
            path = Path(directory) / name
            if path.is_symlink() or path.is_junction():
                return None
        for name in files:
            path = Path(directory) / name
            # Unwrapped archives share their root with our diagnostic marker.
            if path == EXTRACT_ROOT / ".archive-sha256":
                continue
            result[path.relative_to(source_root).as_posix()] = _sha256(path)
    return result


def _require_safe_extract_root() -> None:
    """Never repair a cache through a link or outside this project's build tree."""

    build_root = ROOT / "build"
    if (build_root.is_symlink() or build_root.is_junction()
            or EXTRACT_ROOT.is_symlink() or EXTRACT_ROOT.is_junction()
            or not EXTRACT_ROOT.resolve().is_relative_to(build_root.resolve())
            or EXTRACT_ROOT.resolve() == build_root.resolve()):
        raise RuntimeError("RNAstructure extraction cache must remain inside ROOT/build")


def _require_build_runtime() -> None:
    if sys.version_info[:2] != (3, 12) or struct.calcsize("P") != 8:
        raise RuntimeError("native release build requires 64-bit CPython 3.12")


def _source_root() -> Path:
    _require_safe_extract_root()
    if not ARCHIVE.is_file() or _sha256(ARCHIVE) != EXPECTED_ARCHIVE_SHA256:
        raise RuntimeError("vendored RNAstructureSource.zip hash mismatch")
    expected_sources = _archive_source_manifest()
    marker = EXTRACT_ROOT / ".archive-sha256"
    expected_marker = (
        EXPECTED_ARCHIVE_SHA256 + "\n" +
        _mapping_sha256(expected_sources) + "\n")
    source_root = _located_source_root()
    # The marker is diagnostic only. Compare every upstream file and reject
    # additions too: a new file can shadow a quoted include from another directory.
    if (source_root is None
            or _extracted_source_manifest(source_root) != expected_sources):
        if EXTRACT_ROOT.exists():
            shutil.rmtree(EXTRACT_ROOT)
        EXTRACT_ROOT.mkdir(parents=True)
        with zipfile.ZipFile(ARCHIVE) as archive:
            archive.extractall(EXTRACT_ROOT)
        source_root = _located_source_root()
        if (source_root is None
                or _extracted_source_manifest(source_root) != expected_sources):
            raise RuntimeError(
                "extracted RNAstructure compilation sources differ from pinned archive")
    marker.write_text(expected_marker, encoding="ascii")
    return source_root


def verified_upstream_source_manifest() -> dict[str, str]:
    """Return translation-unit identity after validating the complete upstream tree."""

    source_root = _source_root()
    manifest = _extracted_source_manifest(source_root)
    if manifest is None:  # _source_root already fails closed; defensive guard.
        raise RuntimeError("RNAstructure compilation source manifest is incomplete")
    return {relative: manifest[relative] for relative in UPSTREAM_SOURCES}


def native_extension() -> Extension:
    _require_build_runtime()
    source_root = _source_root()
    # setuptools requires source paths relative to setup.py. The verified
    # extraction is always rooted below ROOT/build, so no trust is lost by
    # expressing the exact same files in repository-relative form.
    sources = ["native/rnastructure_native.cpp"]
    sources.extend(
        (source_root / relative).relative_to(ROOT).as_posix()
        for relative in UPSTREAM_SOURCES)
    compile_args = (
        ["/O2", "/EHsc", "/std:c++17", "/w",
         "/D_CRT_SECURE_NO_WARNINGS", "/DNOMINMAX"]
        if sys.platform == "win32" else ["-O2", "-std=c++17", "-w"]
    )
    return Extension(
        "rnastructure_native._rnastructure_native_v1",
        sources=sources,
        include_dirs=[str(source_root)],
        language="c++",
        extra_compile_args=compile_args,
    )


class ManifestBuildExt(build_ext):
    """Generate exact identity only after the target compiler succeeds."""

    def run(self) -> None:
        super().run()
        compiler_type = getattr(self.compiler, "compiler_type", "unknown")
        if compiler_type not in {"msvc", "unix"}:
            raise RuntimeError(f"unsupported native compiler: {compiler_type}")
        from native.generate_native_manifest import generate_manifest

        if self.inplace:
            package = ROOT / "rnastructure_native"
        else:
            package = Path(self.build_lib) / "rnastructure_native"
            source_tables = ROOT / "rnastructure_native" / "data_tables"
            destination_tables = package / "data_tables"
            if not destination_tables.exists():
                shutil.copytree(source_tables, destination_tables)
        generate_manifest(package, compiler=compiler_type)


if __name__ == "__main__":
    setup(
        name="mbuprime-rnastructure-native",
        version="6.6.0",
        ext_modules=[native_extension()],
        cmdclass={"build_ext": ManifestBuildExt},
    )
