# Contributing

Start with [development setup and tests](docs/development.md) and the
[architecture map](docs/architecture.md). The [file inventory](docs/repository-layout.md)
records retained modules and distinct build inputs; update it when adding or moving
a tracked path. Use Windows x64 with the pinned release
environment for Windows build verification; Ubuntu/macOS qualification is pending.

Keep changes focused and explain their effect on engine measurements, exact
geometry, severity, coverage, archive schemas and scientific policy. Add meaningful
regression coverage for behavior changes and run `python -m pytest tests -q` from
the repository root after installing the documented native/test environment.
Include the actual test summary, skipped checks and limitations in your pull request.

For packaging changes, also validate the extracted source distribution and the
relevant native build/package verifier. A documentation edit does not recertify an
older release binary. Update relative links and distribution inventories together
when moving documentation or adding required build inputs.

Use [Issues](https://github.com/ilyakichigin92/MBUprime-StructLab/issues) for bug reports
and feature requests. Include version, operating system, reproduction steps,
expected/actual behavior and relevant diagnostic excerpts. Prefer the supplied
synthetic example or another minimal public reproducer. Remove private sequences,
personal paths and credentials before posting; analyzed-run archives contain inputs
and results, and detailed verifier diagnostics may contain bundled file contents.

Do not commit private datasets, signing credentials, generated build directories or
local native binaries. Generate assets, native manifests and provenance through
their maintained scripts. Preserve GPL v2 and third-party licensing notices and
corresponding-source obligations. Contributions are distributed under [COPYING](COPYING).
