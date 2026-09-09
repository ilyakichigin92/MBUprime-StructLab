#!/usr/bin/env bash
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
[[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]] || exit 3
python -c "import sys; assert sys.version_info[:2] == (3, 12)" || exit 3
xcrun --find clang >/dev/null || {
  echo "Xcode Command Line Tools with Clang are required" >&2
  exit 3
}
python -m pip install --disable-pip-version-check --require-hashes \
  -r requirements-targets/macos-primer3-build.txt
python -m pip install --disable-pip-version-check --require-hashes \
  --no-build-isolation \
  -r requirements-targets/macos-14-arm64.txt
python -m pip install --disable-pip-version-check --no-deps \
  --no-build-isolation .
python -m mbuprime_structlab --help >/dev/null
python -c "import mbuprime_structlab.cli,sys; forbidden={'tkinter','primer_tool_gui','gui_exports','structure_draw','matplotlib'}; assert not forbidden.intersection(sys.modules)"
printf 'name\trole\tsequence\ncontrol\tprimer\tACGTACGTACGTACGTACGT\n' | \
  mbuprime-structlab analyze - --format tsv --progress none >/dev/null
