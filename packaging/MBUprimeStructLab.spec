# -*- mode: python ; coding: utf-8 -*-

import fnmatch
import json
import sys
from pathlib import Path
import seqfold

from PyInstaller.utils.hooks import collect_all, collect_data_files, copy_metadata


project_root = Path(SPECPATH).parent
python_root = Path(sys.base_prefix)
python_lib_dir = Path(sys.base_prefix) / "Lib"
python_dll_dir = python_root / "DLLs"
python_tcl_dir = python_root / "tcl"
python_tkinter_dir = python_lib_dir / "tkinter"
inventory_path = project_root / "packaging" / "release_inventory.json"
release_inventory = json.loads(inventory_path.read_text(encoding="ascii"))
exclusion_families = release_inventory["clean_build_exclusions"]["families"]
font_allowlist = release_inventory["payload_allowlists"]["matplotlib_fonts"]
MATPLOTLIB_FONT_PREFIX = font_allowlist["path_prefix"].lower()
MATPLOTLIB_FONT_FILES = {
    MATPLOTLIB_FONT_PREFIX + path.lower() for path in font_allowlist["files"]}
AMBIENT_BUILD_MODULES = tuple(
    exclusion_families["ambient_build_modules"]["module_prefixes"])

UNUSED_MODULE_PREFIXES = (
    "PIL._avif", "PIL._webp", "PIL._imagingcms", "PIL._imagingmath",
    "PIL.AvifImagePlugin", "PIL.WebPImagePlugin", "PIL.ImageCms", "PIL.ImageMath",
    "matplotlib.backends.backend_webagg", "matplotlib.backends.backend_webagg_core",
    "matplotlib.backends.web_backend", "numpy.f2py", "numpy.testing",
    "pyparsing.diagram", "pyreadline3", "charset_normalizer",
    "defusedxml", "certifi", "pandas", "scipy", "Cython",
)
UNUSED_PATH_PREFIXES = (
    "_tk_data/demos/", "tkinter/__pycache__/", "numpy/f2py/", "numpy/testing/",
    "pyparsing/diagram/", "matplotlib/tests/",
    "matplotlib/testing/", "matplotlib/mpl-data/sample_data/",
    "matplotlib/mpl-data/web_backend/",
)
UNUSED_TKINTER_SOURCES = {
    "tkinter/__main__.py", "tkinter/colorchooser.py", "tkinter/dnd.py",
    "tkinter/scrolledtext.py", "tkinter/tix.py",
}
UNUSED_PRIMER3_COMMANDS = {
    "amplicon3_core.exe", "ntdpal.exe", "ntthal.exe", "oligotm.exe", "primer3_core.exe",
}
NONESSENTIAL_DIST_INFO = {
    "entry_points.txt", "installer", "record", "requested", "top_level.txt", "wheel",
}
AMBIENT_PACKAGES = ("pyreadline3", "charset_normalizer", "defusedxml", "certifi")


def _matches_canonical_exclusion(path, layers):
    module_name = path.replace("/", ".")
    for rule in exclusion_families.values():
        if not set(layers).intersection(rule.get("enforced_layers", ())):
            continue
        if any(module_name == prefix.lower() or module_name.startswith(prefix.lower() + ".")
               for prefix in rule.get("module_prefixes", ())):
            return True
        if any(path.startswith(prefix.lower())
               for prefix in rule.get("path_prefixes", ())):
            return True
        if any(fnmatch.fnmatch(path, pattern.lower())
               for pattern in rule.get("path_globs", ())):
            return True
        if "hooks" in layers:
            hook = path.rsplit("/", 1)[-1].removesuffix(".py")
            if any(hook == name.lower().removesuffix(".py")
                   for name in rule.get("hook_names", ())):
                return True
    return False


def _is_unneeded_toc_entry(entry, layers):
    path = str(entry[0]).replace("\\", "/").lower().lstrip("./")
    module_name = path.replace("/", ".")
    if path.startswith(MATPLOTLIB_FONT_PREFIX):
        return path not in MATPLOTLIB_FONT_FILES
    if any(module_name == prefix.lower() or module_name.startswith(prefix.lower() + ".")
           for prefix in UNUSED_MODULE_PREFIXES):
        return True
    if path in UNUSED_TKINTER_SOURCES or any(path.startswith(prefix) for prefix in UNUSED_PATH_PREFIXES):
        return True
    if _matches_canonical_exclusion(path, layers):
        return True
    if path.startswith("primer3/") and path.rsplit("/", 1)[-1] in UNUSED_PRIMER3_COMMANDS:
        return True
    head = path.split("/", 1)[0]
    if any(head == package or head.startswith(package + "-") for package in AMBIENT_PACKAGES):
        return True
    return ".dist-info/" in path and path.rsplit("/", 1)[-1] in NONESSENTIAL_DIST_INFO

provenance_path = project_root / "packaging" / "release_provenance.json"
if not provenance_path.exists():
    raise RuntimeError("release_provenance.json is required; run packaging/generate_provenance.py first")

datas = [
    (str(project_root / "README.md"), "."),
    (str(provenance_path), "."),
    (str(project_root / "assets" / "mbu_sl_laboratory_tile.ico"), "assets"),
    (str(project_root / "assets" / "mbu_sl_laboratory_tile.svg"), "assets"),
    (str(project_root / "assets" / "fonts" / "CascadiaMono.ttf"), "assets/fonts"),
    (str(project_root / "assets" / "fonts" / "OFL.txt"), "assets/fonts"),
    (str(project_root / "assets" / "fonts" / "SOURCE.txt"), "assets/fonts"),
]
binaries = []
hiddenimports = [
    "frozen_gui_qualification",
    "primer_tool_gui",
    "_tkinter",
    "tkinter",
    "tkinter.filedialog",
    "tkinter.messagebox",
    "tkinter.ttk",
    "matplotlib.backends.backend_tkagg",
    "matplotlib.backends.backend_agg",
    "matplotlib.backends.backend_pdf",
    "matplotlib.backends.backend_svg",
    "primer3",
    "RNA",
]

if (python_tcl_dir / "tcl8.6").exists():
    datas.append((str(python_tcl_dir / "tcl8.6"), "_tcl_data"))
if (python_tcl_dir / "tk8.6").exists():
    datas.append((str(python_tcl_dir / "tk8.6"), "_tk_data"))
if (python_tcl_dir / "tcl8").exists():
    datas.append((str(python_tcl_dir / "tcl8"), "tcl8"))
if python_tkinter_dir.exists():
    datas.append((str(python_tkinter_dir), "tkinter"))
for tk_binary in ("_tkinter.pyd", "tcl86t.dll", "tk86t.dll"):
    binary_path = python_dll_dir / tk_binary
    if binary_path.exists():
        binaries.append((str(binary_path), "."))

for package in ("RNA", "primer3", "seqfold", "rnastructure_native"):
    package_datas, package_binaries, package_hiddenimports = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hiddenimports
datas += copy_metadata("primer3-py")

# Runtime mandatory-engine attestation hashes these exact seqfold files. Pure
# Python modules normally remain inside PyInstaller's PYZ, so also extract the
# locked bytes beside the bundled native core.
seqfold_root = Path(seqfold.__file__).resolve().parent
for seqfold_member in ("__init__.py", "main.py"):
    datas.append((str(seqfold_root / seqfold_member), "seqfold"))
seqfold_core = seqfold_root / "_core.pyd"
if seqfold_core.exists():
    binaries.append((str(seqfold_core), "seqfold"))

datas += collect_data_files(
    "matplotlib",
    includes=["mpl-data/**"],
    excludes=[
        "**/tests/**",
        "**/testing/**",
        "**/mpl-data/sample_data/**",
    ],
)

a = Analysis(
    [str(project_root / "windows_bootstrap.py")],
    pathex=[str(project_root)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(project_root / "packaging" / "pyi_rth_tcltk.py")],
    excludes=[
        "IPython",
        "jupyter",
        "notebook",
        "matplotlib.tests",
        "matplotlib.testing",
        "pandas",
        "pandas.tests",
        "pytest",
        "scipy",
        "scipy.tests",
        "Cython",
        "cython",
        "setuptools.tests",
        *AMBIENT_BUILD_MODULES,
        *UNUSED_MODULE_PREFIXES,
    ],
    noarchive=False,
    optimize=1,
)
for toc_name, layers in (
        ("binaries", ("physical",)),
        ("datas", ("physical",)),
        ("pure", ("pyz",)),
        ("scripts", ("carchive", "hooks"))):
    toc = getattr(a, toc_name)
    setattr(a, toc_name, [
        entry for entry in toc if not _is_unneeded_toc_entry(entry, layers)])
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="MBUprime StructLab",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(project_root / "assets" / "mbu_sl_laboratory_tile.ico"),
    version=str(project_root / "packaging" / "version_info.txt"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="MBUprime StructLab",
)
