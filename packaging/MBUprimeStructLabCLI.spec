# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import seqfold

from PyInstaller.utils.hooks import collect_all, collect_data_files


project_root = Path(SPECPATH).parent
staging_root = project_root / "build" / "linux-package-staging"
provenance_path = staging_root / "release_provenance.json"
corresponding_source = staging_root / "rnastructure-corresponding-source"
for required in (provenance_path, corresponding_source / "source-manifest.json"):
    if not required.is_file():
        raise RuntimeError(
            f"Linux package staging is incomplete: {required}; "
            "run packaging/stage_linux_bundle.py first")


def collect_packages(packages):
    datas = []
    binaries = []
    hiddenimports = []
    for package in packages:
        package_datas, package_binaries, package_hidden = collect_all(package)
        datas += package_datas
        binaries += package_binaries
        hiddenimports += package_hidden
    return datas, binaries, hiddenimports


common_packages = ("RNA", "primer3", "seqfold", "rnastructure_native")
common_datas, common_binaries, common_hidden = collect_packages(common_packages)

# Runtime engine attestation hashes these exact files. Keep the Python sources
# outside PYZ beside the target-specific native core.
seqfold_root = Path(seqfold.__file__).resolve().parent
for member in ("__init__.py", "main.py"):
    common_datas.append((str(seqfold_root / member), "seqfold"))
seqfold_core = seqfold_root / "_core.abi3.so"
if not seqfold_core.is_file():
    raise RuntimeError("verified Linux seqfold native core is missing")
common_binaries.append((str(seqfold_core), "seqfold"))

release_datas = [
    (str(project_root / "README.md"), "."),
    (str(project_root / "COPYING"), "."),
    (str(project_root / "THIRD_PARTY_NOTICES.md"), "."),
    (str(provenance_path), "."),
    (str(corresponding_source), "rnastructure-corresponding-source"),
    (str(project_root / "assets" / "mbu_sl_laboratory_tile.ico"), "assets"),
    (str(project_root / "assets" / "fonts" / "CascadiaMono.ttf"), "assets/fonts"),
    (str(project_root / "assets" / "fonts" / "OFL.txt"), "assets/fonts"),
    (str(project_root / "assets" / "fonts" / "SOURCE.txt"), "assets/fonts"),
    (str(project_root / "assets" / "fonts" / "linux-fonts.conf"), "assets/fonts"),
]
matplotlib_datas, matplotlib_binaries, matplotlib_hidden = collect_packages(
    ("matplotlib",))
matplotlib_datas += collect_data_files(
    "matplotlib",
    includes=["mpl-data/**"],
    excludes=["**/tests/**", "**/testing/**", "**/mpl-data/sample_data/**"],
)

gui_analysis = Analysis(
    [str(project_root / "packaging" / "linux_gui_bootstrap.py")],
    pathex=[str(project_root)],
    binaries=common_binaries + matplotlib_binaries,
    datas=common_datas + matplotlib_datas + release_datas,
    hiddenimports=common_hidden + matplotlib_hidden + [
        "primer_tool_gui", "_tkinter", "tkinter", "tkinter.filedialog",
        "tkinter.font", "tkinter.messagebox", "tkinter.simpledialog",
        "tkinter.ttk", "matplotlib.backends.backend_tkagg",
        "matplotlib.backends.backend_agg",
    ],
    excludes=[
        "IPython", "jupyter", "notebook", "matplotlib.tests",
        "matplotlib.testing", "pandas", "pytest", "scipy", "Cython",
        "cython", "setuptools.tests",
    ],
    noarchive=False,
    optimize=1,
)
gui_pyz = PYZ(gui_analysis.pure)
gui_exe = EXE(
    gui_pyz, gui_analysis.scripts, [], exclude_binaries=True,
    name="MBUprime StructLab", console=False, debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False,
)

cli_analysis = Analysis(
    [str(project_root / "packaging" / "cli_bootstrap.py")],
    pathex=[str(project_root)],
    binaries=common_binaries,
    datas=common_datas,
    hiddenimports=common_hidden,
    excludes=[
        "tkinter", "_tkinter", "primer_tool_gui", "gui_exports",
        "gui_results", "gui_styles", "structure_draw", "matplotlib",
        "IPython", "jupyter", "notebook", "pandas", "pytest", "scipy",
    ],
    noarchive=False,
    optimize=1,
)
cli_pyz = PYZ(cli_analysis.pure)
cli_exe = EXE(
    cli_pyz, cli_analysis.scripts, [], exclude_binaries=True,
    name="mbuprime-structlab", console=True, debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False,
)

coll = COLLECT(
    gui_exe, cli_exe,
    gui_analysis.binaries, gui_analysis.datas,
    cli_analysis.binaries, cli_analysis.datas,
    strip=False, upx=False, name="mbuprime-structlab-linux-x86_64",
)
