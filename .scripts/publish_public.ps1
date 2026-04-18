<#
.SYNOPSIS
    Canonical script to publish the current state to the public agience-core repo.

.DESCRIPTION
    Strips private content (.dev/, .claude/, .github/ agent configs), stamps build_info.json,
    and pushes a single release commit to the public remote.

    Default: stacks a new commit on top of public/main (preserving release history).
    With -CreateOrphan: wipes all history and force-pushes a fresh root commit (first publish or repo reset).

    Your private remote is never touched. After a successful publish from local main,
    build_info.json is also stamped in your local main working tree (not committed).

.PARAMETER Version
    Optional for main/edge publishes. Required when using -ReleaseBranch. Stamps build_info.json and creates git tag v{Version}.

.PARAMETER Message
    Commit/tag message. Defaults to "Agience v{Version}".

.PARAMETER CreateOrphan
    Wipe public repo history and force-push a single root commit. Use for first publish or repo reset.

.PARAMETER DryRun
    Show what would happen without pushing.

.EXAMPLE
    .\.scripts\publish_public.ps1 -v 0.2.0 -CreateOrphan   # First publish to main
    .\.scripts\publish_public.ps1 -v 0.3.0                  # Normal stacked publish
    .\.scripts\publish_public.ps1 -v 0.3.1 -m "Hotfix: auth token refresh"
    .\.scripts\publish_public.ps1 -v 0.3.0 -DryRun
#>

param(
    [Alias("v")]
    [string]$Version,
    [Alias("m")]
    [string]$Message,
    [Alias("b")]
    [string]$PublicBranch = "main",
    [switch]$ReleaseBranch,
    [switch]$CreateOrphan,
    [switch]$DryRun
)

# Explicitly override any inherited ErrorActionPreference — git writes informational
# messages to stderr which PowerShell treats as terminating errors under "Stop".
# Real errors are detected via $LASTEXITCODE checks throughout this script.
$ErrorActionPreference = "Continue"

$PublicRemote = "public"
$StagingBranch = "public-release-staging"
$OriginalBranch = (git rev-parse --abbrev-ref HEAD).Trim()
$Published = $false
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

# --- Validate required params ---
if ($ReleaseBranch -and -not $Version) {
    Write-Host "ERROR: -Version is required when using -ReleaseBranch." -ForegroundColor Red
    exit 1
}

# --- Validate version format ---
if ($Version -and $Version -notmatch '^\d+\.\d+\.\d+(-[\w.]+)?$') {
    Write-Host "ERROR: Version must be semver (e.g., 0.2.0 or 0.2.0-rc.1)." -ForegroundColor Red
    exit 1
}

# --- Derive release branch from version when -ReleaseBranch is set ---
if ($ReleaseBranch) {
    $null = $Version -match '^(\d+)\.(\d+)\.'
    $PublicBranch = "release/$($Matches[1]).$($Matches[2])"
    Write-Host "  Release branch derived from version: $PublicBranch" -ForegroundColor Gray
}

# --- Validate state ---
$status = git status --porcelain 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: Not a git repository or git not available." -ForegroundColor Red
    exit 1
}
if ($status) {
    Write-Host "ERROR: Working tree is dirty. Commit or stash changes first." -ForegroundColor Red
    exit 1
}

# Verify public remote exists
$remotes = git remote 2>&1
if ($remotes -notcontains $PublicRemote) {
    Write-Host "ERROR: Remote '$PublicRemote' not found. Expected remote pointing to agience-core." -ForegroundColor Red
    exit 1
}

# --- Build message ---
if (-not $Message) {
    $Message = if ($Version) { "Agience v$Version" } else { "publish" }
}

$TagName = if ($Version) { "v$Version" } else { $null }
$Mode = if ($CreateOrphan) { "ORPHAN (force-push)" } else { "STACKED (normal push)" }

Write-Host ""
Write-Host "=== Publish to Public ===" -ForegroundColor Cyan
Write-Host "  Source branch : $OriginalBranch"
Write-Host "  Remote        : $PublicRemote ($PublicBranch)"
Write-Host "  Commit message: $Message"
if ($TagName) { Write-Host "  Tag           : $TagName" }
Write-Host "  Mode          : $Mode"
if ($DryRun)  { Write-Host "  Dry run       : YES" -ForegroundColor Yellow }
Write-Host ""

# --- Paths to exclude from public ---
$ExcludePaths = @(
    ".dev"
    ".claude"
    ".vscode"
    ".github/agents"
    ".github/instructions"
    ".github/prompts"
    ".github/copilot-instructions.md"
    "CLAUDE.md"
    "backend/CLAUDE.md"
    "frontend/CLAUDE.md"
    "servers/CLAUDE.md"
)

try {
    if ($CreateOrphan) {
        # --- Orphan mode: single root commit, no parent ---
        Write-Host "Creating orphan branch '$StagingBranch'..." -ForegroundColor Gray
        git checkout --orphan $StagingBranch 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: Failed to create orphan branch." -ForegroundColor Red
            exit 1
        }
    } else {
        # --- Stacked mode: new commit on top of target branch ---
        Write-Host "Fetching $PublicRemote/$PublicBranch..." -ForegroundColor Gray
        git fetch $PublicRemote $PublicBranch 2>&1 | Out-Null
        $FetchExitCode = $LASTEXITCODE

        if ($FetchExitCode -ne 0) {
            if ($PublicBranch -ne "main") {
                # Release branch does not exist yet — base it on public/main
                Write-Host "  Branch '$PublicBranch' not found on remote. Creating from $PublicRemote/main..." -ForegroundColor DarkYellow
                git fetch $PublicRemote main 2>&1 | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "ERROR: Failed to fetch $PublicRemote/main." -ForegroundColor Red
                    exit 1
                }
                git checkout -b $StagingBranch "$PublicRemote/main" 2>&1 | Out-Null
                if ($LASTEXITCODE -ne 0) {
                    Write-Host "ERROR: Failed to create staging branch from $PublicRemote/main." -ForegroundColor Red
                    exit 1
                }
            } else {
                Write-Host "ERROR: Failed to fetch $PublicRemote/$PublicBranch." -ForegroundColor Red
                exit 1
            }
        } else {
            # Create staging branch from the public remote's current HEAD
            git checkout -b $StagingBranch "$PublicRemote/$PublicBranch" 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Host "ERROR: Failed to create staging branch from $PublicRemote/$PublicBranch." -ForegroundColor Red
                exit 1
            }
        }

        # Replace entire working tree with current source branch content
        Write-Host "Replacing tree with $OriginalBranch content..." -ForegroundColor Gray
        git rm -rf --quiet . 2>&1 | Out-Null
        git checkout $OriginalBranch -- . 2>&1 | Out-Null
    }

    # --- Remove excluded paths ---
    foreach ($path in $ExcludePaths) {
        if (Test-Path $path) {
            Write-Host "  Removing: $path" -ForegroundColor DarkYellow
            git rm -rf --quiet $path 2>&1 | Out-Null
            # Only delete untracked/gitignored filesystem content if it's NOT .claude
            # .claude/worktrees/ contains git worktree checkouts with potentially
            # uncommitted work. Deleting them is destructive and irreversible.
            # The git add -A + git rm --cached safety net below prevents any
            # excluded path from leaking into the public commit regardless.
            if ((Test-Path $path) -and ($path -ne ".claude")) {
                Remove-Item -Recurse -Force $path -ErrorAction SilentlyContinue
            }
        }
    }

    # --- Stamp build_info.json with version (release publishes only) ---
    if ($Version) {
        Write-Host "  Stamping build_info.json with version $Version" -ForegroundColor Gray
        $buildInfo = @{ version = $Version } | ConvertTo-Json
        [System.IO.File]::WriteAllText("build_info.json", $buildInfo, $Utf8NoBom)
        git add build_info.json 2>&1 | Out-Null
    }

    # --- Stage everything ---
    git add -A 2>&1 | Out-Null

    # --- Ensure excluded paths are not in the index ---
    # (locked files may survive Remove-Item and get re-added by git add -A)
    foreach ($path in $ExcludePaths) {
        git rm -rf --cached --quiet --ignore-unmatch $path 2>&1 | Out-Null
    }

    # --- Commit (skip cleanly when there is nothing new to commit) ---
    $hasStagedChanges = $false
    git diff --cached --quiet 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 1) {
        $hasStagedChanges = $true
    } elseif ($LASTEXITCODE -ne 0) {
        Write-Host "ERROR: Failed to inspect staged changes before commit." -ForegroundColor Red
        exit 1
    }

    if ($hasStagedChanges) {
        Write-Host "Committing..." -ForegroundColor Gray
        git commit -m $Message --quiet 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: Commit failed." -ForegroundColor Red
            exit 1
        }
        $commitHash = (git rev-parse --short HEAD 2>&1).Trim()
        Write-Host "  Created commit: $commitHash" -ForegroundColor Green
    } else {
        $commitHash = (git rev-parse --short HEAD 2>&1).Trim()
        Write-Host "  No content changes to commit; publishing existing $PublicRemote/$PublicBranch tip ($commitHash)." -ForegroundColor DarkYellow
    }

    # --- Push ---
    if ($DryRun) {
        Write-Host ""
        if ($CreateOrphan) {
            Write-Host "DRY RUN: Would force-push $commitHash to ${PublicRemote}/${PublicBranch}" -ForegroundColor Yellow
        } else {
            Write-Host "DRY RUN: Would push $commitHash to ${PublicRemote}/${PublicBranch}" -ForegroundColor Yellow
        }
        if ($PublicBranch -ne "main") {
            Write-Host "DRY RUN: Would tag as $TagName and push tag" -ForegroundColor Yellow
        } else {
            Write-Host "DRY RUN: No tag (edge/main publish)" -ForegroundColor Yellow
        }
    } else {
        if ($CreateOrphan) {
            Write-Host "Force-pushing to ${PublicRemote}/${PublicBranch}..." -ForegroundColor Gray
            $pushOutput = git push $PublicRemote "${StagingBranch}:${PublicBranch}" --force 2>&1
        } else {
            Write-Host "Pushing to ${PublicRemote}/${PublicBranch}..." -ForegroundColor Gray
            $pushOutput = git push $PublicRemote "${StagingBranch}:${PublicBranch}" 2>&1
        }
        if ($LASTEXITCODE -ne 0) {
            $pushText = ($pushOutput | Out-String)
            if ($CreateOrphan -and ($pushText -match "GH006" -or $pushText -match "Cannot force-push")) {
                Write-Host "ERROR: Remote branch '${PublicBranch}' is protected and rejects force-push." -ForegroundColor Red
                Write-Host "Hint: Use stacked mode (omit -CreateOrphan), or temporarily publish to an unprotected branch." -ForegroundColor Yellow
            }
            Write-Host "ERROR: Push failed." -ForegroundColor Red
            exit 1
        }

        # Tags are only pushed for release/* branches (stable releases).
        # Pushing to main produces an edge build — no tag, no GitHub Release.
        if ($PublicBranch -eq "main") {
            Write-Host "  Skipping tag (main/edge publish). CI will build :edge images from branch push." -ForegroundColor DarkYellow
            $Published = $true
        } else {

        Write-Host "Tagging as $TagName..." -ForegroundColor Gray
        git rev-parse -q --verify "refs/tags/$TagName" 2>&1 | Out-Null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "  Local tag '$TagName' already exists. Recreating it at current commit." -ForegroundColor DarkYellow
            git tag -d $TagName 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) {
                Write-Host "ERROR: Failed to remove existing local tag '$TagName'." -ForegroundColor Red
                exit 1
            }
        }
        $tagCreateOutput = git tag -a $TagName -m $Message 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: Failed to create tag '$TagName'." -ForegroundColor Red
            Write-Host ($tagCreateOutput | Out-String) -ForegroundColor DarkGray
            exit 1
        }

        $tagPushOutput = git push $PublicRemote $TagName 2>&1
        if ($LASTEXITCODE -ne 0) {
            Write-Host "ERROR: Failed to push tag '$TagName' to remote '$PublicRemote'." -ForegroundColor Red
            Write-Host ($tagPushOutput | Out-String) -ForegroundColor DarkGray
            exit 1
        }
        $Published = $true

        } # end if ($PublicBranch -eq 'main')

        Write-Host ""
        if ($Version) {
            Write-Host "Published v$Version successfully." -ForegroundColor Green
            Write-Host "  CI will build Docker images from tag $TagName." -ForegroundColor Gray
        } else {
            Write-Host "Published successfully." -ForegroundColor Green
        }
    }

} finally {
    # --- Always clean up: switch back and delete staging branch ---
    Write-Host ""
    Write-Host "Cleaning up..." -ForegroundColor Gray
    git checkout $OriginalBranch --force --quiet 2>&1 | Out-Null
    $stagingExists = "$(git branch --list $StagingBranch 2>&1)".Trim()
    if ($stagingExists) {
        git branch -D $StagingBranch 2>&1 | Out-Null
    }

    # Keep local main's build_info version in sync with the published release.
    if ($Published -and $OriginalBranch -eq "main" -and $Version) {
        $buildInfo = @{ version = $Version } | ConvertTo-Json
        [System.IO.File]::WriteAllText("build_info.json", $buildInfo, $Utf8NoBom)
        Write-Host "Updated local main build_info.json to v$Version (not committed)." -ForegroundColor Gray
    }

    Write-Host "Back on '$OriginalBranch'." -ForegroundColor Gray
}
