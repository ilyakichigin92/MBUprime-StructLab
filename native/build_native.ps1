$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location -LiteralPath $Root

if ([IntPtr]::Size -ne 8) {
    throw "RNAstructure native extension requires a 64-bit Python process."
}
$Version = python -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($Version -ne "3.12") {
    throw "RNAstructure native extension requires CPython 3.12; found $Version."
}

python native\setup_native.py build_ext --inplace --force
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

python native\generate_native_manifest.py --compiler msvc
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "Built and recorded the in-process RNAstructure 6.6 extension."
