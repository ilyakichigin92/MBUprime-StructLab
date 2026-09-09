"""Build the pinned CPython security release locally; never install globally.

Python.org no longer supplies 3.12 Windows installers. This recipe verifies the
official source and immutable CPython dependency archives before extraction,
then uses installed MSVC to produce a private runtime with Tk, headers and pip.
The lock identifies inputs, not reproducible compiler output or publisher trust.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile


def verified_download(item: dict, cache: Path) -> Path:
    target = cache / item["filename"]
    if not target.exists():
        partial = target.with_suffix(target.suffix + ".partial")
        with urllib.request.urlopen(item["url"], timeout=120) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
        partial.replace(target)
    with target.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != item["sha256"]:
        raise ValueError(f"SHA-256 mismatch for {target.name}")
    return target


def extract_zip(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as zipped:
        for entry in zipped.infolist():
            parts = PurePosixPath(entry.filename).parts
            if not parts or entry.filename.startswith("/") or any(
                part in {".", ".."} or ":" in part or "\\" in part for part in parts
            ) or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError(f"Unsafe ZIP member: {entry.filename}")
        zipped.extractall(destination)


def run(command: list[str], work: Path, log_name: str, *, cwd: Path, env: dict) -> None:
    with (work / log_name).open("w", encoding="utf-8") as log:
        result = subprocess.run(command, cwd=cwd, env=env, stdout=log,
                                stderr=subprocess.STDOUT, timeout=1800,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError(f"Command failed ({result.returncode}); see {work / log_name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workdir", type=Path, required=True, help="New empty private build directory")
    parser.add_argument("--cache", type=Path, required=True, help="Verified download cache")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("This recipe requires Windows and installed Visual Studio C++ Build Tools")
    work, cache = args.workdir.resolve(), args.cache.resolve()
    if work.exists() and any(work.iterdir()):
        parser.error("--workdir must be empty; retain failed build logs and select a new directory")
    work.mkdir(parents=True, exist_ok=True)
    cache.mkdir(parents=True, exist_ok=True)
    lock = json.loads(Path(__file__).with_name("cpython_windows_runtime.json").read_text(encoding="utf-8"))
    archive = verified_download(lock["source"], cache)
    with tarfile.open(archive) as source_tar:
        source_tar.extractall(work, filter="data")
    source = work / ("Python-" + lock["version"])
    external = source / "externals"
    external.mkdir()
    for item in lock["externals"]:
        archive = verified_download(item, cache)
        extract_zip(archive, external)
        (external / (item["repository"] + "-" + item["commit"])).rename(external / item["name"])
    vswhere = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft Visual Studio/Installer/vswhere.exe"
    msbuild = subprocess.check_output(
        [str(vswhere), "-latest", "-products", "*", "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
         "-find", "MSBuild/Current/Bin/MSBuild.exe"], text=True).strip()
    if not msbuild or not Path(msbuild).is_file():
        raise RuntimeError("MSVC x64 Build Tools with MSBuild are required")
    env = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY", "TCLLIBPATH"):
        env.pop(name, None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["_CL_"] = "/MP1"
    # Direct MSBuild avoids upstream's network fetch and process-killing wrapper.
    command = [msbuild, str(source / "PCbuild/pcbuild.proj"), "/m:1", "/nologo", "/v:minimal",
               "/p:Configuration=Release", "/p:Platform=x64", "/p:PlatformToolset=v143",
               "/p:IncludeTests=false", "/p:KillPython=false", "/p:MultiProcessorCompilation=false",
               "/p:CL_MPCount=1", f"/p:PythonForBuild={sys.executable}"]
    run(command, work, "cpython-build.log", cwd=source, env=env)
    built = source / "PCbuild/amd64/python.exe"
    runtime = work / "python"
    run([str(built), "-B", str(source / "PC/layout"), "--source", str(source),
         "--build", str(built.parent), "--copy", str(runtime), "--include-dev", "--include-tcltk",
         "--include-venv", "--include-pip", "--include-stable"], work, "cpython-layout.log", cwd=source, env=env)
    executable = runtime / "python.exe"
    probe = ("import sys,struct,tkinter,ssl,json; from pathlib import Path; "
             "t=tkinter.Tcl(); p=Path(sys.executable).resolve().parent; "
             "print(json.dumps(dict(version=sys.version,prefix=sys.prefix,base_prefix=sys.base_prefix,"
             "bits=struct.calcsize('P')*8,tcl_library=t.eval('info library'),openssl=ssl.OPENSSL_VERSION))); "
             "assert sys.version_info[:3]==(3,12,14) and struct.calcsize('P')==8; "
             "assert Path(sys.prefix).resolve()==p and Path(sys.base_prefix).resolve()==p; "
             "assert Path(t.eval('info library')).resolve().is_relative_to(p); "
             "assert Path(tkinter.__file__).resolve().is_relative_to(p)")
    run([str(executable), "-I", "-c", probe], work, "runtime-identity.log", cwd=work, env=env)
    evidence = {"python": str(executable), "lock": lock, "command": command,
                "python_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "python_dll_sha256": hashlib.sha256((runtime / "python312.dll").read_bytes()).hexdigest(),
                "stable_abi_dll_sha256": hashlib.sha256((runtime / "python3.dll").read_bytes()).hexdigest()}
    (work / "runtime-build.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(executable)
    if os.environ.get("GITHUB_PATH"):
        with open(os.environ["GITHUB_PATH"], "a", encoding="utf-8") as out:
            out.write(str(runtime) + "\n" + str(runtime / "Scripts") + "\n")


if __name__ == "__main__":
    main()
