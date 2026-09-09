# Architecture and repository map

[Development](development.md) · [Scientific methods](methods.md) · [Release contract](../packaging/RELEASE.md)

The GUI and CLI share the scientific orchestration layer. Input validation builds
oligonucleotides and reaction conditions; analysis discovers candidate geometries,
evaluates them with applicable engines, applies gap and near-duplicate policies,
and publishes a completed report. Mandatory engine identity checks precede analysis.

| Boundary | Files and responsibility |
|---|---|
| Desktop entry and UI | `windows_bootstrap.py` initializes Windows/Tk safeguards; `primer_tool_gui.py` owns input, background analysis, cancellation and views. |
| Headless entry | `mbuprime_structlab/cli.py` validates strict TSV and options, invokes analysis and atomically publishes a report without importing Tk. |
| Scientific orchestration | `thermo_engine.py` owns conditions, oligos, analysis reports, candidate handling and severity. `nn_thermo.py` supports nearest-neighbor calculations. |
| Engines | `external_engines.py`, `vienna_backend.py` and `rnastructure_native/` connect the mandatory engines; `native/` builds the RNAstructure extension. |
| Geometry and presentation | `headless_geometry.py` supplies display-independent geometry; `structure_draw.py` renders diagrams; `gui_results.py`, `gui_styles.py` and `gui_exports.py` support views and exports. |
| Persisted results | `analyzed_run_archive.py` validates and restores typed reports; schema-2 ZIP/JSONL and legacy schema-1 import are distinct from release ZIPs. |
| Scientific identity | `scientific_metadata.py`, native manifests and `packaging/release_inventory.json` bind source, policies, engines and tables. |
| Build inputs | `vendor/` holds pinned upstream source and licenses; `assets/` and `tokens/` retain source and generated UI assets. |
| Packaging | `packaging/`, `setup.py`, `pyproject.toml`, `MANIFEST.in` and target locks define native build, distribution and verification. |
| Public onboarding | `docs/`, `examples/`, `tests/`, `CONTRIBUTING.md` and issue templates document use and regression checks. |

GUI work runs in a background worker with cooperative cancellation; Tk publication
of completed tables remains on the GUI thread. A failed or cancelled analysis keeps
the previous completed report. Archive restoration reuses saved results after
validation and confirmation, without running scientific engines again.

Root implementation modules remain in place because CLI imports, native builds,
PyInstaller collection and provenance inventories depend on those paths. A future
module move must update those contracts and tests together. `setup.py` performs
native compilation/provenance work and is not redundant with `pyproject.toml`.
The `scaled` and `enthalpy_as_dg` table trees serve different scientific evaluations;
neither is disposable build output.

## Packaging navigation

- `build_exe.bat` and `packaging/MBUprimeStructLab.spec`: Windows onedir application.
- `packaging/build_cpython_windows.py`: pinned private release interpreter.
- `packaging/generate_provenance.py` and `verify_release_identity.py`: source and package identity.
- `packaging/build_linux_cli.sh`: unverified native Ubuntu GUI/CLI onedir route.
- `packaging/install_linux_source.py` and `Makefile`: unverified transactional Ubuntu source installation.
- `packaging/verify_macos_cli.sh`: unverified native macOS CLI qualification.
- `packaging/release_inventory.json`: explicit file groups for scientific identity and corresponding source.

Dependency files have different roles: `requirements.in` describes ranges,
`requirements.txt` delegates to the exact runtime lock, `requirements-build.txt`
pins the release toolchain, `requirements-test.txt` adds the public test environment,
and `requirements-targets/` pins target-specific artifacts. The Windows CLI target
lock intentionally omits GUI dependencies.
