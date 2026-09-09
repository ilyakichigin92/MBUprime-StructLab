param(
    [ValidateSet("unsigned", "signed")]
    [string]$Mode = "unsigned",
    [string]$ExecutablePath = "",
    [string]$AttestationPath = "",
    [string]$SignToolPath = $env:MBUPRIME_SIGNTOOL_PATH,
    [string]$SignerThumbprint = $env:MBUPRIME_SIGNING_THUMBPRINT,
    [string]$SignerSubject = $env:MBUPRIME_SIGNING_SUBJECT,
    [string]$TimestampUrl = $env:MBUPRIME_TIMESTAMP_URL
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
if (-not $ExecutablePath) {
    $ExecutablePath = Join-Path $root "dist\MBUprime StructLab\MBUprime StructLab.exe"
}
if (-not $AttestationPath) {
    $AttestationPath = Join-Path $root "dist\MBUprime StructLab.signing-attestation.json"
}
if (-not (Test-Path -LiteralPath $ExecutablePath -PathType Leaf)) {
    throw "Missing executable to attest: $ExecutablePath"
}

function Get-SignatureIdentity([string]$Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    $certificate = $signature.SignerCertificate
    $chainTrusted = $false
    if ($null -ne $certificate) {
        $chainTrusted = $certificate.Verify()
    }
    $status = [string]$signature.Status
    $statusReason = switch ($status) {
        "NotSigned" { "no_authenticode_signature" }
        "Valid" { "valid_authenticode_signature" }
        "HashMismatch" { "authenticode_hash_mismatch" }
        "NotTrusted" { "authenticode_not_trusted" }
        "NotSupportedFileFormat" { "authenticode_format_not_supported" }
        "Incompatible" { "authenticode_incompatible" }
        default { "authenticode_status_" + ($status -replace "[^A-Za-z0-9]+", "_").ToLowerInvariant() }
    }
    return [ordered]@{
        status = $status
        status_reason = $statusReason
        chain_trusted_on_verification_host = [bool]$chainTrusted
        signer_thumbprint = if ($null -ne $certificate) { [string]$certificate.Thumbprint } else { "" }
        signer_subject = if ($null -ne $certificate) { [string]$certificate.Subject } else { "" }
    }
}

if ($Mode -eq "signed") {
    if (-not $SignToolPath -or -not (Test-Path -LiteralPath $SignToolPath -PathType Leaf)) {
        throw "Signed mode requires MBUPRIME_SIGNTOOL_PATH to name an existing SignTool executable."
    }
    if (($SignerThumbprint -and $SignerSubject) -or (-not $SignerThumbprint -and -not $SignerSubject)) {
        throw "Signed mode requires exactly one explicit identity: MBUPRIME_SIGNING_THUMBPRINT or MBUPRIME_SIGNING_SUBJECT."
    }

    $signArguments = @("sign", "/fd", "SHA256")
    if ($SignerThumbprint) {
        $normalizedThumbprint = $SignerThumbprint.Replace(" ", "").ToUpperInvariant()
        if ($normalizedThumbprint -notmatch "^[0-9A-F]{40}$") {
            throw "MBUPRIME_SIGNING_THUMBPRINT must contain exactly 40 hexadecimal characters."
        }
        $signArguments += @("/sha1", $normalizedThumbprint)
    } else {
        $signArguments += @("/n", $SignerSubject)
    }
    if ($TimestampUrl) {
        $timestamp = $null
        if (-not [Uri]::TryCreate($TimestampUrl, [UriKind]::Absolute, [ref]$timestamp) -or
                $timestamp.Scheme -ne "https") {
            throw "MBUPRIME_TIMESTAMP_URL must be an absolute HTTPS RFC3161 endpoint."
        }
        $signArguments += @("/tr", $TimestampUrl, "/td", "SHA256")
    }
    $signArguments += $ExecutablePath
    & $SignToolPath @signArguments
    if ($LASTEXITCODE -ne 0) {
        throw "SignTool failed with exit code $LASTEXITCODE."
    }
}

$identity = Get-SignatureIdentity $ExecutablePath
if ($Mode -eq "unsigned") {
    if ($identity.status -ne "NotSigned" -or $identity.signer_thumbprint) {
        throw "Unsigned development mode requires an executable whose Authenticode status is NotSigned."
    }
    $trustClassification = "unsigned_development"
} else {
    if ($identity.status -ne "Valid" -or -not $identity.chain_trusted_on_verification_host) {
        throw "Signed mode requires Authenticode Valid and a trusted certificate chain on this verification host."
    }
    if ($SignerThumbprint -and
            $identity.signer_thumbprint -ne $SignerThumbprint.Replace(" ", "").ToUpperInvariant()) {
        throw "The verified signer thumbprint does not match MBUPRIME_SIGNING_THUMBPRINT."
    }
    if ($SignerSubject -and
            -not $identity.signer_subject.Equals($SignerSubject, [StringComparison]::OrdinalIgnoreCase)) {
        throw "The verified signer subject does not exactly match MBUPRIME_SIGNING_SUBJECT."
    }
    $trustClassification = "signed_authenticode_valid_trusted_on_verification_host"
}

$attestation = [ordered]@{
    schema_version = "1"
    mode = $Mode
    trust_classification = $trustClassification
    verified_utc = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
    authenticode = $identity
    timestamp = [ordered]@{
        configured = [bool]$TimestampUrl
        rfc3161_url = if ($TimestampUrl) { $TimestampUrl } else { "" }
        digest_algorithm = if ($TimestampUrl) { "SHA256" } else { "" }
    }
    statement = if ($Mode -eq "unsigned") {
        "Unsigned development artifact; no publisher trust is claimed."
    } else {
        "Authenticode was valid with a trusted chain on the verification host; reputation and policy allowlisting are not claimed."
    }
}
$attestationParent = Split-Path -Parent $AttestationPath
New-Item -ItemType Directory -Force -Path $attestationParent | Out-Null
$attestation | ConvertTo-Json -Depth 5 |
    Set-Content -LiteralPath $AttestationPath -Encoding utf8
Write-Host "Release signing attestation: $trustClassification"
