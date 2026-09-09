@echo off
REM Double-click launcher for MBUprime StructLab.
REM Runs the application from source with the exact scientific dependency lock.
cd /d "%~dp0"
python -c "import platform,sys; raise SystemExit(0 if sys.implementation.name == 'cpython' and sys.version_info[:2] == (3,12) and platform.architecture()[0] == '64bit' else 1)"
if errorlevel 1 (
    echo MBUprime StructLab source mode requires 64-bit CPython 3.12 because RNAstructure is compiled in-process.
    goto :dependency_failure
)
call python packaging\verify_locked_environment.py requirements.txt 1>nul 2>nul
if errorlevel 1 (
    call python -m pip install --require-hashes -r requirements.txt
    if errorlevel 1 (
        echo Failed to install the exact locked dependencies.
        goto :dependency_failure
    )
    call python packaging\verify_locked_environment.py requirements.txt
    if errorlevel 1 (
        echo The installed dependencies still do not match the exact scientific lock.
        goto :dependency_failure
    )
)
goto :launch

:dependency_failure
REM cmd /c can discard a top-level EXIT /B status for a .bat file. Preserve
REM the subroutine convention, then terminate the launcher process explicitly.
call :return_failure
exit 1

:return_failure
exit /b 1

:launch
call python windows_bootstrap.py
if errorlevel 1 pause
