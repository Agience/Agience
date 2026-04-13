<#
.SYNOPSIS
    Tag a stable release from the current release/X.Y branch.

.DESCRIPTION
    Validates build_info.json matches the version, creates tag vX.Y.Z locally,
    merges release/X.Y -> main, then prints push instructions.

    Triggering pipelines (after you push):
      - build-and-push-ghcr.yml  ->  :latest  :X.Y  :X.Y.Z  (Docker Hub + GHCR)
      - release.yml              ->  GitHub Release with release notes
      - deploy-suite.yml         ->  my.agience.ai deploy (on release published)

.PARAMETER Version
    Full patch version to tag (X.Y.Z - e.g. 0.2.2).

.EXAMPLE
    .\.scripts\tag-stable.ps1 -Version 0.2.2
#>
param(
    [Parameter(Mandatory)]
    [Alias("v")]
    [string]$Version
)

$ErrorActionPreference = "Stop"

# Validate format
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Write-Host "ERROR: Version must be X.Y.Z (for example: 0.2.2)" -ForegroundColor Red
    exit 1
}

$MinorVersion  = $Version -replace '\.\d+$', ''
$ReleaseBranch = "release/$MinorVersion"
$Tag           = "v$Version"

# Must be on the matching release branch
$CurrentBranch = (git rev-parse --abbrev-ref HEAD 2>&1).Trim()
if ($CurrentBranch -ne $ReleaseBranch) {
    Write-Host "ERROR: Must be on '$ReleaseBranch' (currently on '$CurrentBranch')" -ForegroundColor Red
    Write-Host "  Run: git checkout $ReleaseBranch" -ForegroundColor Yellow
    exit 1
}

# Validate build_info.json
if (-not (Test-Path "build_info.json")) {
    Write-Host "ERROR: build_info.json not found in working directory" -ForegroundColor Red
    exit 1
}
$BuildInfo = Get-Content build_info.json -Raw | ConvertFrom-Json
if ($BuildInfo.version -ne $Version) {
    Write-Host "ERROR: build_info.json version '$($BuildInfo.version)' does not match '$Version'" -ForegroundColor Red
    Write-Host "  Update build_info.json version to '$Version' first, then commit it to $ReleaseBranch." -ForegroundColor Yellow
    exit 1
}

# Check tag doesn't already exist
$TagExists = "$(git tag -l $Tag)".Trim()
if ($TagExists) {
    Write-Host "ERROR: Tag '$Tag' already exists locally. Run: git tag -d $Tag to remove it." -ForegroundColor Red
    exit 1
}
$RemoteTagExists = "$(git ls-remote --tags origin $Tag 2>&1)".Trim()
if ($RemoteTagExists) {
    Write-Host "ERROR: Tag '$Tag' already exists on remote." -ForegroundColor Red
    exit 1
}

git tag $Tag -m "Agience $Tag"

Write-Host ""
Write-Host "  Tagged:  $Tag" -ForegroundColor Green
Write-Host "  On:      $ReleaseBranch @ $(git rev-parse --short HEAD)" -ForegroundColor Gray

# Push release branch
Write-Host ""
Write-Host "  Pushing $ReleaseBranch..." -ForegroundColor Cyan
git push origin $ReleaseBranch
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to push '$ReleaseBranch'." -ForegroundColor Red
    exit 1
}

# Forward-port release branch into main
Write-Host "  Merging $ReleaseBranch -> main..." -ForegroundColor Cyan
git checkout main
git pull origin main
git merge --no-ff $ReleaseBranch -m "Forward-port $ReleaseBranch into main (post $Tag)"

Write-Host "  Merged." -ForegroundColor Green

# Push main and tag
Write-Host ""
Write-Host "  Pushing main..." -ForegroundColor Cyan
git push origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to push main." -ForegroundColor Red
    exit 1
}

Write-Host "  Pushing $Tag..." -ForegroundColor Cyan
git push origin $Tag
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to push tag '$Tag'." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  Done. Triggered:" -ForegroundColor Green
Write-Host "    - build-and-push-ghcr.yml  ->  :latest  :$MinorVersion  :$Version  (Docker Hub + GHCR)" -ForegroundColor Gray
Write-Host "    - release.yml              ->  GitHub Release with release notes" -ForegroundColor Gray
Write-Host "    - deploy-suite.yml         ->  my.agience.ai deploy (on workflow_run)" -ForegroundColor Gray
Write-Host ""
