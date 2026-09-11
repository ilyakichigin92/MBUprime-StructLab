# Windows release contract

## Current release status

The Windows app 2.4.5 deliverable is an **unsigned development** release. It is
never classified as trusted. Windows 10 22H2 and Windows 11 x64 are the
support targets; runtime verification evidence applies to its recorded host; a 32-bit Windows loader may reject the AMD64 executable before
Tk can display an error.

Executed host evidence is Windows 10 22H2 build 19045. Windows 11 is an intended
support target with clean-host qualification pending, not a verified platform.
The published version is [v2.4.5](https://github.com/ilyakichigin92/MBUprime-StructLab/releases/tag/v2.4.5).
The current source targets app 2.4.5, manifest schema 7, and scientific policy
`2026-09-08-context-retention-and-duplex-offsets-1`. New builds use `dist/` in the
selected checkout. Preserve existing release assets and their bundled source
identities; source-only changes do not retroactively certify them.

`python packaging\verify_release_identity.py source` fails unless generated
checkout provenance matches the current source identity, source-tree hash, and
exact dependency-lock hash. It does not assert that an existing release
snapshot contains later checkout changes. The package verifier additionally
binds each maintained PYZ module and bootstrap to its shipped source compiled
with `optimize=1`, retaining nested logic and constants while normalizing build
paths and debug line positions. Canonical provenance identifies build inputs;
it does not prove that separate builds produce identical binaries.

Verifier failures report concise mismatch summaries. To retain full values for
local investigation, explicitly request a new diagnostic file:

```powershell
python -B packaging\verify_release_identity.py package --diagnostics-json C:\mbu-audit\identity-mismatch.json
```

The parent directory must exist and the diagnostic file must not already exist.
Full diagnostics can contain bundled file contents; they are not printed or
written by default. A diagnostic-write failure does not turn verification into
success.

## Authoritative pipeline

Build from the pinned 64-bit CPython 3.12.14 runtime on Windows with Microsoft
Visual C++ Build Tools. The qualified route builds the hash-verified official
CPython source with MSVC in Release mode without profile-guided optimization;
it does not install or replace global Python. Pillow is pinned to 12.3.0.
The private runtime includes Tcl/Tk 8.6.15. Its upstream build dependencies
include OpenSSL 3.0.16; this runtime update is not a claim that every transitive
dependency is the newest available version.
Keep the checkout at a short ASCII-only path (for example `C:\mbu-build`):
upstream source extraction includes long documentation filenames that can exceed
Windows path limits in deeply nested build directories.

```powershell
python -B packaging\build_cpython_windows.py --workdir C:\mbu-build\runtime-01 --cache C:\mbu-build\runtime-cache
$runtimeRoot = 'C:\mbu-build\runtime-01\python'
$env:PATH = "$runtimeRoot;$runtimeRoot\Scripts;$env:PATH"
foreach ($name in 'PYTHONHOME','PYTHONPATH','TCL_LIBRARY','TK_LIBRARY','TCLLIBPATH') {
    Remove-Item "Env:$name" -ErrorAction SilentlyContinue
}
python -B -m pip install --require-hashes -r requirements-build.txt
.\build_exe.bat unsigned
```

Use an empty work directory for each runtime build. The helper records source
and external archive hashes, compiler command and produced runtime identities
in `runtime-build.json`, with separate compilation/layout logs. Its inputs are
locked in `packaging/cpython_windows_runtime.json`; the dependency lock then
installs the application/build wheels into that private runtime. A failed build
retains logs for inspection instead of replacing an earlier environment.

`unsigned` is the safe default. The locked pipeline order is validate/generate,
native build, provenance/source identity, PyInstaller onedir construction,
signing or unsigned attestation, post-sign hashes/archive/manifest, and final
package verification. No final release hash is computed before signing or the
machine-readable `unsigned_development` attestation.

`build_exe.bat signed` fails closed unless `MBUPRIME_SIGNTOOL_PATH` and exactly
one of `MBUPRIME_SIGNING_THUMBPRINT` or `MBUPRIME_SIGNING_SUBJECT` are explicit.
SHA-256 is mandatory. An RFC3161 timestamp is added only when an absolute HTTPS
`MBUPRIME_TIMESTAMP_URL` is explicitly configured. After signing, independent
PowerShell verification requires Authenticode `Valid`, a certificate chain
trusted on that verification host, and the configured signer identity. No
self-signed certificate is created or accepted as publisher trust.

Valid Authenticode reduces publisher ambiguity. It does not guarantee Microsoft
SmartScreen reputation, or AppLocker, WDAC, antivirus, or EDR allowlisting.

## Publication boundary

Public source includes the build entry points, native sources, pinned upstream
archive, runtime/build locks, asset generators and token inputs, packaging
specifications, corresponding-source inventory, and release verification tools.
The public checkout also includes curated tests, examples, contributor guidance
and workflow definitions. These are distinct from the executable payload and the
corresponding-source file selection. Generated `build/`, `dist/`, local provenance,
private signing material and private analysis records must remain untracked.
Ubuntu/macOS native qualification remains pending.

Review explicit file selection before publication. Preparing source or build tooling
does not itself build, promote, sign, tag or upload a release.

Use the qualified private 64-bit CPython 3.12.14 environment for the Windows pipeline; the
`requirements-build.txt` lock installs its runtime and build dependencies. The
asset checks use the published token JSON and icon generator. Native compilation
uses the vendored, hash-pinned RNAstructure archive and Microsoft C++ tools.
The pipeline creates its own `packaging/release_provenance.json` before packaging;
that generated local file is not a source-install input.

For an sdist, run `python setup.py sdist` from a source tree on a supported
CPython 3.12 host with the pinned setuptools installed. Metadata preparation
verifies and extracts the vendored archive but does not compile the extension.
The sdist carries the same build inputs and excludes binaries, workflows and generated work directories.
Inspect `MANIFEST.in` for the maintained documentation, example and test selection. A later wheel/install builds the target-specific
extension and embeds scientific provenance. Installing scientific dependencies
still requires the appropriate hashed target lock before `pip install --no-deps
--no-build-isolation .`.

The output ZIP carries the complete onedir package, licensing notices, and
path-preserving corresponding source under `_internal`. Verify its SHA-256
sidecar before extraction. Publication and any public upload are separate delivery actions. Preserve existing
unsigned release assets while preparing source changes; new source hashes do not certify an older binary.

## Application and archive layout

PyInstaller produces `dist\MBUprime StructLab\MBUprime StructLab.exe` plus
`dist\MBUprime StructLab\_internal\`. Keep the complete application folder
together. Its root contains exactly the EXE and `_internal`. It bundles Tcl/Tk,
Primer3, ViennaRNA, native RNAstructure, seqfold,
matplotlib, both icon formats, and the private Cascadia Mono font with `OFL.txt`
and `SOURCE.txt`. Onedir has no launch-time TEMP extraction.

The folder must be placed at an ASCII-only resolved path, for example
`C:\MBUprimeStructLab`, because primer3-py 2.3.0 can crash while importing from
a non-ASCII frozen path. The Tk-first bootstrap detects this after creating a
real Tk root and normally shows bilingual relocation guidance. Automated
`--self-test` uses an explicit log and exits nonzero without a modal, preventing
release probes from hanging.

The unsigned output names are:

- `dist\MBUprime-StructLab-2.4.5-windows-x64-unsigned.zip`
- `dist\MBUprime-StructLab-2.4.5-windows-x64-unsigned.zip.sha256`
- `dist\MBUprime-StructLab-2.4.5-windows-x64-unsigned.release-manifest.json`
- `dist\MBUprime StructLab.exe.sha256`
- `dist\MBUprime StructLab.signing-attestation.json`

The ZIP has no wrapper application folder: its root contains exactly
`MBUprime StructLab.exe` and `_internal`. Provenance, readme/license notices, an
internal copy of the EXE sidecar, the signing attestation, and the path-preserving
`_internal\rnastructure-corresponding-source\` tree all live under `_internal`.
The corresponding source includes all application, native build, Windows
bootstrap, packaging/signing/verifier, cross-platform target-lock, icon/font,
license-evidence generator/configuration, and pinned
upstream-source inputs plus `source-manifest.json`. The authoritative EXE
sidecar and signing attestation also remain external `dist` release assets. The
release manifest records recursive post-sign file identities. Its ZIP self-hash
remains external because an archive cannot contain a stable hash of itself.

Analyzed runs are a separate user-data format. App 2.4.5 writes deterministic
ZIP64/DEFLATE schema-2 `.mbusl-run` archives with ten fixed members and
line-streamed JSONL result records. Legacy schema-1 JSON is import-only. The
reader accepts at most 5,000,000 aggregate JSONL records, 256 MiB compressed,
512 MiB expanded, 16 MiB per JSONL record, 8 MiB per control member, JSON depth
64, 250,000 values per record or control document, and 250:1 member and
aggregate compression ratios. These bounds are inclusive. Streaming avoids a
second complete serialized graph, but restoring the interactive run still
constructs the complete typed report and therefore requires memory
proportional to that report. SHA-256 member/index checks prove integrity, not
artifact authenticity or publisher identity.

## Portability and failure probes

The package verifier parses the PE header and requires AMD64, verifies the live
Authenticode/trust classification, checks every recursive folder/archive/source
identity, and runs real-Tk/four-engine/font self-tests with `TEMP` and `TMP`
unset, pointed at a nonexistent directory, and pointed at an existing
non-writable path where host filesystem semantics allow. It copies the onedir tree
below a Cyrillic path and requires controlled exit 3, with `tk-root-ready`
logged before the ASCII install-path rejection instead of process crash
`0xC0000005`.

The packaged self-test also performs schema-2 and legacy schema-1 analyzed-run
path round trips using a static report. That archive probe imports no saved run
through the scientific analysis pipeline and does not invoke an engine.

For a supplied completed archive, an explicit qualification option additionally
exercises normal GUI import, cancellation, rendering, localization, show-all,
sorting and stale-input rejection inside the same executable:

```powershell
& '.\MBUprime StructLab.exe' --self-test --self-test-log C:\mbu-audit\large-gui.log --qualify-archive C:\mbu-data\large-run.json
```

Both self-test flags are required. The scenario chooses only the supplied data
file and automatically accepts its import confirmation, then restores its local
dialog callbacks. It does not execute supplied code or save global settings.
Recorded row counts and heartbeat gaps qualify programmatic frozen GUI behavior;
they do not substitute for manual input, accessibility or clean-host testing.
Loading now keeps Tk active and supports cancellation; this does not promise
shorter total decode time. Full report restoration still uses memory proportional
to the archive, including a retained previous report during replacement.

Export failures remain actionable. Windows Controlled Folder Access (CFA),
read-only destinations, disconnected network shares, and OneDrive
synchronization or placeholder errors are preserved and reported; the app does
not silently redirect the requested export or call a failed write successful.
