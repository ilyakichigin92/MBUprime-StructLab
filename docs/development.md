# Development and tests

[Architecture](architecture.md) · [CLI reference](cli.md) · [Release build contract](../packaging/RELEASE.md)

Use a short ASCII-only checkout path on Windows, for example `C:\mbu-build`.
Source metadata supports 64-bit CPython 3.12; the authoritative Windows executable
build **requires exactly CPython 3.12.14 x64**, Microsoft Visual C++ Build Tools,
and the hashed build lock. The private interpreter build is documented in the
[release contract](../packaging/RELEASE.md#authoritative-pipeline).
Use an isolated environment; do not replace system Python.

From the repository root, with that interpreter active:

```powershell
python -m pip install --require-hashes --only-binary=:all: -r requirements-test.txt
.\native\build_native.ps1
python packaging\generate_provenance.py
python packaging\verify_release_identity.py source
python -m pytest tests -q
```

The native build verifies the vendored RNAstructure archive, compiles its extension,
and generates a manifest for that exact native artifact. Native preparation must
precede provenance generation. Run tests after these steps: source imports without
the extension and matching mandatory-engine identities are not a complete setup.
Native compilation can take substantially longer than the small example analysis.

To run the desktop or exercise the supplied headless example:

```powershell
python primer_tool_gui.py
python -m mbuprime_structlab analyze examples/small-panel/panel.tsv --format tsv --max-workers 1 --output example-result.tsv
```

The test lock includes the release build lock. The curated public suite is rooted
at `tests/` and can also be run in separate groups:

```powershell
python -m pytest -q -m "not engine and not gui"
python -m pytest -q -m engine
python -m pytest -q -m gui
```

The first group contains fast checks and can run before native compilation.
The `engine` group invokes compiled engines or checks their runtime identities;
prepare the native environment first. The `gui` group creates a Windows Tk window
and needs an interactive desktop. Skipped display-dependent checks are not qualification evidence.
Run the documented command from a clean checkout and report the pass/skip/failure
summary with changes. [Validation scope](validation.md) separates automated tests
from manual and target-host qualification. Avoid committing generated test reports.

## Building distributions

`build_exe.bat unsigned` builds and verifies a fresh Windows onedir application and
ZIP under `dist/`. It does not publish a release. For exact interpreter prerequisites,
signing, artifact layout and verification use the [release contract](../packaging/RELEASE.md).

For a source distribution, use `python setup.py sdist` with the pinned setuptools
on CPython 3.12. The maintained gate builds a clean sdist, checks its required inputs,
builds a native wheel from the extracted archive, installs it into an isolated target
directory, and runs a live native probe:

```powershell
python packaging/verify_source_distribution.py --workdir C:\mbu-sdist-check-01
```

Choose a new or empty work directory outside the checkout and its parent paths.
Use a short ASCII-only path: upstream source extraction can exceed Windows path limits.
The gate retains logs and build products there, including after failure. Add
`--contents-only` for archive inspection without the wheel/native build; this
cheaper check does not prove that the extracted package builds. A wheel
or source installation builds the target extension; install the appropriate hashed
target lock before `python -m pip install --no-deps --no-build-isolation .`.

Ubuntu uses `make install` or `bash packaging/build_linux_cli.sh`; the bundle verifier
requires `xvfb` and `xauth`, including on desktop hosts. The display-dependent source
check is `xvfb-run -a make verify-gui`. macOS uses
`bash packaging/verify_macos_cli.sh` for its CLI-only route. Run platform builds
on their native hosts. Both routes remain unverified; full installation details
are in [platforms](platforms.md).

Do not manually edit generated tokens, icons, provenance or native identities.
Preserve component licenses, corresponding-source inputs and prior release bytes.
See [CONTRIBUTING.md](../CONTRIBUTING.md) for change and issue guidance.
