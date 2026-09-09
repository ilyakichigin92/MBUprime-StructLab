# Third-party notices

The Windows distribution, Ubuntu desktop/CLI onedir package, and target-built
source CLI installations of MBUprime StructLab
include a compiled, in-process RNAstructure 6.6 extension. The combined
distribution and its corresponding source are subject to the GNU General
Public License, version 2; see `COPYING`. Complete corresponding source must
accompany a binary release.

The checked corresponding-source staging satisfies this delivery obligation.
`JUD-20260824-001` is closed: the application/native build inputs, generators,
generated runtime modules, icon assets, licenses, and source hash manifest are
included in the release ZIP.

## RNAstructure 6.6

RNAstructure is distributed under the GNU General Public License, version 2.
The exact upstream source archive, upstream license text, custom CPython
wrapper, build scripts, compatibility tables, and their hash manifest are
included in the release's `_internal/rnastructure-corresponding-source/`
directory.

## seqfold 0.10.2

seqfold is Copyright (c) 2019 Lattice Automation and is distributed under the
MIT License. The exact license text used for the locked 0.10.2 dependency is
included at `vendor/seqfold-0.10.2/LICENSE` in corresponding source.
The runtime checks target-qualified hashes for the Windows x86-64, Linux
x86-64, and macOS arm64 native cores; an unknown target is rejected.

## Cascadia Mono

The Windows and Ubuntu application folders include Cascadia Mono from Microsoft under the
SIL Open Font License 1.1. `assets/fonts/CascadiaMono.ttf`, `OFL.txt`, and
`SOURCE.txt` are included both in the packaged application and corresponding
source. Windows registers it process-locally through GDI; Ubuntu resolves the
same hash-locked font through the bundled fontconfig configuration.

## Release component and license evidence

The Windows build inventories the third-party Python modules, distribution
metadata, native runtimes, engines, and font payload that PyInstaller actually
emitted. It copies the authoritative license and notice bytes available in the
build environment to `_internal/licenses/`, records their SHA-256 hashes
in `_internal/LICENSE-MANIFEST.json`, and emits a whole-application SPDX 2.3
JSON inventory at `_internal/SBOM.spdx.json`. The package verifier rejects
missing, extra, mutated, stale, or unclassified evidence.

The SPDX inventory deliberately uses `NOASSERTION` when upstream metadata does
not provide a machine-readable license expression. The automated evidence gate
does not replace human or legal review of redistribution obligations before a
public release.

This notice does not change release trust: local packages are unsigned
development artifacts. Authenticode signing, when explicitly configured, does
not by itself establish SmartScreen reputation or AppLocker/WDAC/EDR
allowlisting. The macOS delivery is source installation only and makes no code
signing or notarization claim.
