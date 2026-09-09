"""Canonical native-target identifiers shared by scientific integrity gates."""

from __future__ import annotations

import platform
import struct
import sys


class UnsupportedNativeTargetError(RuntimeError):
    """The process is not one of the explicitly supported CPython targets."""


def current_target() -> str:
    """Return the exact supported target or fail closed."""

    implementation = platform.python_implementation()
    version = sys.version_info[:2]
    bits = struct.calcsize("P") * 8
    machine = platform.machine().lower()
    if implementation != "CPython" or version != (3, 12) or bits != 64:
        raise UnsupportedNativeTargetError(
            "scientific native engines require 64-bit CPython 3.12; "
            f"found {implementation} {version[0]}.{version[1]} {bits}-bit"
        )
    normalized_machine = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }.get(machine, machine)
    if sys.platform == "win32" and normalized_machine == "x86_64":
        return "cp312-windows-x86_64"
    if sys.platform.startswith("linux") and normalized_machine == "x86_64":
        return "cp312-linux-x86_64"
    if sys.platform == "darwin" and normalized_machine == "arm64":
        return "cp312-macos-arm64"
    raise UnsupportedNativeTargetError(
        "unsupported scientific native target: "
        f"platform={sys.platform}, machine={machine}, python=cp312"
    )
