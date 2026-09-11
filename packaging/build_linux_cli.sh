#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

[[ "$(uname -s)" == "Linux" && "$(uname -m)" == "x86_64" ]] || {
  echo "Ubuntu x86-64 native host required" >&2
  exit 3
}
python -c "import sys; assert sys.version_info[:2] == (3, 12)" || exit 3
python -m pip install --disable-pip-version-check --require-hashes \
  -r requirements-targets/ubuntu-24.04-x86_64.txt
python -m pip install --disable-pip-version-check --require-hashes \
  -r requirements-targets/ubuntu-build-common.txt
python -c "import tkinter; assert tkinter.TkVersion >= 8.6" || {
  echo "Ubuntu python3-tk/Tcl-Tk runtime is required" >&2
  exit 3
}
python setup.py build_ext --inplace
python -c "from native_target import current_target; from rnastructure_native import create_backend; assert current_target() == 'cp312-linux-x86_64'; assert create_backend().runtime_identity()['target'] == current_target()"
python packaging/stage_linux_bundle.py
python -m PyInstaller --clean --noconfirm packaging/MBUprimeStructLabCLI.spec

archive="dist/mbuprime-structlab-2.4.4-ubuntu24.04-x86_64.tar.gz"
archive_name="$(basename "$archive")"
tar --sort=name --mtime='UTC 1970-01-01' --owner=0 --group=0 --numeric-owner \
  -C dist -czf "$archive" mbuprime-structlab-linux-x86_64
python packaging/verify_linux_cli_tar.py "$archive"
(
  cd dist
  sha256sum "$archive_name" > "$archive_name.sha256"
  sha256sum --check "$archive_name.sha256"
)
