"""Build a clean sdist, check its inputs, then build and probe its installed wheel.

Use the locked build environment. --contents-only performs the cheaper archive
gate; the default also compiles native code from the extracted archive. Retain
the work directory on failure for inspection and choose a new one for a rerun.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile


ROOT = Path(__file__).resolve().parents[1]
GENERATED = shutil.ignore_patterns(
    ".git", ".agents", ".claude", ".codex", ".venv", "__pycache__",
    ".pytest_cache", "*.egg-info", "*.pyc", "*.pyd", "*.so", "build",
    "dist", "release", ".buildtmp", ".env", ".env.*", "*.pfx", "*.key",
)


def run(command: list[str], cwd: Path, work: Path, name: str) -> None:
    environment = os.environ.copy()
    for key in ("PYTHONHOME", "PYTHONPATH"):
        environment.pop(key, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PIP_NO_INDEX"] = "1"
    environment["_CL_"] = "/MP1" if sys.platform == "win32" else ""
    log = work / (name + ".log")
    print(f"{name}: {log}", flush=True)
    with log.open("w", encoding="utf-8") as output:
        result = subprocess.run(
            command, cwd=cwd, env=environment, stdout=output,
            stderr=subprocess.STDOUT, timeout=3600, check=False)
    if result.returncode:
        raise RuntimeError(f"{name} failed ({result.returncode}); see {log}")


def required_files(source: Path) -> set[str]:
    inventory = json.loads(
        (source / "packaging/release_inventory.json").read_text("utf-8"))
    required = {
        "frozen_gui_qualification.py", "packaging/cpython_windows_runtime.json",
        "packaging/verify_source_distribution.py", "requirements-test.txt",
        "pytest.ini", "tests/conftest.py",
    }
    for group in inventory["source_groups"].values():
        required.update(group.get("files", []))
        for tree in group.get("trees", []):
            required.update(path.relative_to(source).as_posix()
                            for path in (source / tree).rglob("*")
                            if path.is_file())
    required.update(path.relative_to(source).as_posix()
                    for path in (source / "tests").rglob("*.py"))
    for directory in ("docs", "examples"):
        required.update(path.relative_to(source).as_posix()
                        for path in (source / directory).rglob("*")
                        if path.is_file())
    required.update(name for name in ("CONTRIBUTING.md", "SECURITY.md", "CITATION.cff")
                    if (source / name).is_file())
    return required


def inspect_and_extract(archive: Path, source: Path, destination: Path) -> Path:
    with tarfile.open(archive, "r:gz") as package:
        members = package.getmembers()
        roots = set()
        paths = set()
        for member in members:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or not path.parts or
                    any(part in {".", ".."} or ":" in part or "\\" in part
                        for part in path.parts) or
                    not (member.isfile() or member.isdir())):
                raise ValueError(f"Unsafe sdist member: {member.name}")
            roots.add(path.parts[0])
            if member.isfile():
                relative = PurePosixPath(*path.parts[1:]).as_posix()
                if relative in paths:
                    raise ValueError(f"Duplicate sdist member: {relative}")
                if (path.suffix.lower() in {".pyd", ".so", ".pyc", ".pfx", ".key"}
                        or set(path.parts[1:]) & {
                            "build", "dist", "release", ".git", "__pycache__"}):
                    raise ValueError(f"Generated/private sdist member: {relative}")
                paths.add(relative)
        if len(roots) != 1:
            raise ValueError("sdist must have exactly one top-level directory")
        missing = required_files(source) - paths
        if missing:
            raise ValueError("Missing sdist inputs: " + ", ".join(sorted(missing)))
        package.extractall(destination, filter="data")
    extracted = destination / roots.pop()
    for relative in required_files(source):
        if (extracted / relative).read_bytes() != (source / relative).read_bytes():
            raise ValueError(f"sdist changed source bytes: {relative}")
    print(f"Validated {len(paths)} archive files and all declared source groups", flush=True)
    return extracted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--contents-only", action="store_true")
    args = parser.parse_args()
    work = args.workdir.resolve()
    if work.is_relative_to(ROOT) or ROOT.is_relative_to(work):
        parser.error("--workdir must be outside the source tree and its parents")
    if work.exists() and any(work.iterdir()):
        parser.error("--workdir must be empty")
    work.mkdir(parents=True, exist_ok=True)
    source = work / "clean-source"
    shutil.copytree(ROOT, source, ignore=GENERATED)
    sdist_dir = work / "sdist"
    sdist_dir.mkdir()
    run([sys.executable, "-c",
         "from setuptools.build_meta import build_sdist; import sys; build_sdist(sys.argv[1])",
         str(sdist_dir)], source, work, "sdist-build")
    archives = list(sdist_dir.glob("*.tar.gz"))
    if len(archives) != 1:
        raise RuntimeError("Expected exactly one freshly built sdist")
    extracted = inspect_and_extract(archives[0], source, work / "extracted")
    if args.contents_only:
        print("Source contents gate passed; wheel/native build not requested")
        return
    wheels = work / "wheel"
    wheels.mkdir()
    run([sys.executable, "-c",
         "from setuptools.build_meta import build_wheel; import sys; build_wheel(sys.argv[1])",
         str(wheels)], extracted, work, "extracted-wheel-build")
    artifacts = list(wheels.glob("*.whl"))
    if len(artifacts) != 1:
        raise RuntimeError("Expected exactly one freshly built wheel")
    installed = work / "installed"
    run([sys.executable, "-m", "pip", "install", "--no-deps", "--no-index",
         "--target", str(installed), str(artifacts[0])], work, work, "wheel-install")
    probe = (
        "import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
        "import scientific_metadata as sm, rnastructure_native as rn; "
        "assert Path(sm.__file__).resolve().is_relative_to(Path(sys.argv[1])); "
        "assert Path(rn.__file__).resolve().is_relative_to(Path(sys.argv[1])); "
        "assert len(sm.source_tree_hash()) == 64; "
        "backend=rn.create_backend(); identity=backend.runtime_identity(); "
        "assert identity['engine_version']=='6.6'; "
        "assert identity['integration']=='in_process_native'; "
        "backend.fold_hairpin('GCGAAACGC',25.0,10); "
        "print(sm.source_tree_hash()); print(identity)"
    )
    run([sys.executable, "-I", "-c", probe, str(installed)],
        work, work, "installed-native-probe")
    print("Source distribution, extracted wheel build, and installed native probe passed")


if __name__ == "__main__":
    main()
