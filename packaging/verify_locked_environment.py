"""Check installed distribution versions against an exact requirements lock.

This checks package metadata, not installed-file integrity. Native file hashes
and the packaged payload are validated at their separate integrity boundaries.
"""

from __future__ import annotations

import argparse
import importlib.metadata
from pathlib import Path
import re
import sys


_PIN = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s;]+)$")
_HASHES = re.compile(r"(?: --hash=sha256:[0-9a-fA-F]{64})*$")


def _pins_from(requirements_path: Path, seen: set[Path] | None = None) -> dict[str, str]:
    seen = seen or set()
    path = requirements_path.resolve()
    if path in seen:
        raise ValueError(f"recursive requirements include: {path}")
    seen.add(path)
    pins: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            pins.update(_pins_from(path.parent / line[3:].strip(), seen))
            continue
        requirement, _, hashes = line.partition(" --hash=")
        if not hashes:
            raise ValueError(
                f"requirement is missing SHA-256: {line!r} in {path.name}")
        if not _HASHES.fullmatch(" --hash=" + hashes):
            raise ValueError(f"invalid hash declaration: {line!r} in {path.name}")
        match = _PIN.fullmatch(requirement)
        if not match:
            raise ValueError(f"requirement is not exactly pinned: {line!r} in {path.name}")
        pins[match.group(1).lower().replace("_", "-")] = match.group(2)
    return pins


def verify(requirements_path: Path) -> list[str]:
    """Return deterministic mismatch diagnostics for an exact requirements file."""
    diagnostics: list[str] = []
    for package, expected in sorted(_pins_from(requirements_path).items()):
        try:
            actual = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            diagnostics.append(f"missing {package}=={expected}")
        else:
            if actual != expected:
                diagnostics.append(f"{package}: expected {expected}, found {actual}")
    return diagnostics


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements", type=Path)
    args = parser.parse_args(argv)
    try:
        diagnostics = verify(args.requirements)
    except (OSError, ValueError) as error:
        print(f"Invalid lock: {error}", file=sys.stderr)
        return 2
    if diagnostics:
        print("Installed environment does not match the exact lock:", file=sys.stderr)
        print("\n".join(diagnostics), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
