# Building the corresponding source

## Current status

`JUD-20260824-001` is closed. The checked staging tree includes the token/icon
generators, icon assets, generated tokens, and runtime asset module needed by
`build_exe.bat`; `source-manifest.json` covers every staged member.

The current source target is app 2.4.3, manifest schema 7, policy
`2026-09-08-context-retention-and-duplex-offsets-1`. The locally built
distribution targets app 2.4.3, manifest schema 7, and this policy.
The preserved `release/` snapshot uses the earlier
`2026-09-02-exact-engine-and-concrete-coverage-1` policy. A new build goes to
`dist/`; it does not replace that snapshot.
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
build scripts. The supported build target is 64-bit CPython 3.12 with Microsoft
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

The source staging also carries `windows_bootstrap.py`, the onedir PyInstaller
spec and Windows version resource, signing/package/verifier scripts, the
privately bundled Cascadia Mono font, `OFL.txt`, and `SOURCE.txt`. These inputs
are shipped at `_internal/rnastructure-corresponding-source/` in the flat release
layout. They rebuild the complete application folder; they neither establish
publisher identity nor authorize treating an unsigned development artifact as
trusted.
