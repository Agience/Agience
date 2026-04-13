<#
.SYNOPSIS
    Create or finish a hotfix branch.

.DESCRIPTION
    Without -Finish:
      Creates hotfix/X.Y.Z from release/X.Y locally, stamps build_info.json,
      commits, and pushes. Make your fix commits on top, then run with -Finish.

    With -Finish:
      Merges hotfix/X.Y.Z back into release/X.Y locally.
      Prints instructions to push and tag.
      Optionally triggers the hotfix-merge GitHub workflow to also
      forward-port release/X.Y into main.

.PARAMETER Version
    Patch version being hotfixed (X.Y.Z - e.g. 0.2.1).

.PARAMETER Finish
    Merge the hotfix back into its release branch.

.PARAMETER ForwardPort
    When used with -Finish: also trigger forward-port of release -> main
    via the hotfix-merge GitHub Actions workflow (requires gh CLI).

.EXAMPLE
    # Create hotfix branch
    .\.scripts\hotfix.ps1 -Version 0.2.1

    # After fixing and testing:
    .\.scripts\hotfix.ps1 -Version 0.2.1 -Finish

    # Finish + forward-port to main via GitHub Actions:
    .\.scripts\hotfix.ps1 -Version 0.2.1 -Finish -ForwardPort
#>
param(
    [Parameter(Mandatory)]
    [Alias("v")]
    [string]$Version,
    [switch]$Finish,
    [switch]$ForwardPort
)

$ErrorActionPreference = "Stop"

# Validate format
if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    Write-Host "ERROR: Version must be X.Y.Z (for example: 0.2.1)" -ForegroundColor Red
    exit 1
}

$MinorVersion  = $Version -replace '\.\d+$', ''
$ReleaseBranch = "release/$MinorVersion"
$HotfixBranch  = "hotfix/$Version"

if (-not $Finish) {
    # -- Create hotfix branch --

    # Verify release branch exists on remote
    $RemoteRelease = "$(git ls-remote --heads origin $ReleaseBranch 2>&1)".Trim()
    if (-not $RemoteRelease) {
        Write-Host "ERROR: '$ReleaseBranch' does not exist on origin." -ForegroundColor Red
        Write-Host "  Available release branches:" -ForegroundColor Yellow
        git branch -r --list "origin/release/*" | ForEach-Object { Write-Host "  $_" -ForegroundColor Gray }
        exit 1
    }

    # Check hotfix branch doesn't already exist
    $LocalExists  = "$(git branch --list $HotfixBranch)".Trim()
    $RemoteExists = "$(git ls-remote --heads origin $HotfixBranch 2>&1)".Trim()
    if ($LocalExists -or $RemoteExists) {
        Write-Host "ERROR: '$HotfixBranch' already exists." -ForegroundColor Red
        exit 1
    }

    # Fetch and create from release branch
    git fetch origin $ReleaseBranch
    $FromSha = (git ls-remote origin --refs $ReleaseBranch 2>&1 | ForEach-Object { $_.Split()[0].Substring(0,7) })
    git checkout -b $HotfixBranch "origin/$ReleaseBranch"

    # Stamp build_info.json
    $Utf8NoBom = New-Object System.Text.UTF8Encoding $false
    $buildInfoContent = "{`n    `"version`": `"$Version`"`n}`n"
    [System.IO.File]::WriteAllText("build_info.json", $buildInfoContent, $Utf8NoBom)
    git add build_info.json
    git commit -m "Stamp build_info.json for $Version"
    git push -u origin $HotfixBranch
    if ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to push '$HotfixBranch' to origin." -ForegroundColor Red
        exit 1
    }

    Write-Host ""
    Write-Host "  Created: $HotfixBranch" -ForegroundColor Green
    Write-Host "  From:    $ReleaseBranch @ $FromSha" -ForegroundColor Gray
    Write-Host "  Stamped: build_info.json -> $Version" -ForegroundColor Green
    Write-Host "  Pushed:  origin/$HotfixBranch" -ForegroundColor Green
    Write-Host ""
    Write-Host "  Make your fix, commit, push, then:" -ForegroundColor Cyan
    Write-Host "    .\.scripts\hotfix.ps1 -Version $Version -Finish" -ForegroundColor White
    Write-Host ""

} else {
    # -- Finish hotfix (local merge back) --

    $CurrentBranch = (git rev-parse --abbrev-ref HEAD 2>&1).Trim()
    if ($CurrentBranch -ne $HotfixBranch) {
        Write-Host "ERROR: Must be on '$HotfixBranch' (currently on '$CurrentBranch')" -ForegroundColor Red
        exit 1
    }

    # Ensure hotfix is up to date
    git fetch origin $HotfixBranch
    $Behind = (git rev-list "HEAD..origin/$HotfixBranch" --count 2>&1).Trim()
    if ($Behind -ne "0") {
        Write-Host "WARNING: Local hotfix is $Behind commit(s) behind origin. Pull first." -ForegroundColor Yellow
    }

    # Merge into release
    git checkout $ReleaseBranch
    git pull origin $ReleaseBranch
    git merge --no-ff $HotfixBranch -m "Merge $HotfixBranch into $ReleaseBranch"

    Write-Host ""
    Write-Host "  Merged: $HotfixBranch -> $ReleaseBranch" -ForegroundColor Green
    Write-Host ""

    if ($ForwardPort) {
        # Trigger GitHub Actions workflow for forward-port
        $GhAvailable = (Get-Command gh -ErrorAction SilentlyContinue)
        if (-not $GhAvailable) {
            Write-Host "WARNING: gh CLI not found. Cannot trigger forward-port workflow." -ForegroundColor Yellow
            Write-Host "  Run manually in GitHub Actions: hotfix-merge.yml" -ForegroundColor Yellow
        } else {
            Write-Host "  Triggering hotfix-merge workflow to forward-port $ReleaseBranch -> main..." -ForegroundColor Cyan
            gh workflow run hotfix-merge.yml `
                -f hotfix_branch=$HotfixBranch `
                -f release_branch=$ReleaseBranch `
                -f forward_port=true
            Write-Host "  Workflow triggered. Check GitHub Actions for status." -ForegroundColor Green
        }
    }

    Write-Host "  Next steps:" -ForegroundColor Cyan
    Write-Host "    git push origin $ReleaseBranch" -ForegroundColor White
    Write-Host "    .\.scripts\tag-stable.ps1 -Version $Version" -ForegroundColor White
    Write-Host ""
}
