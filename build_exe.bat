@echo off
setlocal
cd /d "%~dp0"

set "RELEASE_MODE=%~1"
if "%RELEASE_MODE%"=="" set "RELEASE_MODE=unsigned"
if /I "%RELEASE_MODE%"=="unsigned" goto :mode_ok
if /I "%RELEASE_MODE%"=="signed" goto :mode_ok
echo Usage: build_exe.bat [unsigned^|signed]
exit /b 2

:mode_ok
rem Resolve Tcl/Tk from the selected private interpreter, not ambient overrides.
set "TCL_LIBRARY="
set "TK_LIBRARY="
set "TCLLIBPATH="
python -c "import sys,struct; assert sys.version_info[:3] == (3,12,14) and struct.calcsize('P') == 8, 'Release builds require verified CPython 3.12.14 x64; see packaging/build_cpython_windows.py'"
if errorlevel 1 exit /b 1
python packaging\verify_locked_environment.py requirements-build.txt 1>nul 2>nul
if errorlevel 1 (
    echo Build environment does not match the exact release lock. Install with:
    echo python -m pip install --require-hashes -r requirements-build.txt
    exit /b 1
)

python packaging\generate_ui_tokens.py --check
if errorlevel 1 exit /b 1

python assets\generate_app_icon.py --check
if errorlevel 1 exit /b 1

powershell -NoProfile -ExecutionPolicy Bypass -File native\build_native.ps1
if errorlevel 1 exit /b 1

python packaging\generate_provenance.py
if errorlevel 1 exit /b 1

python packaging\verify_release_identity.py source
if errorlevel 1 exit /b 1

python -m PyInstaller --noconfirm --clean packaging\MBUprimeStructLab.spec
if errorlevel 1 exit /b 1

python packaging\generate_release_evidence.py
if errorlevel 1 exit /b 1

powershell -NoProfile -ExecutionPolicy Bypass -File packaging\sign_release.ps1 -Mode "%RELEASE_MODE%"
if errorlevel 1 exit /b 1

powershell -NoProfile -ExecutionPolicy Bypass -File packaging\make_release_zip.ps1 -Mode "%RELEASE_MODE%"
if errorlevel 1 exit /b 1

python packaging\verify_release_identity.py package --mode "%RELEASE_MODE%"
if errorlevel 1 exit /b 1

for /f "delims=" %%V in ('python -c "import scientific_metadata as s; print(s.APPLICATION_VERSION)"') do set "APP_VERSION=%%V"
set "ARTIFACT_STEM=MBUprime-StructLab-%APP_VERSION%-windows-x64-%RELEASE_MODE%"

echo.
echo Build complete ^(%RELEASE_MODE%^):
echo   dist\MBUprime StructLab\MBUprime StructLab.exe
echo   dist\%ARTIFACT_STEM%.zip
echo   dist\MBUprime StructLab.exe.sha256
echo   dist\%ARTIFACT_STEM%.zip.sha256
echo   dist\%ARTIFACT_STEM%.release-manifest.json
