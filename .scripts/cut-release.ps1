<#
.SYNOPSIS
    Create a release/X.Y branch from local main.

.DESCRIPTION
    Validates you are on main, creates release/X.Y locally, stamps
    build_info.json with X.Y.0, commits, and pushes the branch.
    CI will build :X.Y-rc images on push.

.PARAMETER Version
    Minor version line to cut (X.Y - e.g. 0.3).

.EXAMPLE
    .\.scripts\cut-release.ps1 -Version 0.3
#>
param(
    [Parameter(Mandatory)]
    [Alias("v")]
    [string]$Version
)

$ErrorActionPreference = "Stop"

# Validate format
if ($Version -notmatch '^\d+\.\d+$') {
    Write-Host "ERROR: Version must be X.Y (for example: 0.3)" -ForegroundColor Red
    exit 1
}

# Must be on main
$CurrentBranch = (git rev-parse --abbrev-ref HEAD 2>&1).Trim()
if ($CurrentBranch -ne "main") {
    Write-Host "ERROR: Must be on main branch (currently on '$CurrentBranch')" -ForegroundColor Red
    exit 1
}

# Check local is up to date
$Behind = (git rev-list HEAD..origin/main --count 2>&1).Trim()
if ($Behind -ne "0") {
    Write-Host "WARNING: Your main is $Behind commit(s) behind origin/main. Consider pulling first." -ForegroundColor Yellow
}

$ReleaseBranch = "release/$Version"

# Check branch doesn't already exist locally or on remote
$LocalExists  = "$(git branch --list $ReleaseBranch)".Trim()
$RemoteExists = "$(git ls-remote --heads origin $ReleaseBranch 2>&1)".Trim()

if ($LocalExists) {
    Write-Host "ERROR: Branch '$ReleaseBranch' already exists locally." -ForegroundColor Red
    exit 1
}
if ($RemoteExists) {
    Write-Host "ERROR: Branch '$ReleaseBranch' already exists on remote." -ForegroundColor Red
    exit 1
}

$InitialVersion = "$Version.0"
$FromSha = (git rev-parse --short HEAD 2>&1).Trim()
git checkout -b $ReleaseBranch
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to create branch '$ReleaseBranch'." -ForegroundColor Red
    exit 1
}

# Stamp build_info.json
$Utf8NoBom = New-Object System.Text.UTF8Encoding $false
$buildInfoContent = "{`n    `"version`": `"$InitialVersion`"`n}`n"
[System.IO.File]::WriteAllText("build_info.json", $buildInfoContent, $Utf8NoBom)
git add build_info.json
git commit -m "Stamp build_info.json for $InitialVersion"
git push -u origin $ReleaseBranch
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to push '$ReleaseBranch' to origin." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  Created: $ReleaseBranch" -ForegroundColor Green
Write-Host "  From:    main @ $FromSha" -ForegroundColor Gray
Write-Host "  Stamped: build_info.json -> $InitialVersion" -ForegroundColor Green
Write-Host "  Pushed:  origin/$ReleaseBranch" -ForegroundColor Green
Write-Host ""
Write-Host "  CI is now building :$Version-rc images." -ForegroundColor Cyan
Write-Host ""
Write-Host "  When ready to tag stable:" -ForegroundColor Cyan
Write-Host "    .\.scripts\tag-stable.ps1 -Version $InitialVersion" -ForegroundColor White
Write-Host ""
