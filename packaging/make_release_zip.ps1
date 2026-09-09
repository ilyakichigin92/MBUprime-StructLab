param(
    [ValidateSet("unsigned", "signed")]
    [string]$Mode = "unsigned"
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$dist = Join-Path $root "dist"
$applicationDir = Join-Path $dist "MBUprime StructLab"
$exePath = Join-Path $applicationDir "MBUprime StructLab.exe"
$internalDir = Join-Path $applicationDir "_internal"
$provenancePath = Join-Path $root "packaging\release_provenance.json"
$attestationPath = Join-Path $dist "MBUprime StructLab.signing-attestation.json"
$exeSidecarPath = Join-Path $dist "MBUprime StructLab.exe.sha256"
$internalAttestationPath = Join-Path $internalDir "MBUprime StructLab.signing-attestation.json"
$internalExeSidecarPath = Join-Path $internalDir "MBUprime StructLab.exe.sha256"
$sourceRoot = Join-Path $internalDir "rnastructure-corresponding-source"
$licenseManifestPath = Join-Path $internalDir "LICENSE-MANIFEST.json"
$sbomPath = Join-Path $internalDir "SBOM.spdx.json"

foreach ($required in @(
        $applicationDir, $exePath, $internalDir, $provenancePath, $attestationPath,
        $licenseManifestPath, $sbomPath)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Missing release input: $required"
    }
}

$provenance = Get-Content -LiteralPath $provenancePath -Raw | ConvertFrom-Json
$version = [string]$provenance.application_version
if ($version -notmatch "^\d+\.\d+\.\d+$") {
    throw "Invalid application version in release provenance: $version"
}
$artifactStem = "MBUprime-StructLab-$version-windows-x64-$Mode"
$zipPath = Join-Path $dist "$artifactStem.zip"
$zipSidecarPath = "$zipPath.sha256"
$releaseManifestPath = Join-Path $dist "$artifactStem.release-manifest.json"

# A converted onedir release must not leave obsolete onefile deliverables in dist.
$legacyOnefileArtifacts = @(
    (Join-Path $dist "MBUprime StructLab.exe"),
    (Join-Path $applicationDir "MBUprime StructLab.exe.sha256"),
    (Join-Path $dist "MBUprime-StructLab-windows.zip"),
    (Join-Path $dist "MBUprime-StructLab-windows.zip.sha256"),
    (Join-Path $dist "MBUprime-StructLab-windows.release-manifest.json")
)
foreach ($legacy in $legacyOnefileArtifacts) {
    if (Test-Path -LiteralPath $legacy -PathType Leaf) {
        Remove-Item -LiteralPath $legacy -Force
    }
}

$legacySourceRoot = Join-Path $dist "rnastructure-corresponding-source"
if (Test-Path -LiteralPath $legacySourceRoot -PathType Container) {
    $resolvedDist = (Resolve-Path -LiteralPath $dist).Path
    $resolvedLegacySource = (Resolve-Path -LiteralPath $legacySourceRoot).Path
    if (-not $resolvedLegacySource.StartsWith(
            $resolvedDist + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clear legacy corresponding source outside dist: $resolvedLegacySource"
    }
    Remove-Item -LiteralPath $resolvedLegacySource -Recurse -Force
}

# Signing or unsigned attestation must be complete before the first final hash.
$attestation = Get-Content -LiteralPath $attestationPath -Raw | ConvertFrom-Json
if ([string]$attestation.mode -ne $Mode) {
    throw "Signing attestation mode does not match requested release mode."
}
$signature = Get-AuthenticodeSignature -LiteralPath $exePath
if ($Mode -eq "unsigned") {
    if ([string]$attestation.trust_classification -ne "unsigned_development" -or
            [string]$signature.Status -ne "NotSigned") {
        throw "Unsigned package creation requires NotSigned plus unsigned_development attestation."
    }
} else {
    $chainTrusted = $null -ne $signature.SignerCertificate -and
        $signature.SignerCertificate.Verify()
    if ([string]$attestation.trust_classification -ne
            "signed_authenticode_valid_trusted_on_verification_host" -or
            [string]$signature.Status -ne "Valid" -or -not $chainTrusted) {
        throw "Signed package creation requires a valid trusted Authenticode signature and matching attestation."
    }
}

$exeHash = (Get-FileHash -LiteralPath $exePath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $exeSidecarPath -Value "$exeHash *MBUprime StructLab.exe" -NoNewline -Encoding ascii
Copy-Item -LiteralPath $exeSidecarPath -Destination $internalExeSidecarPath -Force
Copy-Item -LiteralPath $attestationPath -Destination $internalAttestationPath -Force

$internalSupportFiles = @(
    "README.md",
    "COPYING",
    "THIRD_PARTY_NOTICES.md",
    "CORRESPONDING_SOURCE.md"
)
foreach ($relative in $internalSupportFiles) {
    Copy-Item -LiteralPath (Join-Path $root $relative) `
        -Destination (Join-Path $internalDir $relative) -Force
}

foreach ($obsolete in @($zipPath, $zipSidecarPath, $releaseManifestPath)) {
    Remove-Item -LiteralPath $obsolete -Force -ErrorAction SilentlyContinue
}
if (Test-Path -LiteralPath $sourceRoot) {
    $resolvedDist = (Resolve-Path -LiteralPath $dist).Path
    $resolvedSource = (Resolve-Path -LiteralPath $sourceRoot).Path
    if (-not $resolvedSource.StartsWith($resolvedDist, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clear corresponding-source staging outside dist: $resolvedSource"
    }
    Remove-Item -LiteralPath $sourceRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $sourceRoot | Out-Null

# Explicit canonical staging fails closed when any target input is absent.
$inventoryPath = Join-Path $root "packaging\release_inventory.json"
$inventory = Get-Content -LiteralPath $inventoryPath -Raw | ConvertFrom-Json
$sourceTarget = $inventory.targets.windows_corresponding_source
$sourcePaths = @()
foreach ($groupName in $sourceTarget.groups) {
    $group = $inventory.source_groups.$groupName
    $sourcePaths += @($group.files)
    $sourcePaths += @($group.trees)
}
foreach ($artifactName in $sourceTarget.generated_artifacts) {
    $sourcePaths += [string]$inventory.generated_artifacts.$artifactName.path
}

foreach ($relative in @($sourcePaths | Sort-Object -Unique)) {
    $source = Join-Path $root $relative
    if (-not (Test-Path -LiteralPath $source)) {
        throw "Missing corresponding-source input: $relative"
    }
    $destination = Join-Path $sourceRoot $relative
    $parent = Split-Path -Parent $destination
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    if ((Get-Item -LiteralPath $source).PSIsContainer) {
        Copy-Item -LiteralPath $source -Destination $parent -Recurse -Force
    } else {
        Copy-Item -LiteralPath $source -Destination $destination -Force
    }
}

$sourceManifest = [ordered]@{}
Get-ChildItem -LiteralPath $sourceRoot -Recurse -File |
    Sort-Object FullName |
    ForEach-Object {
        $relative = $_.FullName.Substring($sourceRoot.Length + 1).Replace("\", "/")
        $sourceManifest[$relative] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
$sourceManifest | ConvertTo-Json -Depth 3 |
    Set-Content -LiteralPath (Join-Path $sourceRoot "source-manifest.json") -Encoding utf8

$applicationFiles = @(
    Get-ChildItem -LiteralPath $applicationDir -Recurse -File |
        Sort-Object FullName |
        ForEach-Object {
            [ordered]@{
                path = $_.FullName.Substring($applicationDir.Length + 1).Replace("\", "/")
                size = $_.Length
                sha256 = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
            }
        }
)

$releaseRootEntries = @(Get-ChildItem -LiteralPath $applicationDir -Force)
$releaseRootNames = @($releaseRootEntries | ForEach-Object { $_.Name } | Sort-Object)
if (Compare-Object -ReferenceObject @("MBUprime StructLab.exe", "_internal") `
        -DifferenceObject $releaseRootNames) {
    throw "Release root must contain only MBUprime StructLab.exe and _internal."
}
if (-not (Test-Path -LiteralPath $internalDir -PathType Container)) {
    throw "Release _internal payload is missing."
}

# Archive the application directory's children so the EXE is at ZIP root.
$archiveInputs = @($releaseRootEntries | ForEach-Object { $_.FullName })
Compress-Archive -LiteralPath $archiveInputs -DestinationPath $zipPath -Force

$zipHash = (Get-FileHash -LiteralPath $zipPath -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath $zipSidecarPath -Value "$zipHash *$($zipPath | Split-Path -Leaf)" -NoNewline -Encoding ascii
$provenanceHash = (Get-FileHash -LiteralPath $provenancePath -Algorithm SHA256).Hash.ToLowerInvariant()
$attestationHash = (Get-FileHash -LiteralPath $attestationPath -Algorithm SHA256).Hash.ToLowerInvariant()
$sourceManifestPath = Join-Path $sourceRoot "source-manifest.json"
$sourceManifestHash = (Get-FileHash -LiteralPath $sourceManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
$licenseManifest = Get-Content -LiteralPath $licenseManifestPath -Raw | ConvertFrom-Json
if ([int]$licenseManifest.schema_version -ne 1 -or
        @($licenseManifest.components).Count -eq 0 -or
        @($licenseManifest.license_files).Count -eq 0) {
    throw "Release license evidence is empty or invalid."
}

$releaseManifest = [ordered]@{
    release_manifest_schema = "2"
    build_utc = $provenance.build_utc
    release_mode = $Mode
    trust_classification = [string]$attestation.trust_classification
    application_identity = [ordered]@{
        application_version = $version
        schema_version = [string]$provenance.schema_version
        scientific_policy_version = [string]$provenance.scientific_policy_version
    }
    application_folder = [ordered]@{
        name = "MBUprime StructLab"
        files = $applicationFiles
    }
    executable = [ordered]@{ name = "MBUprime StructLab.exe"; sha256 = $exeHash; pe_machine = "AMD64" }
    executable_sidecar = [ordered]@{
        name = "MBUprime StructLab.exe.sha256"
        internal_path = "_internal/MBUprime StructLab.exe.sha256"
        sha256 = (Get-FileHash -LiteralPath $exeSidecarPath -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    archive = [ordered]@{ name = (Split-Path -Leaf $zipPath); sha256 = $zipHash }
    provenance = [ordered]@{
        name = "release_provenance.json"
        internal_path = "_internal/release_provenance.json"
        sha256 = $provenanceHash
    }
    signing_attestation = [ordered]@{
        name = (Split-Path -Leaf $attestationPath)
        internal_path = "_internal/MBUprime StructLab.signing-attestation.json"
        sha256 = $attestationHash
    }
    corresponding_source = [ordered]@{
        directory = "_internal/rnastructure-corresponding-source"
        manifest = "source-manifest.json"
        manifest_sha256 = $sourceManifestHash
    }
    release_evidence = [ordered]@{
        license_manifest = "_internal/LICENSE-MANIFEST.json"
        license_manifest_sha256 = (Get-FileHash -LiteralPath $licenseManifestPath -Algorithm SHA256).Hash.ToLowerInvariant()
        sbom = "_internal/SBOM.spdx.json"
        sbom_sha256 = (Get-FileHash -LiteralPath $sbomPath -Algorithm SHA256).Hash.ToLowerInvariant()
        license_directory = "_internal/licenses"
        component_count = @($licenseManifest.components).Count
        license_file_count = @($licenseManifest.license_files).Count
    }
    note = "The ZIP self-hash is intentionally external because an archive cannot contain a stable hash of itself. All recorded artifact hashes were computed after signing or unsigned attestation."
}
$releaseManifest | ConvertTo-Json -Depth 8 |
    Set-Content -LiteralPath $releaseManifestPath -Encoding utf8

Write-Host "Created $zipPath, post-sign SHA-256 sidecars, and $releaseManifestPath"
