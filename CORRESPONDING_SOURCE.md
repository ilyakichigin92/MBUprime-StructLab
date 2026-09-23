# Building the corresponding source

## Current status

The current source target is app 2.4.6, manifest schema 7, policy
`2026-09-08-context-retention-and-duplex-offsets-1`.
The locally built distribution targets app 2.4.6, manifest schema 7, and this policy.
Each binary carries its own
path-preserving corresponding-source tree and `source-manifest.json` under
`_internal/rnastructure-corresponding-source/`. The token/icon generators, assets,
generated tokens and runtime asset module are maintained rebuild inputs.

New builds go to `dist/`. Existing published releases retain their original source
snapshots and hashes; later checkout edits do not change or certify those binaries.
See [the release contract](packaging/RELEASE.md) for the full build procedure.

Version-level agreement alone does not establish source identity. Use
`python packaging/verify_release_identity.py source` to compare live source
with generated provenance; the packaged corresponding-source manifest records
the source snapshot shipped with that binary. After changing source or included
documentation, rebuild and verify before promoting a new binary. An existing
release continues to describe its own bundled snapshot, not later checkout edits.
The package verifier compares all maintained PYZ modules and bootstrap entries
with their corresponding source compiled at optimization level 1. Build paths
and debug line positions are normalized; executable instructions, constants,
exception handling and nested code must match.

## Rebuild procedure

The release source tree preserves every project-relative path expected by the
build scripts. The supported build target is exactly 64-bit CPython 3.12.14 with Microsoft
Visual C++ Build Tools on Windows.

1. Install the exact Python build environment with
   `python -m pip install --require-hashes -r requirements-build.txt`.
2. Run `build_exe.bat unsigned` (or the fail-closed `signed` mode with an
   explicitly configured SignTool and certificate identity). The authoritative
   pipeline must verify the lock and
   generated token/icon assets, build the in-process RNAstructure module and its
   native/table manifest, then generate and verify provenance before creating
   the complete onedir application and release ZIP. Native preparation must
   precede provenance because the target-qualified manifest under
   `rnastructure_native/native_manifests/` is a scientific source input.
   Signing or `unsigned_development` attestation must
   precede every final hash, archive, and release manifest.

`native/setup_native.py` verifies the upstream source archive before
extraction. The target-qualified native manifest records the exact
extension, wrapper sources, and compatibility-table identities. Runtime code
rejects any missing, extra, or modified table.

The corresponding-source tree also carries `windows_bootstrap.py`, the onedir PyInstaller
spec and Windows version resource, signing/package/verifier scripts, the
privately bundled Cascadia Mono font, `OFL.txt`, and `SOURCE.txt`. These inputs
are shipped at `_internal/rnastructure-corresponding-source/` in the flat release
layout. They rebuild the complete application folder; they neither establish
publisher identity nor authorize treating an unsigned development artifact as
trusted.

The pinned RNAstructure source archive SHA-256 is
`4e30fa06f10a89556ad070c8d141fff6090165c330df786e19f9627df4407fd4`.
