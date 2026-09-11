# Platforms and source installation

[Windows quick start](../README.md) · [Development](development.md)

## Platform support

| Target | Delivery | Status |
|---|---|---|
| Windows 10 22H2 x86-64, CPython 3.12 | Unsigned GUI onedir release; source CLI | Tested on build 19045; artifact evidence and qualification limits are recorded separately |
| Windows 11 x86-64, CPython 3.12 | Intended GUI onedir support target | Clean Windows 11 qualification pending; Windows 10 evidence does not verify Windows 11 |
| Ubuntu 24.04 x86-64, CPython 3.12 | Native PyInstaller **onedir** tarball, or transactional `make install` into a private versioned environment | Implementation-ready; native source-install, Xvfb GUI, DISPLAY-free CLI, and artifact verification pending |
| macOS 14 arm64, CPython 3.12 | Tagged-source virtual-environment CLI only | Implementation-ready; target-host build/test pending |

macOS has no GUI, `.app`, DMG, standalone binary, signing, or notarization route. A source-installed command is not a notarized application. All targets fail closed unless exact seqfold and target-built RNAstructure identities validate; there is no optional-engine mode.

The Windows release is unsigned development software. File hashes identify bytes but do not authenticate its publisher. Consult [the release contract](../packaging/RELEASE.md) for candidate identity, clean-host qualification and publication conditions.

For macOS, install CPython 3.12 and the Xcode Command Line Tools (`xcode-select --install`), then check out a signed or otherwise independently verified release tag and create a virtual environment from that source tree. Confirm that `xcrun --find clang` succeeds. Inside the environment, install `requirements-targets/macos-primer3-build.txt` with `--require-hashes`, install `requirements-targets/macos-14-arm64.txt` with `--require-hashes --no-build-isolation`, and finally install this project with `--no-deps --no-build-isolation`. `packaging/verify_macos_cli.sh` performs that exact sequence, builds and validates RNAstructure, checks the headless import boundary, and runs a live four-engine analysis. Do not use an arbitrary moving branch or a plain dependency-resolving `pipx install .` command as the verified route.

## Ubuntu desktop and source routes (unverified)

On Ubuntu, after the native workflow is green and its artifact has been published, verify the adjacent `.sha256` file, extract the complete tarball, and keep both launchers beside the shared `_internal` directory. Start the desktop interface with `./MBUprime\ StructLab`; run the packaged CLI as `./mbuprime-structlab analyze INPUT [options]`. This is an onedir package: moving either launcher away from `_internal` breaks it. No Python installation is required to run that packaged artifact. The Ubuntu package is not yet a verified release artifact and must not be advertised as one until the native job records evidence and a final checksum.

### Install from source on Ubuntu

Ubuntu 24.04 x86-64 can also build a private, versioned installation from a verified source checkout. Install 64-bit CPython 3.12 with `venv`, development-header, and Tk support, plus `g++`, `make`, and Fontconfig. From the repository root, run:

```sh
make install
make verify-install
```

For a normal user, `make install` defaults to `$HOME/.local`; when run as root it defaults to `/usr/local`. The installer never runs `sudo` and never installs packages into the system Python. It creates an isolated environment under `$PREFIX/lib/mbuprime-structlab/2.4.5/venv`, builds the pinned in-process RNAstructure extension locally, validates all mandatory engines, performs a display-free live CLI analysis, and only then atomically updates the relative `current` link. The commands are `$PREFIX/bin/mbuprime-structlab` and `$PREFIX/bin/mbuprime-structlab-gui`; a desktop entry is installed under `$PREFIX/share/applications`.

Use an absolute custom prefix with `make install PREFIX=/absolute/path`. The filesystem root is deliberately rejected as a prefix. Packaging roots are supported with an empty or absolute non-root `DESTDIR`, for example `make install PREFIX=/usr/local DESTDIR=/tmp/package-root`. `DESTDIR` is not embedded in installed launchers. For an offline build, first collect every artifact named by `requirements-targets/ubuntu-source-build.txt` and `requirements-targets/ubuntu-24.04-x86_64.txt`, then pass the absolute directory as `WHEELHOUSE=/absolute/wheelhouse`.

`make print-install-layout` shows every destination. `make verify-gui` performs the real Tk self-test and therefore needs an active display or `xvfb-run -a make verify-gui`. `make uninstall` removes only files bearing the matching installation markers; it refuses to recursively remove unmarked paths. A failed build or same-version reinstall leaves the previously activated version and launchers intact. After an interrupted replacement, rerun `make install`; recovery recognizes matching transaction markers and restores the prior version or removes a completed transaction remnant before retrying. This does not guarantee recovery after power loss or filesystem corruption.

The source-install route remains unverified until the native Ubuntu workflow completes successfully; the existing PyInstaller onedir tarball workflow is unchanged.
