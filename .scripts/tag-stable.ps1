<#
.SYNOPSIS
    Tag a stable release from the current release/X.Y branch.

.DESCRIPTION
    Validates build_info.json matches the version, creates tag vX.Y.Z locally,
    merges release/X.Y -> main, then prints push instructions.

    Triggering pipelines (after you push):
      - build-and-push-ghcr.yml  ->  :stable  :X.Y  :X.Y.Z  (Docker Hub + GHCR)
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
$Utf8NoBom     = New-Object System.Text.UTF8Encoding($false)

# Auto-checkout the release branch if not already on it
$CurrentBranch = (git rev-parse --abbrev-ref HEAD 2>&1).Trim()
if ($CurrentBranch -ne $ReleaseBranch) {
    Write-Host "  Switching to $ReleaseBranch..." -ForegroundColor Cyan
    git checkout --quiet $ReleaseBranch
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Could not checkout '$ReleaseBranch'. Does it exist?" -ForegroundColor Red
        exit 1
    }
}

# Auto-bump build_info.json if version doesn't match, then commit
if (-not (Test-Path "build_info.json")) {
    Write-Host "ERROR: build_info.json not found in working directory" -ForegroundColor Red
    exit 1
}
$BuildInfo = Get-Content build_info.json -Raw | ConvertFrom-Json
if ($BuildInfo.version -ne $Version) {
    Write-Host "  Bumping build_info.json to $Version..." -ForegroundColor Cyan
    $newBuildInfo = @{ version = $Version } | ConvertTo-Json
    $newBuildInfo = $newBuildInfo -replace "`r`n", "`n"
    [System.IO.File]::WriteAllText("build_info.json", $newBuildInfo, $Utf8NoBom)
    git add build_info.json > $null 2>$null
    git commit -m "Bump version to $Version" --quiet > $null 2>$null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to commit version bump." -ForegroundColor Red
        exit 1
    }
    Write-Host "  Committed version bump." -ForegroundColor Gray
}

# Check tag doesn't already exist
$TagExists = "$(git tag -l $Tag)".Trim()
if ($TagExists) {
    Write-Host "ERROR: Tag '$Tag' already exists locally. Run: git tag -d $Tag to remove it." -ForegroundColor Red
    exit 1
}
$RemoteTagExists = "$(git ls-remote --tags origin $Tag 2>&1)".Trim()
if ($RemoteTagExists) {
    Write-Host "ERROR: Tag '$Tag' already exists on origin (private)." -ForegroundColor Red
    exit 1
}
$PublicTagExists = "$(git ls-remote --tags public $Tag 2>&1)".Trim()
if ($PublicTagExists) {
    Write-Host "ERROR: Tag '$Tag' already exists on public." -ForegroundColor Red
    exit 1
}

git tag $Tag -m "Agience $Tag"

Write-Host ""
Write-Host "  Tagged:  $Tag" -ForegroundColor Green
Write-Host "  On:      $ReleaseBranch @ $(git rev-parse --short HEAD)" -ForegroundColor Gray

# Push release branch to origin (private)
Write-Host ""
Write-Host "  Pushing $ReleaseBranch to origin..." -ForegroundColor Cyan
git push origin $ReleaseBranch
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to push '$ReleaseBranch' to origin." -ForegroundColor Red
    exit 1
}

# Forward-port release branch into main
Write-Host "  Merging $ReleaseBranch -> main..." -ForegroundColor Cyan
git checkout --quiet main
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to checkout main for forward-port." -ForegroundColor Red
    exit 1
}
git pull origin main
git merge --no-ff $ReleaseBranch -m "Forward-port $ReleaseBranch into main (post $Tag)"

Write-Host "  Merged." -ForegroundColor Green

# Push main to origin (private)
Write-Host ""
Write-Host "  Pushing main to origin..." -ForegroundColor Cyan
git push origin main
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Failed to push main to origin." -ForegroundColor Red
    exit 1
}

# Publish to public repo (strips private content, stacks commit on release/X.Y, pushes tag -> triggers CI)
Write-Host ""
Write-Host "  Publishing to public repo..." -ForegroundColor Cyan
$PublishScript = Join-Path $PSScriptRoot "publish_public.ps1"
& $PublishScript -Version $Version -ReleaseBranch
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: publish_public.ps1 failed." -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "  Done. Triggered:" -ForegroundColor Green
Write-Host "    - build-and-push-ghcr.yml  ->  :stable  :$MinorVersion  :$Version  (Docker Hub + GHCR)" -ForegroundColor Gray
Write-Host "    - release.yml              ->  GitHub Release with release notes" -ForegroundColor Gray
Write-Host "    - deploy-suite.yml         ->  my.agience.ai deploy (on workflow_run)" -ForegroundColor Gray
Write-Host ""
